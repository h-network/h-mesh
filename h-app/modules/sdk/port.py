"""SDK port: one-off Claude Agent SDK query() calls, no persistent session.

Unlike tmux (a persistent pane the switch pastes into) or openshell (a
resumable sandbox with its own session files), this port keeps nothing to
resume across deliveries. Each ``Message`` triggers exactly one ``query()``
call -- no ``ClaudeSDKClient``, no ``continue``/``resume``/``fork_session``
-- and the opener itself sends the reply, because there is no human or
long-lived process on the other end to notice a paste and answer later
(the same reply-inside-the-opener shape ``modules/openshell/port.py`` already
uses for the same reason).

Any envelope kind other than ``Message`` (``Command``, ``AddTicket``,
``Attachment``) is out of scope for this PoC and is dead-lettered by
``core.channels``'s own "unknown kind" handling -- nothing sdk-specific to
build for that.
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
from core.logging import configure_logging
from lib.profile_env import resolve_claude_profile_env
from lib.reply_correlation import record_delivered


def _agent_profile(r, pod: str, tenant: str, agent: str) -> str | None:
    raw = r.get(prefix(pod, tenant, agent=agent, resource="profile"))
    return raw.decode() if isinstance(raw, bytes) else raw


def _run_query(prompt: str, profile_env: dict[str, str]) -> str:
    """Run exactly one query() call against the Claude Agent SDK.

    Returns the final ``ResultMessage.result`` text, or ``""`` if the query
    ends without one. ``profile_env`` is passed through ``ClaudeAgentOptions.env``,
    which the SDK merges on top of its own inherited process environment for
    the CLI subprocess it spawns -- so one profile's ``CLAUDE_CONFIG_DIR``/
    ``CLAUDE_CODE_OAUTH_TOKEN`` never has to touch this port process's own
    ``os.environ``, and a later delivery for a different agent/profile in the
    same drained batch is unaffected.

    ``CLAUDE_CODE_SKIP_PROMPT_HISTORY`` keeps this one-off call from writing a
    session transcript to disk: this port has nothing to resume, so there is
    nothing to gain from persisting one, and every agent sharing a
    ``CLAUDE_CONFIG_DIR`` would otherwise accumulate transcripts that no port
    here will ever read back.
    """
    from claude_agent_sdk import ClaudeAgentOptions, ResultMessage
    from claude_agent_sdk import query as claude_query

    env = dict(profile_env)
    env.setdefault("CLAUDE_CODE_SKIP_PROMPT_HISTORY", "1")
    options = ClaudeAgentOptions(env=env)

    async def _collect() -> str:
        async for message in claude_query(prompt=prompt, options=options):
            if isinstance(message, ResultMessage):
                return message.result or ""
        return ""

    return asyncio.run(_collect())


def _deliver_message(
    r, pod: str, tenant: str, agent: str, envelope: dict, profile_env: dict[str, str]
) -> None:
    source = envelope.get("l2", {}).get("source", "unknown")
    payload = envelope.get("payload", {})
    text = payload.get("text", "") if isinstance(payload, dict) else str(payload)
    if not text:
        # Provably pre-call: no query() has run yet, so this is a clean
        # rejection rather than an unresolved effect.
        raise DeadLetter("empty message text")

    stream_id = envelope.get("stream_id")
    result_text = _run_query(f"[message from {source}] {text}", profile_env)

    # Recorded only after the query call returns: an in_reply_to claim must
    # not validate for a delivery whose model call never actually completed.
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
        # The reply's correlation_id anchors to the *incoming* message's own
        # stream_id, same convention modules/openshell/port.py's _reply
        # uses -- not the incoming envelope's own correlation_id, which
        # would just propagate whatever (possibly absent) thread id the
        # original sender happened to set.
        correlation_id=stream_id,
        module="sdk",
        in_reply_to=stream_id,
    )


def deliver_sdk(
    r,
    pod: str,
    tenant: str,
    agent: str,
    timeout: int = 0,
    blocking: bool = False,
    **kwargs,
) -> None:
    """Drain one agent's ingress, running one query() call per Message."""
    profile = _agent_profile(r, pod, tenant, agent)
    profile_env = resolve_claude_profile_env(profile)

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
        module="sdk",
    )


def main(argv: list[str] | None = None) -> None:
    # First thing in the process, and only in the process: this is the entry
    # point, so it is the one place allowed to set the root logger's level.
    configure_logging()
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print("usage: python -m modules.sdk.port <agent>", file=sys.stderr)
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
        deliver_sdk(r, pod=pod, tenant=tenant, agent=agent)


if __name__ == "__main__":
    main()
