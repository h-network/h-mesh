"""Agent lifecycle logic: start/stop/pause/resume.

This is the library a module uses to keep track of agent lifecycles --
it keeps track of which agent ran where, which profile.
It's used AFTER the agent has started; the starting of the agent is done by
h-agent. This library keeps track of it.
"""

import json
import time
from collections.abc import Callable
from functools import wraps
from uuid import uuid4

from core.keys import (
    SEGMENT_REGEX, incarnation_key, prefix, receive_opening_key, receive_processing_key,
    receive_undeliverable_key, receive_unresolved_key, retired_inbox_key,
)
from core.dispatch import delivery_lock_key
from core.logging import log_record
from core.policy import tags_key
from core.registry import port_type


_STARTABLE_VABS = {"tmux", "api"}
_FIXED_PARTICIPANTS = {"api", "host"}
_START_AGENT_KEYS = frozenset(
    {
        "agent", "port_type", "cli", "profile", "provider", "export", "import", "resume",
        "skip_permissions", "claude_tools", "hmac_secret", "kid", "revoke_kid",
    }
)
_MIN_HMAC_SECRET_LEN = 16
_TARGET_ONLY_KEYS = frozenset({"agent"})

_PUBLISH_WINDOW_CAUSE_LUA = """
-- Atomic, not merely isolated: the only type-sensitive write is HSET, and it
-- is the first mutation. HEXISTS performs the same hash-type check before any
-- write, and its result -- together with whether an incarnation id already
-- exists -- decides incarnation minting: a genuinely NEW membership always
-- gets a fresh id (overwriting any orphan a failed prior attempt could have
-- left); an EXISTING membership with no incarnation yet (every agent alive
-- before this binding shipped) self-heals with one fresh mint; an existing
-- membership that already has one keeps it -- an idempotent re-enrol of an
-- already-registered name must not rebind it, or a process restart would
-- invalidate its own just-established delivery claims. The following SETs
-- accept and replace a key of every Redis type.
local already_member = redis.call('HEXISTS', KEYS[2], ARGV[2])
local has_incarnation = redis.call('EXISTS', KEYS[3])
if already_member == 0 or has_incarnation == 0 then
    redis.call('SET', KEYS[3], ARGV[4])
end
redis.call('HSET', KEYS[2], ARGV[2], ARGV[3])
redis.call('SET', KEYS[1], ARGV[1])
return 1
"""

_PUBLISH_LEAD_MEMBERSHIP_LUA = """
-- Atomic through preflight, not because EVAL rolls back (it does not).
-- Registry is the only key whose command can fail with WRONGTYPE; validate it
-- as a hash before the optional cause SET becomes the first mutation. The
-- same HEXISTS-plus-EXISTS decision as _PUBLISH_WINDOW_CAUSE_LUA decides
-- incarnation minting -- see that script's comment for the three cases and
-- why idempotent re-enrols must not rebind it. Lead is claimed dynamically,
-- not by an explicit flag: the first hire to find KEYS[1] empty becomes
-- lead, and every later hire of any other name preserves the incumbent.
-- All SET writes accept and replace keys of every Redis type.
local current_lead = redis.call('GET', KEYS[1])
local already_member = redis.call('HEXISTS', KEYS[2], ARGV[1])
local has_incarnation = redis.call('EXISTS', KEYS[4])
if ARGV[3] ~= '' then
    redis.call('SET', KEYS[3], ARGV[3])
end
if already_member == 0 or has_incarnation == 0 then
    redis.call('SET', KEYS[4], ARGV[4])
end
redis.call('HSET', KEYS[2], ARGV[1], ARGV[2])
if not current_lead or current_lead == '' then
    redis.call('SET', KEYS[1], ARGV[1])
    return 1
end
return 0
"""

_PUBLISH_MEMBERSHIP_LUA = """
-- Atomic through preflight, not because EVAL rolls back (it does not).
-- Same incarnation-minting rule as _PUBLISH_WINDOW_CAUSE_LUA and
-- _PUBLISH_LEAD_MEMBERSHIP_LUA, for the plain membership-only path (no lead,
-- no fresh window cause) neither of those two Lua scripts covers.
local already_member = redis.call('HEXISTS', KEYS[1], ARGV[1])
local has_incarnation = redis.call('EXISTS', KEYS[2])
if already_member == 0 or has_incarnation == 0 then
    redis.call('SET', KEYS[2], ARGV[3])
end
redis.call('HSET', KEYS[1], ARGV[1], ARGV[2])
return 1
"""

# tools/conservation_harness.py's retirement-conservation scenario assumes
# this is one of exactly two scripts (the other is core/channels.py's
# _TRANSFER_RECEIVE_CUSTODY) that ever move a raw ENVELOPE (something with
# a `stream_id`, sourced from KEYS[3]/[4]/[5] processing/opening/ingress)
# out of custody -- a new envelope-shaped destination added here needs
# that instrument's sink list updated too (see tools/conservation_harness.md).
# The inbox-conservation branch below is NOT that: it moves stream ENTRIES
# (keyed by `entry_id`, sourced from KEYS[20], a disjoint identifier space
# with no `stream_id` at all) and does not need that instrument's scan to
# reach it -- see the .md for why that's a real distinction, not a gap.
_REMOVE_MEMBERSHIP_AND_OWN_LEAD_LUA = """
-- Atomic through deterministic preflight, not because EVAL rolls back (it
-- does not). Validate every type-sensitive source and destination, snapshot
-- every raw, and encode every evidence record before HDEL is the first write.
-- Port-type classification for the inbox branch below is read HERE, from
-- the registry itself, before it is removed -- not trusted from an earlier
-- Python-side observation, so classification stays at the same
-- linearization point as the removal it gates.
local this_port_type = redis.call('HGET', KEYS[1], ARGV[1])
redis.call('HEXISTS', KEYS[1], ARGV[1])
local current_lead = redis.call('GET', KEYS[2])
for _, spec in ipairs({{3, 'list'}, {4, 'list'}, {5, 'list'}, {17, 'list'}, {18, 'list'}, {20, 'stream'}, {21, 'list'}}) do
    local index, expected = spec[1], spec[2]
    local kind = redis.call('TYPE', KEYS[index])['ok']
    if kind ~= 'none' and kind ~= expected then
        return redis.error_reply('lifecycle custody key is not a ' .. expected .. ': ' .. KEYS[index])
    end
end

local processing = redis.call('LRANGE', KEYS[3], 0, -1)
local opening = redis.call('LRANGE', KEYS[4], 0, -1)
local ingress = redis.call('LRANGE', KEYS[5], 0, -1)
local function hex(raw)
    local encoded = {}
    for index = 1, #raw do
        encoded[index] = string.format('%02x', string.byte(raw, index))
    end
    return table.concat(encoded)
end
local undeliverable = {}
for _, raw in ipairs(processing) do
    table.insert(undeliverable, cjson.encode({
        agent=ARGV[1], reason='destination retired before opening',
        encoding='hex', envelope=hex(raw)
    }))
end
for _, raw in ipairs(ingress) do
    table.insert(undeliverable, cjson.encode({
        agent=ARGV[1], reason='destination retired before opening',
        encoding='hex', envelope=hex(raw)
    }))
end
local unresolved = {}
for _, raw in ipairs(opening) do
    table.insert(unresolved, cjson.encode({
        agent=ARGV[1], reason='opener outcome unknown when destination retired',
        encoding='hex', envelope=hex(raw)
    }))
end

-- Inbox conservation: keyed on whether a STREAM actually exists at the
-- canonical inbox resource, not on what the registry currently says this
-- agent's port type is. A valid stream there is already-delivered
-- mailbox content regardless of why it exists -- modules/api is the sole
-- writer today, but "preserve in place instead of conserving" for a
-- port-type mismatch was a real bypass in an earlier version: a same-
-- named successor hired later as an api-type agent would read straight
-- through to whatever an untouched predecessor inbox still held, the
-- exact inheritance this whole script exists to close, reached through a
-- port-type change instead of a same-type stop+rehire. So this always
-- conserves and always deletes; a non-api port type at retirement only
-- changes the RECORDED REASON, to make an operator reading retired-inbox
-- aware the source was not the expected api shape. XRANGE and encode
-- every entry before HDEL is the first mutation, same discipline as
-- everything above. Field NAMES and VALUES are hex-encoded -- Redis
-- permits arbitrary binary in either, including from a manual or legacy
-- write this script has no way to rule out -- as an ORDERED ARRAY of
-- [field_hex, value_hex] pairs (a Lua table with sequential integer keys
-- encodes as a JSON array), not a JSON object, so duplicate field names
-- and field order are preserved exactly rather than reconstructed. The
-- stream entry id itself is Redis-generated/validated ASCII
-- (milliseconds-sequence) and is stored as plain text: a real storage
-- invariant backs it, unlike the fields.
local retired_inbox = {}
local inbox_reason = 'destination retired with unread inbox content'
if this_port_type ~= 'api' then
    inbox_reason = 'destination retired with unread inbox content for a non-api port type'
end
local entries = redis.call('XRANGE', KEYS[20], '-', '+')
for _, entry in ipairs(entries) do
    local entry_id = entry[1]
    local raw_fields = entry[2]
    local fields = {}
    for index = 1, #raw_fields, 2 do
        table.insert(fields, {hex(raw_fields[index]), hex(raw_fields[index + 1])})
    end
    table.insert(retired_inbox, cjson.encode({
        agent=ARGV[1], reason=inbox_reason,
        entry_id=entry_id, encoding='hex', fields=fields
    }))
end

redis.call('HDEL', KEYS[1], ARGV[1])
if current_lead == ARGV[1] then
    redis.call('DEL', KEYS[2])
end
-- Keep every mutation's command arity constant. Expanding either unbounded
-- table with unpack() can itself fail after HDEL (Lua stack/argument limit),
-- and EVAL would preserve that partial membership removal.
for _, record in ipairs(undeliverable) do
    redis.call('RPUSH', KEYS[17], record)
end
for _, record in ipairs(unresolved) do
    redis.call('RPUSH', KEYS[18], record)
end
for _, record in ipairs(retired_inbox) do
    redis.call('RPUSH', KEYS[21], record)
end
-- Unconditional, matching the unconditional read/conserve above -- a
-- no-op if no inbox stream ever existed at this key.
redis.call('DEL', KEYS[20])
redis.call('DEL', KEYS[3])
redis.call('DEL', KEYS[4])
redis.call('DEL', KEYS[5])
-- Opened receipts are deliberately absent from KEYS: completed effects are
-- never replayed, and their bounded acknowledgement evidence survives stop.
-- Pause state and the delivery lease are disposable instance state.
redis.call('DEL', KEYS[6])
redis.call('DEL', KEYS[7])
-- Desired launch identity and security configuration must not cross a
-- stop/re-hire boundary when the successor omits optional StartAgent fields.
redis.call('DEL', KEYS[8])
redis.call('DEL', KEYS[9])
redis.call('DEL', KEYS[10])
redis.call('DEL', KEYS[11])
redis.call('DEL', KEYS[12])
redis.call('DEL', KEYS[13])
redis.call('DEL', KEYS[14])
redis.call('DEL', KEYS[15])
redis.call('DEL', KEYS[16])
-- The incarnation id: deleting it here, rather than at the next hire,
-- means the id exists for exactly the span between a real stop and that
-- name's next hire. A later start_agent (genuine re-hire or an ordinary
-- restart) mints a fresh one only if it finds this key already absent --
-- see _PUBLISH_MEMBERSHIP_LUA and its siblings.
redis.call('DEL', KEYS[19])
return {#processing, #ingress, #opening, #retired_inbox}
"""


class _IncompleteLifecycle(RuntimeError):
    """A desired or actual-state attempt has an UNKNOWN outcome."""


class ProvableActualFailure(RuntimeError):
    """An actual-state action was observably rejected and did not occur."""


class ProvableLifecycleRejection(ValueError):
    """Lifecycle validation rejected the request before any mutation began."""


class _PartialLifecycle(RuntimeError):
    """Some work was acknowledged before a later action provably failed."""


def _lifecycle_log(event: str, **fields) -> None:
    """Secondary observation must never replace lifecycle classification."""
    try:
        log_record("agentlifecycle", event, **fields)
    except Exception:
        pass


def _record_lifecycle(kind: str):
    """Record accepted, partially failed, incomplete, or pre-mutation failure outcomes."""
    def decorate(opener):
        @wraps(opener)
        def recorded(r, *, pod, tenant, envelope, **kwargs):
            payload = envelope.get("payload", {}) if isinstance(envelope, dict) else {}
            agent = payload.get("agent") if isinstance(payload, dict) else None
            correlation_id = envelope.get("correlation_id") if isinstance(envelope, dict) else None
            try:
                result = opener(r, pod=pod, tenant=tenant, envelope=envelope, **kwargs)
            except _PartialLifecycle as exc:
                _lifecycle_log(
                    f"{kind}_partially_failed", correlation_id=correlation_id,
                    destination=agent if isinstance(agent, str) else None,
                    reason=str(exc),
                )
                raise
            except _IncompleteLifecycle as exc:
                _lifecycle_log(
                    f"{kind}_incomplete", correlation_id=correlation_id,
                    destination=agent if isinstance(agent, str) else None,
                    reason=str(exc),
                )
                raise
            except Exception as exc:
                _lifecycle_log(
                    f"{kind}_failed", correlation_id=correlation_id,
                    destination=agent if isinstance(agent, str) else None,
                    reason=str(exc) or type(exc).__name__,
                )
                raise
            fields = {"correlation_id": correlation_id,
                      "destination": agent if isinstance(agent, str) else None}
            if result in ("lead_claimed", "lead_preserved"):
                fields["lead_outcome"] = result
            _lifecycle_log(f"{kind}_accepted", **fields)
            return result
        return recorded
    return decorate


def _write_desired(
    committed: list[str],
    committed_label: str,
    failure_label: str,
    mutation: Callable[[], object],
) -> object:
    """Run one desired-state write and preserve the observed commit boundary."""
    try:
        result = mutation()
    except Exception as exc:
        acknowledged = f"acknowledged: {', '.join(committed)}" if committed else "none acknowledged"
        raise _IncompleteLifecycle(
            f"{acknowledged}; {failure_label} outcome UNKNOWN after {exc}"
        ) from exc
    committed.append(committed_label)
    return result


def _actual_unknown(
    committed: list[str],
    action: str,
    exc: Exception,
    *,
    actual_acknowledged: list[str] | None = None,
) -> _IncompleteLifecycle:
    """Describe observed acknowledgements separately from an unanswered attempt."""
    parts = [f"acknowledged: {', '.join(committed)}"]
    if actual_acknowledged:
        parts.append(f"actual acknowledged: {', '.join(actual_acknowledged)}")
    parts.append(f"{action} outcome UNKNOWN after {exc}")
    return _IncompleteLifecycle("; ".join(parts))


def _actual_failed(
    committed: list[str],
    action: str,
    exc: ProvableActualFailure,
    *,
    actual_acknowledged: list[str] | None = None,
) -> _PartialLifecycle:
    """Name acknowledged work and a later actual-state action known not to occur."""
    parts = [f"acknowledged: {', '.join(committed)}"]
    if actual_acknowledged:
        parts.append(f"actual acknowledged: {', '.join(actual_acknowledged)}")
    parts.append(f"{action} failed: {exc}")
    return _PartialLifecycle("; ".join(parts))


def _target(envelope: dict, allowed_keys: frozenset[str]) -> tuple[str, dict]:
    if not isinstance(envelope, dict):
        raise ProvableLifecycleRejection("lifecycle envelope must be an object")
    try:
        payload = envelope["payload"]
    except KeyError as exc:
        raise ProvableLifecycleRejection("lifecycle envelope requires payload") from exc
    if not isinstance(payload, dict):
        raise ProvableLifecycleRejection("lifecycle payload must be an object")
    unknown_keys = sorted(set(payload) - allowed_keys)
    if unknown_keys:
        raise ProvableLifecycleRejection(f"unknown payload key {unknown_keys[0]!r}")
    agent = payload.get("agent")
    if not isinstance(agent, str):
        raise ProvableLifecycleRejection("lifecycle payload.agent must be a string")
    # Constructing an agent key validates the target before any state changes.
    try:
        prefix("check", "check", agent=agent, resource="launch")
    except KeyError as exc:
        raise ProvableLifecycleRejection(str(exc)) from exc
    return agent, payload


@_record_lifecycle("start_agent")
def start_agent(
    r,
    *,
    pod: str,
    tenant: str,
    envelope: dict,
    replace_window: Callable[[str], object],
    available_profiles: Callable[[str, str], list[str] | None],
) -> None:
    """Publish desired state using a tenant-wide profile lookup supplied by the caller."""
    agent, payload = _target(envelope, _START_AGENT_KEYS)
    policy = {}
    policy_supplied = any(side in payload for side in ("export", "import"))
    for side in ("export", "import"):
        values = payload.get(side)
        if values is None:
            continue
        if (
            not isinstance(values, list)
            or not all(isinstance(value, str) and SEGMENT_REGEX.fullmatch(value) for value in values)
        ):
            raise ProvableLifecycleRejection(
                f"StartAgent payload.{side} must be a list of tag names"
            )
        policy[side] = sorted(set(values))

    agent_port_type = payload.get("port_type", "tmux")
    if agent_port_type not in _STARTABLE_VABS:
        raise ProvableLifecycleRejection(
            f"StartAgent payload.port_type must be one of: {', '.join(sorted(_STARTABLE_VABS))}"
        )


    hmac_secret = payload.get("hmac_secret")
    kid = payload.get("kid")
    revoke_kid = payload.get("revoke_kid")
    if (hmac_secret is None) != (kid is None):
        raise ProvableLifecycleRejection(
            "StartAgent payload.hmac_secret and payload.kid must be supplied together"
        )
    if kid is not None and not (isinstance(kid, str) and SEGMENT_REGEX.fullmatch(kid)):
        raise ProvableLifecycleRejection("StartAgent payload.kid must be a segment string")
    if hmac_secret is not None and not (
        isinstance(hmac_secret, str) and len(hmac_secret) >= _MIN_HMAC_SECRET_LEN
    ):
        raise ProvableLifecycleRejection(
            f"StartAgent payload.hmac_secret must be a string of at least {_MIN_HMAC_SECRET_LEN} characters"
        )
    if revoke_kid is not None and not (isinstance(revoke_kid, str) and SEGMENT_REGEX.fullmatch(revoke_kid)):
        raise ProvableLifecycleRejection("StartAgent payload.revoke_kid must be a segment string")
    if agent_port_type != "api" and (kid is not None or revoke_kid is not None):
        raise ProvableLifecycleRejection(
            "StartAgent payload.hmac_secret/kid/revoke_kid only apply to port_type 'api'"
        )

    # Complete validation, key construction, and fallible serialization before
    # the first desired-state mutation. This is the only boundary from which a
    # validation rejection can truthfully be labelled pre-mutation.
    serialized_policy = {
        side: json.dumps(values, separators=(",", ":"))
        for side, values in policy.items()
    }
    hmac_keys_key = prefix(pod, tenant, agent=agent, resource="hmac-keys")
    policy_key = tags_key(pod, tenant, agent)
    key_record = (
        json.dumps({"secret": hmac_secret, "created_ts": time.time()}, separators=(",", ":"))
        if kid is not None else None
    )
    registry_key = prefix(pod, tenant, resource="registry")
    launch_key = prefix(pod, tenant, agent=agent, resource="launch")
    profile_key = prefix(pod, tenant, agent=agent, resource="profile")
    provider_key = prefix(pod, tenant, agent=agent, resource="provider")
    resume_key = prefix(pod, tenant, agent=agent, resource="resume")
    skip_key = prefix(pod, tenant, agent=agent, resource="skip-permissions")
    tools_key = prefix(pod, tenant, agent=agent, resource="claude-tools")
    cause_key = prefix(pod, tenant, agent=agent, resource="window.cause")

    cli = payload.get("cli", "claude")
    profile = payload.get("profile")
    provider = payload.get("provider")
    resume = payload.get("resume")
    skip_permissions = payload.get("skip_permissions")
    claude_tools = payload.get("claude_tools") if "claude_tools" in payload else None
    if agent_port_type == "tmux":
        if not isinstance(cli, str) or not cli:
            raise ProvableLifecycleRejection("StartAgent payload.cli must be a non-empty string")
        if profile:
            try:
                prefix("check", "check", agent=profile, resource="profile")
            except KeyError as exc:
                raise ProvableLifecycleRejection(str(exc)) from exc
            profiles = available_profiles(pod, tenant)
            if profiles is not None and profile not in profiles:
                raise ProvableLifecycleRejection(
                    f"unknown account {profile!r}; available accounts: {', '.join(profiles)}"
                )
        elif profile not in (None, ""):
            raise ProvableLifecycleRejection("StartAgent payload.profile must be a segment string")
        if provider:
            try:
                prefix("check", "check", agent=provider, resource="provider")
            except KeyError as exc:
                raise ProvableLifecycleRejection(str(exc)) from exc
        elif provider not in (None, ""):
            raise ProvableLifecycleRejection("StartAgent payload.provider must be a segment string")
        if resume is not None and not isinstance(resume, bool):
            raise ProvableLifecycleRejection("StartAgent payload.resume must be a boolean")
        if skip_permissions is not None and not isinstance(skip_permissions, bool):
            raise ProvableLifecycleRejection(
                "StartAgent payload.skip_permissions must be a boolean"
            )
        if "claude_tools" in payload and not isinstance(claude_tools, str):
            raise ProvableLifecycleRejection("StartAgent payload.claude_tools must be a string")

    committed: list[str] = []
    if agent_port_type == "api":
        # Both self-supplied: the enrolling client generates its own secret and
        # hands it to lifecycle, rather than lifecycle minting one it has no
        # synchronous channel to hand back over (StartAgent is fire-and-forget,
        # HMAC verification needs the same secret on both sides to
        # recompute the MAC, so it is stored in the clear here, not hashed —
        # unlike a password hash, a digest of the secret could never reproduce
        # a matching signature.
        if revoke_kid is not None:
            _write_desired(
                committed, f"hmac key {revoke_kid!r} revoked", "hmac key revoke",
                lambda: r.hdel(hmac_keys_key, revoke_kid),
            )
        if kid is not None:
            _write_desired(
                committed, f"hmac key {kid!r} published", "hmac key publish",
                lambda: r.hset(hmac_keys_key, kid, key_record),
            )
        if policy_supplied:
            _write_desired(
                committed, "policy reset", "policy reset", lambda: r.delete(policy_key)
            )
            for side, values in policy.items():
                _write_desired(
                    committed, f"{side} policy published", f"{side} policy publish",
                    lambda side=side, values=values: r.hset(
                        policy_key, side, serialized_policy[side]
                    ),
                )
        _write_desired(
            committed, "registry row published", "registry row publish",
            lambda: r.eval(
                _PUBLISH_MEMBERSHIP_LUA,
                2,
                registry_key,
                incarnation_key(pod, tenant, agent),
                agent,
                agent_port_type,
                uuid4().hex,
            ),
        )
        return

    existing_port_type = port_type(r, pod=pod, tenant=tenant, agent=agent)
    old_launch = r.get(launch_key) if existing_port_type == "tmux" else None
    old_launch = old_launch.decode() if isinstance(old_launch, bytes) else old_launch

    config_changed = existing_port_type == "tmux" and old_launch != cli
    if profile:
        # A profile becomes part of a config-directory path. Validate it before
        # any state mutation, then persist it before registry visibility: tmuxhost
        # may reconcile as soon as the row appears and must see the right account.
        old_profile = r.get(profile_key) if existing_port_type == "tmux" else None
        old_profile = old_profile.decode() if isinstance(old_profile, bytes) else old_profile
        config_changed = config_changed or (existing_port_type == "tmux" and old_profile != profile)
        _write_desired(
            committed, "profile published", "profile publish",
            lambda: r.set(profile_key, profile),
        )

    if provider:
        # Same ordering rule as profile: published before registry visibility, or
        # tmuxhost builds the window against the vendor's provider instead.
        old_provider = r.get(provider_key) if existing_port_type == "tmux" else None
        old_provider = old_provider.decode() if isinstance(old_provider, bytes) else old_provider
        config_changed = config_changed or (existing_port_type == "tmux" and old_provider != provider)
        _write_desired(
            committed, "provider published", "provider publish",
            lambda: r.set(provider_key, provider),
        )

    # Publish all launch state before registry membership: tmuxhost reconciles on
    # that row and an early window cannot be corrected by name-idempotent create.
    _write_desired(
        committed, "launch published", "launch publish", lambda: r.set(launch_key, cli)
    )

    if resume is not None:
        old_resume = r.get(resume_key) if existing_port_type == "tmux" else None
        old_resume = old_resume.decode() if isinstance(old_resume, bytes) else old_resume
        desired_resume = "1" if resume else "0"
        config_changed = config_changed or (existing_port_type == "tmux" and old_resume != desired_resume)
        _write_desired(
            committed, "resume published", "resume publish",
            lambda: r.set(resume_key, desired_resume),
        )

    if skip_permissions is not None:
        old_skip = r.get(skip_key) if existing_port_type == "tmux" else None
        old_skip = old_skip.decode() if isinstance(old_skip, bytes) else old_skip
        desired_skip = "1" if skip_permissions else "0"
        config_changed = config_changed or (existing_port_type == "tmux" and old_skip != desired_skip)
        _write_desired(
            committed, "skip_permissions published", "skip_permissions publish",
            lambda: r.set(skip_key, desired_skip),
        )

    # ⚠ Presence, not truthiness: `""` (unrestricted tools) is a valid desired
    # value, distinct from the key being absent from the payload at all — same
    # "absent is not empty" rule `window_env` applies on the way out.
    if "claude_tools" in payload:
        old_tools = r.get(tools_key) if existing_port_type == "tmux" else None
        old_tools = old_tools.decode() if isinstance(old_tools, bytes) else old_tools
        config_changed = config_changed or (existing_port_type == "tmux" and old_tools != claude_tools)
        _write_desired(
            committed, "claude_tools published", "claude_tools publish",
            lambda: r.set(tools_key, claude_tools),
        )
    if policy_supplied:
        _write_desired(
            committed, "policy reset", "policy reset", lambda: r.delete(policy_key)
        )
        for side, values in policy.items():
            _write_desired(
                committed, f"{side} policy published", f"{side} policy publish",
                lambda side=side, values=values: r.hset(
                    policy_key, side, serialized_policy[side]
                ),
            )
    correlation_id = envelope.get("correlation_id")
    cause = correlation_id if (
        existing_port_type != "tmux" and isinstance(correlation_id, str) and correlation_id
    ) else ""
    # Generated unconditionally, written only by whichever Lua branch below
    # finds this is a genuinely new membership (HEXISTS was false) --
    # wasted entropy on an idempotent re-enrol costs nothing, and keeping
    # the mint decision inside the SAME atomic script as the registry
    # HSET (rather than a separate SETNX call before or after it) is load
    # -bearing: a start that mints an incarnation and then fails or races
    # before its OWN registry write must not leave an orphaned id a later,
    # unrelated hire of the same name could silently inherit.
    fresh_incarnation = uuid4().hex
    if agent_port_type == "tmux":
        lead_result = _write_desired(
            committed,
            "lead and registry row published",
            "lead and registry row publish",
            lambda: r.eval(
                _PUBLISH_LEAD_MEMBERSHIP_LUA,
                4,
                prefix(pod, tenant, resource="lead"),
                registry_key,
                prefix(pod, tenant, agent=agent, resource="window.cause"),
                incarnation_key(pod, tenant, agent),
                agent,
                agent_port_type,
                cause,
                fresh_incarnation,
            ),
        )
    elif cause:
        # A fresh tmux membership makes a later window necessary. Publish its
        # cause before registry visibility so tmuxhost cannot observe the hire
        # without also observing the join key. Idempotent starts do not replace
        # this marker: their envelope did not cause a new window.
        _write_desired(
            committed,
            "window cause and registry row published",
            "window cause and registry row publish",
            lambda: r.eval(
                _PUBLISH_WINDOW_CAUSE_LUA,
                3,
                cause_key,
                registry_key,
                incarnation_key(pod, tenant, agent),
                cause,
                agent,
                agent_port_type,
                fresh_incarnation,
            ),
        )
    else:
        _write_desired(
            committed, "registry row published", "registry row publish",
            lambda: r.eval(
                _PUBLISH_MEMBERSHIP_LUA,
                2,
                registry_key,
                incarnation_key(pod, tenant, agent),
                agent,
                agent_port_type,
                fresh_incarnation,
            ),
        )
    if config_changed:
        # Remove only stale actual state. tmuxhost observes the registry row and
        # recreates the window through its canonical lead/profile/provider path.
        try:
            replace_window(agent)
        except Exception as exc:
            raise _actual_unknown(committed, "replacing the stale window", exc) from exc
    if agent_port_type == "tmux":
        return "lead_claimed" if lead_result == 1 else "lead_preserved"


@_record_lifecycle("stop_agent")
def stop_agent(
    r,
    *,
    pod: str,
    tenant: str,
    envelope: dict,
    kill_window: Callable[[str], object],
) -> None:
    """Remove desired state, then any port_type-specific state or actual window."""
    agent, _ = _target(envelope, _TARGET_ONLY_KEYS)
    if agent in _FIXED_PARTICIPANTS:
        raise ProvableLifecycleRejection(f"cannot stop fixed participant: {agent}")
    registry_key = prefix(pod, tenant, resource="registry")
    agent_port_type = port_type(r, pod=pod, tenant=tenant, agent=agent)
    committed: list[str] = []
    _write_desired(
        committed, "registry row removed and owned lead cleared", "registry/lead removal",
        lambda: r.eval(
            _REMOVE_MEMBERSHIP_AND_OWN_LEAD_LUA,
            21,
            registry_key,
            prefix(pod, tenant, resource="lead"),
            receive_processing_key(pod, tenant, agent),
            receive_opening_key(pod, tenant, agent),
            prefix(pod, tenant, agent=agent, resource="ingress"),
            prefix(pod, tenant, agent=agent, resource="paused"),
            delivery_lock_key(pod, tenant, agent),
            prefix(pod, tenant, agent=agent, resource="launch"),
            prefix(pod, tenant, agent=agent, resource="profile"),
            prefix(pod, tenant, agent=agent, resource="provider"),
            prefix(pod, tenant, agent=agent, resource="resume"),
            prefix(pod, tenant, agent=agent, resource="skip-permissions"),
            prefix(pod, tenant, agent=agent, resource="claude-tools"),
            tags_key(pod, tenant, agent),
            prefix(pod, tenant, agent=agent, resource="hmac-keys"),
            prefix(pod, tenant, agent=agent, resource="window.cause"),
            receive_undeliverable_key(pod, tenant),
            receive_unresolved_key(pod, tenant),
            incarnation_key(pod, tenant, agent),
            prefix(pod, tenant, agent=agent, resource="inbox"),
            retired_inbox_key(pod, tenant),
            agent,
        ),
    )
    if agent_port_type != "api":
        try:
            kill_window(agent)
        except Exception as exc:
            raise _actual_unknown(committed, "killing the window", exc) from exc


@_record_lifecycle("pause_agent")
def pause_agent(
    r,
    *,
    pod: str,
    tenant: str,
    envelope: dict,
    interrupt_window: Callable[[str], object],
) -> None:
    """Mark an agent paused, then interrupt its CLI without changing membership."""
    agent, _ = _target(envelope, _TARGET_ONLY_KEYS)
    committed: list[str] = []
    _write_desired(
        committed, "paused marker published", "paused marker publish",
        lambda: r.set(prefix(pod, tenant, agent=agent, resource="paused"), 1),
    )
    try:
        interrupt_window(agent)
    except Exception as exc:
        raise _actual_unknown(committed, "interrupting the window", exc) from exc


@_record_lifecycle("resume_agent")
def resume_agent(
    r,
    *,
    pod: str,
    tenant: str,
    envelope: dict,
    resume_window: Callable[[str], object],
    kick_agent: Callable[[str], object],
) -> None:
    """Clear pause, resume the CLI, then kick once per queued ingress envelope."""
    agent, _ = _target(envelope, _TARGET_ONLY_KEYS)
    committed: list[str] = []
    _write_desired(
        committed, "paused marker removed", "paused marker removal",
        lambda: r.delete(prefix(pod, tenant, agent=agent, resource="paused")),
    )
    actual_acknowledged: list[str] = []
    try:
        resume_window(agent)
        actual_acknowledged.append("window resumed")
    except Exception as exc:
        raise _actual_unknown(committed, "resuming the window", exc) from exc
    try:
        depth = r.llen(prefix(pod, tenant, agent=agent, resource="ingress"))
    except Exception as exc:
        raise _actual_unknown(
            committed, "reading ingress depth", exc,
            actual_acknowledged=actual_acknowledged,
        ) from exc
    for index in range(depth):
        try:
            kick_agent(agent)
            actual_acknowledged.append(f"kick {index + 1}")
        except ProvableActualFailure as exc:
            raise _actual_failed(
                committed, f"kick {index + 1}", exc,
                actual_acknowledged=actual_acknowledged,
            ) from exc
        except Exception as exc:
            raise _actual_unknown(
                committed, f"kick {index + 1}", exc,
                actual_acknowledged=actual_acknowledged,
            ) from exc
