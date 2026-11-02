"""Where is this stream_id? A read-only trace through the custody lists.

Every custody stage already keeps the frame (or a record wrapping it), so the
delivery state of one send can be read without adding a single write:

    egress           sent, the switch has not forwarded it yet          queued
    ingress          admitted, no port has claimed it yet               queued
    processing       claimed by a port; the effect has NOT begun        queued
    opening          the effect may have begun, outcome not recorded    delivering
    opened / inbox   delivered: pasted into the pane / stored for the   delivered
                     api client's mailbox
    dead             rejected (by the switch or by the destination)     failed
    undeliverable    destination retired before opening                 failed
    unresolved       an opener failed after the effect may have begun   unresolved

⚠ "delivered" is not "read". For a terminal pane it means the text was pasted and
submitted; for an api participant it means the envelope is in its mailbox. No
component records that an agent or client consumed it, so none is claimed here.
`confirmation` reports only what the watchdog's later activity check retains.

The scan runs in one Lua call and compares the stream_id at its fixed header
offset (bytes 2-33), so no payload crosses the wire and none is parsed here.
"""

from dataclasses import dataclass

from .keys import (
    prefix,
    receive_opened_key,
    receive_opening_key,
    receive_processing_key,
    receive_undeliverable_key,
    receive_unresolved_key,
)
from .registry import members

# stage -> state. Order is irrelevant; every stage maps to exactly one state.
STAGE_STATE = {
    "egress": "queued",
    "ingress": "queued",
    "processing": "queued",
    "opening": "delivering",
    "opened": "delivered",
    "inbox": "delivered",
    "dead": "failed",
    "undeliverable": "failed",
    "unresolved": "unresolved",
}

_LIST, _JSON, _STREAM = "list", "json", "stream"

# KEYS[i] is scanned as ARGV[i + 1] says. A "list" holds raw frames, a "json" list
# holds {"envelope": raw|hex, "encoding": ...} custody records, a "stream" is an
# api inbox whose entries carry the envelope as JSON text. Returns the 1-based
# indexes of every matching key.
_TRACE = """
-- core receipt trace v1
local id = ARGV[1]
local needle = '"stream_id": "' .. id .. '"'
local function head_matches(raw)
    return type(raw) == 'string' and string.sub(raw, 2, 33) == id
end
local function unhex(text)
    return (string.gsub(string.sub(text, 1, 66), '..', function(pair)
        return string.char(tonumber(pair, 16) or 63)
    end))
end
-- A key of the wrong type must not blind the trace for every other key.
local function items_of(reply)
    if type(reply) == 'table' and reply['err'] == nil then return reply end
    return {}
end
local found = {}
for index, key in ipairs(KEYS) do
    local kind = ARGV[index + 1]
    local hit = false
    if kind == 'list' then
        for _, raw in ipairs(items_of(redis.pcall('LRANGE', key, 0, -1))) do
            if head_matches(raw) then hit = true break end
        end
    elseif kind == 'json' then
        for _, record in ipairs(items_of(redis.pcall('LRANGE', key, 0, -1))) do
            local ok, data = pcall(cjson.decode, record)
            if ok and type(data) == 'table' and type(data['envelope']) == 'string' then
                local envelope = data['envelope']
                if data['encoding'] == 'hex' then envelope = unhex(envelope) end
                if head_matches(envelope) then hit = true break end
            end
        end
    else
        for _, entry in ipairs(items_of(redis.pcall('XRANGE', key, '-', '+'))) do
            local fields = entry[2]
            for i = 2, #fields, 2 do
                if string.find(fields[i], needle, 1, true) then hit = true break end
            end
            if hit then break end
        end
    end
    if hit then table.insert(found, index) end
end
return found
"""


@dataclass(frozen=True)
class Location:
    agent: str | None  # None for tenant-level evidence lists
    stage: str


def _is_stream_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 32
        and all(c in "0123456789abcdef" for c in value)
    )


def _scan_plan(pod: str, tenant: str, agents: list[str]) -> list[tuple[str, str, Location]]:
    plan: list[tuple[str, str, Location]] = []
    for agent in agents:
        plan += [
            (prefix(pod, tenant, agent, "egress"), _LIST, Location(agent, "egress")),
            (prefix(pod, tenant, agent, "ingress"), _LIST, Location(agent, "ingress")),
            (receive_processing_key(pod, tenant, agent), _LIST, Location(agent, "processing")),
            (receive_opening_key(pod, tenant, agent), _LIST, Location(agent, "opening")),
            (receive_opened_key(pod, tenant, agent), _LIST, Location(agent, "opened")),
            (prefix(pod, tenant, agent, "dead"), _LIST, Location(agent, "dead")),
            (prefix(pod, tenant, agent, "inbox"), _STREAM, Location(agent, "inbox")),
        ]
    plan += [
        (receive_unresolved_key(pod, tenant), _JSON, Location(None, "unresolved")),
        (receive_undeliverable_key(pod, tenant), _JSON, Location(None, "undeliverable")),
    ]
    return plan


def locate(r, *, pod: str, tenant: str, stream_id: str, extra_agents=()) -> list[Location]:
    """Every custody location currently holding ``stream_id``."""
    if not _is_stream_id(stream_id):
        raise ValueError("stream_id must be 32 lowercase hex characters")
    agents = sorted(set(members(r, pod=pod, tenant=tenant)) | set(extra_agents))
    plan = _scan_plan(pod, tenant, agents)
    keys = [key for key, _, _ in plan]
    kinds = [kind for _, kind, _ in plan]
    hits = r.eval(_TRACE, len(keys), *keys, stream_id, *kinds)
    return [plan[int(index) - 1][2] for index in hits]


def overall_state(locations: list[Location]) -> str:
    """One word for the whole trace: ``unknown`` when nothing holds the frame,
    the single stage state when they agree, else ``partial`` (a broadcast whose
    copies are at different stages)."""
    states = {STAGE_STATE[location.stage] for location in locations}
    if not states:
        return "unknown"
    return states.pop() if len(states) == 1 else "partial"


def confirmation(r, *, pod: str, tenant: str, stream_id: str, agents) -> str | None:
    """What the watchdog's activity check still retains about a pasted delivery.

    ``pending``: marker awaiting the first later agent activity.
    ``unverified``: the check ran and saw none (the ``blocked`` record).
    ``None``: nothing retained -- either confirmed (markers are removed on
    confirmation) or never tracked (non-verifiable CLI, api participant).
    """
    for agent in agents:
        blocked = r.hget(prefix(pod, tenant, agent, "blocked"), "stream_id")
        if blocked is not None and _text(blocked) == stream_id:
            return "unverified"
    for agent in agents:
        for _, fields in r.xrange(prefix(pod, tenant, agent, "pending.verify"), min="-", max="+"):
            value = fields.get(b"stream_id", fields.get("stream_id"))
            if value is not None and _text(value) == stream_id:
                return "pending"
    return None


def _text(value) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def trace(r, *, pod: str, tenant: str, stream_id: str, extra_agents=()) -> dict:
    """The full answer: state, where the frame is, and the retained confirmation."""
    locations = locate(
        r, pod=pod, tenant=tenant, stream_id=stream_id, extra_agents=extra_agents
    )
    state = overall_state(locations)
    delivered_to = sorted({l.agent for l in locations if l.stage in ("opened", "inbox") and l.agent})
    result = {
        "stream_id": stream_id,
        "state": state,
        "locations": [{"agent": l.agent, "stage": l.stage} for l in locations],
    }
    if delivered_to:
        result["confirmation"] = confirmation(
            r, pod=pod, tenant=tenant, stream_id=stream_id, agents=delivered_to
        )
    return result
