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
import time

from core.keys import prefix
from core.logging import log_record

DELIVERED_MAXLEN = 200
_ID_RE = re.compile(r"^[0-9a-f]{32}$")

# Single hash, not hash+sorted-set: after the TYPE preflight confirms this
# one key is absent or already a hash, every subsequent redis.call() in this
# script operates on that same, now-known-good-typed key -- there is no
# second key whose type could independently be wrong. Recency lives in the
# stored value (cjson-encoded), read back and sorted in Lua only when a trim
# is actually needed, not maintained as a separate always-updated index.
_RECORD_DELIVERED = """
-- reply_correlation record_delivered v2: single hash, preflight-typed
local key = KEYS[1]
local stream_id = ARGV[1]
local source = ARGV[2]
local score = tonumber(ARGV[3])
local maxlen = tonumber(ARGV[4])

local key_type = redis.call('TYPE', key)['ok']
if key_type ~= 'none' and key_type ~= 'hash' then
    return redis.error_reply('reply_correlation: delivered key has the wrong type')
end
if score == nil then
    return redis.error_reply('reply_correlation: score must be a number')
end
if maxlen == nil or maxlen <= 0 then
    return redis.error_reply('reply_correlation: maxlen must be a positive integer')
end

redis.call('HSET', key, stream_id, cjson.encode({source = source, score = score}))

local count = redis.call('HLEN', key)
if count > maxlen then
    local all = redis.call('HGETALL', key)
    local entries = {}
    for i = 1, #all, 2 do
        local ok, decoded = pcall(cjson.decode, all[i + 1])
        local entry_score = 0
        if ok and type(decoded) == 'table' and decoded.score ~= nil then
            entry_score = decoded.score
        end
        table.insert(entries, {field = all[i], score = entry_score})
    end
    table.sort(entries, function(a, b) return a.score < b.score end)
    for i = 1, count - maxlen do
        redis.call('HDEL', key, entries[i].field)
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

    The write, the eviction check, and the trim happen inside one Lua
    script (_RECORD_DELIVERED) against a single key, preflighted for type
    before any mutation. What that buys: EVAL removes the network round
    trip and cross-client interleaving between what used to be separate
    HSET/ZADD/ZREM/HDEL calls, and the type preflight removes the specific
    runtime-error path that was found to leave unindexed, permanently-valid
    provenance (see the module docstring). It does NOT mean this script
    cannot half-apply in any conceivable sense -- Redis Lua provides
    isolation, not transactional rollback, so a runtime error the preflight
    doesn't anticipate could still, in principle, leave earlier writes
    applied. There is exactly one write call in this script (the HSET/trim
    sequence operates on a single key already confirmed to be the right
    type), so the remaining surface for that is deliberately as small as
    this design gets it -- not zero by construction, only known and correct
    for every input this code actually produces.

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
        r.eval(_RECORD_DELIVERED, 1, key, stream_id, source, time.time(), DELIVERED_MAXLEN)
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
