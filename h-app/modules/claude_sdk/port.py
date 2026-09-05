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

Any envelope kind other than ``Message``/``ListContexts`` (``Command``,
``AddTicket``, ``Attachment``) is out of scope for this PoC and is
dead-lettered by ``core.channels``'s own "unknown kind" handling -- nothing
port-specific to build for that.

"No persistent session" above is about the SDK's own session mechanism
specifically (still true: no ``ClaudeSDKClient``, no ``continue``/``resume``/
``fork_session``, one ``query()`` per delivery). It is not the same claim as
"no memory": a ``Message`` whose payload names a ``context`` gets
``lib/chat_memory.py``'s hot tier -- that context's recent turns prepended
as prompt text before the ``query()`` call (see ``_deliver_message``/
``lib/chat_cycle.py``) -- a different, simpler mechanism than SDK session
resume, TTL-bounded and independent of it.

``context`` is the caller's own identifier, not derived from ``source`` or
any envelope field: this port has no opinion on what makes two messages
"the same conversation," only on giving a name to it something it's asked
to remember by. A ``Message`` with no ``context`` is a genuine one-off --
no memory read, no memory write, not merely a `context` that happens not to
repeat. ``ListContexts`` (see ``_deliver_list_contexts``) lets a caller
discover which contexts an agent currently has live memory for, rather than
requiring every caller to already know its own vocabulary in advance.
Ticket 38c7ab0d covers why addressing moved from the earlier ``source``-
keyed default (ticket 0902ee96) to this explicit, caller-named scheme.

A ``Message`` payload's ``live_to`` (see ``_deliver_message``/``_log_hop``/
``_send_progress``) additionally fans every hop of the underlying
``query()`` call out live as a ``Progress`` envelope to the named
destination -- typically a ``modules/webui``-registered agent, though this
port has no opinion on what ``live_to`` actually is beyond a valid
destination name. ``live_cc_source`` (bool) also sends the same hops back to
whoever sent the ``Message``. Neither field set is today's behavior exactly,
byte-for-byte unchanged: this is additive to, never a replacement for, the
single final ``Message`` reply every delivery already sends.
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
from core.keys import prefix, validate_segment
from core.logging import configure_logging, log_record
from lib.chat_cycle import run_chat_cycle
from lib.chat_memory import HOT_KEEP_COUNT as CHAT_MEMORY_HOT_KEEP_COUNT
from lib.chat_memory import TTL_SECONDS_MAX as CHAT_MEMORY_TTL_SECONDS
from lib.chat_memory import ChatMemory
from lib.profile_env import resolve_claude_profile_env
from lib.reply_correlation import record_delivered

# Hot-tier conversation memory (lib/chat_memory.py) applies only when a
# Message payload names a `context` -- see _deliver_message. No separate
# "is this a one-off" flag beyond that: `context` present is the only
# signal, absent means a genuine one-off, no memory read or write at all.
# An earlier version of this port keyed memory off `source` unconditionally
# (ticket 0902ee96); ticket 38c7ab0d replaced that with this explicit,
# caller-named scheme once it became clear a caller may want several
# independent contexts with the same counterparty (or none at all), which
# `source` alone can't express. The TTL/keep-count values themselves live in
# lib/chat_memory.py (re-exported here under these names for every existing
# call site in this file), not redefined here -- see that module for why:
# modules/api/server.py's read-only /agents/{agent}/contexts needs the same
# constant without depending on this port module.

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


def _send_progress(
    r,
    *,
    pod: str,
    tenant: str,
    agent: str,
    original_source: str,
    event: str,
    detail: str,
    stream_id: str | None,
    correlation_id: str | None,
    live_to: str,
    live_cc_source: bool,
) -> None:
    """Fan a single hop out as a ``Progress`` envelope, best-effort.

    Never allowed to break the underlying ``query()`` call: a ``live_to``
    that isn't registered, or a policy denial between this agent and it,
    dead-letters or raises inside ``send()`` the same way any other misrouted
    envelope would -- caught and logged here rather than propagated, since a
    live-viewing convenience failing is not a reason to fail the delivery
    that convenience was only ever describing. ``correlation_id``/
    ``in_reply_to`` both anchor to the *incoming* Message's own ``stream_id``,
    same convention the final reply already uses, so a viewer can group every
    hop (and the eventual reply) under one id.
    """
    destinations = [live_to]
    if live_cc_source and original_source not in destinations:
        destinations.append(original_source)
    for destination in destinations:
        try:
            send(
                r, pod=pod, tenant=tenant, source=agent, destination=destination,
                payload={"event": event, "detail": detail},
                kind="Progress", correlation_id=stream_id, module="claude_sdk",
                in_reply_to=stream_id,
            )
        except Exception as exc:
            log_record(
                "claude_sdk", "progress_send_failed",
                stream_id=stream_id, correlation_id=correlation_id,
                source=agent, destination=destination, reason=str(exc),
            )


def _log_hop(
    message,
    *,
    r=None,
    pod: str | None = None,
    tenant: str | None = None,
    stream_id: str | None,
    correlation_id: str | None,
    source: str,
    destination: str,
    live_to: str | None = None,
    live_cc_source: bool = False,
) -> None:
    """Log one message the query() stream yields -- every hop from pickup to
    result, not just the two endpoints a naive implementation would keep.

    The first hop is always a ``SystemMessage`` with ``subtype="init"``,
    emitted as soon as the CLI subprocess actually starts -- this port's
    equivalent of tmux's ``mark_delivery_pending`` + ``ActivityTailer`` pair:
    proof the query was picked up, logged before anything else and well
    before the final ``ResultMessage`` that proves it finished. Every
    ``AssistantMessage`` turn in between stays visible too.

    ``live_to`` set (see ``_deliver_message``) additionally fans this same
    hop out as a ``Progress`` envelope via ``_send_progress`` -- purely
    additive: absent (the default, and every call site before this ticket),
    this function's own log_record behavior is untouched byte-for-byte.
    """
    from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, ToolUseBlock

    common = dict(
        stream_id=stream_id, correlation_id=correlation_id,
        source=source, destination=destination,
    )
    if isinstance(message, SystemMessage):
        event = "claude_sdk_query_started"
        detail = f"subtype={message.subtype}"
        log_record("claude_sdk", event, evidence=message.subtype, **common)
    elif isinstance(message, AssistantMessage):
        tool_names = sorted(
            {block.name for block in message.content if isinstance(block, ToolUseBlock)}
        )
        reason = f"stop_reason={message.stop_reason}"
        if tool_names:
            reason += f" tools={','.join(tool_names)}"
        event = "claude_sdk_turn"
        detail = reason
        log_record("claude_sdk", event, reason=reason, **common)
    elif isinstance(message, ResultMessage):
        reason = f"is_error={message.is_error} num_turns={message.num_turns}"
        event = "claude_sdk_query_finished"
        detail = f"{message.subtype}: {reason}"
        log_record("claude_sdk", event, evidence=message.subtype, reason=reason, **common)
    else:
        # Defensive: query() doesn't yield StreamEvent/RateLimitEvent/
        # ConversationResetMessage without include_partial_messages, but the
        # Message union can grow -- an unrecognized hop is still logged, not
        # silently dropped.
        event = "claude_sdk_hop"
        detail = type(message).__name__
        log_record("claude_sdk", event, evidence=detail, **common)

    if live_to is not None:
        _send_progress(
            r, pod=pod, tenant=tenant, agent=destination, original_source=source,
            event=event, detail=detail, stream_id=stream_id, correlation_id=correlation_id,
            live_to=live_to, live_cc_source=live_cc_source,
        )


def _run_query(
    prompt: str,
    profile_env: dict[str, str],
    *,
    sdk_options: dict | None = None,
    stream_id: str | None,
    correlation_id: str | None,
    source: str,
    destination: str,
    r=None,
    pod: str | None = None,
    tenant: str | None = None,
    live_to: str | None = None,
    live_cc_source: bool = False,
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
    not just the final result -- see that function's docstring. ``r``/``pod``/
    ``tenant``/``live_to``/``live_cc_source`` only matter when ``live_to`` is
    set (see ``_deliver_message``); every other call site, including every
    pre-existing test, omits them and gets today's behavior unchanged.
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
                r=r, pod=pod, tenant=tenant,
                stream_id=stream_id, correlation_id=correlation_id,
                source=source, destination=destination,
                live_to=live_to, live_cc_source=live_cc_source,
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

    # `context` is the caller's own memory-scoping id, entirely their
    # choice -- absent (key missing or explicitly null) means a genuine
    # one-off below, not a fallback to any implicit id of ours. Present but
    # invalid is rejected the same way empty text is: validate_segment runs
    # before any query() call, so this is still provably pre-call, a clean
    # DeadLetter rather than an unresolved effect.
    raw_context = payload.get("context") if isinstance(payload, dict) else None
    context = None
    if raw_context is not None:
        try:
            context = validate_segment(raw_context)
        except KeyError:
            raise DeadLetter(f"invalid context: {raw_context!r}")

    # `live_to` names a destination (typically a webui-registered agent,
    # see modules/webui/port.py) to fan every hop out to as a Progress
    # envelope, additive to the existing single final reply -- absent (the
    # default) is today's behavior exactly, untouched. Validated the same
    # way `context` is: pre-call, so an invalid value is a clean DeadLetter
    # rather than an unresolved effect. `live_cc_source` is a plain display
    # convenience (also send the same hops to whoever sent this Message),
    # not an identifier, so a non-bool value is merely coerced rather than
    # rejected.
    raw_live_to = payload.get("live_to") if isinstance(payload, dict) else None
    live_to = None
    if raw_live_to is not None:
        try:
            live_to = validate_segment(raw_live_to)
        except KeyError:
            raise DeadLetter(f"invalid live_to: {raw_live_to!r}")
    live_cc_source = bool(payload.get("live_cc_source", False)) if isinstance(payload, dict) else False

    stream_id = envelope.get("stream_id")
    correlation_id = envelope.get("correlation_id")
    message = f"[message from {source}] {text}"

    def dispatch(prompt: str) -> str:
        return _run_query(
            prompt,
            profile_env,
            sdk_options=sdk_options,
            stream_id=stream_id,
            correlation_id=correlation_id,
            source=source,
            destination=agent,
            r=r, pod=pod, tenant=tenant,
            live_to=live_to, live_cc_source=live_cc_source,
        )

    if context is not None:
        memory = ChatMemory(r, pod, tenant, agent, ttl_seconds_max=CHAT_MEMORY_TTL_SECONDS)
        result_text, _prior_turn_count = run_chat_cycle(
            memory,
            context,
            message,
            dispatch,
            ttl_seconds=CHAT_MEMORY_TTL_SECONDS,
            hot_keep_count=CHAT_MEMORY_HOT_KEEP_COUNT,
        )
    else:
        result_text = dispatch(message)

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


def _deliver_list_contexts(r, pod: str, tenant: str, agent: str, envelope: dict) -> None:
    """Reply to a ``ListContexts`` envelope with the requesting agent's
    currently-live memory contexts for `agent`.

    A query, not a write -- there's no established "command reply" kind in
    this codebase (``AddTicket``'s own opener, ``lib/board_interaction.py``'s
    ``add_ticket``, replies to nothing), so this reuses the same
    request/response shape ``_deliver_message`` already does: a ``Message``
    reply, correlated the same way (``in_reply_to``/``correlation_id`` both
    the incoming ``stream_id``), sent from ``agent`` back to the caller.
    """
    source = envelope.get("l2", {}).get("source", "unknown")
    stream_id = envelope.get("stream_id")

    memory = ChatMemory(r, pod, tenant, agent, ttl_seconds_max=CHAT_MEMORY_TTL_SECONDS)
    contexts = memory.list_chat_ids()

    send(
        r,
        pod=pod,
        tenant=tenant,
        source=agent,
        destination=source,
        payload={"contexts": contexts},
        kind="Message",
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
        "ListContexts": lambda env: _deliver_list_contexts(r, pod, tenant, agent, env),
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
