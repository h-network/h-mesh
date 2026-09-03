import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

H_APP = Path(__file__).resolve().parents[1]
if str(H_APP) not in sys.path:
    sys.path.insert(0, str(H_APP))
TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

from core.channels import DeadLetter, send
from core.envelope import parse
from core.keys import prefix
from core.registry import port_type
from lib.profile_env import resolve_claude_profile_env
from modules.sdk.port import deliver_sdk
from test_tmux_port import FakeRedis

POD = "testpod"
TENANT = "testtenant"


class SdkPortTests(unittest.TestCase):
    def setUp(self):
        self.no_ambient_claude_token = patch.dict(
            os.environ, {"CLAUDE_OAUTH_TOKEN_DEFAULT": ""}, clear=False
        )
        self.no_ambient_claude_token.start()
        self.addCleanup(self.no_ambient_claude_token.stop)
        self.redis = FakeRedis()
        registry = prefix(POD, TENANT, resource="registry")
        self.redis.hset(registry, "alice", "tmux")
        # Registering the destination's port_type as "sdk" directly through
        # the same registry hash core/registry.py reads -- no hire command
        # involved, per this PoC's validation scope.
        self.redis.hset(registry, "bob", "sdk")

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
        self.assertEqual(port_type(self.redis, pod=POD, tenant=TENANT, agent="bob"), "sdk")

    def test_message_runs_one_query_and_replies(self):
        stream_id = self.queue(payload={"text": "what's 2+2"})
        with patch("modules.sdk.port._run_query", return_value="4") as mock_query:
            deliver_sdk(self.redis, pod=POD, tenant=TENANT, agent="bob")

        mock_query.assert_called_once_with("[message from alice] what's 2+2", {})

        raw = self.redis.lpop(prefix(POD, TENANT, "bob", "egress"))
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
        with patch("modules.sdk.port._run_query", return_value="   "):
            deliver_sdk(self.redis, pod=POD, tenant=TENANT, agent="bob")

        raw = self.redis.lpop(prefix(POD, TENANT, "bob", "egress"))
        self.assertIsNone(raw)

    def test_empty_text_is_dead_lettered_without_calling_the_sdk(self):
        self.queue(payload={"text": ""})
        with patch("modules.sdk.port._run_query") as mock_query:
            deliver_sdk(self.redis, pod=POD, tenant=TENANT, agent="bob")

        mock_query.assert_not_called()
        dead = self.redis.lpop(prefix(POD, TENANT, "bob", "dead"))
        self.assertIsNotNone(dead)

    def test_query_failure_lands_in_unresolved_not_dead(self):
        self.queue(payload={"text": "boom"})
        with patch("modules.sdk.port._run_query", side_effect=RuntimeError("sdk exploded")):
            deliver_sdk(self.redis, pod=POD, tenant=TENANT, agent="bob")

        # An exception after the query started is an unknown-effect outcome,
        # not a clean rejection -- it must not land in `dead`.
        self.assertIsNone(self.redis.lpop(prefix(POD, TENANT, "bob", "dead")))
        unresolved_key = prefix(POD, TENANT, resource="unresolved")
        self.assertEqual(self.redis.llen(unresolved_key), 1)

    def test_unknown_kind_is_dead_lettered_by_core_not_sdk_code(self):
        self.queue(kind="Command", payload={"text": "irrelevant"})
        with patch("modules.sdk.port._run_query") as mock_query:
            deliver_sdk(self.redis, pod=POD, tenant=TENANT, agent="bob")

        mock_query.assert_not_called()
        dead = self.redis.lpop(prefix(POD, TENANT, "bob", "dead"))
        self.assertIsNotNone(dead)

    def test_profile_env_is_threaded_into_the_query_call(self):
        self.redis.set(prefix(POD, TENANT, agent="bob", resource="profile"), "work")
        self.queue()
        with patch.dict(os.environ, {"CLAUDE_OAUTH_TOKEN_WORK": "tok-work"}, clear=False):
            with patch("modules.sdk.port._run_query", return_value="ok") as mock_query:
                deliver_sdk(self.redis, pod=POD, tenant=TENANT, agent="bob")

        home_dir = os.environ.get("HOME", os.path.expanduser("~"))
        mock_query.assert_called_once_with(
            "[message from alice] hello",
            {
                "CLAUDE_CONFIG_DIR": f"{home_dir}/.claude-work",
                "CODEX_HOME": f"{home_dir}/.codex-work",
                "CLAUDE_CODE_OAUTH_TOKEN": "tok-work",
            },
        )

    def test_drains_multiple_queued_messages_independently(self):
        self.queue(payload={"text": "first"})
        self.queue(payload={"text": "second"})
        with patch(
            "modules.sdk.port._run_query", side_effect=["reply-1", "reply-2"]
        ) as mock_query:
            deliver_sdk(self.redis, pod=POD, tenant=TENANT, agent="bob")

        self.assertEqual(mock_query.call_count, 2)
        first = parse(self.redis.lpop(prefix(POD, TENANT, "bob", "egress")))
        second = parse(self.redis.lpop(prefix(POD, TENANT, "bob", "egress")))
        self.assertEqual(first["payload"], {"text": "reply-1"})
        self.assertEqual(second["payload"], {"text": "reply-2"})


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


if __name__ == "__main__":
    unittest.main()
