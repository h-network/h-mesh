"""The only constructor for Redis keys."""

import re

SEGMENT_REGEX = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
# "all" is the broadcast destination, so no agent may carry that name — it
# would be unaddressable.
RESERVED = {"pod", "tenant", "agent", "all"}

# ⚠ An all-digit name is unaddressable in tmux and is rejected for the same
# reason "all" is: it cannot be routed unambiguously.
#
# tmux resolves `session:2` as window *index* 2, not the window named "2".
# Measured: with windows [1:first, 2:second, 3:"2"], both `s:2` and the
# exact-name form `s:=2` resolve to `second`. So a message for an agent named
# "2" is pasted into whichever agent happens to sit at index 2 — silent
# delivery to the wrong destination, with an honest `opened` record.
#
# Hyphens and digits are fine; `sme-2` resolves correctly. Only an all-digit
# name collides.
ALL_DIGITS = re.compile(r"^[0-9]+$")

_SEGMENT = SEGMENT_REGEX
_RESERVED = RESERVED


def validate_segment(value: str | None) -> str:
    """Validate one public pod, tenant, agent, or resource-name segment."""
    if not isinstance(value, str) or not _SEGMENT.fullmatch(value) or value in _RESERVED:
        raise KeyError(value)
    if ALL_DIGITS.fullmatch(value):
        raise KeyError(value)
    return value


# Private compatibility name for the internal resource/agent helpers below.
_validate = validate_segment


def _validate_resource(value: str | None) -> str:
    # ⚠ Resources compose with a dot — `tasks.todo`, `activity.offset`,
    # `pending.verify` — and each part is validated as a segment. Do not widen
    # this to admit a name; pick a name that fits. Widening it once also
    # bypassed the all-digit rejection above, which was not the intent.
    if not isinstance(value, str) or not value:
        raise KeyError(value)
    for segment in value.split("."):
        _validate(segment)
    return value


def _validate_agent(value: str | None) -> str:
    return _validate(value)


def prefix(
    pod: str,
    tenant: str,
    agent: str | None = None,
    resource: str | None = None,
) -> str:
    """Build a structurally tenant-scoped key, validating every value segment."""
    parts = ["pod", _validate(pod), "tenant", _validate(tenant)]
    if agent is not None:
        parts.extend(("agent", _validate_agent(agent)))
    if resource is not None:
        parts.append(_validate_resource(resource))
    return ":".join(parts)


def delivery_lock_key(pod: str, tenant: str, agent: str) -> str:
    """Canonical Redis key for one destination's delivery lease."""
    return prefix(pod, tenant, agent=agent, resource="delivering")


def receive_processing_key(pod: str, tenant: str, agent: str) -> str:
    """Canonical Redis key for one destination's durably claimed envelope."""
    return prefix(pod, tenant, agent=agent, resource="processing")


def receive_opening_key(pod: str, tenant: str, agent: str) -> str:
    return prefix(pod, tenant, agent=agent, resource="opening")


def receive_opened_key(pod: str, tenant: str, agent: str) -> str:
    return prefix(pod, tenant, agent=agent, resource="opened")


def receive_unresolved_key(pod: str, tenant: str) -> str:
    return prefix(pod, tenant, resource="unresolved")


def receive_undeliverable_key(pod: str, tenant: str) -> str:
    """Tenant evidence for envelopes whose destination retired before opening."""
    return prefix(pod, tenant, resource="undeliverable")


def incarnation_key(pod: str, tenant: str, agent: str) -> str:
    """One agent's current incarnation id -- minted atomically with registry
    membership on a genuinely NEW hire (never on an idempotent re-enrol of an
    already-registered name), deleted at retirement. Absent means no
    incarnation has been established: a legacy pre-feature agent, or the
    window between a stop and that name's next hire. Consumers that bind a
    claim's validity to a specific incarnation (lib/reply_correlation.py)
    must treat an absent id as "matches nothing", never "matches anything".
    """
    return prefix(pod, tenant, agent=agent, resource="incarnation")
