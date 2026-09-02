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

⚠ THIS FILE WENT THROUGH FOUR DESIGNS IN ONE REVIEW CYCLE, and each one was
a real bug, not a style objection:

Generation 1: two keys -- a provenance hash and a separate sorted set for
eviction order -- written as HSET then ZADD. If the sorted-set key ever
held the wrong type, HSET committed real, permanent provenance and ZADD's
WRONGTYPE error left it with no eviction-index entry, on real Redis. That
state validated forever -- the confident-lie outcome this feature exists to
prevent -- *inside* the single EVAL meant to make it impossible.

Generation 2: collapsed to one key, TYPE-preflighted -- but decoding and
sorting existing entries for eviction still happened AFTER the new entry's
HSET, so a corrupted entry with a non-numeric score reached the sort
comparator and raised there, write already committed. Same failure
*shape*, one step later.

Generation 3: every error-producing step moved ahead of the mutation, but
recency was scored with Python's time.time() -- two calls close together
could tie, and Lua's table.sort has no defined tie-break, so a redelivery
meant to refresh recency could still be evicted alongside whatever it tied
with. Fixed with a second, INCR-based counter key.

Generation 4: the counter fix itself had a cardinality bug -- HGETALL
picked up a redelivered stream_id's OLD entry, then the script appended it
AGAIN with the new score, inflating the entry count by one and evicting an
unrelated entry one delivery too early on every redelivery. Found by a
third independent audit of the same script.

That is four real defects in a hand-rolled bounded-and-ordered structure
whose entire job is "remember a delivery for a while, then forget it".
Asked directly whether that machinery was load-bearing, the honest answer
was no: nothing downstream needs COUNT-bounded retention specifically, only
retention that eventually expires. Redis already has a primitive for that.

THE ACTUAL DESIGN: one key per (agent, stream_id), a plain `SET key source
EX DELIVERED_TTL_SECONDS`, read back with `GET`. No hash, no sorting, no
counter, no eviction loop, no cardinality, no Lua script -- SET and GET are
each already atomic as single commands, so no multi-step script exists for
a runtime error to land inside. A redelivery is just SET again: the TTL
naturally resets, which IS the recency refresh, not something coded
separately. A wrong-typed key cannot occur, because SET overwrites a key
unconditionally regardless of what it held before. Retention is
TIME-BOUNDED ("delivered within the last DELIVERED_TTL_SECONDS") rather
than count-bounded ("among the last N deliveries") -- for this feature,
"recent" was always what count-bounding was a proxy for, so this is a more
direct expression of the actual requirement, not a weaker one. A wrong
(too short) TTL costs exactly one thing: a false negative -- a stream_id
that really was delivered, past its window, fails to validate, and a
correlation that should have worked doesn't. That is a deliberate choice,
not an accidental consequence: it fails toward absent, the same direction
every version of this file has chosen throughout every generation above,
and it is the same failure a too-small count-based cap already produced.
"""

import re

from core.keys import prefix
from core.logging import log_record

# Generous relative to any realistic reply latency; small enough that a
# forgotten reply doesn't linger meaningfully. Not tuned from a measurement
# -- if a real workflow needs longer (or shorter), change this constant,
# not the mechanism around it.
DELIVERED_TTL_SECONDS = 3600
_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def is_valid_reply_id(value: object) -> bool:
    """Format check only: a well-formed 32-hex-char stream_id."""
    return isinstance(value, str) and bool(_ID_RE.match(value))


def _key(pod: str, tenant: str, agent: str, stream_id: str) -> str:
    # The "s" prefix is load-bearing, not cosmetic: core.keys rejects any
    # dotted-resource segment that is ALL digits (tmux resolves an
    # all-digit agent name as a window *index*, a different module's
    # concern entirely, but the segment validator is shared and doesn't
    # know this segment is a stream_id, not an agent name). A 32-hex-char
    # stream_id is all-digits whenever it happens to contain no a-f
    # characters -- rare, but real: it must not make certain otherwise-
    # valid stream_ids permanently unable to be recorded. "s" guarantees
    # the segment can never be all-digits, regardless of the id itself.
    return prefix(pod, tenant, agent=agent, resource=f"delivered.s{stream_id}")


def record_delivered(r, *, pod: str, tenant: str, agent: str, stream_id: str, source: str) -> None:
    """Remember that `stream_id` was delivered to `agent`, originating from
    `source`, for DELIVERED_TTL_SECONDS.

    One `SET key source EX DELIVERED_TTL_SECONDS` per (agent, stream_id) --
    see the module docstring for why this replaced a hand-rolled bounded,
    ordered structure that went through four real-bug generations. SET is
    a single Redis command: there is no multi-step script for a runtime
    error to land inside, and it overwrites a key of any prior type
    unconditionally, so there is no type contract to violate. A
    redelivery is exactly the same call again, which resets the TTL --
    recency-refresh is a property of the primitive, not code this module
    has to get right on its own.

    Best-effort, same policy as modules/tmux/port.py's mark_delivery_pending
    and modules/api/port.py's _record: a bookkeeping write failure here
    must never fail the delivery it's recording. Logged, not swallowed
    silently, because correlation quietly stopping is still worth knowing
    about even when it stopped safely (fails toward "cannot correlate",
    the same direction a genuine lookup miss does).
    """
    if not is_valid_reply_id(stream_id):
        return
    try:
        r.set(_key(pod, tenant, agent, stream_id), source, ex=DELIVERED_TTL_SECONDS)
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
    `source` specifically, within the last DELIVERED_TTL_SECONDS -- not
    merely delivered to `agent` from anywhere, and not indefinitely.

    Returns True (verified match), False (verified mismatch, never
    delivered, or expired), or None (could not verify -- storage was
    unreachable). False and None both mean "do not trust this" to a caller
    deciding whether to keep or drop a claimed correlation -- that is the
    fail-safe direction either way -- but they are not the same *fact*,
    and a caller that logs why must not report "never delivered" when the
    true reason was "could not check". modules/api/port.py's deliver_api
    is the one caller today and preserves that distinction in its own log
    reason.
    """
    if not is_valid_reply_id(stream_id):
        return False
    try:
        stored = r.get(_key(pod, tenant, agent, stream_id))
    except Exception:
        return None
    if stored is None:
        return False
    stored = stored.decode() if isinstance(stored, bytes) else stored
    return stored == source
