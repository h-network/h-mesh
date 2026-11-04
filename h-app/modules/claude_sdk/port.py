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

A ``Message`` payload's ``live_to`` (an agent name) opts into live streaming:
a ``Progress`` envelope per ``query()`` hop (init / each turn / final
result), sent to ``live_to`` as it happens rather than batched, correlated to
the incoming ``stream_id`` the same way the eventual one-shot ``Message``
reply already is. ``live_cc_source`` (bool, default ``False``) additionally
sends the same Progress envelopes to the incoming envelope's own ``source``.
Both are purely additive: absent, this port's behavior is byte-for-byte what
it was before they existed, and the final ``Message`` reply is unchanged
either way. See ``_classify_hop`` for what a Progress envelope's payload
contains, and ``modules/claude_sdk/README.md`` for the wire shape.

``live_to`` is the *incoming message's own sender's* choice, not this
agent's -- so it is authorized against that sender, not against this agent.
``_send_progress`` sends as this agent (the only identity able to speak for
a live query it is running), but a bare ``send()`` checks the acting
identity's own export tags, not the requester's -- naively honoring any
``live_to`` would let a sender with no ability to reach some third agent
directly use this one as an open relay to it (and, wherever tag policy
grants this agent broader export rights than the requester has, escalate
through it too). ``_deliver_message`` therefore requires the *incoming
message's ``source``* to independently pass the same ``core.policy`` check
against ``live_to`` before it is ever added to the live-fanout set,
pre-``query()``, the same DeadLetter-before-any-effect posture invalid
``live_to`` syntax already gets. This makes the feature no more capable of
reaching a third party than the sender already was on their own -- routing
through this agent never grants more reach than the sender already had.

Every ``_send_progress`` call inside ``on_hop`` is also independently
best-effort: caught and logged, never left to propagate out of the
``query()`` stream. Progress is a bonus channel layered on top of the one
guaranteed outcome (the final reply) -- a single bad ``live_to``/policy
change mid-stream, a transient Redis hiccup, or any other one-off send
failure must not turn an otherwise-successful query into a dropped final
reply and an unresolved delivery.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from collections.abc import Callable

import redis

from core.channels import DeadLetter, receive, send
from core.dispatch import delivery_lock
from core.keys import prefix, validate_segment
from core.logging import configure_logging, log_record
from core.policy import allows as policy_allows
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


def _classify_hop(message) -> tuple[str, str | None, str | None]:
    """Classify one message the query() stream yields into (event, evidence,
    reason) -- the shared vocabulary both ``_log_hop`` (a log record) and
    live Progress envelopes (see ``_send_progress``) build on top of, so the
    two never drift into disagreeing about what a given hop *is*.

    The first hop is always a ``SystemMessage`` with ``subtype="init"``,
    emitted as soon as the CLI subprocess actually starts -- this port's
    equivalent of tmux's ``mark_delivery_pending`` + ``ActivityTailer`` pair:
    proof the query was picked up, well before the final ``ResultMessage``
    that proves it finished. Every ``AssistantMessage`` turn in between stays
    visible too.
    """
    from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, ToolUseBlock

    if isinstance(message, SystemMessage):
        return "claude_sdk_query_started", message.subtype, None
    if isinstance(message, AssistantMessage):
        tool_names = sorted(
            {block.name for block in message.content if isinstance(block, ToolUseBlock)}
        )
        reason = f"stop_reason={message.stop_reason}"
        if tool_names:
            reason += f" tools={','.join(tool_names)}"
        return "claude_sdk_turn", None, reason
    if isinstance(message, ResultMessage):
        reason = f"is_error={message.is_error} num_turns={message.num_turns}"
        return "claude_sdk_query_finished", message.subtype, reason
    # Defensive: query() doesn't yield StreamEvent/RateLimitEvent/
    # ConversationResetMessage without include_partial_messages, but the
    # Message union can grow -- an unrecognized hop is still classified, not
    # silently dropped.
    return "claude_sdk_hop", type(message).__name__, None


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
    """
    event, evidence, reason = _classify_hop(message)
    log_record(
        "claude_sdk", event,
        stream_id=stream_id, correlation_id=correlation_id,
        source=source, destination=destination,
        evidence=evidence, reason=reason,
    )


def _send_progress(
    r,
    pod: str,
    tenant: str,
    *,
    agent: str,
    destination: str,
    stream_id: str | None,
    event: str,
    evidence: str | None,
    reason: str | None,
) -> None:
    """Send one live "Progress" envelope for a single query() hop.

    Correlated the same way the existing one-shot final "Message" reply
    already is -- ``correlation_id``/``in_reply_to`` both the originating
    ``stream_id`` -- so a caller can line up every Progress envelope and the
    eventual Message reply under the same thread.
    """
    payload = {"event": event}
    if evidence is not None:
        payload["evidence"] = evidence
    if reason is not None:
        payload["reason"] = reason
    send(
        r,
        pod=pod,
        tenant=tenant,
        source=agent,
        destination=destination,
        payload=payload,
        kind="Progress",
        correlation_id=stream_id,
        module="claude_sdk",
        in_reply_to=stream_id,
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
    on_hop: Callable[[object], None] | None = None,
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
    not just the final result -- see that function's docstring. ``on_hop``,
    when given, is additionally called with each raw message as it arrives
    (after logging) -- ``_deliver_message`` uses it to stream live Progress
    envelopes; absent (the default), this is exactly today's behavior.
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
            if on_hop is not None:
                on_hop(message)
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

    # `live_to`/`live_cc_source` are purely additive opt-ins: absent (the
    # common case today), `live_targets` stays empty, `on_hop` below stays a
    # no-op, and dispatch's call to _run_query is byte-for-byte what it was
    # before this existed. Invalid `live_to` is rejected the same pre-call
    # way empty text/bad context already are -- validate_segment runs before
    # any query() call, so this is still provably pre-call.
    raw_live_to = payload.get("live_to") if isinstance(payload, dict) else None
    live_to = None
    if raw_live_to is not None:
        try:
            live_to = validate_segment(raw_live_to)
        except KeyError:
            raise DeadLetter(f"invalid live_to: {raw_live_to!r}")
        # Authorized against `source` -- the party that actually chose
        # `live_to` -- not against `agent`. A bare send() below would check
        # *this agent's* export tags, and this agent did not choose the
        # destination: naively honoring `live_to` would let any sender turn
        # this agent into an open relay to a third party the sender has no
        # tag-policy standing to reach directly (or, wherever this agent's
        # own export tags are broader, escalate through it). Same DeadLetter
        # posture as an invalid-syntax `live_to`: rejected before any
        # query() call, not silently dropped from an otherwise-run query.
        if not policy_allows(r, pod=pod, tenant=tenant, source=source, destination=live_to):
            raise DeadLetter(f"live_to not authorized for {source!r}: {live_to!r}")

    # Only the JSON boolean `true` counts -- a truthy check here would also
    # accept the string `"false"`, non-empty strings, and other values a
    # sender could plausibly send while intending `false`.
    live_cc_source = (
        payload.get("live_cc_source") is True if isinstance(payload, dict) else False
    )

    live_targets = []
    if live_to is not None:
        live_targets.append(live_to)
        # Dedupe: live_to == source would otherwise double-send every hop to
        # the same agent for no benefit -- cc means "also", not "again".
        if live_cc_source and source != live_to:
            live_targets.append(source)

    stream_id = envelope.get("stream_id")
    correlation_id = envelope.get("correlation_id")
    message = f"[message from {source}] {text}"

    def on_hop(hop_message: object) -> None:
        event, evidence, reason = _classify_hop(hop_message)
        for target in live_targets:
            # Best-effort and isolated per target: Progress is a bonus
            # channel layered on the one guaranteed outcome (the final
            # reply). A policy change, bad destination, or transient Redis
            # error here must not abort the query mid-stream and take the
            # final reply/record_delivered/chat-memory completion down with
            # it -- see the module docstring's paragraph on this.
            try:
                _send_progress(
                    r, pod, tenant,
                    agent=agent, destination=target, stream_id=stream_id,
                    event=event, evidence=evidence, reason=reason,
                )
            except Exception as exc:
                # The observation itself must not reopen the same hole it
                # exists to close -- log_record does real I/O and is not
                # guaranteed not to raise (same reasoning as
                # core.channels._emit_observation). A failure here is
                # swallowed, not left to propagate and abort the query.
                try:
                    log_record(
                        "claude_sdk", "claude_sdk_progress_send_failed",
                        stream_id=stream_id, correlation_id=correlation_id,
                        source=agent, destination=target, reason=str(exc),
                    )
                except Exception:
                    pass

    def dispatch(prompt: str) -> str:
        return _run_query(
            prompt,
            profile_env,
            sdk_options=sdk_options,
            stream_id=stream_id,
            correlation_id=correlation_id,
            source=source,
            destination=agent,
            on_hop=on_hop if live_targets else None,
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
