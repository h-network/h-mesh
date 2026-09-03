"""Claude SDK port: one-off Claude Agent SDK query() calls, no persistent session.

Named for the vendor SDK it wraps, not "sdk" generically -- a Codex or
Agy/Antigravity SDK port would be its own separate module and port_type
(``modules/codex_sdk/``, ``modules/agy_sdk/``, ...), each independently
addable the same way this one was, not branches inside this file. The three
vendor SDKs are different enough (different libraries, different call
shapes, different auth wiring) that unifying them the way tmux unifies
claude/codex/agy behind one pane-paste mechanism doesn't hold: tmux's
mechanism (paste text, tail a pane) is identical across CLIs, these SDKs'
mechanisms are not.

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
``core.channels``'s own "unknown kind" handling -- nothing port-specific to
build for that.

"No persistent session" above is about the SDK's own session mechanism
specifically (still true: no ``ClaudeSDKClient``, no ``continue``/``resume``/
``fork_session``, one ``query()`` per delivery). It is not the same claim as
"no memory": ``lib/chat_memory.py``'s hot tier gives each delivery its
counterparty's recent turns as prompt text, prepended before the ``query()``
call (see ``_deliver_message``/``lib/chat_cycle.py``) -- a different,
simpler mechanism than SDK session resume, TTL-bounded and independent of
it. Ticket 0902ee96 covers why prompt-level replay was chosen over wiring
up the SDK's own session/``resume`` machinery.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys

import redis

from core.channels import DeadLetter, receive, send
from core.dispatch import delivery_lock
from core.keys import prefix
from core.logging import configure_logging, log_record
from lib.chat_cycle import run_chat_cycle
from lib.chat_memory import ChatMemory
from lib.profile_env import resolve_claude_profile_env
from lib.reply_correlation import record_delivered

# Hot-tier conversation memory (lib/chat_memory.py) applies to every
# delivery unconditionally -- there is no separate "is this a one-off"
# flag or port_type. A counterparty (`source`) that never talks to this
# agent again simply never accumulates history; one that does gets it
# for free. See ticket 0902ee96's discussion for why detecting intent
# was rejected in favor of this. Not operator-tunable per agent (yet):
# these are the same kind of fixed, change-the-constant-not-the-mechanism
# defaults lib/reply_correlation.py's DELIVERED_TTL_SECONDS already is.
CHAT_MEMORY_TTL_SECONDS = 86_400
CHAT_MEMORY_HOT_KEEP_COUNT = 20

# Per-agent, operator-set ClaudeAgentOptions overrides -- read from the same
# kind of per-agent Redis resource `profile` already is, but nothing writes
# it yet (no StartAgent/lifecycle.py wiring in this ticket -- that touches
# lifecycle.py's fixed-arity StartAgent/StopAgent Lua scripts, a materially
# bigger and riskier change than this port; deliberately left as follow-up,
# not silently half-built). An operator (or a future CLI flag) can set this
# resource directly today; the port already honors it once present.
#
# Deliberately NOT settable per-message from the wire: several of these
# fields (system_prompt especially) are the operator's actual behavioral
# constraints on the agent, not something any sender should be able to
# override by crafting a Message payload. A per-message override surface is
# a real, separate security decision -- not built here, not implied by
# "every option in a call or not" meaning "from anywhere unauthenticated."
#
# Restricted to a hand-picked allowlist, not every ClaudeAgentOptions
# field: several fields (`env`, `can_use_tool`, `hooks`, `cli_path`,
# `stderr`/`debug_stderr`, `session_store`) are either owned exclusively by
# this port (`env`, from resolve_claude_profile_env -- see _run_query) or
# live Python objects a JSON blob in Redis cannot express in the first
# place. Everything in this set is a JSON-primitive-compatible field an
# operator plausibly wants to set per agent.
ALLOWED_SDK_OPTION_FIELDS = frozenset({
    "system_prompt", "allowed_tools", "disallowed_tools", "permission_mode",
    "max_turns", "max_budget_usd", "model", "fallback_model", "cwd",
    "add_dirs", "betas", "setting_sources",
})


def _agent_profile(r, pod: str, tenant: str, agent: str) -> str | None:
    raw = r.get(prefix(pod, tenant, agent=agent, resource="profile"))
    return raw.decode() if isinstance(raw, bytes) else raw


def _agent_sdk_options(r, pod: str, tenant: str, agent: str) -> dict:
    """This agent's operator-configured ClaudeAgentOptions overrides, or
    `{}` if none are set -- absence must behave exactly like today's
    env-only `ClaudeAgentOptions(env=env)`, not raise or warn. A stored
    value that isn't a JSON object, or that names a field outside
    ALLOWED_SDK_OPTION_FIELDS, is dropped (per-field, not all-or-nothing)
    rather than failing the delivery over a config mistake -- the same
    fails-toward-absent posture this project uses elsewhere (see
    lib/reply_correlation.py's module docstring) for a config surface
    with no verified-good state to fall back to.
    """
    raw = r.get(prefix(pod, tenant, agent=agent, resource="sdk-options"))
    if not raw:
        return {}
    try:
        options = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(options, dict):
        return {}
    return {key: value for key, value in options.items() if key in ALLOWED_SDK_OPTION_FIELDS}


def _log_hop(
    message,
    *,
    stream_id: str | None,
    correlation_id: str | None,
    source: str,
    destination: str,
) -> None:
    """Log one message the query() stream yields -- every hop from pickup to
    result, not just the two endpoints a naive implementation would keep.

    The first hop is always a ``SystemMessage`` with ``subtype="init"``,
    emitted as soon as the CLI subprocess actually starts -- this port's
    equivalent of tmux's ``mark_delivery_pending`` + ``ActivityTailer`` pair:
    proof the query was picked up, logged before anything else and well
    before the final ``ResultMessage`` that proves it finished. Every
    ``AssistantMessage`` turn in between stays visible too.
    """
    from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, ToolUseBlock

    common = dict(
        stream_id=stream_id, correlation_id=correlation_id,
        source=source, destination=destination,
    )
    if isinstance(message, SystemMessage):
        log_record("claude_sdk", "claude_sdk_query_started", evidence=message.subtype, **common)
    elif isinstance(message, AssistantMessage):
        tool_names = sorted(
            {block.name for block in message.content if isinstance(block, ToolUseBlock)}
        )
        reason = f"stop_reason={message.stop_reason}"
        if tool_names:
            reason += f" tools={','.join(tool_names)}"
        log_record("claude_sdk", "claude_sdk_turn", reason=reason, **common)
    elif isinstance(message, ResultMessage):
        log_record(
            "claude_sdk", "claude_sdk_query_finished",
            evidence=message.subtype,
            reason=f"is_error={message.is_error} num_turns={message.num_turns}",
            **common,
        )
    else:
        # Defensive: query() doesn't yield StreamEvent/RateLimitEvent/
        # ConversationResetMessage without include_partial_messages, but the
        # Message union can grow -- an unrecognized hop is still logged, not
        # silently dropped.
        log_record("claude_sdk", "claude_sdk_hop", evidence=type(message).__name__, **common)


def _run_query(
    prompt: str,
    profile_env: dict[str, str],
    *,
    sdk_options: dict | None = None,
    stream_id: str | None,
    correlation_id: str | None,
    source: str,
    destination: str,
) -> str:
    """Run exactly one query() call against the Claude Agent SDK.

    Returns the final ``ResultMessage.result`` text, or ``""`` if the query
    ends without one. ``profile_env`` is passed through ``ClaudeAgentOptions.env``,
    which the SDK merges on top of its own inherited process environment for
    the CLI subprocess it spawns -- so one profile's ``CLAUDE_CONFIG_DIR``/
    ``CLAUDE_CODE_OAUTH_TOKEN`` never has to touch this port process's own
    ``os.environ``, and a later delivery for a different agent/profile in the
    same drained batch is unaffected.

    ``sdk_options`` (see ``_agent_sdk_options``/``ALLOWED_SDK_OPTION_FIELDS``)
    is applied on top of ``env`` -- an operator-configured override for any
    other ``ClaudeAgentOptions`` field. Absent or empty (today's default for
    every agent until one is explicitly configured), this is exactly the
    prior ``ClaudeAgentOptions(env=env)`` call, byte-for-byte.

    ``CLAUDE_CODE_SKIP_PROMPT_HISTORY`` keeps this one-off call from writing a
    session transcript to disk: this port has nothing to resume, so there is
    nothing to gain from persisting one, and every agent sharing a
    ``CLAUDE_CONFIG_DIR`` would otherwise accumulate transcripts that no port
    here will ever read back.

    Every message the stream yields is logged via ``_log_hop`` as it arrives,
    not just the final result -- see that function's docstring.
    """
    from claude_agent_sdk import ClaudeAgentOptions, ResultMessage
    from claude_agent_sdk import query as claude_query

    env = dict(profile_env)
    env.setdefault("CLAUDE_CODE_SKIP_PROMPT_HISTORY", "1")
    options = ClaudeAgentOptions(env=env, **(sdk_options or {}))

    async def _collect() -> str:
        result_text = ""
        async for message in claude_query(prompt=prompt, options=options):
            _log_hop(
                message,
                stream_id=stream_id, correlation_id=correlation_id,
                source=source, destination=destination,
            )
            if isinstance(message, ResultMessage):
                result_text = message.result or ""
        return result_text

    return asyncio.run(_collect())


def _deliver_message(
    r,
    pod: str,
    tenant: str,
    agent: str,
    envelope: dict,
    profile_env: dict[str, str],
    sdk_options: dict,
) -> None:
    source = envelope.get("l2", {}).get("source", "unknown")
    payload = envelope.get("payload", {})
    text = payload.get("text", "") if isinstance(payload, dict) else str(payload)
    if not text:
        # Provably pre-call: no query() has run yet, so this is a clean
        # rejection rather than an unresolved effect.
        raise DeadLetter("empty message text")

    stream_id = envelope.get("stream_id")
    correlation_id = envelope.get("correlation_id")

    def dispatch(prompt: str) -> str:
        return _run_query(
            prompt,
            profile_env,
            sdk_options=sdk_options,
            stream_id=stream_id,
            correlation_id=correlation_id,
            source=source,
            destination=agent,
        )

    # chat_id is `source`, not correlation_id: the one real external-caller
    # path (modules/api/server.py's POST /agents/{agent}/envelopes) mints a
    # fresh correlation_id on every single call, so it never actually
    # threads a conversation in practice -- verified, not assumed, see
    # ticket 0902ee96. `source` is stable across every message the same
    # counterparty ever sends this agent, needs no caller cooperation, and
    # is already scoped correctly: lib/chat_memory.py's ChatMemory is
    # per-(pod, tenant, agent), so `source` alone is "this counterparty's
    # thread with this agent," exactly the shape wanted.
    memory = ChatMemory(r, pod, tenant, agent, ttl_seconds_max=CHAT_MEMORY_TTL_SECONDS)
    result_text, _prior_turn_count = run_chat_cycle(
        memory,
        source,
        f"[message from {source}] {text}",
        dispatch,
        ttl_seconds=CHAT_MEMORY_TTL_SECONDS,
        hot_keep_count=CHAT_MEMORY_HOT_KEEP_COUNT,
    )

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
        module="claude_sdk",
        in_reply_to=stream_id,
    )


def deliver_claude_sdk(
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
    sdk_options = _agent_sdk_options(r, pod, tenant, agent)

    openers = {
        "Message": lambda env: _deliver_message(
            r, pod, tenant, agent, env, profile_env, sdk_options
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
        module="claude_sdk",
    )


def main(argv: list[str] | None = None) -> None:
    # First thing in the process, and only in the process: this is the entry
    # point, so it is the one place allowed to set the root logger's level.
    configure_logging()
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print("usage: python -m modules.claude_sdk.port <agent>", file=sys.stderr)
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
        deliver_claude_sdk(r, pod=pod, tenant=tenant, agent=agent)


if __name__ == "__main__":
    main()
