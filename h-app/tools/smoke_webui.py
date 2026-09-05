"""Prove Progress/webui end-to-end against real Redis: claude_sdk's live_to
fan-out actually reaches a webui-registered agent's inbox stream, and an
unfamiliar kind sent to a destination with no opener for it dead-letters
cleanly rather than crashing the delivery subprocess -- the exact scenario
this ticket calls out by name.

Deliberately the same mocking depth as tools/smoke_claude_sdk.py: only
``claude_agent_sdk.query`` is patched, so ``_log_hop``/``_send_progress``/
``core.channels.send``/``modules.webui.port.deliver_webui`` all run for real
against real Redis.
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
from modules.webui.port import deliver_webui  # noqa: E402


def _fake_query_module():
    import claude_agent_sdk as sdk

    async def fake_query(*, prompt, options=None):
        yield sdk.SystemMessage(subtype="init", data={"session_id": "smoke"})
        yield sdk.ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=1, session_id="smoke", result="4",
        )

    return fake_query


def main() -> None:
    url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
    pod = os.environ.get("POD", "ci-webui")
    tenant = os.environ.get("TENANT", "ci-webui")
    caller = "smoke-caller"
    claude_agent = "smoke-claude-sdk"
    webui_agent = "smoke-webui"
    r = redis.Redis.from_url(url)
    r.ping()

    registry = prefix(pod, tenant, resource="registry")

    def cleanup() -> None:
        r.hdel(registry, caller, claude_agent, webui_agent)
        for name in (caller, claude_agent, webui_agent):
            keys = list(r.scan_iter(match=prefix(pod, tenant, agent=name) + ":*"))
            if keys:
                r.delete(*keys)

    def route_one(source: str) -> str:
        """Move exactly one queued item from source's own egress onto
        whichever destination it names -- send() always writes to the
        SENDER's own egress (see core.channels.send); nothing here decides
        routing, it just reads the destination the envelope already
        carries, the same way core.service.transmission's real switch does."""
        raw = r.lpop(prefix(pod, tenant, source, "egress"))
        if raw is None:
            raise AssertionError(f"expected a queued envelope on {source}'s egress, found none")
        envelope = parse(raw)
        destination = envelope["l2"]["destination"]
        r.rpush(prefix(pod, tenant, destination, "ingress"), raw)
        return destination

    cleanup()
    try:
        r.hset(registry, mapping={
            caller: "tmux", claude_agent: "claude_sdk", webui_agent: "webui",
        })

        send(
            r, pod=pod, tenant=tenant, source=caller, destination=claude_agent,
            payload={"text": "what's 2+2", "live_to": webui_agent},
        )
        r.rpush(
            prefix(pod, tenant, claude_agent, "ingress"),
            r.lpop(prefix(pod, tenant, caller, "egress")),
        )

        with patch("claude_agent_sdk.query", new=_fake_query_module()):
            deliver_claude_sdk(r, pod=pod, tenant=tenant, agent=claude_agent)

        # claude_agent's own egress now holds 3 items: two Progress (to
        # webui_agent) and one final Message reply (to caller) -- route each
        # to whatever destination it actually names, not an assumed order.
        destinations = [route_one(claude_agent) for _ in range(3)]
        if sorted(destinations) != sorted([webui_agent, webui_agent, caller]):
            raise AssertionError(f"unexpected fan-out destinations: {destinations!r}")

        deliver_webui(r, pod=pod, tenant=tenant, agent=webui_agent)

        inbox_key = prefix(pod, tenant, webui_agent, "inbox")
        entries = r.xrange(inbox_key, min="-", max="+")
        if len(entries) != 2:
            raise AssertionError(f"expected 2 relayed Progress envelopes, got {len(entries)}")
        import json as _json
        events = [_json.loads(fields[b"envelope"])["payload"]["event"] for _entry_id, fields in entries]
        if events != ["claude_sdk_query_started", "claude_sdk_query_finished"]:
            raise AssertionError(f"unexpected relayed event order/content: {events!r}")

        # The ticket's own explicit scenario: a Progress envelope sent to a
        # destination with no opener for it (claude_agent's own openers are
        # Message/ListContexts only) must dead-letter cleanly, not crash the
        # delivery subprocess -- core.channels' generic "unknown kind"
        # handling, nothing webui/claude_sdk-specific.
        send(r, pod=pod, tenant=tenant, source=webui_agent, destination=claude_agent, payload={"event": "irrelevant"}, kind="Progress")
        r.rpush(
            prefix(pod, tenant, claude_agent, "ingress"),
            r.lpop(prefix(pod, tenant, webui_agent, "egress")),
        )
        deliver_claude_sdk(r, pod=pod, tenant=tenant, agent=claude_agent)
        dead = r.lpop(prefix(pod, tenant, claude_agent, "dead"))
        if dead is None:
            raise AssertionError("unfamiliar Progress kind should have been dead-lettered, was not")
    finally:
        cleanup()

    print(
        "real Redis webui smoke passed: claude_sdk live_to fan-out relayed into "
        "webui's inbox stream, unfamiliar kind dead-lettered cleanly"
    )


if __name__ == "__main__":
    main()
