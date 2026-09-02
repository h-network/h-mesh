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

# Four Redis writes (HSET, ZADD, and conditionally ZREM+HDEL) done as
# separate commands were found to leave a real gap: an injected failure
# between HSET and ZADD left provenance present in the hash with no
# corresponding entry in the eviction index, so it could never be evicted
# and would validate forever -- the confident-lie direction this whole
# feature exists to prevent, not merely a bookkeeping inconsistency. Same
# problem on the ZREM/HDEL trim boundary. One EVAL is the fix: Redis
# executes a script's redis.call()s with no other command interleaved and
# no partial application from a mid-script network failure, because there
# is no network round trip between them -- they are not separate calls
# from here at all, only one.
_RECORD_DELIVERED = """
-- reply_correlation record_delivered v1
local hash_key = KEYS[1]
local order_key = KEYS[2]
local stream_id = ARGV[1]
local source = ARGV[2]
local score = ARGV[3]
local maxlen = tonumber(ARGV[4])

redis.call('HSET', hash_key, stream_id, source)
redis.call('ZADD', order_key, score, stream_id)
local count = redis.call('ZCARD', order_key)
if count > maxlen then
    local stale = redis.call('ZRANGE', order_key, 0, count - maxlen - 1)
    if #stale > 0 then
        redis.call('ZREM', order_key, unpack(stale))
        redis.call('HDEL', hash_key, unpack(stale))
    end
end
return 1
"""


def is_valid_reply_id(value: object) -> bool:
    """Format check only: a well-formed 32-hex-char stream_id."""
    return isinstance(value, str) and bool(_ID_RE.match(value))


def record_delivered(r, *, pod: str, tenant: str, agent: str, stream_id: str, source: str) -> None:
    """Remember that `stream_id` was delivered to `agent`, originating from
    `source`, bounded to the most recent DELIVERED_MAXLEN ids.

    A hash (stream_id -> source) answers "was this delivered, and by whom"
    in one read. A sorted set (stream_id -> recency) exists only to decide
    eviction order. The write, the eviction check, and the trim all happen
    inside one Lua script (_RECORD_DELIVERED) via a single EVAL, not as
    separate HSET/ZADD/ZREM/HDEL round trips -- an earlier version used
    separate commands and left a real gap: an injected failure between the
    HSET and the ZADD left provenance present in the hash with nothing in
    the eviction index, so it could never age out and would validate
    forever. A single script has no network round trip between its
    internal steps for a failure to land between.

    What this call site cannot fully know: if `r.eval(...)` itself raises,
    that could mean the script never reached Redis (nothing written), OR
    that Redis executed it completely but the client lost the response
    (the network dropped after the write, before the acknowledgement) --
    those are genuinely different facts and this code cannot always tell
    them apart. Either way there is no possibility of a HALF-applied
    script -- the write and its eviction index either both happened or
    (from this process's point of view) did not observably happen -- so
    the outcome is always safe (fails toward "cannot correlate" or
    correlates correctly), never the wrong-in-kind failure a torn
    multi-command write produced. The failure itself is logged (not
    swallowed silently), because correlation quietly stopping is still
    worth knowing about even when it stopped safely.
    """
    if not is_valid_reply_id(stream_id):
        return
    try:
        hash_key = prefix(pod, tenant, agent=agent, resource="delivered")
        order_key = prefix(pod, tenant, agent=agent, resource="delivered.order")
        r.eval(_RECORD_DELIVERED, 2, hash_key, order_key, stream_id, source, time.time(), DELIVERED_MAXLEN)
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
