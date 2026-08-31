import base64
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
from core.keys import prefix
from modules.openshell.headless import headless_command
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

    def test_command_has_no_message_prefix(self):
        self.queue("Command", {"text": "git status"})
        deliver_openshell(self.redis, pod=POD, tenant=TENANT, agent="bob", client=self.client)
        self.assertEqual(self.client.exec_sandbox.call_args.kwargs["stdin"], b"git status")

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
