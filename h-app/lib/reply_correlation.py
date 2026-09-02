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

⚠ A Redis Lua script is ISOLATED, not transactional: no other client command
interleaves during its execution, but a runtime error partway through does
NOT roll back redis.call()s that already ran. An earlier version of
_RECORD_DELIVERED used two keys (a hash for provenance, a separate sorted
set for eviction order) and called HSET then ZADD; if the order key ever
held the wrong type, HSET committed real, permanent provenance and the
following ZADD's WRONGTYPE error left it with no eviction-index entry --
found on real Redis, reproduced by seeding that key as a string first. That
state validates forever, exactly the confident-lie outcome this feature
exists to prevent, and it happened *inside* the single EVAL that was
supposed to make that impossible. Two things fix it, both required: collapse
to ONE key (below), and preflight its TYPE before the first mutation so a
wrong-type key is rejected before anything is written, not discovered by a
later command failing after an earlier one already committed.
"""

import json
import re

from core.keys import prefix
from core.logging import log_record

DELIVERED_MAXLEN = 200
_ID_RE = re.compile(r"^[0-9a-f]{32}$")

# Single hash for provenance, not hash+sorted-set: after the TYPE preflight
# confirms this key is absent or already a hash, every subsequent
# redis.call() against IT operates on a now-known-good-typed key. Recency
# uses a second, deliberately trivial key: an INCR counter, not a wall-clock
# timestamp. An earlier version scored entries with time.time() from Python
# -- under a fast, tight sequence of calls (exactly what a real burst of
# deliveries looks like) two calls could get the same float value, and
# table.sort's tie-breaking for equal scores is unspecified, so a redelivery
# meant to refresh an id's recency could still be evicted alongside older
# entries it tied with. Found by this branch's own test suite once eviction
# was actually exercised under real timing rather than synthetic scores.
# INCR guarantees a strictly increasing, collision-free sequence with no
# dependency on wall-clock resolution or the two calls' real-world timing.
# The counter key is a second key, which sounds like the exact shape
# reviewer's WRONGTYPE finding argued against -- but its risk is not the
# same in kind: TYPE-preflighted the same way, and once preflighted an INCR
# on a validated absent-or-integer-string key has no further failure mode
# comparable to a sorted set's type/member/score contract.
_RECORD_DELIVERED = """
-- reply_correlation record_delivered v4: single hash + INCR counter,
-- fully preflighted -- every operation that can error (both keys' types,
-- input validation, decoding and ordering existing entries for eviction)
-- happens before the first mutation. A prior version validated the hash
-- key's type but decoded and sorted existing entries AFTER already
-- writing the new one; a legacy or corrupted entry with a non-numeric
-- score reached table.sort and raised there, with the new entry already
-- persisted -- found by openshell-agent's audit of this exact script.
local key = KEYS[1]
local counter_key = KEYS[2]
local stream_id = ARGV[1]
local source = ARGV[2]
local maxlen = tonumber(ARGV[3])

local key_type = redis.call('TYPE', key)['ok']
if key_type ~= 'none' and key_type ~= 'hash' then
    return redis.error_reply('reply_correlation: delivered key has the wrong type')
end
local counter_type = redis.call('TYPE', counter_key)['ok']
if counter_type ~= 'none' and counter_type ~= 'string' then
    return redis.error_reply('reply_correlation: delivered counter key has the wrong type')
end
if maxlen == nil or maxlen <= 0 or maxlen ~= math.floor(maxlen) then
    return redis.error_reply('reply_correlation: maxlen must be a positive integer')
end

-- Read and validate every existing entry's score BEFORE any write. An
-- entry that doesn't decode to {score: <number>, ...} -- corrupt or from
-- some future/legacy shape -- is excluded from ordering rather than
-- allowed to abort the script; it is never chosen for eviction (so it
-- cannot be silently lost) and never blocks this call (so one bad field
-- cannot break delivery recording for everyone). That decision is made
-- here, before any mutation, specifically so it cannot happen after one.
local all = redis.call('HGETALL', key)
local entries = {}
for i = 1, #all, 2 do
    local ok, decoded = pcall(cjson.decode, all[i + 1])
    if ok and type(decoded) == 'table' and type(decoded.score) == 'number' then
        table.insert(entries, {field = all[i], score = decoded.score})
    end
end

-- The only remaining mutation before the real write: advance the
-- counter. Both keys are already TYPE-preflighted, so INCR on
-- counter_key cannot discover a type it wasn't already checked for, and
-- every entry already in `entries` is already known-numeric -- so the
-- sort below, now that this call's own entry is appended with a value
-- INCR is guaranteed to return, cannot fail on a comparison it hasn't
-- already been proven safe against.
local score = redis.call('INCR', counter_key)
table.insert(entries, {field = stream_id, score = score})
table.sort(entries, function(a, b) return a.score < b.score end)

-- #entries counts only the decodable ones (plus the one being added) --
-- an undecodable field is excluded from this count too, not just from
-- eviction targeting, so it is never selected for removal but is also
-- never counted toward the cap. In a system where this script is the
-- only writer to this key, that field should never exist; if it somehow
-- does (external tampering, a future format this version can't read),
-- the cap is honored for everything this script understands, and the
-- unreadable leftover neither blocks recording nor is silently deleted.
local to_evict = {}
if #entries > maxlen then
    for i = 1, #entries - maxlen do
        if entries[i].field ~= stream_id then
            table.insert(to_evict, entries[i].field)
        end
    end
end

redis.call('HSET', key, stream_id, cjson.encode({source = source, score = score}))
if #to_evict > 0 then
    redis.call('HDEL', key, unpack(to_evict))
end
return 1
"""


def is_valid_reply_id(value: object) -> bool:
    """Format check only: a well-formed 32-hex-char stream_id."""
    return isinstance(value, str) and bool(_ID_RE.match(value))


def record_delivered(r, *, pod: str, tenant: str, agent: str, stream_id: str, source: str) -> None:
    """Remember that `stream_id` was delivered to `agent`, originating from
    `source`, bounded to the most recent DELIVERED_MAXLEN ids.

    The write, the eviction check, and the trim happen inside one Lua
    script (_RECORD_DELIVERED) against two keys: the provenance hash, and
    a trivial INCR counter that provides recency. Every operation that can
    error -- both keys' TYPE, input validation, AND decoding/ordering the
    existing entries used to decide what to evict -- runs before the
    script's first mutation, not just the type check. An earlier version
    validated the hash key's type up front but decoded and sorted existing
    entries only after already writing the new one; a legacy or corrupted
    entry with a non-numeric score reached the sort comparator and raised
    there, with the write already committed -- found by review, twice, on
    the same script, which is why every remaining error path was moved
    ahead of the mutation rather than patched individually.

    Recency is an INCR'd sequence number, not a wall-clock timestamp: an
    earlier version scored entries with time.time() from Python, and under
    a fast, tight sequence of calls -- exactly what a real delivery burst
    looks like -- two calls could get the identical float value, and Lua's
    table.sort has no defined tie-breaking for equal scores, so a
    redelivery meant to refresh an id's recency could still be evicted
    alongside whatever it tied with. Caught by this branch's own test
    suite once eviction was exercised under real call timing rather than
    synthetic scores. A monotonic counter has no such tie by construction.

    It does NOT mean this script cannot half-apply in any conceivable
    sense -- Redis Lua provides isolation (no other client command
    interleaves during execution), not transactional rollback (a runtime
    error partway through does not undo redis.call()s that already ran in
    the same execution). What EVAL actually buys is removing the network
    round trip and cross-client interleaving that made separate
    HSET/ZADD/ZREM/HDEL calls unsafe; what the preflight-everything
    ordering buys is that every input this code is known to be able to
    produce has its error surfaced before any mutation, not after. Neither
    is a proof that no sequence of redis.call()s in this script could ever
    fail partway through for a cause nobody has found yet -- only that the
    causes that have been found are closed by construction, not by hoping
    a check written for one of them also covers the others.

    What this call site cannot fully know: if `r.eval(...)` itself raises,
    that could mean the script never reached Redis (nothing written), the
    preflight rejected it (nothing written), OR that Redis executed it
    completely but the client lost the response (the network dropped after
    the write, before the acknowledgement) -- genuinely different facts
    this code cannot always tell apart. The failure itself is logged (not
    swallowed silently), because correlation quietly stopping is still
    worth knowing about even when it stopped safely.
    """
    if not is_valid_reply_id(stream_id):
        return
    try:
        key = prefix(pod, tenant, agent=agent, resource="delivered")
        counter_key = prefix(pod, tenant, agent=agent, resource="delivered.seq")
        r.eval(_RECORD_DELIVERED, 2, key, counter_key, stream_id, source, DELIVERED_MAXLEN)
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

    Returns True (verified match), False (verified mismatch, never
    delivered, or an unreadable stored value), or None (could not verify --
    storage was unreachable). False and None both mean "do not trust this"
    to a caller deciding whether to keep or drop a claimed correlation --
    that is the fail-safe direction either way -- but they are not the same
    *fact*, and a caller that logs why must not report "never delivered"
    when the true reason was "could not check". modules/api/port.py's
    deliver_api is the one caller today and preserves that distinction in
    its own log reason.
    """
    if not is_valid_reply_id(stream_id):
        return False
    try:
        key = prefix(pod, tenant, agent=agent, resource="delivered")
        stored = r.hget(key, stream_id)
    except Exception:
        return None
    if stored is None:
        return False
    stored = stored.decode() if isinstance(stored, bytes) else stored
    try:
        decoded = json.loads(stored)
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(decoded, dict):
        return False
    return decoded.get("source") == source
