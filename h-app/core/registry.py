"""Read-only access to the tenant registry."""

import json

from .keys import coop_tokens_key, interoffice_participants_key, offices_key, prefix

# The local api participant the reply relay delivers remote replies as. Fixed
# by `office office-add`, which refuses to reuse the name for anything else.
RELAY_PARTICIPANT = "relay"


def members(r, *, pod: str, tenant: str) -> set[str]:
    values = r.hkeys(prefix(pod, tenant, resource="registry"))
    return {value.decode() if isinstance(value, bytes) else value for value in values}


def member_types(r, *, pod: str, tenant: str) -> dict[str, str]:
    """Return one registry snapshot preserving each participant's port type."""
    values = r.hgetall(prefix(pod, tenant, resource="registry"))
    return {
        key.decode() if isinstance(key, bytes) else key:
        value.decode() if isinstance(value, bytes) else value
        for key, value in values.items()
    }


def is_member(r, *, pod: str, tenant: str, agent: str) -> bool:
    return bool(r.hexists(prefix(pod, tenant, resource="registry"), agent))


def port_type(r, *, pod: str, tenant: str, agent: str) -> str | None:
    value = r.hget(prefix(pod, tenant, resource="registry"), agent)
    if isinstance(value, bytes):
        return value.decode()
    return value


def is_interoffice(r, *, pod: str, tenant: str, agent: str) -> bool:
    """True if ``agent`` is a local api participant that speaks for another office.

    Such a sender is not a client waiting for an answer: the relay has no local
    reply target (replies go out with ``send --office``) and a coop participant's
    reply is optional. Authoritative source is the ``interoffice-participants``
    marker written when the participant is enrolled. Two fallbacks cover offices
    set up before the marker existed: a participant named by any coop token
    record (revoked ones included -- the participant stays enrolled), and the
    reserved ``relay`` name once the address book has an entry.
    """
    if r.hexists(interoffice_participants_key(pod, tenant), agent):
        return True
    for raw in (r.hgetall(coop_tokens_key(pod, tenant)) or {}).values():
        try:
            record = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        except (ValueError, TypeError, UnicodeDecodeError):
            continue
        if isinstance(record, dict) and record.get("participant") == agent:
            return True
    if agent == RELAY_PARTICIPANT:
        return bool(r.hgetall(offices_key(pod, tenant)))
    return False
