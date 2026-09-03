import os
import sys
import unittest
from pathlib import Path
from unittest.mock import ANY, patch

H_APP = Path(__file__).resolve().parents[1]
if str(H_APP) not in sys.path:
    sys.path.insert(0, str(H_APP))
TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

from core.channels import send
from core.envelope import parse
from core.keys import prefix
from core.registry import port_type
from modules.codex_sdk.port import deliver_codex_sdk
from test_tmux_port import FakeRedis

POD = "testpod"
TENANT = "testtenant"


class CodexSdkPortTests(unittest.TestCase):
    def setUp(self):
        self.no_ambient_claude_token = patch.dict(
            os.environ, {"CLAUDE_OAUTH_TOKEN_DEFAULT": ""}, clear=False
        )
        self.no_ambient_claude_token.start()
        self.addCleanup(self.no_ambient_claude_token.stop)
        self.redis = FakeRedis()
        registry = prefix(POD, TENANT, resource="registry")
        self.redis.hset(registry, "alice", "tmux")
        # Registering the destination's port_type as "codex_sdk" directly
        # through the same registry hash core/registry.py reads -- no hire
        # command involved, same validation scope as the claude_sdk PoC.
        self.redis.hset(registry, "bob", "codex_sdk")

    def queue(self, kind="Message", payload=None):
        stream_id = send(
            self.redis,
            pod=POD,
            tenant=TENANT,
            source="alice",
            destination="bob",
            kind=kind,
            payload=payload or {"text": "hello"},
        )
        raw = self.redis.lpop(prefix(POD, TENANT, "alice", "egress"))
        self.redis.rpush(prefix(POD, TENANT, "bob", "ingress"), raw)
        return stream_id

    def test_registers_cleanly_as_a_generic_port_type(self):
        self.assertEqual(port_type(self.redis, pod=POD, tenant=TENANT, agent="bob"), "codex_sdk")

    def test_message_runs_one_turn_and_replies(self):
        stream_id = self.queue(payload={"text": "what's 2+2"})
        with patch("modules.codex_sdk.port._run_turn", return_value="4") as mock_turn:
            deliver_codex_sdk(self.redis, pod=POD, tenant=TENANT, agent="bob")

        mock_turn.assert_called_once_with(
            "[message from alice] what's 2+2",
            {},
            stream_id=stream_id,
            correlation_id=ANY,
            source="alice",
            destination="bob",
        )

        raw = self.redis.lpop(prefix(POD, TENANT, "bob", "egress"))
        reply = parse(raw)
        self.assertEqual(reply["payload"], {"text": "4"})
        self.assertEqual(reply["correlation_id"], stream_id)
        self.assertEqual(reply["in_reply_to"], stream_id)
        self.assertEqual(reply["l2"]["source"], "bob")
        self.assertEqual(reply["l2"]["destination"], "alice")

    def test_no_reply_sent_when_result_is_blank(self):
        self.queue(payload={"text": "be silent"})
        with patch("modules.codex_sdk.port._run_turn", return_value="   "):
            deliver_codex_sdk(self.redis, pod=POD, tenant=TENANT, agent="bob")

        raw = self.redis.lpop(prefix(POD, TENANT, "bob", "egress"))
        self.assertIsNone(raw)

    def test_empty_text_is_dead_lettered_without_calling_the_sdk(self):
        self.queue(payload={"text": ""})
        with patch("modules.codex_sdk.port._run_turn") as mock_turn:
            deliver_codex_sdk(self.redis, pod=POD, tenant=TENANT, agent="bob")

        mock_turn.assert_not_called()
        dead = self.redis.lpop(prefix(POD, TENANT, "bob", "dead"))
        self.assertIsNotNone(dead)

    def test_turn_failure_lands_in_unresolved_not_dead(self):
        self.queue(payload={"text": "boom"})
        with patch("modules.codex_sdk.port._run_turn", side_effect=RuntimeError("codex exploded")):
            deliver_codex_sdk(self.redis, pod=POD, tenant=TENANT, agent="bob")

        self.assertIsNone(self.redis.lpop(prefix(POD, TENANT, "bob", "dead")))
        unresolved_key = prefix(POD, TENANT, resource="unresolved")
        self.assertEqual(self.redis.llen(unresolved_key), 1)

    def test_unknown_kind_is_dead_lettered_by_core_not_sdk_code(self):
        self.queue(kind="Command", payload={"text": "irrelevant"})
        with patch("modules.codex_sdk.port._run_turn") as mock_turn:
            deliver_codex_sdk(self.redis, pod=POD, tenant=TENANT, agent="bob")

        mock_turn.assert_not_called()
        dead = self.redis.lpop(prefix(POD, TENANT, "bob", "dead"))
        self.assertIsNotNone(dead)

    def test_profile_env_is_threaded_into_the_turn_call(self):
        self.redis.set(prefix(POD, TENANT, agent="bob", resource="profile"), "work")
        self.queue()
        with patch.dict(os.environ, {"CLAUDE_OAUTH_TOKEN_WORK": "tok-work"}, clear=False):
            with patch("modules.codex_sdk.port._run_turn", return_value="ok") as mock_turn:
                deliver_codex_sdk(self.redis, pod=POD, tenant=TENANT, agent="bob")

        home_dir = os.environ.get("HOME", os.path.expanduser("~"))
        mock_turn.assert_called_once_with(
            "[message from alice] hello",
            {
                "CLAUDE_CONFIG_DIR": f"{home_dir}/.claude-work",
                "CODEX_HOME": f"{home_dir}/.codex-work",
                "CLAUDE_CODE_OAUTH_TOKEN": "tok-work",
            },
            stream_id=ANY,
            correlation_id=ANY,
            source="alice",
            destination="bob",
        )

    def test_drains_multiple_queued_messages_independently(self):
        self.queue(payload={"text": "first"})
        self.queue(payload={"text": "second"})
        with patch(
            "modules.codex_sdk.port._run_turn", side_effect=["reply-1", "reply-2"]
        ) as mock_turn:
            deliver_codex_sdk(self.redis, pod=POD, tenant=TENANT, agent="bob")

        self.assertEqual(mock_turn.call_count, 2)
        first = parse(self.redis.lpop(prefix(POD, TENANT, "bob", "egress")))
        second = parse(self.redis.lpop(prefix(POD, TENANT, "bob", "egress")))
        self.assertEqual(first["payload"], {"text": "reply-1"})
        self.assertEqual(second["payload"], {"text": "reply-2"})


def _fake_codex_class(notifications):
    """Build a fake AsyncCodex whose one thread's one turn streams the given
    Notification objects -- standing in for a real app-server round trip."""

    class _FakeHandle:
        async def stream(self):
            for note in notifications:
                yield note

    class _FakeThread:
        async def turn(self, prompt):
            return _FakeHandle()

    class _FakeCodex:
        def __init__(self, config=None):
            self.config = config

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return None

        async def thread_start(self, **kwargs):
            return _FakeThread()

    return _FakeCodex


class LogHopTests(unittest.TestCase):
    """Exercise _run_turn/_log_notification against the real openai_codex
    dataclasses standing in for a Notification stream -- no live app-server
    call, just the actual pydantic models a real stream would carry."""

    def test_every_hop_is_logged_in_order_before_the_result_returns(self):
        from openai_codex._run import (
            ItemCompletedNotification,
            ThreadTokenUsageUpdatedNotification,
            ThreadItem,
            Turn,
            TurnCompletedNotification,
            TurnStatus,
        )
        from openai_codex.api import Notification
        from openai_codex.generated.v2_all import AgentMessageThreadItem, TokenUsageBreakdown

        agent_message = AgentMessageThreadItem(id="item1", text="4", type="agentMessage")
        item_hop = Notification(
            method="item/completed",
            payload=ItemCompletedNotification(
                completed_at_ms=1, item=ThreadItem(root=agent_message),
                thread_id="t1", turn_id="turn1",
            ),
        )
        breakdown = TokenUsageBreakdown(
            cached_input_tokens=0, input_tokens=1, output_tokens=1,
            reasoning_output_tokens=0, total_tokens=2,
        )
        usage_hop = Notification(
            method="usage/updated",
            payload=ThreadTokenUsageUpdatedNotification(
                thread_id="t1", turn_id="turn1",
                token_usage={"last": breakdown, "total": breakdown},
            ),
        )
        finished_hop = Notification(
            method="turn/completed",
            payload=TurnCompletedNotification(
                thread_id="t1",
                turn=Turn(id="turn1", items=[], status=TurnStatus.completed, duration_ms=42),
            ),
        )

        from modules.codex_sdk.port import _run_turn

        with patch(
            "openai_codex.AsyncCodex",
            new=_fake_codex_class([item_hop, usage_hop, finished_hop]),
        ), patch("modules.codex_sdk.port.log_record") as mock_log:
            text = _run_turn(
                "prompt", {},
                stream_id="sid", correlation_id="cid",
                source="alice", destination="bob",
            )

        self.assertEqual(text, "4")

        events = [call.args[1] for call in mock_log.call_args_list]
        self.assertEqual(
            events,
            [
                "codex_sdk_query_started",
                "codex_sdk_turn",
                "codex_sdk_usage",
                "codex_sdk_query_finished",
            ],
        )

        for call in mock_log.call_args_list:
            self.assertEqual(call.args[0], "codex_sdk")
            self.assertEqual(call.kwargs["stream_id"], "sid")
            self.assertEqual(call.kwargs["correlation_id"], "cid")
            self.assertEqual(call.kwargs["source"], "alice")
            self.assertEqual(call.kwargs["destination"], "bob")

        started_kwargs = mock_log.call_args_list[0].kwargs
        self.assertEqual(started_kwargs["evidence"], "started")

        turn_kwargs = mock_log.call_args_list[1].kwargs
        self.assertEqual(turn_kwargs["evidence"], "agentMessage")

        finished_kwargs = mock_log.call_args_list[3].kwargs
        self.assertEqual(finished_kwargs["evidence"], "completed")
        self.assertIn("duration_ms=42", finished_kwargs["reason"])

    def test_unrecognized_notification_payload_is_logged_not_dropped(self):
        from openai_codex.api import Notification

        from modules.codex_sdk.port import _run_turn

        sentinel = object()
        weird_hop = Notification.__new__(Notification)
        weird_hop.method = "mystery/event"
        weird_hop.payload = sentinel

        with patch(
            "openai_codex.AsyncCodex", new=_fake_codex_class([weird_hop])
        ), patch("modules.codex_sdk.port.log_record") as mock_log:
            text = _run_turn(
                "prompt", {},
                stream_id="sid", correlation_id="cid",
                source="alice", destination="bob",
            )

        self.assertEqual(text, "")
        events = [call.args[1] for call in mock_log.call_args_list]
        self.assertEqual(events, ["codex_sdk_query_started", "codex_sdk_hop"])
        self.assertEqual(mock_log.call_args_list[1].kwargs["evidence"], "object")


if __name__ == "__main__":
    unittest.main()
