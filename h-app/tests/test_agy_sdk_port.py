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
from modules.agy_sdk.port import deliver_agy_sdk
from test_tmux_port import FakeRedis

POD = "testpod"
TENANT = "testtenant"


class AgySdkPortTests(unittest.TestCase):
    def setUp(self):
        self.no_ambient_agy_key = patch.dict(
            os.environ, {"AGY_API_KEY_DEFAULT": ""}, clear=False
        )
        self.no_ambient_agy_key.start()
        self.addCleanup(self.no_ambient_agy_key.stop)
        self.redis = FakeRedis()
        registry = prefix(POD, TENANT, resource="registry")
        self.redis.hset(registry, "alice", "tmux")
        # Registering the destination's port_type as "agy_sdk" directly
        # through the same registry hash core/registry.py reads -- no hire
        # command involved, same validation scope as the other two ports.
        self.redis.hset(registry, "bob", "agy_sdk")

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
        self.assertEqual(port_type(self.redis, pod=POD, tenant=TENANT, agent="bob"), "agy_sdk")

    def test_message_runs_one_chat_and_replies(self):
        stream_id = self.queue(payload={"text": "what's 2+2"})
        with patch("modules.agy_sdk.port._run_chat", return_value="4") as mock_chat:
            deliver_agy_sdk(self.redis, pod=POD, tenant=TENANT, agent="bob")

        mock_chat.assert_called_once_with(
            "[message from alice] what's 2+2",
            None,
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
        with patch("modules.agy_sdk.port._run_chat", return_value="   "):
            deliver_agy_sdk(self.redis, pod=POD, tenant=TENANT, agent="bob")

        raw = self.redis.lpop(prefix(POD, TENANT, "bob", "egress"))
        self.assertIsNone(raw)

    def test_empty_text_is_dead_lettered_without_calling_the_sdk(self):
        self.queue(payload={"text": ""})
        with patch("modules.agy_sdk.port._run_chat") as mock_chat:
            deliver_agy_sdk(self.redis, pod=POD, tenant=TENANT, agent="bob")

        mock_chat.assert_not_called()
        dead = self.redis.lpop(prefix(POD, TENANT, "bob", "dead"))
        self.assertIsNotNone(dead)

    def test_chat_failure_lands_in_unresolved_not_dead(self):
        self.queue(payload={"text": "boom"})
        with patch("modules.agy_sdk.port._run_chat", side_effect=RuntimeError("agy exploded")):
            deliver_agy_sdk(self.redis, pod=POD, tenant=TENANT, agent="bob")

        self.assertIsNone(self.redis.lpop(prefix(POD, TENANT, "bob", "dead")))
        unresolved_key = prefix(POD, TENANT, resource="unresolved")
        self.assertEqual(self.redis.llen(unresolved_key), 1)

    def test_unknown_kind_is_dead_lettered_by_core_not_sdk_code(self):
        self.queue(kind="Command", payload={"text": "irrelevant"})
        with patch("modules.agy_sdk.port._run_chat") as mock_chat:
            deliver_agy_sdk(self.redis, pod=POD, tenant=TENANT, agent="bob")

        mock_chat.assert_not_called()
        dead = self.redis.lpop(prefix(POD, TENANT, "bob", "dead"))
        self.assertIsNotNone(dead)

    def test_profile_scoped_api_key_is_threaded_into_the_chat_call(self):
        self.redis.set(prefix(POD, TENANT, agent="bob", resource="profile"), "work")
        self.queue()
        with patch.dict(os.environ, {"AGY_API_KEY_WORK": "key-work"}, clear=False):
            with patch("modules.agy_sdk.port._run_chat", return_value="ok") as mock_chat:
                deliver_agy_sdk(self.redis, pod=POD, tenant=TENANT, agent="bob")

        mock_chat.assert_called_once_with(
            "[message from alice] hello",
            "key-work",
            stream_id=ANY,
            correlation_id=ANY,
            source="alice",
            destination="bob",
        )

    def test_drains_multiple_queued_messages_independently(self):
        self.queue(payload={"text": "first"})
        self.queue(payload={"text": "second"})
        with patch(
            "modules.agy_sdk.port._run_chat", side_effect=["reply-1", "reply-2"]
        ) as mock_chat:
            deliver_agy_sdk(self.redis, pod=POD, tenant=TENANT, agent="bob")

        self.assertEqual(mock_chat.call_count, 2)
        first = parse(self.redis.lpop(prefix(POD, TENANT, "bob", "egress")))
        second = parse(self.redis.lpop(prefix(POD, TENANT, "bob", "egress")))
        self.assertEqual(first["payload"], {"text": "reply-1"})
        self.assertEqual(second["payload"], {"text": "reply-2"})


class ProfileEnvTests(unittest.TestCase):
    def setUp(self):
        self.no_ambient_keys = patch.dict(
            os.environ,
            {"AGY_API_KEY_DEFAULT": "", "AGY_API_KEY_WORK": ""},
            clear=False,
        )
        self.no_ambient_keys.start()
        self.addCleanup(self.no_ambient_keys.stop)

    def test_key_absent_resolves_to_none(self):
        from lib.profile_env import resolve_agy_api_key

        self.assertIsNone(resolve_agy_api_key(None))
        self.assertIsNone(resolve_agy_api_key("work"))

    def test_profiled_key_resolves(self):
        from lib.profile_env import resolve_agy_api_key

        with patch.dict(os.environ, {"AGY_API_KEY_WORK": "key-work"}):
            self.assertEqual(resolve_agy_api_key("work"), "key-work")

    def test_unprofiled_agent_still_resolves_the_default_key(self):
        from lib.profile_env import resolve_agy_api_key

        with patch.dict(os.environ, {"AGY_API_KEY_DEFAULT": "key-default"}):
            self.assertEqual(resolve_agy_api_key(None), "key-default")


def _fake_agent_class(chunks, stop_reason="stop"):
    """Build a fake Agent whose one chat call streams the given chunk
    objects -- standing in for a real Agy API round trip."""

    class _FakeResponse:
        def __init__(self):
            self.stop_reason = stop_reason

        async def chunks_gen(self):
            for chunk in chunks:
                yield chunk

        @property
        def chunks(self):
            return self.chunks_gen()

    class _FakeAgent:
        def __init__(self, config=None):
            self.config = config

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return None

        async def chat(self, prompt):
            return _FakeResponse()

    return _FakeAgent


class LogHopTests(unittest.TestCase):
    """Exercise _run_chat/_log_chunk against the real google.antigravity
    chunk dataclasses standing in for a chat stream -- no live API call,
    just the actual pydantic models a real stream would carry."""

    def test_every_hop_is_logged_in_order_before_the_result_returns(self):
        from google.antigravity.types import Text, Thought, ToolCall, ToolResult

        chunks = [
            Thought(step_index=0, text="thinking about it"),
            ToolCall(name="search", args={"q": "2+2"}),
            ToolResult(name="search", result="4"),
            Text(step_index=1, text="4"),
        ]

        from modules.agy_sdk.port import _run_chat

        with patch(
            "google.antigravity.Agent", new=_fake_agent_class(chunks)
        ), patch("modules.agy_sdk.port.log_record") as mock_log:
            text = _run_chat(
                "prompt", None,
                stream_id="sid", correlation_id="cid",
                source="alice", destination="bob",
            )

        self.assertEqual(text, "4")

        events = [call.args[1] for call in mock_log.call_args_list]
        self.assertEqual(
            events,
            [
                "agy_sdk_query_started",
                "agy_sdk_thought",
                "agy_sdk_tool_call",
                "agy_sdk_tool_result",
                "agy_sdk_text",
                "agy_sdk_query_finished",
            ],
        )

        for call in mock_log.call_args_list:
            self.assertEqual(call.args[0], "agy_sdk")
            self.assertEqual(call.kwargs["stream_id"], "sid")
            self.assertEqual(call.kwargs["correlation_id"], "cid")
            self.assertEqual(call.kwargs["source"], "alice")
            self.assertEqual(call.kwargs["destination"], "bob")

        tool_call_kwargs = mock_log.call_args_list[2].kwargs
        self.assertEqual(tool_call_kwargs["evidence"], "search")

        tool_result_kwargs = mock_log.call_args_list[3].kwargs
        self.assertEqual(tool_result_kwargs["evidence"], "search")
        self.assertIn("error=False", tool_result_kwargs["reason"])

        finished_kwargs = mock_log.call_args_list[5].kwargs
        self.assertEqual(finished_kwargs["evidence"], "stop")

    def test_unrecognized_chunk_type_is_logged_not_dropped(self):
        sentinel = object()

        from modules.agy_sdk.port import _run_chat

        with patch(
            "google.antigravity.Agent", new=_fake_agent_class([sentinel])
        ), patch("modules.agy_sdk.port.log_record") as mock_log:
            text = _run_chat(
                "prompt", None,
                stream_id="sid", correlation_id="cid",
                source="alice", destination="bob",
            )

        self.assertEqual(text, "")
        events = [call.args[1] for call in mock_log.call_args_list]
        self.assertEqual(events, ["agy_sdk_query_started", "agy_sdk_hop", "agy_sdk_query_finished"])
        self.assertEqual(mock_log.call_args_list[1].kwargs["evidence"], "object")


if __name__ == "__main__":
    unittest.main()
