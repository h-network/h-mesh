"""Read-dispatch-write composite over a lib/chat_memory.py hot tier.

Reimplements the *pattern* of h-nat's h-orchestrator `h_chat_cycle` --
read bounded history, dispatch the assembled prompt, persist both turns --
natively. Nothing there is imported: its actual code is NAT's own plugin
scaffolding (Builder/FunctionRef/pydantic FunctionBaseConfig), which this
project has none of and has no reason to grow just to reuse an ~80-line
composite. Dispatcher-agnostic by the same design it copies: `dispatch` is
any `str -> str` callable, so this file has no Claude-specific knowledge --
modules/claude_sdk/port.py wires in `_run_query` today, nothing here.
"""
from __future__ import annotations

from typing import Callable

from lib.chat_memory import ChatMemory


def build_chat_prompt(turns: list[dict], message: str) -> str:
    """Prefix `message` with a plain-text transcript of `turns`, oldest first.

    Empty `turns` returns `message` unchanged -- a chat_id with no prior
    history produces byte-identical output to calling the dispatcher
    directly, which is what makes a genuine one-off indistinguishable from
    a chat_id nobody has reused yet: there is no separate branch for it.
    """
    if not turns:
        return message
    history = "\n".join(
        f"[{turn.get('role', '?')}] {turn.get('content', '')}" for turn in turns
    )
    return f"Previous conversation:\n{history}\n\nCurrent message:\n{message}\n"


def run_chat_cycle(
    memory: ChatMemory,
    chat_id: str,
    message: str,
    dispatch: Callable[[str], str],
    *,
    ttl_seconds: int,
    hot_keep_count: int | None = None,
) -> tuple[str, int]:
    """Read prior turns for `chat_id`, dispatch the assembled prompt, persist
    both the incoming and the resulting turn. Returns `(reply, prior_turn_count)`.

    `message` (not the assembled prompt) is what gets written as the "user"
    turn -- writing the already-history-prefixed prompt instead would bake
    the same history into every future read, growing every turn's prompt
    quadratically with conversation length.

    Both turns are written only after `dispatch` returns successfully: a
    failed dispatch (`dispatch` raising) leaves this chat_id's history
    exactly as it was before the call, rather than recording a turn whose
    reply never actually happened.
    """
    prior = memory.read_turns(chat_id)
    prompt = build_chat_prompt(prior, message)
    reply = dispatch(prompt)
    memory.write_turn(chat_id, "user", message, ttl_seconds, hot_keep_count=hot_keep_count)
    memory.write_turn(chat_id, "assistant", reply, ttl_seconds, hot_keep_count=hot_keep_count)
    return reply, len(prior)
