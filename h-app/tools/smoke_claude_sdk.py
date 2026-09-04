"""Prove claude_sdk's context-addressed memory against real Redis.

Deliberately a different mocking depth than tests/test_claude_sdk_port.py's
own pytest coverage: that suite patches ``modules.claude_sdk.port._run_query``
itself (a higher-level seam) for most of its context-memory assertions. This
smoke test patches only ``claude_agent_sdk.query`` -- the actual vendor SDK
boundary -- so ``_run_query``'s own ClaudeAgentOptions construction and
``_log_hop``/``deliver_claude_sdk``/``lib.chat_cycle``/``lib.chat_memory`` all
run for real against real Redis. No live network call and no real
CLAUDE_OAUTH_TOKEN is needed either way: the vendor SDK's own query() is
never actually invoked, only its return shape is stood in for, the same
``claude_agent_sdk`` dataclasses tests/test_claude_sdk_port.py's own
LogHopTests uses instead of a hand-rolled fake.

Correctness here is checked on the *prompt text* `_run_query` actually
received, not on a real model's comprehension of it (unlike a live deployment
smoke test against a real account) -- deterministic and exactly what a CI run
needs: proof the hot-tier read-prepend-write cycle really happened against
real Redis, not proof any particular model can follow instructions.
"""

import os
import sys
from unittest.mock import patch

import redis

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.channels import send  # noqa: E402
from core.envelope import parse  # noqa: E402
from core.keys import prefix  # noqa: E402
from modules.claude_sdk.port import deliver_claude_sdk  # noqa: E402


def _fake_query_module(prompts: list[str]):
    """A stand-in for the ``claude_agent_sdk`` module's own ``query``,
    recording every prompt it's called with and yielding one canned
    ResultMessage per call -- same dataclasses LogHopTests uses, not a
    hand-rolled fake, so this exercises _log_hop's real isinstance checks
    too."""
    import claude_agent_sdk as sdk

    async def fake_query(*, prompt, options=None):
        prompts.append(prompt)
        yield sdk.SystemMessage(subtype="init", data={"session_id": "smoke"})
        yield sdk.ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=1, session_id="smoke", result=f"reply-{len(prompts)}",
        )

    return fake_query


def main() -> None:
    url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
    pod = os.environ.get("POD", "ci-claude-sdk")
    tenant = os.environ.get("TENANT", "ci-claude-sdk")
    sender = "smoke-caller"
    agent = "smoke-claude-sdk"
    r = redis.Redis.from_url(url)
    r.ping()

    registry = prefix(pod, tenant, resource="registry")

    def cleanup() -> None:
        r.hdel(registry, sender, agent)
        for name in (sender, agent):
            keys = list(r.scan_iter(match=prefix(pod, tenant, agent=name) + ":*"))
            if keys:
                r.delete(*keys)

    cleanup()
    try:
        r.hset(registry, mapping={sender: "tmux", agent: "claude_sdk"})

        def queue(payload: dict) -> None:
            send(r, pod=pod, tenant=tenant, source=sender, destination=agent, payload=payload)
            raw = r.lpop(prefix(pod, tenant, sender, "egress"))
            r.rpush(prefix(pod, tenant, agent, "ingress"), raw)

        prompts: list[str] = []
        fake_query = _fake_query_module(prompts)

        with patch("claude_agent_sdk.query", new=fake_query):
            queue({"text": "first", "context": "ctx-a"})
            deliver_claude_sdk(r, pod=pod, tenant=tenant, agent=agent)

            queue({"text": "second", "context": "ctx-a"})
            deliver_claude_sdk(r, pod=pod, tenant=tenant, agent=agent)

            queue({"text": "no context at all"})
            deliver_claude_sdk(r, pod=pod, tenant=tenant, agent=agent)

        if len(prompts) != 3:
            raise AssertionError(f"expected 3 query() calls, got {len(prompts)}: {prompts!r}")
        # _deliver_message always wraps the raw text as "[message from
        # {source}] {text}" before it reaches dispatch() -- that's the
        # actual "message" a chat_id's history is built from, not the raw
        # payload text (see modules/claude_sdk/port.py's own comment on
        # why: it's what keeps today's attribution-wrapping behavior
        # unchanged while layering memory on top of it).
        first_wrapped = "[message from smoke-caller] first"
        second_wrapped = "[message from smoke-caller] second"
        no_context_wrapped = "[message from smoke-caller] no context at all"
        if prompts[0] != first_wrapped:
            raise AssertionError(f"first call's prompt should be the message unprefixed by history: {prompts[0]!r}")
        if f"[user] {first_wrapped}" not in prompts[1] or "[assistant] reply-1" not in prompts[1]:
            raise AssertionError(f"second call did not see the first exchange as history: {prompts[1]!r}")
        if f"Current message:\n{second_wrapped}" not in prompts[1]:
            raise AssertionError(f"second call's own message is missing/wrong: {prompts[1]!r}")
        if prompts[2] != no_context_wrapped:
            raise AssertionError(
                f"no-context call must be byte-identical to the wrapped message, no history prefix: {prompts[2]!r}"
            )

        # Drain the three Message replies before ListContexts so its own
        # reply is the next thing on the queue.
        for _ in range(3):
            r.lpop(prefix(pod, tenant, agent, "egress"))

        send(r, pod=pod, tenant=tenant, source=sender, destination=agent, payload={}, kind="ListContexts")
        raw = r.lpop(prefix(pod, tenant, sender, "egress"))
        r.rpush(prefix(pod, tenant, agent, "ingress"), raw)
        deliver_claude_sdk(r, pod=pod, tenant=tenant, agent=agent)

        reply = parse(r.lpop(prefix(pod, tenant, agent, "egress")))
        if reply["payload"] != {"contexts": ["ctx-a"]}:
            raise AssertionError(f"ListContexts reply wrong: {reply['payload']!r}")
    finally:
        cleanup()

    print(
        "real Redis claude_sdk smoke passed: context recall, no-context isolation, "
        "ListContexts all verified against real ChatMemory state"
    )


if __name__ == "__main__":
    main()
