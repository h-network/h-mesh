import base64
import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

H_APP = Path(__file__).resolve().parents[1]
if str(H_APP) not in sys.path:
    sys.path.insert(0, str(H_APP))
TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

from openshell import ExecResult

from core.channels import send
from core.envelope import parse
from core.keys import incarnation_key, prefix
from modules.openshell.headless import headless_command
from modules.openshell.client import OpenShellUnavailable
from modules.openshell.naming import sandbox_name, short_name, workspace_name
from modules.openshell.port import _exec_headless, deliver_openshell
from test_tmux_port import FakeRedis

POD = "testpod"
TENANT = "testtenant"


class OpenShellPortTests(unittest.TestCase):
    def setUp(self):
        self.no_ambient_claude_token = patch.dict(
            os.environ, {"CLAUDE_OAUTH_TOKEN_DEFAULT": ""}, clear=False
        )
        self.no_ambient_claude_token.start()
        self.addCleanup(self.no_ambient_claude_token.stop)
        self.redis = FakeRedis()
        registry = prefix(POD, TENANT, resource="registry")
        self.redis.hset(registry, "alice", "tmux")
        self.redis.hset(registry, "bob", "openshell")
        self.client = MagicMock()
        self.client.get_sandbox.return_value = SimpleNamespace(id="sbx-1")
        self.client.exec_sandbox.return_value = ExecResult(0, "reply", "")

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

    def test_headless_commands_and_names(self):
        self.assertEqual(headless_command("claude", resume=True), ["claude", "-p", "-c"])
        self.assertEqual(
            headless_command("codex", resume=True),
            ["codex", "exec", "--skip-git-repo-check", "resume", "--last", "-"],
        )
        self.assertEqual(short_name("short"), "short")
        self.assertLessEqual(len(sandbox_name("a" * 63)), 19)
        self.assertEqual(workspace_name("pod", "tenant"), "pod-tenant")

    def test_message_executes_and_replies(self):
        stream_id = self.queue()
        deliver_openshell(self.redis, pod=POD, tenant=TENANT, agent="bob", client=self.client)

        self.client.exec_sandbox.assert_called_once_with(
            "sbx-1",
            ["claude", "-p", "-c"],
            stdin=b"[message from alice] hello",
            env=None,
        )
        raw = self.redis.lpop(prefix(POD, TENANT, "bob", "egress"))
        reply = parse(raw)
        self.assertEqual(reply["payload"], {"text": "reply"})
        self.assertEqual(reply["correlation_id"], stream_id)
        # Openshell replies are generated mechanically in the same call that
        # received the envelope, so correlation is automatic and exact --
        # no --reply-to, no agent cooperation, unlike tmux.
        self.assertEqual(reply["in_reply_to"], stream_id)

    def test_reply_correlation_records_delivery_for_validation(self):
        from lib.reply_correlation import was_delivered

        self.redis.set(incarnation_key(POD, TENANT, "bob"), "test-incarnation")
        stream_id = self.queue()
        self.assertFalse(
            was_delivered(self.redis, pod=POD, tenant=TENANT, agent="bob", stream_id=stream_id, source="alice")
        )
        deliver_openshell(self.redis, pod=POD, tenant=TENANT, agent="bob", client=self.client)
        # deliver_api validates in_reply_to against real provenance --
        # recipient AND originating source -- regardless of how it was
        # produced. This confirms an openshell reply's automatic
        # in_reply_to would actually pass that check when the destination
        # matches who really sent it (alice).
        self.assertTrue(
            was_delivered(self.redis, pod=POD, tenant=TENANT, agent="bob", stream_id=stream_id, source="alice")
        )
        # And that it does NOT validate toward a different claimed source --
        # the cross-client case.
        self.assertFalse(
            was_delivered(self.redis, pod=POD, tenant=TENANT, agent="bob", stream_id=stream_id, source="mallory")
        )

    def test_command_has_no_message_prefix(self):
        self.queue("Command", {"text": "git status"})
        deliver_openshell(self.redis, pod=POD, tenant=TENANT, agent="bob", client=self.client)
        self.assertEqual(self.client.exec_sandbox.call_args.kwargs["stdin"], b"git status")

    def test_remote_outcome_failure_is_unresolved_not_dead(self):
        stream_id = self.queue("Command", {"text": "may have executed"})
        self.client.exec_sandbox.side_effect = OpenShellUnavailable("response lost")

        deliver_openshell(self.redis, pod=POD, tenant=TENANT, agent="bob", client=self.client)

        unresolved = self.redis.lists[prefix(POD, TENANT, resource="unresolved")]
        self.assertEqual(
            [parse(json.loads(record)["envelope"])["stream_id"] for record in unresolved],
            [stream_id],
        )
        self.assertEqual(list(self.redis.lists[prefix(POD, TENANT, "bob", "dead")]), [])

    def test_sandbox_lookup_failure_is_proven_dead_before_submission(self):
        stream_id = self.queue("Command", {"text": "never submitted"})
        self.client.get_sandbox.side_effect = OpenShellUnavailable("lookup failed")

        deliver_openshell(self.redis, pod=POD, tenant=TENANT, agent="bob", client=self.client)

        dead = self.redis.lists[prefix(POD, TENANT, "bob", "dead")]
        self.assertEqual([parse(raw)["stream_id"] for raw in dead], [stream_id])
        self.assertEqual(
            list(self.redis.lists[prefix(POD, TENANT, resource="unresolved")]), []
        )
        self.client.exec_sandbox.assert_not_called()

    def test_unsupported_cli_is_proven_dead_before_submission(self):
        stream_id = self.queue("Command", {"text": "never submitted"})
        self.redis.set(
            prefix(POD, TENANT, agent="bob", resource="launch"), "unsupported"
        )

        deliver_openshell(self.redis, pod=POD, tenant=TENANT, agent="bob", client=self.client)

        dead = self.redis.lists[prefix(POD, TENANT, "bob", "dead")]
        self.assertEqual([parse(raw)["stream_id"] for raw in dead], [stream_id])
        self.assertEqual(
            list(self.redis.lists[prefix(POD, TENANT, resource="unresolved")]), []
        )
        self.client.get_sandbox.assert_not_called()
        self.client.exec_sandbox.assert_not_called()

    def test_attachment_writes_atomically_then_notifies(self):
        stream_id = self.queue(
            "Attachment",
            {
                "filename": "note.txt",
                "mime_type": "text/plain",
                "content_base64": base64.b64encode(b"hello").decode(),
            },
        )
        self.client.exec_sandbox.side_effect = [
            ExecResult(0, "", ""),
            ExecResult(0, "received", ""),
        ]
        deliver_openshell(self.redis, pod=POD, tenant=TENANT, agent="bob", client=self.client)

        write = self.client.exec_sandbox.call_args_list[0]
        self.assertEqual(
            write.args[1][:3],
            ["/bin/sh", "-c", 'mkdir -p "$1" && base64 -d > "$2" && mv -f "$2" "$3"'],
        )
        self.assertEqual(base64.b64decode(write.kwargs["stdin"]), b"hello")
        self.assertIn(f"/sandbox/attachments/{stream_id}/note.txt", write.args[1])
        notice = self.client.exec_sandbox.call_args_list[1].kwargs["stdin"]
        self.assertIn(b"[attachment from alice]", notice)

    def test_codex_credential_is_written_and_wiped_after_failed_exec(self):
        self.client.exec_sandbox.side_effect = [
            ExecResult(0, "", ""),
            RuntimeError("cli failed"),
            ExecResult(0, "", ""),
        ]
        with patch.dict(os.environ, {"CODEX_AUTH_JSON_WORK": '{"token":"secret"}'}, clear=False):
            with self.assertRaises(RuntimeError):
                _exec_headless(self.client, "bob", "codex", "hello", profile="work")

        calls = self.client.exec_sandbox.call_args_list
        self.assertEqual(len(calls), 3)
        self.assertEqual(base64.b64decode(calls[0].kwargs["stdin"]), b'{"token":"secret"}')
        self.assertEqual(calls[2].args[1][2], 'shred -u "$1" 2>/dev/null || rm -f "$1" 2>/dev/null || true')

    @patch("modules.openshell.port.deliver_openshell")
    @patch("modules.openshell.port.delivery_lock")
    @patch("modules.openshell.port.redis.Redis.from_url")
    def test_main_contract(self, from_url, lock, deliver):
        from modules.openshell.port import main

        from_url.return_value = self.redis
        lock.return_value.__enter__.return_value = True
        with patch.dict(os.environ, {"POD": POD, "TENANT": TENANT, "REDIS_URL": "redis://example"}):
            main(["bob"])
        from_url.assert_called_once_with("redis://example")
        lock.assert_called_once_with(self.redis, pod=POD, tenant=TENANT, agent="bob")
        deliver.assert_called_once_with(self.redis, pod=POD, tenant=TENANT, agent="bob")


if __name__ == "__main__":
    unittest.main()
