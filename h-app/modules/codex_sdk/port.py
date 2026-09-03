"""Codex SDK port: one-off openai-codex thread turns, no persistent session.

Same contract as ``modules/claude_sdk/port.py`` (its docstring explains why
this is a separate module/port_type rather than a branch inside that one),
adapted to the Codex Python SDK's own shape:

- ``AsyncCodex.thread_start(ephemeral=True)`` starts a fresh thread with no
  persisted rollout file -- the Codex equivalent of Claude's
  ``CLAUDE_CODE_SKIP_PROMPT_HISTORY``, and its return is this port's pickup
  proof (a real round trip to the local ``codex app-server`` happened).
- ``thread.turn(prompt)`` returns an ``AsyncTurnHandle``; its ``.stream()``
  yields ``Notification`` objects -- ``item/completed`` (one per turn item:
  agent message, command execution, reasoning, tool call, ...),
  ``ThreadTokenUsageUpdatedNotification``, and ``turn/completed`` (the final
  status). This is the same "log every hop, not just start/end" shape as the
  claude_sdk port, built on Codex's own notification stream instead of
  Claude's message stream.

Each ``Message`` triggers exactly one turn -- no thread reuse across
deliveries, no ``resume``, and the opener sends the reply itself, same
reasons as claude_sdk. Any envelope kind other than ``Message`` is
dead-lettered by ``core.channels``'s own "unknown kind" handling.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys

import redis

from core.channels import DeadLetter, receive, send
from core.dispatch import delivery_lock
from core.keys import prefix
from core.logging import configure_logging, log_record
from lib.profile_env import read_agent_profile, resolve_cli_profile_env
from lib.reply_correlation import record_delivered


def _log_notification(
    event,
    *,
    stream_id: str | None,
    correlation_id: str | None,
    source: str,
    destination: str,
) -> str | None:
    """Log one Codex notification hop. Returns updated result text, if this
    hop carried a new agent-message item -- the caller folds that into its
    running result the same way claude_sdk's ``_log_hop`` doesn't need to
    (Claude's ResultMessage carries the final text directly; Codex's
    ``turn/completed`` doesn't, so the running agent-message text from
    ``item/completed`` hops is what this port returns).
    """
    from openai_codex._run import (
        ItemCompletedNotification,
        ThreadTokenUsageUpdatedNotification,
        TurnCompletedNotification,
    )
    from openai_codex.generated.v2_all import AgentMessageThreadItem

    common = dict(
        stream_id=stream_id, correlation_id=correlation_id,
        source=source, destination=destination,
    )
    payload = event.payload

    if isinstance(payload, ItemCompletedNotification):
        item = payload.item.root
        item_type = getattr(item, "type", type(item).__name__)
        status = getattr(item, "status", None)
        reason = f"status={status}" if status is not None else None
        log_record("codex_sdk", "codex_sdk_turn", evidence=str(item_type), reason=reason, **common)
        if isinstance(item, AgentMessageThreadItem):
            return item.text or None
        return None

    if isinstance(payload, ThreadTokenUsageUpdatedNotification):
        log_record("codex_sdk", "codex_sdk_usage", **common)
        return None

    if isinstance(payload, TurnCompletedNotification):
        turn = payload.turn
        status = getattr(turn.status, "value", str(turn.status))
        log_record(
            "codex_sdk", "codex_sdk_query_finished",
            evidence=status,
            reason=f"duration_ms={turn.duration_ms}",
            **common,
        )
        return None

    # Defensive: the Notification union can grow -- an unrecognized hop is
    # still logged, not silently dropped.
    log_record("codex_sdk", "codex_sdk_hop", evidence=type(payload).__name__, **common)
    return None


def _run_turn(
    prompt: str,
    profile_env: dict[str, str],
    *,
    stream_id: str | None,
    correlation_id: str | None,
    source: str,
    destination: str,
) -> str:
    """Run exactly one Codex turn. Returns the last agent-message text seen
    on the notification stream, or "" if none arrived.

    ``profile_env`` is passed through ``CodexConfig.env``, merged on top of
    this process's own inherited environment for the app-server process the
    SDK spawns/talks to -- same non-mutating-``os.environ`` approach as
    claude_sdk's ``_run_query``, and the same dict (``CLAUDE_CONFIG_DIR`` +
    ``CODEX_HOME`` + an optional token) works unmodified here: Codex only
    ever reads ``CODEX_HOME`` out of it, ignoring the rest.
    """
    from openai_codex import AsyncCodex, CodexConfig

    config = CodexConfig(env=dict(profile_env))

    async def _collect() -> str:
        result_text = ""
        async with AsyncCodex(config) as codex:
            thread = await codex.thread_start(ephemeral=True)
            log_record(
                "codex_sdk", "codex_sdk_query_started", evidence="started",
                stream_id=stream_id, correlation_id=correlation_id,
                source=source, destination=destination,
            )
            handle = await thread.turn(prompt)
            async for event in handle.stream():
                text = _log_notification(
                    event,
                    stream_id=stream_id, correlation_id=correlation_id,
                    source=source, destination=destination,
                )
                if text:
                    result_text = text
        return result_text

    return asyncio.run(_collect())


def _deliver_message(
    r, pod: str, tenant: str, agent: str, envelope: dict, profile_env: dict[str, str]
) -> None:
    source = envelope.get("l2", {}).get("source", "unknown")
    payload = envelope.get("payload", {})
    text = payload.get("text", "") if isinstance(payload, dict) else str(payload)
    if not text:
        # Provably pre-call: no turn has run yet, so this is a clean
        # rejection rather than an unresolved effect.
        raise DeadLetter("empty message text")

    stream_id = envelope.get("stream_id")
    result_text = _run_turn(
        f"[message from {source}] {text}",
        profile_env,
        stream_id=stream_id,
        correlation_id=envelope.get("correlation_id"),
        source=source,
        destination=agent,
    )

    # Recorded only after the turn call returns: an in_reply_to claim must
    # not validate for a delivery whose turn never actually completed.
    if stream_id:
        record_delivered(r, pod=pod, tenant=tenant, agent=agent, stream_id=stream_id, source=source)

    if not result_text.strip():
        return
    send(
        r,
        pod=pod,
        tenant=tenant,
        source=agent,
        destination=source,
        payload={"text": result_text},
        kind="Message",
        # Same convention as claude_sdk/openshell: anchor to the incoming
        # message's own stream_id, not its (possibly absent) correlation_id.
        correlation_id=stream_id,
        module="codex_sdk",
        in_reply_to=stream_id,
    )


def deliver_codex_sdk(
    r,
    pod: str,
    tenant: str,
    agent: str,
    timeout: int = 0,
    blocking: bool = False,
    **kwargs,
) -> None:
    """Drain one agent's ingress, running one Codex turn per Message."""
    profile = read_agent_profile(r, pod=pod, tenant=tenant, agent=agent)
    profile_env = resolve_cli_profile_env(profile)

    openers = {
        "Message": lambda env: _deliver_message(
            r, pod, tenant, agent, env, profile_env
        ),
    }

    receive(
        r,
        pod=pod,
        tenant=tenant,
        agent=agent,
        openers=openers,
        timeout=timeout,
        blocking=blocking,
        module="codex_sdk",
    )


def main(argv: list[str] | None = None) -> None:
    # First thing in the process, and only in the process: this is the entry
    # point, so it is the one place allowed to set the root logger's level.
    configure_logging()
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print("usage: python -m modules.codex_sdk.port <agent>", file=sys.stderr)
        sys.exit(1)
    agent = args[0]
    pod = os.environ["POD"]
    tenant = os.environ["TENANT"]
    redis_url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")

    r = redis.Redis.from_url(redis_url)
    with delivery_lock(r, pod=pod, tenant=tenant, agent=agent):
        paused_key = prefix(pod, tenant, agent=agent, resource="paused")
        if r.get(paused_key):
            return
        deliver_codex_sdk(r, pod=pod, tenant=tenant, agent=agent)


if __name__ == "__main__":
    main()
