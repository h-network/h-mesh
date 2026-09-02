import asyncio
import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import redis

H_APP = Path(__file__).resolve().parents[1]
if str(H_APP) not in sys.path:
    sys.path.insert(0, str(H_APP))

from core.channels import send
from core.envelope import build, encode, parse
from core.keys import prefix
from modules.api import server as server_module
from modules.api.port import deliver_api
from modules.api.server import ApiSettings, create_app


class FakeRedis:
    def __init__(self):
        self.registry = {"api": "api", "alice": "tmux", "telegram": "api"}
        self.hashes = {}
        self.lists = {}
        self.streams = {}
        self.zsets = {}

    def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = value

    def zadd(self, key, mapping):
        self.zsets.setdefault(key, {}).update(mapping)

    def zcard(self, key):
        return len(self.zsets.get(key, {}))

    def zrange(self, key, start, end):
        members = [m for m, _ in sorted(self.zsets.get(key, {}).items(), key=lambda kv: kv[1])]
        return members[start:] if end == -1 else members[start:end + 1]

    def zrem(self, key, *members):
        for member in members:
            self.zsets.get(key, {}).pop(member, None)

    def hkeys(self, key):
        if key.endswith(":registry"):
            return [name.encode() for name in self.registry]
        return list(self.hashes.get(key, {}))

    def hexists(self, key, field):
        return field in self.registry if key.endswith(":registry") else field in self.hashes.get(key, {})

    def hget(self, key, field):
        if key.endswith(":registry"):
            return self.registry.get(field)
        return self.hashes.get(key, {}).get(field)

    def hgetall(self, key):
        return self.hashes.get(key, {})

    def hdel(self, key, *fields):
        count = 0
        for field in fields:
            if self.hashes.get(key, {}).pop(field, None) is not None:
                count += 1
        return count

    def get(self, key):
        return None

    def llen(self, key):
        return len(self.lists.get(key, []))

    def lrange(self, key, start, end):
        values = self.lists.get(key, [])
        return values[start:] if end == -1 else values[start : end + 1]

    def lpop(self, key):
        values = self.lists.get(key, [])
        return values.pop(0) if values else None

    def rpush(self, key, *values):
        self.lists.setdefault(key, []).extend(values)
        return len(self.lists[key])

    def xadd(self, key, fields, maxlen=None, approximate=True):
        entries = self.streams.setdefault(key, [])
        entry_id = f"{len(entries) + 1}-0"
        entries.append((entry_id, fields))
        if maxlen and len(entries) > maxlen:
            del entries[:-maxlen]
        return entry_id

    def xrange(self, key, min="-", max="+", count=None):
        return self.streams.get(key, [])[:count]

    def eval(self, script, numkeys, key, *args):
        if "LRANGE" in script and "DEL" in script:
            return self.lists.pop(key, [])
        return 1

    def pipeline(self, transaction=False):
        return FakePipeline(self)


class FakePipeline:
    def __init__(self, redis):
        self.redis = redis
        self.calls = []

    def lrange(self, key, start, end):
        self.calls.append((key, start, end))
        return self

    def execute(self):
        return [self.redis.lrange(*call) for call in self.calls]


def request(app, method, path, *, token=None, body=None):
    sent = []
    received = False
    encoded = json.dumps(body).encode() if body is not None else b""
    headers = [(b"content-type", b"application/json")]
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode()))

    async def receive():
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": encoded, "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 1234),
        "server": ("127.0.0.1", 8080),
        "root_path": "",
    }
    asyncio.run(app(scope, receive, send))
    start = next(item for item in sent if item["type"] == "http.response.start")
    raw_body = b"".join(item.get("body", b"") for item in sent if item["type"] == "http.response.body")
    return start["status"], json.loads(raw_body)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.redis = FakeRedis()
        self.app = create_app(
            settings=ApiSettings(pod="test", tenant="office", api_token="secret"),
            redis_client=self.redis,
        )

    def test_auth_and_health(self):
        self.assertEqual(request(self.app, "GET", "/health")[0], 401)
        self.assertEqual(request(self.app, "GET", "/health", token="secret"), (200, {"status": "ok"}))

    def test_external_route_contract(self):
        routes = {(method, route.path) for route in self.app.routes for method in getattr(route, "methods", set())}
        expected = {
            ("GET", "/health"),
            ("GET", "/agents"),
            ("GET", "/agents/{agent}"),
            ("POST", "/agents/{agent}/envelopes"),
            ("GET", "/agents/{agent}/messages"),
            ("GET", "/agents/{agent}/messages/stream"),
            ("GET", "/agents/{agent}/activity"),
            ("GET", "/agents/{agent}/activity/stream"),
            ("GET", "/agents/{agent}/board"),
            ("GET", "/board"),
            ("GET", "/alerts"),
            ("GET", "/alerts/stream"),
            ("GET", "/restdoc"),
            ("GET", "/docs"),
            ("GET", "/redoc"),
            ("GET", "/openapi.json"),
        }
        self.assertTrue(expected.issubset(routes))

    def test_qualified_destination_routes_through_core_channel(self):
        status, body = request(
            self.app,
            "POST",
            "/agents/test:office:alice/envelopes",
            token="secret",
            body={"text": "hello"},
        )
        self.assertEqual(status, 202)
        self.assertIn("stream_id", body)
        queued = parse(self.redis.lists[prefix("test", "office", "api", "egress")][0])
        self.assertEqual(queued["l2"]["destination"], "alice")
        self.assertEqual(queued["l3"]["destination"], "test:office:alice")

    def test_nonlocal_and_malformed_destination_statuses(self):
        status, _ = request(
            self.app, "POST", "/agents/other:office:alice/envelopes",
            token="secret", body={"text": "hello"},
        )
        self.assertEqual(status, 422)
        status, _ = request(
            self.app, "POST", "/agents/test:office:alice:extra/envelopes",
            token="secret", body={"text": "hello"},
        )
        self.assertEqual(status, 404)

    def test_api_port_writes_mailbox_and_dead_letters_corrupt_input(self):
        ingress = prefix("test", "office", "telegram", "ingress")
        valid = encode(build("Message", "alice", "telegram", {"text": "reply"}, pod="test", tenant="office"))
        self.redis.lists[ingress] = [valid, "not an envelope"]

        deliver_api(r=self.redis, pod="test", tenant="office", agent="telegram")

        inbox = prefix("test", "office", "telegram", "inbox")
        dead = prefix("test", "office", "telegram", "dead")
        self.assertEqual(len(self.redis.streams[inbox]), 1)
        self.assertEqual(json.loads(self.redis.streams[inbox][0][1]["envelope"])["payload"], {"text": "reply"})
        self.assertEqual(self.redis.lists[dead], ["not an envelope"])

    def _tamper_in_reply_to(self, envelope, value):
        """Bypass build()/encode()'s strict validation to simulate an
        already-parsed, permissive frame carrying whatever the wire said --
        the shape deliver_api actually has to defend against."""
        raw = encode(envelope)
        header, body = raw[:256], raw[256:]
        body_dict = json.loads(body)
        body_dict["in_reply_to"] = value
        return header + json.dumps(body_dict, separators=(",", ":"))

    def test_deliver_api_keeps_in_reply_to_when_delivered_by_the_claimed_client(self):
        from lib.reply_correlation import record_delivered

        target = "a" * 32
        # alice was really sent `target` by telegram, and is now replying
        # to telegram -- the claimed source matches deliver_api's own agent.
        record_delivered(self.redis, pod="test", tenant="office", agent="alice", stream_id=target, source="telegram")
        ingress = prefix("test", "office", "telegram", "ingress")
        envelope = build(
            "Message", "alice", "telegram", {"text": "reply"},
            pod="test", tenant="office", in_reply_to=target,
        )
        self.redis.lists[ingress] = [encode(envelope)]

        deliver_api(r=self.redis, pod="test", tenant="office", agent="telegram")

        inbox = prefix("test", "office", "telegram", "inbox")
        stored = json.loads(self.redis.streams[inbox][0][1]["envelope"])
        self.assertEqual(stored["in_reply_to"], target)

    def test_deliver_api_drops_in_reply_to_that_was_never_delivered(self):
        # Well-formed, but this agent never received it -- the confident-lie
        # case, not the format-error case. Must be dropped just like a
        # malformed id, and must not be surfaced to the client at all.
        target = "b" * 32
        ingress = prefix("test", "office", "telegram", "ingress")
        envelope = build(
            "Message", "alice", "telegram", {"text": "reply"},
            pod="test", tenant="office", in_reply_to=target,
        )
        self.redis.lists[ingress] = [encode(envelope)]

        deliver_api(r=self.redis, pod="test", tenant="office", agent="telegram")

        inbox = prefix("test", "office", "telegram", "inbox")
        stored = json.loads(self.redis.streams[inbox][0][1]["envelope"])
        self.assertNotIn("in_reply_to", stored)

    def test_deliver_api_drops_in_reply_to_delivered_by_a_different_client(self):
        # The cross-client case: telegram really sent `target` to alice.
        # alice now replies to webconsole (a different API client) naming
        # it. was_delivered must check WHO delivered it, not just whether
        # it was delivered to alice from anywhere -- otherwise webconsole
        # would receive a confident correlation to a turn it never
        # originated.
        from lib.reply_correlation import record_delivered

        target = "c" * 32
        record_delivered(self.redis, pod="test", tenant="office", agent="alice", stream_id=target, source="telegram")
        ingress = prefix("test", "office", "webconsole", "ingress")
        envelope = build(
            "Message", "alice", "webconsole", {"text": "reply"},
            pod="test", tenant="office", in_reply_to=target,
        )
        self.redis.lists[ingress] = [encode(envelope)]

        deliver_api(r=self.redis, pod="test", tenant="office", agent="webconsole")

        inbox = prefix("test", "office", "webconsole", "inbox")
        stored = json.loads(self.redis.streams[inbox][0][1]["envelope"])
        self.assertNotIn("in_reply_to", stored)

    def test_deliver_api_drops_a_peer_tmux_originated_id_toward_any_api_client(self):
        # The second direction of the cross-client fix, distinct from the
        # test above: `target` was never sent by ANY api client -- alice
        # sent it to bob, tmux-to-tmux, entirely off the api door's radar.
        # bob must not be able to launder that peer message into a
        # validated correlation by naming it in a reply to telegram (or any
        # other api client). This is not "wrong client", it's "no client at
        # all originated it" -- a distinct case from the one above, and the
        # one this fix is most likely to have missed if the binding were
        # only checked against a specific known-wrong client rather than
        # against the true recorded source in general.
        from lib.reply_correlation import record_delivered

        target = "e" * 32
        record_delivered(self.redis, pod="test", tenant="office", agent="bob", stream_id=target, source="alice")
        ingress = prefix("test", "office", "telegram", "ingress")
        envelope = build(
            "Message", "bob", "telegram", {"text": "reply"},
            pod="test", tenant="office", in_reply_to=target,
        )
        self.redis.lists[ingress] = [encode(envelope)]

        deliver_api(r=self.redis, pod="test", tenant="office", agent="telegram")

        inbox = prefix("test", "office", "telegram", "inbox")
        stored = json.loads(self.redis.streams[inbox][0][1]["envelope"])
        self.assertNotIn("in_reply_to", stored)

    def test_real_tmux_delivery_then_deliver_api_rejects_peer_originated_correlation(self):
        # Same case as above, but through the actual delivery path rather
        # than calling record_delivered directly -- removes any doubt that
        # this only holds because the unit test constructed the provenance
        # record by hand rather than the way message_opener really would.
        from unittest.mock import MagicMock
        from modules.tmux.port import message_opener

        target = send(
            self.redis, pod="test", tenant="office", source="alice",
            destination="bob", payload={"text": "peer message"},
        )
        raw = self.redis.lpop(prefix("test", "office", "alice", "egress"))
        envelope = parse(raw)
        with patch("modules.tmux.port.list_windows", return_value={"bob"}), \
             patch("modules.tmux.port.submit_text"):
            message_opener(self.redis, "test", "office", "bob", envelope, "sess", socket=None)

        reply_ingress = prefix("test", "office", "telegram", "ingress")
        reply_envelope = build(
            "Message", "bob", "telegram", {"text": "claiming the peer message"},
            pod="test", tenant="office", in_reply_to=target,
        )
        self.redis.lists[reply_ingress] = [encode(reply_envelope)]

        deliver_api(r=self.redis, pod="test", tenant="office", agent="telegram")

        inbox = prefix("test", "office", "telegram", "inbox")
        stored = json.loads(self.redis.streams[inbox][0][1]["envelope"])
        self.assertNotIn("in_reply_to", stored)

    def test_deliver_api_drops_malformed_in_reply_to(self):
        ingress = prefix("test", "office", "telegram", "ingress")
        envelope = build("Message", "alice", "telegram", {"text": "reply"}, pod="test", tenant="office")
        self.redis.lists[ingress] = [self._tamper_in_reply_to(envelope, "not-a-valid-id")]

        deliver_api(r=self.redis, pod="test", tenant="office", agent="telegram")

        inbox = prefix("test", "office", "telegram", "inbox")
        stored = json.loads(self.redis.streams[inbox][0][1]["envelope"])
        self.assertNotIn("in_reply_to", stored)

    def test_deliver_api_drops_present_null_in_reply_to(self):
        # A key PRESENT with value null is not the same as absent -- must
        # be caught and dropped, not passed through as a stored null.
        ingress = prefix("test", "office", "telegram", "ingress")
        envelope = build("Message", "alice", "telegram", {"text": "reply"}, pod="test", tenant="office")
        self.redis.lists[ingress] = [self._tamper_in_reply_to(envelope, None)]

        deliver_api(r=self.redis, pod="test", tenant="office", agent="telegram")

        inbox = prefix("test", "office", "telegram", "inbox")
        stored = json.loads(self.redis.streams[inbox][0][1]["envelope"])
        self.assertNotIn("in_reply_to", stored)

    def test_deliver_api_drops_present_empty_string_in_reply_to(self):
        ingress = prefix("test", "office", "telegram", "ingress")
        envelope = build("Message", "alice", "telegram", {"text": "reply"}, pod="test", tenant="office")
        self.redis.lists[ingress] = [self._tamper_in_reply_to(envelope, "")]

        deliver_api(r=self.redis, pod="test", tenant="office", agent="telegram")

        inbox = prefix("test", "office", "telegram", "inbox")
        stored = json.loads(self.redis.streams[inbox][0][1]["envelope"])
        self.assertNotIn("in_reply_to", stored)

    def test_deliver_api_leaves_absent_in_reply_to_untouched(self):
        ingress = prefix("test", "office", "telegram", "ingress")
        envelope = build("Message", "alice", "telegram", {"text": "reply"}, pod="test", tenant="office")
        self.redis.lists[ingress] = [encode(envelope)]

        deliver_api(r=self.redis, pod="test", tenant="office", agent="telegram")

        inbox = prefix("test", "office", "telegram", "inbox")
        stored = json.loads(self.redis.streams[inbox][0][1]["envelope"])
        self.assertNotIn("in_reply_to", stored)

    def test_deliver_api_drops_and_logs_distinct_reason_when_provenance_unavailable(self):
        # A storage outage must not be recorded as "was never delivered" --
        # that would be a false claim about what actually happened.
        target = "d" * 32
        ingress = prefix("test", "office", "telegram", "ingress")
        envelope = build(
            "Message", "alice", "telegram", {"text": "reply"},
            pod="test", tenant="office", in_reply_to=target,
        )
        self.redis.lists[ingress] = [encode(envelope)]

        def broken_hget(key, field):
            raise ConnectionError("redis unavailable")

        captured = io.StringIO()
        with patch.object(self.redis, "hget", side_effect=broken_hget), \
             redirect_stdout(captured):
            deliver_api(r=self.redis, pod="test", tenant="office", agent="telegram")

        inbox = prefix("test", "office", "telegram", "inbox")
        stored = json.loads(self.redis.streams[inbox][0][1]["envelope"])
        self.assertNotIn("in_reply_to", stored)
        dropped_lines = [
            line for line in captured.getvalue().splitlines()
            if json.loads(line).get("event") == "reply_correlation_dropped"
        ]
        self.assertEqual(len(dropped_lines), 1)
        self.assertIn("provenance unavailable", dropped_lines[0])
        self.assertNotIn("was never delivered", dropped_lines[0])

    def test_idle_sse_stream_emits_keepalive_without_new_entries(self):
        """No existing test opened an SSE stream and left it idle -- every
        prior test either never connects to /alerts/stream or /agents/{a}/
        activity/stream at all (test_external_route_contract just checks the
        route is registered), or the client-side bot.py tests feed entries
        into a mocked stream_fn immediately. So the silent-idle path -- an
        open connection with nothing ever written to the underlying Redis
        stream -- was never exercised by anything. This drives the real
        ASGI app with a receive() that never signals disconnect, shrinks the
        keepalive interval so the test doesn't wait multiple real seconds,
        and asserts a comment line actually reaches the wire while idle.
        """
        first = True

        async def receive():
            nonlocal first
            if first:
                first = False
                return {"type": "http.request", "body": b"", "more_body": False}
            await asyncio.sleep(3600)
            return {"type": "http.disconnect"}

        body_chunks = []

        async def send(message):
            if message["type"] == "http.response.body":
                body_chunks.append(message.get("body", b""))

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/alerts/stream",
            "raw_path": b"/alerts/stream",
            "query_string": b"",
            "headers": [(b"authorization", b"Bearer secret")],
            "client": ("127.0.0.1", 1234),
            "server": ("127.0.0.1", 8080),
            "root_path": "",
        }

        async def run():
            with patch.object(server_module, "SSE_KEEPALIVE_INTERVAL_S", 0.05):
                task = asyncio.ensure_future(self.app(scope, receive, send))
                await asyncio.sleep(0.3)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        asyncio.run(run())

        combined = b"".join(body_chunks)
        self.assertIn(b": keepalive\n\n", combined)


class RealApiPortSubprocessTests(unittest.TestCase):
    """Runs `python -m modules.api.port` as a real subprocess, the way the
    switch actually invokes it. A bare unittest of deliver_api() -- or a mock
    asserting Popen was called with the right argv -- would both stay green
    even with no main()/__main__ guard at all, since neither ever imports the
    module as __main__ and lets it exit. This is the class of test that
    catches that: it fails loudly (nonzero exit, or ingress left undrained)
    if the module has nothing runnable behind `python -m`.
    """

    def setUp(self):
        self.redis_url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
        self.r = redis.Redis.from_url(self.redis_url)
        self.r.ping()
        self.pod = "real-api-test"
        self.tenant = f"tenant-{os.urandom(4).hex()}"
        self.registry = prefix(self.pod, self.tenant, resource="registry")
        self.r.hset(self.registry, mapping={"harry": "tmux", "ivy": "api"})

    def tearDown(self):
        keys = self.r.keys(f"{self.pod}.{self.tenant}.*")
        if keys:
            self.r.delete(*keys)

    def test_real_api_module_subprocess_invocation(self):
        send(
            self.r,
            pod=self.pod,
            tenant=self.tenant,
            source="harry",
            destination="ivy",
            payload={"text": "invoked via python -m modules.api.port"},
        )
        raw = self.r.lpop(prefix(self.pod, self.tenant, "harry", "egress"))
        self.r.rpush(prefix(self.pod, self.tenant, "ivy", "ingress"), raw)

        env = dict(os.environ)
        env["PYTHONPATH"] = str(H_APP)
        env["POD"] = self.pod
        env["TENANT"] = self.tenant
        env["REDIS_URL"] = self.redis_url

        res = subprocess.run(
            [sys.executable, "-m", "modules.api.port", "ivy"],
            cwd=str(H_APP),
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"port main failed: {res.stderr}")

        self.assertIsNone(self.r.lpop(prefix(self.pod, self.tenant, "ivy", "ingress")))

        inbox_key = prefix(self.pod, self.tenant, "ivy", "inbox")
        entries = self.r.xrange(inbox_key, min="-", max="+")
        self.assertEqual(len(entries), 1)
        delivered = json.loads(entries[0][1][b"envelope"])
        self.assertEqual(delivered["payload"], {"text": "invoked via python -m modules.api.port"})

        delivering_key = prefix(self.pod, self.tenant, agent="ivy", resource="delivering")
        self.assertIsNone(self.r.get(delivering_key))


if __name__ == "__main__":
    unittest.main()
