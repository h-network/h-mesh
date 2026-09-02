"""Delivery provenance for opt-in reply correlation.

Answers exactly one question: was this stream_id actually delivered to this
agent, from this specific source, recently? Nothing here knows about turns,
conversations, or replies -- that meaning is applied by the caller. An
opener records a delivery (recipient, stream_id, original source); a
receipt-side validator checks a claimed `in_reply_to` against it before that
claim is ever stored or surfaced to a client. Deliberately not the same key
watchdog's `mark_delivery_pending` writes (modules/tmux/port.py): that one is
gated to verifiable CLIs for liveness verification, a different question,
and coupling this to it would tie an unrelated concern to its bookkeeping.

The source binding matters as much as the stream_id: without it, an agent
talked to by two different API clients could claim in_reply_to for a
message one of them sent while replying to the *other*, and the wrong
client would receive a confident, wrong correlation -- worse than no
correlation, and worse than a mismatch caught by format alone.
"""

import re
import time

from core.keys import prefix
from core.logging import log_record

DELIVERED_MAXLEN = 200
_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def is_valid_reply_id(value: object) -> bool:
    """Format check only: a well-formed 32-hex-char stream_id."""
    return isinstance(value, str) and bool(_ID_RE.match(value))


def record_delivered(r, *, pod: str, tenant: str, agent: str, stream_id: str, source: str) -> None:
    """Remember that `stream_id` was delivered to `agent`, originating from
    `source`, bounded to the most recent DELIVERED_MAXLEN ids.

    A hash (stream_id -> source) answers "was this delivered, and by whom"
    in one read. A sorted set (stream_id -> recency) exists only to decide
    eviction order. Both HSET and ZADD are idempotent per member, so
    redelivery of the same stream_id -- normal in this system, ingress
    snapshots can replay -- refreshes its recency instead of creating a
    duplicate entry that a plain set+list design mishandles on trim (a
    second copy in an order list, trimmed independently of the set entry it
    was supposed to correspond to, could evict a still-current id).

    Partial-failure reasoning: if this fails between the HSET/ZADD and the
    eviction step, the window is left slightly larger than
    DELIVERED_MAXLEN -- never wrong, never missing an entry that should be
    there, just temporarily over budget. It self-corrects on the next
    successful call. If the write itself fails, that is logged (not
    swallowed silently, so correlation does not just quietly stop working)
    and nothing is recorded -- fails toward "cannot correlate", the same
    direction a genuine lookup miss does.
    """
    if not is_valid_reply_id(stream_id):
        return
    try:
        hash_key = prefix(pod, tenant, agent=agent, resource="delivered")
        order_key = prefix(pod, tenant, agent=agent, resource="delivered.order")
        r.hset(hash_key, stream_id, source)
        r.zadd(order_key, {stream_id: time.time()})
        count = r.zcard(order_key)
        if count > DELIVERED_MAXLEN:
            stale = r.zrange(order_key, 0, count - DELIVERED_MAXLEN - 1)
            if stale:
                stale = [item.decode() if isinstance(item, bytes) else item for item in stale]
                r.zrem(order_key, *stale)
                r.hdel(hash_key, *stale)
    except Exception as exc:
        try:
            log_record(
                "reply_correlation", "record_delivered_failed",
                stream_id=stream_id, destination=agent, reason=str(exc),
            )
        except Exception:
            pass


def was_delivered(r, *, pod: str, tenant: str, agent: str, stream_id: str, source: str) -> bool | None:
    """Whether `stream_id` was recorded as delivered to `agent` from
    `source` specifically -- not merely delivered to `agent` from anywhere.

    Returns True (verified match), False (verified mismatch, or never
    delivered), or None (could not verify -- storage was unreachable).
    False and None both mean "do not trust this" to a caller deciding
    whether to keep or drop a claimed correlation -- that is the fail-safe
    direction either way -- but they are not the same *fact*, and a caller
    that logs why must not report "never delivered" when the true reason
    was "could not check". modules/api/port.py's deliver_api is the one
    caller today and preserves that distinction in its own log reason.
    """
    if not is_valid_reply_id(stream_id):
        return False
    try:
        hash_key = prefix(pod, tenant, agent=agent, resource="delivered")
        stored_source = r.hget(hash_key, stream_id)
    except Exception:
        return None
    if stored_source is None:
        return False
    stored_source = stored_source.decode() if isinstance(stored_source, bytes) else stored_source
    return stored_source == source
