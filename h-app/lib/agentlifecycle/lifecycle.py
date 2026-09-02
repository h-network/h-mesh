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

from core.keys import (
    SEGMENT_REGEX, prefix, receive_opened_key, receive_opening_key,
    receive_processing_key,
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
        "lead",
    }
)
_MIN_HMAC_SECRET_LEN = 16
_TARGET_ONLY_KEYS = frozenset({"agent"})

_PUBLISH_WINDOW_CAUSE_LUA = """
-- Atomic, not merely isolated: the only type-sensitive write is HSET, and it
-- is the first mutation. HEXISTS performs the same hash-type check before any
-- write; the following SET accepts and replaces a key of every Redis type.
redis.call('HEXISTS', KEYS[2], ARGV[2])
redis.call('HSET', KEYS[2], ARGV[2], ARGV[3])
redis.call('SET', KEYS[1], ARGV[1])
return 1
"""

_PUBLISH_LEAD_MEMBERSHIP_LUA = """
-- Atomic through preflight, not because EVAL rolls back (it does not).
-- Registry is the only key whose command can fail with WRONGTYPE; validate it
-- as a hash before the optional cause SET becomes the first mutation. Both SET
-- writes accept and replace keys of every Redis type.
redis.call('HEXISTS', KEYS[2], ARGV[1])
if ARGV[3] ~= '' then
    redis.call('SET', KEYS[3], ARGV[3])
end
redis.call('HSET', KEYS[2], ARGV[1], ARGV[2])
redis.call('SET', KEYS[1], ARGV[1])
return 1
"""

_REMOVE_MEMBERSHIP_AND_OWN_LEAD_LUA = """
-- Atomic through preflight, not because EVAL rolls back (it does not). Check
-- both type-sensitive reads before HDEL becomes the first mutation; otherwise
-- a WRONGTYPE from GET would leave membership removed but the lead untouched.
redis.call('HEXISTS', KEYS[1], ARGV[1])
local current_lead = redis.call('GET', KEYS[2])
redis.call('HDEL', KEYS[1], ARGV[1])
if current_lead == ARGV[1] then
    redis.call('DEL', KEYS[2])
end
-- In-flight receive custody and bounded completion receipts belong to the
-- membership removed at this linearization point. Keeping these DELs here
-- prevents a later hire reusing ARGV[1] from losing successor state.
redis.call('DEL', KEYS[3])
redis.call('DEL', KEYS[4])
redis.call('DEL', KEYS[5])
-- Ingress, pause state, and the delivery lease belong to the same removed
-- membership. Deleting them inside this isolated script prevents cleanup for
-- the predecessor from racing a successor published after this script.
-- DEL accepts keys of every Redis type, so the two reads above are the full
-- runtime-error preflight before HDEL becomes the first mutation.
redis.call('DEL', KEYS[6])
redis.call('DEL', KEYS[7])
redis.call('DEL', KEYS[8])
-- Desired launch identity and security configuration must not cross a
-- stop/re-hire boundary when the successor omits optional StartAgent fields.
redis.call('DEL', KEYS[9])
redis.call('DEL', KEYS[10])
redis.call('DEL', KEYS[11])
redis.call('DEL', KEYS[12])
redis.call('DEL', KEYS[13])
redis.call('DEL', KEYS[14])
redis.call('DEL', KEYS[15])
redis.call('DEL', KEYS[16])
redis.call('DEL', KEYS[17])
return 1
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
            _lifecycle_log(
                f"{kind}_accepted", correlation_id=correlation_id,
                destination=agent if isinstance(agent, str) else None,
            )
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

    make_lead = payload.get("lead", False)
    if not isinstance(make_lead, bool):
        raise ProvableLifecycleRejection("StartAgent payload.lead must be a boolean")
    if make_lead and agent_port_type != "tmux":
        raise ProvableLifecycleRejection("StartAgent payload.lead only applies to port_type 'tmux'")

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
            lambda: r.hset(registry_key, agent, agent_port_type),
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
    if make_lead:
        _write_desired(
            committed,
            "lead and registry row published",
            "lead and registry row publish",
            lambda: r.eval(
                _PUBLISH_LEAD_MEMBERSHIP_LUA,
                3,
                prefix(pod, tenant, resource="lead"),
                registry_key,
                prefix(pod, tenant, agent=agent, resource="window.cause"),
                agent,
                agent_port_type,
                cause,
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
                2,
                cause_key,
                registry_key,
                cause,
                agent,
                agent_port_type,
            ),
        )
    else:
        _write_desired(
            committed, "registry row published", "registry row publish",
            lambda: r.hset(registry_key, agent, agent_port_type),
        )
    if config_changed:
        # Remove only stale actual state. tmuxhost observes the registry row and
        # recreates the window through its canonical lead/profile/provider path.
        try:
            replace_window(agent)
        except Exception as exc:
            raise _actual_unknown(committed, "replacing the stale window", exc) from exc


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
            17,
            registry_key,
            prefix(pod, tenant, resource="lead"),
            receive_processing_key(pod, tenant, agent),
            receive_opening_key(pod, tenant, agent),
            receive_opened_key(pod, tenant, agent),
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
