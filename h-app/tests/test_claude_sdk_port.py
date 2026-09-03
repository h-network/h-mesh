import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import ANY, patch
from uuid import uuid4

import redis

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
from lib.profile_env import resolve_claude_profile_env
from modules.claude_sdk.port import deliver_claude_sdk

POD = "testpod"


class ClaudeSdkPortTests(unittest.TestCase):
    # A real Redis client, not tests/test_tmux_port.py's FakeRedis: this
    # port's delivery path now goes through lib/chat_memory.py's ChatMemory,
    # which pipelines real ZSET/EXPIRE/ZREMRANGEBY* commands FakeRedis
    # (a hand-matched stand-in for core.channels' own Lua scripts
    # specifically) never implemented and has no reason to grow to cover --
    # see tests/test_agentlifecycle.py's identical real_redis convention for
    # the same class of need.
    def setUp(self):
        self.no_ambient_claude_token = patch.dict(
            os.environ, {"CLAUDE_OAUTH_TOKEN_DEFAULT": ""}, clear=False
        )
        self.no_ambient_claude_token.start()
        self.addCleanup(self.no_ambient_claude_token.stop)
        self.redis = redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"))
        try:
            self.redis.ping()
        except Exception:
            self.skipTest("real Redis server not available at REDIS_URL")
        self.tenant = f"claudesdk-{uuid4().hex[:12]}"
        registry = prefix(POD, self.tenant, resource="registry")
        self.redis.hset(registry, "alice", "tmux")
        # Registering the destination's port_type as "claude_sdk" directly through
        # the same registry hash core/registry.py reads -- no hire command
        # involved, per this PoC's validation scope.
        self.redis.hset(registry, "bob", "claude_sdk")

    def queue(self, kind="Message", payload=None, source="alice", destination="bob"):
        stream_id = send(
            self.redis,
            pod=POD,
            tenant=self.tenant,
            source=source,
            destination=destination,
            kind=kind,
            payload=payload or {"text": "hello"},
        )
        raw = self.redis.lpop(prefix(POD, self.tenant, source, "egress"))
        self.redis.rpush(prefix(POD, self.tenant, destination, "ingress"), raw)
        return stream_id

    def test_registers_cleanly_as_a_generic_port_type(self):
        self.assertEqual(port_type(self.redis, pod=POD, tenant=self.tenant, agent="bob"), "claude_sdk")

    def test_message_runs_one_query_and_replies(self):
        stream_id = self.queue(payload={"text": "what's 2+2"})
        with patch("modules.claude_sdk.port._run_query", return_value="4") as mock_query:
            deliver_claude_sdk(self.redis, pod=POD, tenant=self.tenant, agent="bob")

        mock_query.assert_called_once_with(
            "[message from alice] what's 2+2",
            {},
            sdk_options={},
            stream_id=stream_id,
            correlation_id=ANY,
            source="alice",
            destination="bob",
        )

        raw = self.redis.lpop(prefix(POD, self.tenant, "bob", "egress"))
        reply = parse(raw)
        self.assertEqual(reply["payload"], {"text": "4"})
        self.assertEqual(reply["correlation_id"], stream_id)
        # The opener sends the reply itself -- no human/CLI in the loop to
        # correlate one later, unlike tmux's message_opener.
        self.assertEqual(reply["in_reply_to"], stream_id)
        self.assertEqual(reply["l2"]["source"], "bob")
        self.assertEqual(reply["l2"]["destination"], "alice")

    def test_no_reply_sent_when_result_is_blank(self):
        self.queue(payload={"text": "be silent"})
        with patch("modules.claude_sdk.port._run_query", return_value="   "):
            deliver_claude_sdk(self.redis, pod=POD, tenant=self.tenant, agent="bob")

        raw = self.redis.lpop(prefix(POD, self.tenant, "bob", "egress"))
        self.assertIsNone(raw)

    def test_empty_text_is_dead_lettered_without_calling_the_sdk(self):
        self.queue(payload={"text": ""})
        with patch("modules.claude_sdk.port._run_query") as mock_query:
            deliver_claude_sdk(self.redis, pod=POD, tenant=self.tenant, agent="bob")

        mock_query.assert_not_called()
        dead = self.redis.lpop(prefix(POD, self.tenant, "bob", "dead"))
        self.assertIsNotNone(dead)

    def test_query_failure_lands_in_unresolved_not_dead(self):
        self.queue(payload={"text": "boom"})
        with patch("modules.claude_sdk.port._run_query", side_effect=RuntimeError("sdk exploded")):
            deliver_claude_sdk(self.redis, pod=POD, tenant=self.tenant, agent="bob")

        # An exception after the query started is an unknown-effect outcome,
        # not a clean rejection -- it must not land in `dead`.
        self.assertIsNone(self.redis.lpop(prefix(POD, self.tenant, "bob", "dead")))
        unresolved_key = prefix(POD, self.tenant, resource="unresolved")
        self.assertEqual(self.redis.llen(unresolved_key), 1)

    def test_unknown_kind_is_dead_lettered_by_core_not_sdk_code(self):
        self.queue(kind="Command", payload={"text": "irrelevant"})
        with patch("modules.claude_sdk.port._run_query") as mock_query:
            deliver_claude_sdk(self.redis, pod=POD, tenant=self.tenant, agent="bob")

        mock_query.assert_not_called()
        dead = self.redis.lpop(prefix(POD, self.tenant, "bob", "dead"))
        self.assertIsNotNone(dead)

    def test_profile_env_is_threaded_into_the_query_call(self):
        self.redis.set(prefix(POD, self.tenant, agent="bob", resource="profile"), "work")
        self.queue()
        with patch.dict(os.environ, {"CLAUDE_OAUTH_TOKEN_WORK": "tok-work"}, clear=False):
            with patch("modules.claude_sdk.port._run_query", return_value="ok") as mock_query:
                deliver_claude_sdk(self.redis, pod=POD, tenant=self.tenant, agent="bob")

        home_dir = os.environ.get("HOME", os.path.expanduser("~"))
        mock_query.assert_called_once_with(
            "[message from alice] hello",
            {
                "CLAUDE_CONFIG_DIR": f"{home_dir}/.claude-work",
                "CODEX_HOME": f"{home_dir}/.codex-work",
                "CLAUDE_CODE_OAUTH_TOKEN": "tok-work",
            },
            sdk_options={},
            stream_id=ANY,
            correlation_id=ANY,
            source="alice",
            destination="bob",
        )

    def test_drains_multiple_queued_messages_independently(self):
        self.queue(payload={"text": "first"})
        self.queue(payload={"text": "second"})
        with patch(
            "modules.claude_sdk.port._run_query", side_effect=["reply-1", "reply-2"]
        ) as mock_query:
            deliver_claude_sdk(self.redis, pod=POD, tenant=self.tenant, agent="bob")

        self.assertEqual(mock_query.call_count, 2)
        first = parse(self.redis.lpop(prefix(POD, self.tenant, "bob", "egress")))
        second = parse(self.redis.lpop(prefix(POD, self.tenant, "bob", "egress")))
        self.assertEqual(first["payload"], {"text": "reply-1"})
        self.assertEqual(second["payload"], {"text": "reply-2"})

    def test_a_second_message_from_the_same_source_sees_the_first_as_history(self):
        self.queue(payload={"text": "first"})
        with patch("modules.claude_sdk.port._run_query", return_value="reply-1"):
            deliver_claude_sdk(self.redis, pod=POD, tenant=self.tenant, agent="bob")

        self.queue(payload={"text": "second"})
        with patch("modules.claude_sdk.port._run_query", return_value="reply-2") as mock_query:
            deliver_claude_sdk(self.redis, pod=POD, tenant=self.tenant, agent="bob")

        prompt = mock_query.call_args.args[0]
        self.assertIn("[user] [message from alice] first", prompt)
        self.assertIn("[assistant] reply-1", prompt)
        self.assertIn("Current message:\n[message from alice] second", prompt)

    def test_a_different_source_talking_to_the_same_agent_gets_no_shared_history(self):
        self.redis.hset(prefix(POD, self.tenant, resource="registry"), "carol", "tmux")
        self.queue(source="alice", payload={"text": "alice's message"})
        with patch("modules.claude_sdk.port._run_query", return_value="reply-to-alice"):
            deliver_claude_sdk(self.redis, pod=POD, tenant=self.tenant, agent="bob")

        self.queue(source="carol", payload={"text": "carol's message"})
        with patch("modules.claude_sdk.port._run_query", return_value="reply-to-carol") as mock_query:
            deliver_claude_sdk(self.redis, pod=POD, tenant=self.tenant, agent="bob")

        prompt = mock_query.call_args.args[0]
        self.assertNotIn("alice", prompt)
        self.assertEqual(prompt, "[message from carol] carol's message")

    def test_configured_sdk_options_are_threaded_into_the_query_call(self):
        self.redis.set(
            prefix(POD, self.tenant, agent="bob", resource="sdk-options"),
            json.dumps({"system_prompt": "be terse", "model": "claude-x"}),
        )
        self.queue()
        with patch("modules.claude_sdk.port._run_query", return_value="ok") as mock_query:
            deliver_claude_sdk(self.redis, pod=POD, tenant=self.tenant, agent="bob")

        self.assertEqual(
            mock_query.call_args.kwargs["sdk_options"],
            {"system_prompt": "be terse", "model": "claude-x"},
        )

    def test_disallowed_sdk_option_fields_are_dropped_not_fatal(self):
        self.redis.set(
            prefix(POD, self.tenant, agent="bob", resource="sdk-options"),
            json.dumps({"model": "claude-x", "env": {"INJECTED": "1"}, "cli_path": "/evil"}),
        )
        self.queue()
        with patch("modules.claude_sdk.port._run_query", return_value="ok") as mock_query:
            deliver_claude_sdk(self.redis, pod=POD, tenant=self.tenant, agent="bob")

        self.assertEqual(mock_query.call_args.kwargs["sdk_options"], {"model": "claude-x"})

    def test_malformed_sdk_options_config_falls_back_to_empty_not_fatal(self):
        self.redis.set(prefix(POD, self.tenant, agent="bob", resource="sdk-options"), "not json")
        self.queue()
        with patch("modules.claude_sdk.port._run_query", return_value="ok") as mock_query:
            deliver_claude_sdk(self.redis, pod=POD, tenant=self.tenant, agent="bob")

        self.assertEqual(mock_query.call_args.kwargs["sdk_options"], {})


class ProfileEnvTests(unittest.TestCase):
    def setUp(self):
        self.no_ambient_tokens = patch.dict(
            os.environ,
            {"CLAUDE_OAUTH_TOKEN_DEFAULT": "", "CLAUDE_OAUTH_TOKEN_WORK": ""},
            clear=False,
        )
        self.no_ambient_tokens.start()
        self.addCleanup(self.no_ambient_tokens.stop)

    def test_unprofiled_agent_gets_no_config_dir_override(self):
        self.assertEqual(resolve_claude_profile_env(None), {})

    def test_profiled_agent_gets_config_dirs(self):
        env = resolve_claude_profile_env("work", home_dir="/home/test")
        self.assertEqual(
            env,
            {
                "CLAUDE_CONFIG_DIR": "/home/test/.claude-work",
                "CODEX_HOME": "/home/test/.codex-work",
            },
        )

    def test_token_absent_is_omitted_not_empty(self):
        env = resolve_claude_profile_env("work", home_dir="/home/test")
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)

    def test_token_present_is_included(self):
        with patch.dict(os.environ, {"CLAUDE_OAUTH_TOKEN_WORK": "tok-work"}):
            env = resolve_claude_profile_env("work", home_dir="/home/test")
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "tok-work")

    def test_unprofiled_agent_still_resolves_the_default_token(self):
        with patch.dict(os.environ, {"CLAUDE_OAUTH_TOKEN_DEFAULT": "tok-default"}):
            env = resolve_claude_profile_env(None, home_dir="/home/test")
        self.assertEqual(env, {"CLAUDE_CODE_OAUTH_TOKEN": "tok-default"})


class LogHopTests(unittest.TestCase):
    """Exercise _run_query/_log_hop against a stubbed claude_agent_sdk.query --
    no live model call, just the real SDK dataclasses standing in for what a
    real stream would yield, so the hop-logging shape is verified against the
    actual Message union rather than a hand-rolled fake."""

    def setUp(self):
        self.no_ambient_claude_token = patch.dict(
            os.environ, {"CLAUDE_OAUTH_TOKEN_DEFAULT": ""}, clear=False
        )
        self.no_ambient_claude_token.start()
        self.addCleanup(self.no_ambient_claude_token.stop)

    def test_every_hop_is_logged_in_order_before_the_result_returns(self):
        import claude_agent_sdk as sdk

        started = sdk.SystemMessage(subtype="init", data={"session_id": "s1"})
        turn = sdk.AssistantMessage(
            content=[
                sdk.ToolUseBlock(id="t1", name="Read", input={}),
                sdk.TextBlock(text="looking"),
            ],
            model="claude-x",
            stop_reason="tool_use",
        )
        finished = sdk.ResultMessage(
            subtype="success",
            duration_ms=10,
            duration_api_ms=8,
            is_error=False,
            num_turns=2,
            session_id="s1",
            result="done",
        )

        async def fake_query(*, prompt, options=None):
            for message in (started, turn, finished):
                yield message

        from modules.claude_sdk.port import _run_query

        with patch("claude_agent_sdk.query", new=fake_query), patch(
            "modules.claude_sdk.port.log_record"
        ) as mock_log:
            text = _run_query(
                "prompt",
                {},
                stream_id="sid",
                correlation_id="cid",
                source="alice",
                destination="bob",
            )

        self.assertEqual(text, "done")

        events = [call.args[1] for call in mock_log.call_args_list]
        self.assertEqual(events, ["claude_sdk_query_started", "claude_sdk_turn", "claude_sdk_query_finished"])

        for call in mock_log.call_args_list:
            self.assertEqual(call.args[0], "claude_sdk")
            self.assertEqual(call.kwargs["stream_id"], "sid")
            self.assertEqual(call.kwargs["correlation_id"], "cid")
            self.assertEqual(call.kwargs["source"], "alice")
            self.assertEqual(call.kwargs["destination"], "bob")

        started_kwargs = mock_log.call_args_list[0].kwargs
        self.assertEqual(started_kwargs["evidence"], "init")

        turn_kwargs = mock_log.call_args_list[1].kwargs
        self.assertIn("stop_reason=tool_use", turn_kwargs["reason"])
        self.assertIn("tools=Read", turn_kwargs["reason"])

        finished_kwargs = mock_log.call_args_list[2].kwargs
        self.assertEqual(finished_kwargs["evidence"], "success")
        self.assertIn("is_error=False", finished_kwargs["reason"])
        self.assertIn("num_turns=2", finished_kwargs["reason"])

    def test_unrecognized_message_type_is_logged_not_dropped(self):
        sentinel = object()

        async def fake_query(*, prompt, options=None):
            yield sentinel

        from modules.claude_sdk.port import _run_query

        with patch("claude_agent_sdk.query", new=fake_query), patch(
            "modules.claude_sdk.port.log_record"
        ) as mock_log:
            text = _run_query(
                "prompt", {},
                stream_id="sid", correlation_id="cid",
                source="alice", destination="bob",
            )

        self.assertEqual(text, "")
        mock_log.assert_called_once()
        self.assertEqual(mock_log.call_args.args[1], "claude_sdk_hop")
        self.assertEqual(mock_log.call_args.kwargs["evidence"], "object")

    def test_sdk_options_are_merged_onto_claude_agent_options(self):
        import claude_agent_sdk as sdk

        finished = sdk.ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=1, session_id="s1", result="ok",
        )
        captured_options = {}

        async def fake_query(*, prompt, options=None):
            captured_options["value"] = options
            yield finished

        from modules.claude_sdk.port import _run_query

        with patch("claude_agent_sdk.query", new=fake_query):
            _run_query(
                "prompt", {}, sdk_options={"model": "claude-x", "max_turns": 3},
                stream_id="sid", correlation_id="cid", source="alice", destination="bob",
            )

        options = captured_options["value"]
        self.assertEqual(options.model, "claude-x")
        self.assertEqual(options.max_turns, 3)


if __name__ == "__main__":
    unittest.main()
