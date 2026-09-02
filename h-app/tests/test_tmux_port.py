import base64
import json
import os
import sys
import tempfile
import unittest
from collections import defaultdict, deque
from pathlib import Path
from unittest.mock import MagicMock, patch

H_APP = Path(__file__).resolve().parents[1]
if str(H_APP) not in sys.path:
    sys.path.insert(0, str(H_APP))

from core.channels import send
from core.envelope import parse
from core.keys import prefix
from modules.tmux import (
    attachment_opener,
    command_opener,
    deliver_tmux,
    mark_delivery_pending,
    message_opener,
    messages_opener,
)


POD = "testpod"
TENANT = "testtenant"


class FakeRedis:
    def __init__(self):
        self.lists = defaultdict(deque)
        self.hashes = defaultdict(dict)
        self.kv = {}
        self.streams = defaultdict(list)
        self.sets = defaultdict(set)

    def rpush(self, key, *values):
        self.lists[key].extend(values)
        return len(self.lists[key])

    def lpop(self, key):
        return self.lists[key].popleft() if self.lists[key] else None

    def llen(self, key):
        return len(self.lists[key])

    def sadd(self, key, *values):
        self.sets.setdefault(key, set()).update(values)
        return len(values)

    def srem(self, key, *values):
        for value in values:
            self.sets.get(key, set()).discard(value)

    def sismember(self, key, value):
        return value in self.sets.get(key, set())

    def blpop(self, keys, timeout=0):
        if isinstance(keys, str):
            keys = [keys]
        for key in keys:
            if self.lists[key]:
                return key, self.lists[key].popleft()
        return None

    def hset(self, key, field=None, value=None, mapping=None):
        if mapping:
            self.hashes[key].update(mapping)
            return len(mapping)
        self.hashes[key][field] = value
        return 1

    def hsetnx(self, key, field, value):
        if field in self.hashes[key]:
            return 0
        self.hashes[key][field] = value
        return 1

    def hget(self, key, field):
        return self.hashes[key].get(field)

    def hdel(self, key, field):
        return int(self.hashes[key].pop(field, None) is not None)

    def get(self, key):
        return self.kv.get(key)

    def set(self, key, value, nx=False, px=None):
        if nx and key in self.kv:
            return False
        self.kv[key] = value
        return True

    def xadd(self, key, fields, maxlen=None, approximate=False):
        self.streams[key].append(fields)
        return f"{len(self.streams[key])}-0"

    def eval(self, script, key_count, *args):
        keys = args[:key_count]
        argv = args[key_count:]
        if "core delivery lock release" in script:
            key, token = keys[0], argv[0]
            if self.kv.get(key) != token:
                return 0
            self.kv.pop(key, None)
            return 1
        if "core delivery lock renew" in script:
            key, token = keys[0], argv[0]
            return int(self.kv.get(key) == token)
        if "core unreplied increment" in script:
            key, client, since = keys[0], argv[0], argv[1]
            existing = self.hget(key, client)
            data = json.loads(existing) if existing else None
            if (
                isinstance(data, dict)
                and isinstance(data.get("count"), (int, float))
                and isinstance(data.get("since"), str)
                and data["since"]
            ):
                value = {"count": data["count"] + 1, "since": min(data["since"], since)}
            else:
                value = {"count": 1, "since": since}
            self.hset(key, client, json.dumps(value, separators=(",", ":")))
            return value["count"]
        raise AssertionError(f"unexpected Lua script: {script}")


class TmuxPortTests(unittest.TestCase):
    def setUp(self):
        self.redis = FakeRedis()
        self.registry = prefix(POD, TENANT, resource="registry")
        self.recipient_observations = patch("core.channels._emit_for_recipient")
        self.emit_for_recipient = self.recipient_observations.start()
        self.addCleanup(self.recipient_observations.stop)

    def register(self, **agents):
        for agent, port_type in agents.items():
            self.redis.hset(self.registry, agent, port_type)

    @patch("modules.tmux.port.submit_text")
    @patch("modules.tmux.port.list_windows", return_value={"alice", "bob"})
    def test_deliver_tmux_message(self, mock_list, mock_submit):
        self.register(alice="tmux", bob="tmux")
        stream_id = send(
            self.redis, pod=POD, tenant=TENANT, source="alice",
            destination="bob", payload={"text": "hello bob"},
        )
        # Move from alice egress to bob ingress
        raw = self.redis.lpop(prefix(POD, TENANT, "alice", "egress"))
        self.redis.rpush(prefix(POD, TENANT, "bob", "ingress"), raw)

        deliver_tmux(self.redis, pod=POD, tenant=TENANT, agent="bob", session_name="testtenant")

        mock_submit.assert_called_once_with(
            "testtenant", "bob", "[message from alice] hello bob\n",
            stream_id=stream_id, socket=None,
        )

    @patch("modules.tmux.port.submit_text")
    @patch("modules.tmux.port.list_windows", return_value={"alice", "bob"})
    def test_deliver_tmux_message_from_api_adds_reply_notice(self, mock_list, mock_submit):
        self.register(telegram="api", bob="tmux")
        stream_id = send(
            self.redis, pod=POD, tenant=TENANT, source="telegram",
            destination="bob", payload={"text": "ping"},
        )
        raw = self.redis.lpop(prefix(POD, TENANT, "telegram", "egress"))
        self.redis.rpush(prefix(POD, TENANT, "bob", "ingress"), raw)

        deliver_tmux(self.redis, pod=POD, tenant=TENANT, agent="bob", session_name="testtenant")

        expected_msg = (
            f"[message from telegram] ping\n"
            f'[reply to telegram: office send -a telegram --reply-to {stream_id} "..."]\n'
        )
        mock_submit.assert_called_once_with(
            "testtenant", "bob", expected_msg,
            stream_id=stream_id, socket=None,
        )

    @patch("modules.tmux.port.submit_text")
    @patch("modules.tmux.port.list_windows", return_value={"bob"})
    def test_deliver_tmux_command(self, mock_list, mock_submit):
        self.register(alice="tmux", bob="tmux")
        stream_id = send(
            self.redis, pod=POD, tenant=TENANT, source="alice",
            destination="bob", payload={"text": "echo hi"}, kind="Command",
        )
        raw = self.redis.lpop(prefix(POD, TENANT, "alice", "egress"))
        self.redis.rpush(prefix(POD, TENANT, "bob", "ingress"), raw)

        deliver_tmux(self.redis, pod=POD, tenant=TENANT, agent="bob", session_name="testtenant")

        mock_submit.assert_called_once_with(
            "testtenant", "bob", "echo hi\n",
            stream_id=stream_id, socket=None,
        )

    def test_deliver_tmux_add_ticket(self):
        self.register(alice="tmux", bob="tmux")
        stream_id = send(
            self.redis, pod=POD, tenant=TENANT, source="alice",
            destination="bob", payload={"id": "t1", "title": "Fix bug", "description": "urgent"},
            kind="AddTicket",
        )
        raw = self.redis.lpop(prefix(POD, TENANT, "alice", "egress"))
        self.redis.rpush(prefix(POD, TENANT, "bob", "ingress"), raw)

        deliver_tmux(self.redis, pod=POD, tenant=TENANT, agent="bob", session_name="testtenant")

        todo_key = prefix(POD, TENANT, agent="bob", resource="tasks.todo")
        ticket_raw = self.redis.lpop(todo_key)
        self.assertIsNotNone(ticket_raw)
        ticket = json.loads(ticket_raw)
        self.assertEqual(ticket["id"], "t1")
        self.assertEqual(ticket["title"], "Fix bug")

    @patch("modules.tmux.port.submit_text")
    @patch("modules.tmux.port.list_windows", return_value={"bob"})
    def test_deliver_tmux_attachment(self, mock_list, mock_submit):
        self.register(alice="tmux", bob="tmux")
        content = b"sample content"
        b64 = base64.b64encode(content).decode("ascii")
        with tempfile.TemporaryDirectory() as tmpdir:
            stream_id = send(
                self.redis, pod=POD, tenant=TENANT, source="alice",
                destination="bob",
                payload={
                    "filename": "sample.txt",
                    "mime_type": "text/plain",
                    "content_base64": b64,
                    "caption": "look at this",
                },
                kind="Attachment",
            )
            raw = self.redis.lpop(prefix(POD, TENANT, "alice", "egress"))
            self.redis.rpush(prefix(POD, TENANT, "bob", "ingress"), raw)

            # We pass attachment_opener kwargs or patch workdir_root
            with patch("modules.tmux.port.attachment_opener", side_effect=lambda **kw: attachment_opener(workdir_root=tmpdir, **kw)):
                deliver_tmux(self.redis, pod=POD, tenant=TENANT, agent="bob", session_name="testtenant")

            saved_file = Path(tmpdir) / "bob" / "attachments" / stream_id / "sample.txt"
            self.assertTrue(saved_file.is_file())
            self.assertEqual(saved_file.read_bytes(), content)

            expected_notice = (
                f"[attachment from alice] saved to {saved_file} (text/plain, {len(content)} bytes)\n"
                "[attachment caption] look at this\n"
            )
            mock_submit.assert_called_once_with(
                "testtenant", "bob", expected_notice,
                stream_id=stream_id, socket=None,
            )

    @patch("modules.tmux.port.list_windows", return_value=set())
    def test_deliver_tmux_missing_window_dead_letters(self, mock_list):
        self.register(alice="tmux", bob="tmux")
        stream_id = send(
            self.redis, pod=POD, tenant=TENANT, source="alice",
            destination="bob", payload={"text": "hello"},
        )
        raw = self.redis.lpop(prefix(POD, TENANT, "alice", "egress"))
        self.redis.rpush(prefix(POD, TENANT, "bob", "ingress"), raw)

        deliver_tmux(self.redis, pod=POD, tenant=TENANT, agent="bob", session_name="testtenant")

        dead_key = prefix(POD, TENANT, "bob", "dead")
        dead_raw = self.redis.lpop(dead_key)
        self.assertIsNotNone(dead_raw)
        self.assertEqual(parse(dead_raw)["stream_id"], stream_id)

    @patch("modules.tmux.port.submit_text")
    @patch("modules.tmux.port.list_windows", return_value={"bob"})
    def test_deliver_tmux_pops_one_envelope_per_call(self, mock_list, mock_submit):
        self.register(alice="tmux", bob="tmux")
        id1 = send(self.redis, pod=POD, tenant=TENANT, source="alice", destination="bob", payload={"text": "msg 1"})
        raw1 = self.redis.lpop(prefix(POD, TENANT, "alice", "egress"))
        id2 = send(self.redis, pod=POD, tenant=TENANT, source="alice", destination="bob", payload={"text": "msg 2"})
        raw2 = self.redis.lpop(prefix(POD, TENANT, "alice", "egress"))

        self.redis.rpush(prefix(POD, TENANT, "bob", "ingress"), raw1, raw2)

        # First call pops msg 1
        deliver_tmux(self.redis, pod=POD, tenant=TENANT, agent="bob", session_name="testtenant")
        self.assertEqual(mock_submit.call_count, 1)
        mock_submit.assert_called_with("testtenant", "bob", "[message from alice] msg 1\n", stream_id=id1, socket=None)

        # Second call pops msg 2
        deliver_tmux(self.redis, pod=POD, tenant=TENANT, agent="bob", session_name="testtenant")
        self.assertEqual(mock_submit.call_count, 2)
        mock_submit.assert_called_with("testtenant", "bob", "[message from alice] msg 2\n", stream_id=id2, socket=None)

        # Ingress is now empty
        self.assertIsNone(self.redis.lpop(prefix(POD, TENANT, "bob", "ingress")))

    def test_mark_delivery_pending(self):
        # When CLI is not set or bash, no markers are written
        mark_delivery_pending(self.redis, POD, TENANT, "bob", "sid-1")
        self.assertEqual(len(self.redis.streams[prefix(POD, TENANT, agent="bob", resource="pending.verify")]), 0)

        # When CLI is claude, markers are written
        self.redis.set(prefix(POD, TENANT, agent="bob", resource="launch"), "claude")
        mark_delivery_pending(self.redis, POD, TENANT, "bob", "sid-2", correlation_id="corr-1")
        verify_entries = self.redis.streams[prefix(POD, TENANT, agent="bob", resource="pending.verify")]
        markers_entries = self.redis.streams[prefix(POD, TENANT, agent="bob", resource="delivery.markers")]
        self.assertEqual(len(verify_entries), 1)
        self.assertEqual(verify_entries[0]["stream_id"], "sid-2")
        self.assertEqual(verify_entries[0]["correlation_id"], "corr-1")
        self.assertEqual(len(markers_entries), 1)
        self.assertEqual(markers_entries[0]["stream_id"], "sid-2")

        # When CLI is agy, markers are written
        self.redis.set(prefix(POD, TENANT, agent="bob", resource="launch"), "agy")
        mark_delivery_pending(self.redis, POD, TENANT, "bob", "sid-3")
        self.assertEqual(len(self.redis.streams[prefix(POD, TENANT, agent="bob", resource="pending.verify")]), 2)

    @patch("modules.tmux.port.deliver_tmux")
    @patch("redis.Redis.from_url")
    @patch("signal.signal")
    def test_main_entrypoint(self, mock_signal, mock_redis_from_url, mock_deliver):
        import signal
        from modules.tmux.port import main

        mock_redis_from_url.return_value = self.redis
        env = {"POD": POD, "TENANT": TENANT, "REDIS_URL": "redis://127.0.0.1:6379/0"}
        with patch.dict(os.environ, env):
            main(["bob"])

        mock_signal.assert_called_once_with(signal.SIGCHLD, signal.SIG_DFL)
        mock_deliver.assert_called_once_with(self.redis, pod=POD, tenant=TENANT, agent="bob")

    @patch("modules.tmux.port.deliver_tmux")
    @patch("redis.Redis.from_url")
    @patch("signal.signal")
    def test_main_entrypoint_skips_when_paused(self, mock_signal, mock_redis_from_url, mock_deliver):
        from modules.tmux.port import main

        mock_redis_from_url.return_value = self.redis
        self.redis.set(prefix(POD, TENANT, agent="bob", resource="paused"), "1")
        env = {"POD": POD, "TENANT": TENANT, "REDIS_URL": "redis://127.0.0.1:6379/0"}
        with patch.dict(os.environ, env):
            main(["bob"])

        mock_deliver.assert_not_called()

    @patch("signal.signal")
    def test_main_entrypoint_missing_arg_exits(self, mock_signal):
        from modules.tmux.port import main

        with self.assertRaises(SystemExit) as cm:
            main([])
        self.assertEqual(cm.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
