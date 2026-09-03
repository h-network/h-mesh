"""Agy (Antigravity) SDK port: one-off google-antigravity chats, no
persistent session.

Same contract as ``modules/claude_sdk/port.py`` and ``modules/codex_sdk/port.py``
(the former's docstring explains why each vendor is its own module/port_type
rather than a branch inside one), adapted to Agy's own shape:

- ``Agent(config)`` is an async context manager; entering it is this port's
  pickup proof (the SDK's own lifecycle -- binary discovery, tool wiring,
  policy defaults -- has actually run by the time ``__aenter__`` returns).
- ``await agent.chat(prompt)`` returns a ``ChatResponse`` whose ``.chunks``
  is an ``AsyncIterator[StreamChunk | ToolCall | ToolResult]`` -- the same
  "log every hop" shape as the other two ports, built on Agy's own semantic
  chunk stream instead of Claude's message stream or Codex's notification
  stream: ``Text``/``Thought`` deltas, ``ToolCall``, ``ToolResult``, with a
  generic fallback for anything unrecognized.
- ``SessionContinuationMode.CREATE_ONLY`` is the closest available
  parity with claude_sdk's ``CLAUDE_CODE_SKIP_PROMPT_HISTORY`` and
  codex_sdk's ``ephemeral=True``: it guarantees this call creates a fresh
  conversation and never resumes a prior one, though (unlike those two
  vendors' explicit flags) Agy exposes no equivalent "don't persist
  anything to disk" switch at this API level.

Credentials aren't the ``CLAUDE_CONFIG_DIR``/``CODEX_HOME`` directory
convention the other two ports share: Agy takes a plain API key
(``LocalAgentConfig(api_key=...)``), resolved via
``lib.profile_env.resolve_agy_api_key`` rather than
``resolve_cli_profile_env`` -- see that function's docstring.

Each ``Message`` triggers exactly one chat call -- no ``conversation_id``
reuse across deliveries -- and the opener sends the reply itself, same
reasons as the other two ports. Any envelope kind other than ``Message`` is
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
from lib.profile_env import read_agent_profile, resolve_agy_api_key
from lib.reply_correlation import record_delivered


def _log_chunk(
    chunk,
    *,
    stream_id: str | None,
    correlation_id: str | None,
    source: str,
    destination: str,
) -> str | None:
    """Log one Agy stream chunk. Returns text to fold into the running
    result if this chunk carried one (a ``Text`` delta) -- ``ChatResponse``
    has no single "final text" field the way Claude's ``ResultMessage``
    does, so the running concatenation of ``Text`` deltas is this port's
    result, same shape as codex_sdk's running ``AgentMessageThreadItem``
    text.
    """
    from google.antigravity.types import Text, Thought, ToolCall, ToolResult

    common = dict(
        stream_id=stream_id, correlation_id=correlation_id,
        source=source, destination=destination,
    )

    if isinstance(chunk, Text):
        # No raw text logged -- metadata only, same convention as every
        # other hop in every port here.
        log_record("agy_sdk", "agy_sdk_text", **common)
        return chunk.text or None

    if isinstance(chunk, Thought):
        log_record("agy_sdk", "agy_sdk_thought", **common)
        return None

    if isinstance(chunk, ToolCall):
        log_record("agy_sdk", "agy_sdk_tool_call", evidence=chunk.name, **common)
        return None

    if isinstance(chunk, ToolResult):
        reason = f"error={bool(chunk.error)}"
        log_record("agy_sdk", "agy_sdk_tool_result", evidence=chunk.name, reason=reason, **common)
        return None

    # Defensive: the StreamChunk union can grow -- an unrecognized hop is
    # still logged, not silently dropped.
    log_record("agy_sdk", "agy_sdk_hop", evidence=type(chunk).__name__, **common)
    return None


def _run_chat(
    prompt: str,
    api_key: str | None,
    *,
    stream_id: str | None,
    correlation_id: str | None,
    source: str,
    destination: str,
) -> str:
    """Run exactly one Agy chat turn. Returns the concatenated text of every
    ``Text`` delta seen on the chunk stream, or "" if none arrived.

    ``api_key`` is passed straight through to ``LocalAgentConfig`` -- when
    None, the SDK falls back to its own resolution (``GEMINI_API_KEY``, or
    Vertex ADC), the same "let the vendor's own default apply" posture
    claude_sdk/codex_sdk take when no profile-scoped credential is
    configured.
    """
    from google.antigravity import Agent, LocalAgentConfig
    from google.antigravity.types import SessionContinuationMode

    config = LocalAgentConfig(
        api_key=api_key,
        session_continuation_mode=SessionContinuationMode.CREATE_ONLY,
    )

    async def _collect() -> str:
        result_text = ""
        async with Agent(config) as agent:
            log_record(
                "agy_sdk", "agy_sdk_query_started", evidence="started",
                stream_id=stream_id, correlation_id=correlation_id,
                source=source, destination=destination,
            )
            response = await agent.chat(prompt)
            async for chunk in response.chunks:
                text = _log_chunk(
                    chunk,
                    stream_id=stream_id, correlation_id=correlation_id,
                    source=source, destination=destination,
                )
                if text:
                    result_text += text
            log_record(
                "agy_sdk", "agy_sdk_query_finished",
                evidence=str(response.stop_reason),
                stream_id=stream_id, correlation_id=correlation_id,
                source=source, destination=destination,
            )
        return result_text

    return asyncio.run(_collect())


def _deliver_message(
    r, pod: str, tenant: str, agent: str, envelope: dict, api_key: str | None
) -> None:
    source = envelope.get("l2", {}).get("source", "unknown")
    payload = envelope.get("payload", {})
    text = payload.get("text", "") if isinstance(payload, dict) else str(payload)
    if not text:
        # Provably pre-call: no chat has run yet, so this is a clean
        # rejection rather than an unresolved effect.
        raise DeadLetter("empty message text")

    stream_id = envelope.get("stream_id")
    result_text = _run_chat(
        f"[message from {source}] {text}",
        api_key,
        stream_id=stream_id,
        correlation_id=envelope.get("correlation_id"),
        source=source,
        destination=agent,
    )

    # Recorded only after the chat call returns: an in_reply_to claim must
    # not validate for a delivery whose chat never actually completed.
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
        # Same convention as claude_sdk/codex_sdk/openshell: anchor to the
        # incoming message's own stream_id, not its (possibly absent)
        # correlation_id.
        correlation_id=stream_id,
        module="agy_sdk",
        in_reply_to=stream_id,
    )


def deliver_agy_sdk(
    r,
    pod: str,
    tenant: str,
    agent: str,
    timeout: int = 0,
    blocking: bool = False,
    **kwargs,
) -> None:
    """Drain one agent's ingress, running one Agy chat call per Message."""
    profile = read_agent_profile(r, pod=pod, tenant=tenant, agent=agent)
    api_key = resolve_agy_api_key(profile)

    openers = {
        "Message": lambda env: _deliver_message(
            r, pod, tenant, agent, env, api_key
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
        module="agy_sdk",
    )


def main(argv: list[str] | None = None) -> None:
    # First thing in the process, and only in the process: this is the entry
    # point, so it is the one place allowed to set the root logger's level.
    configure_logging()
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print("usage: python -m modules.agy_sdk.port <agent>", file=sys.stderr)
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
        deliver_agy_sdk(r, pod=pod, tenant=tenant, agent=agent)


if __name__ == "__main__":
    main()
