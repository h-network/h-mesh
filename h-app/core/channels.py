"""Agent/port-facing channels: send into own egress, receive from own ingress."""

import json
from collections.abc import Callable
from datetime import datetime, timezone

from .envelope import (
    EnvelopeError,
    build,
    encode,
    parse,
    parse_for_switch,
    resolve_destination,
    resolve_source,
)
from .keys import (
    prefix, receive_opened_key, receive_opening_key, receive_processing_key,
    receive_unresolved_key,
)
from .logging import emit, log_record
from .policy import require_allowed
from .registry import port_type

# Kinds a client can plausibly be owed a reply for. `Command`, `AddTicket` and
# similar structured kinds are never sent by a client and would only ever
# reset a count that could not have been opened.
_UNREPLIED_KINDS = {"Message", "Attachment"}

_INCREMENT_UNREPLIED = """
-- core unreplied increment v1
local count = 1
local since = ARGV[2]
local existing = redis.call('HGET', KEYS[1], ARGV[1])
if existing then
    local ok, data = pcall(cjson.decode, existing)
    if ok and type(data) == 'table' and tonumber(data['count'])
       and type(data['since']) == 'string' and data['since'] ~= '' then
        count = tonumber(data['count']) + 1
        since = data['since']
        if ARGV[2] < since then
            since = ARGV[2]
        end
    end
end
redis.call('HSET', KEYS[1], ARGV[1], cjson.encode({count=count, since=since}))
return count
"""

_TRANSFER_RECEIVE_CUSTODY = """
-- core receive custody transfer v1
for _, key in ipairs(KEYS) do
    local kind = redis.call('TYPE', key)['ok']
    if kind ~= 'none' and kind ~= 'list' then
        return redis.error_reply('receive custody key is not a list: ' .. key)
    end
end
if redis.call('LREM', KEYS[1], 1, ARGV[1]) ~= 1 then return 0 end
redis.call('RPUSH', KEYS[2], ARGV[2])
local cap = tonumber(ARGV[3])
if cap and cap > 0 then redis.call('LTRIM', KEYS[2], -cap, -1) end
return 1
"""

_OPENED_RECEIPT_MAX = 1000

def _unreplied_key(pod: str, tenant: str, agent: str) -> str:
    return prefix(pod, tenant, agent=agent, resource="unreplied")


def _increment_unreplied(r, *, key: str, client: str, since: str) -> None:
    """Atomically increment one client backlog while preserving first since."""
    r.eval(_INCREMENT_UNREPLIED, 1, key, client, since)


def _track_unreplied(r, *, pod: str, tenant: str, source: str, destination: str, kind: str) -> None:
    """Open or clear a tmux agent's per-client unanswered-message count.

    ⚠ Reads both port types itself, via two fresh registry HGETs — it does NOT
    reuse work `require_allowed` above already did. That call checks policy
    export/import *tags*, a separate hash, and never touches port_type. A
    client (`api` port_type,
    e.g. `telegram`) sending to a tmux agent opens or extends a count; that
    same agent sending anything back to the same client closes it outright.
    Peer traffic between two tmux agents never touches this key: ticket age
    already covers that responsiveness question via the watchdog's
    doing/todo/hold family.
    """
    if destination == "all":
        return
    source_type = port_type(r, pod=pod, tenant=tenant, agent=source)
    destination_type = port_type(r, pod=pod, tenant=tenant, agent=destination)
    if (
        source_type == "api"
        and destination_type == "tmux"
        and kind in _UNREPLIED_KINDS
    ):
        key = _unreplied_key(pod, tenant, destination)
        since = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        _increment_unreplied(r, key=key, client=source, since=since)
    elif source_type == "tmux" and destination_type == "api":
        r.hdel(_unreplied_key(pod, tenant, source), destination)


class DeadLetter(Exception):
    """Signal that an opener rejected an envelope after receive took custody."""


def _emit_observation(
    module: str, event: str, envelope: dict, reason: str | None = None
) -> None:
    """Keep a logging fault from replacing the observed operation's result."""
    try:
        emit(module, event, envelope, reason)
    except Exception:
        # Logging is secondary observation: it must neither turn a committed
        # handover into failure nor replace an outcome-unknown Redis exception.
        pass


def _emit_for_recipient(
    module: str,
    event: str,
    envelope: dict,
    recipient: str,
    reason: str | None = None,
) -> None:
    """Best-effort receive custody about the participant, not L2 fan-out."""
    try:
        log_record(
            module,
            event,
            stream_id=envelope.get("stream_id"),
            correlation_id=envelope.get("correlation_id"),
            source=envelope.get("l2", {}).get("source"),
            destination=recipient,
            reason=reason,
        )
    except Exception:
        # receive() and burst delivery have already destructively popped or
        # drained ingress. Observation cannot strand that accepted custody.
        pass


def _notify_dead_letter_sender(
    r,
    *,
    pod: str,
    tenant: str,
    recipient: str,
    envelope: dict,
    reason: str,
    module: str,
) -> None:
    """Best-effort feedback to an agent whose envelope was rejected.

    Only tmux sources can reliably open this Message. The restriction also
    prevents feedback loops between fixed API/control participants.
    """
    source = envelope.get("l2", {}).get("source")
    if not source or source == recipient:
        return
    try:
        if port_type(r, pod=pod, tenant=tenant, agent=source) != "tmux":
            return
        stream_id = envelope.get("stream_id", "unknown")
        send(
            r,
            pod=pod,
            tenant=tenant,
            source=recipient,
            destination=source,
            payload={
                "text": f"Delivery to {recipient} failed for message {stream_id}: {reason}"
            },
            kind="Message",
            correlation_id=stream_id if isinstance(stream_id, str) else None,
            module=module,
        )
    except Exception:
        # The rejected envelope is already safely in the dead-letter queue.
        # Feedback is secondary and must not change receive custody.
        pass


def send(
    r,
    *,
    pod: str,
    tenant: str,
    source: str,
    destination: str,
    payload: dict,
    kind: str = "Message",
    correlation_id: str | None = None,
    module: str = "channels",
    in_reply_to: str | None = None,
) -> str:
    try:
        _, local_source = resolve_source(pod=pod, tenant=tenant, source=source)
        _, local_destination = resolve_destination(
            pod=pod, tenant=tenant, destination=destination
        )
        if local_destination != "all":
            require_allowed(
                r,
                pod=pod,
                tenant=tenant,
                source=local_source,
                destination=local_destination,
            )
        envelope = build(
            kind, source, destination, payload, correlation_id,
            pod=pod, tenant=tenant, in_reply_to=in_reply_to,
        )
    except EnvelopeError as exc:
        log_record(
            module,
            "send_refused",
            source=source,
            destination=destination,
            reason=str(exc),
        )
        raise
    # Finish every operation that can provably fail before Redis is called.
    # Only RPUSH belongs inside the outcome-unknown window: prefix/encoding
    # errors prove that no queue write was attempted.
    egress_key = prefix(pod, tenant, source, "egress")
    raw = encode(envelope)
    try:
        r.rpush(egress_key, raw)
    except Exception as exc:
        _emit_observation(
            module,
            "send_unknown",
            envelope,
            f"egress write outcome UNKNOWN after {exc}",
        )
        raise
    _emit_observation(module, "sent", envelope)
    # The message is already durably enqueued above; this bookkeeping is a
    # secondary effect and must never make a successful send look failed to
    # the caller, so a fault here is logged and swallowed, not raised.
    try:
        _track_unreplied(
            r, pod=pod, tenant=tenant, source=local_source, destination=local_destination, kind=kind
        )
    except Exception as exc:
        _emit_observation(
            module,
            "unreplied_tracking_failed",
            envelope,
            f"unreplied bookkeeping failed: {exc}",
        )
    return envelope["stream_id"]


def _open_received(
    r,
    *,
    pod: str,
    tenant: str,
    agent: str,
    openers: dict[str, Callable[[dict], None]],
    raw,
    processing_key: str,
    opening_key: str,
    opened_key: str,
    unresolved_key: str,
    module: str,
) -> None:
    """Open one claim through the not-started/possibly-started boundary."""
    dead_key = prefix(pod, tenant, agent, "dead")

    def transfer(source: str, destination: str, value=raw, cap: int = 0) -> None:
        moved = r.eval(
            _TRANSFER_RECEIVE_CUSTODY, 2, source, destination, raw, value, cap
        )
        if moved != 1:
            raise RuntimeError("receive lost ownership before custody transfer")

    def unresolved(reason: str) -> None:
        raw_text = raw.decode() if isinstance(raw, bytes) else str(raw)
        record = json.dumps(
            {"agent": agent, "reason": reason, "envelope": raw_text},
            separators=(",", ":"),
        )
        transfer(opening_key, unresolved_key, record)
        _emit_for_recipient(module, "open_unresolved", envelope, agent, reason)

    try:
        envelope = parse(raw)
    except EnvelopeError as exc:
        transfer(processing_key, dead_key)
        # A valid v4 header remains joinable when its corrupt body is rejected
        # here. A malformed header has no trustworthy custody identifiers.
        try:
            header = parse_for_switch(raw)
        except EnvelopeError:
            header = {}
        _emit_for_recipient(module, "dead_lettered", header, agent, str(exc))
        return
    _emit_for_recipient(module, "received", envelope, agent)
    opener = openers.get(envelope["kind"])
    if opener is None:
        reason = f"unknown kind: {envelope['kind']}"
        transfer(processing_key, dead_key)
        _emit_for_recipient(module, "dead_lettered", envelope, agent, reason)
        _notify_dead_letter_sender(
            r, pod=pod, tenant=tenant, recipient=agent,
            envelope=envelope, reason=reason, module=module,
        )
        return
    # This is the critical truth boundary: processing is safe to replay because
    # no effect began; opening is never replayed because one may have begun.
    transfer(processing_key, opening_key)
    try:
        opener(envelope)
    except DeadLetter as exc:
        reason = str(exc)
        transfer(opening_key, dead_key)
        _emit_for_recipient(module, "dead_lettered", envelope, agent, reason)
        _notify_dead_letter_sender(
            r, pod=pod, tenant=tenant, recipient=agent,
            envelope=envelope, reason=reason, module=module,
        )
        return
    except Exception as exc:
        reason = f"opener failed: {exc}"
        unresolved(reason)
        return
    transfer(opening_key, opened_key, cap=_OPENED_RECEIPT_MAX)
    _emit_for_recipient(module, "opened", envelope, agent)


def receive(
    r,
    *,
    pod: str,
    tenant: str,
    agent: str,
    openers: dict[str, Callable[[dict], None]],
    timeout: int,
    blocking: bool = True,
    module: str = "port",
) -> None:
    """Wait for ingress when requested, then drain it one envelope at a time.

    A switch kick means work is available, not that exactly one queue entry is
    paired with this process. Draining prevents an older entry left by a missed
    or crashed kick from consuming the only attempt for the request behind it.
    Each envelope moves atomically from ingress to a per-agent processing list
    before it is opened. A dead process therefore leaves durable work for its
    successor; a rejected envelope moves from processing to dead in one
    preflighted Redis execution. Once custody enters `opening`, a death or
    ambiguous opener failure is surfaced as unresolved and never replayed.
    """
    ingress_key = prefix(pod, tenant, agent, "ingress")
    processing_key = receive_processing_key(pod, tenant, agent)
    opening_key = receive_opening_key(pod, tenant, agent)
    opened_key = receive_opened_key(pod, tenant, agent)
    unresolved_key = receive_unresolved_key(pod, tenant)

    # A predecessor crossed the effect boundary. Preserve and surface that
    # uncertainty before considering safely replayable processing custody.
    while (uncertain := r.lindex(opening_key, 0)) is not None:
        raw_text = uncertain.decode() if isinstance(uncertain, bytes) else str(uncertain)
        record = json.dumps(
            {"agent": agent, "reason": "opener outcome unknown after process exit", "envelope": raw_text},
            separators=(",", ":"),
        )
        moved = r.eval(
            _TRANSFER_RECEIVE_CUSTODY, 2,
            opening_key, unresolved_key, uncertain, record, 0,
        )
        if moved != 1:
            raise RuntimeError("receive lost ownership while surfacing unresolved custody")
        try:
            uncertain_envelope = parse(uncertain)
        except EnvelopeError:
            uncertain_envelope = {}
        _emit_for_recipient(
            module, "open_unresolved", uncertain_envelope, agent,
            "opener outcome unknown after process exit",
        )

    # Recover a predecessor's claimed raw before admitting newer ingress. The
    # delivery lease serializes real port processes, so there is one owner of
    # this per-agent processing head at a time.
    raw = r.lindex(processing_key, 0)
    if raw is None:
        if blocking:
            raw = r.blmove(ingress_key, processing_key, timeout, "LEFT", "RIGHT")
        else:
            raw = r.lmove(ingress_key, processing_key, "LEFT", "RIGHT")

    while raw is not None:
        _open_received(
            r,
            pod=pod,
            tenant=tenant,
            agent=agent,
            openers=openers,
            raw=raw,
            processing_key=processing_key,
            opening_key=opening_key,
            opened_key=opened_key,
            unresolved_key=unresolved_key,
            module=module,
        )
        raw = r.lmove(ingress_key, processing_key, "LEFT", "RIGHT")
