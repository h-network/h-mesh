import asyncio
import json
import sys
import unittest
from pathlib import Path


H_APP = Path(__file__).resolve().parents[1]
if str(H_APP) not in sys.path:
    sys.path.insert(0, str(H_APP))

from core.envelope import build, encode, parse
from core.keys import prefix
from modules.api.port import deliver_api
from modules.api.server import ApiSettings, create_app


class FakeRedis:
    def __init__(self):
        self.registry = {"api": "api", "alice": "tmux", "telegram": "api"}
        self.hashes = {}
        self.lists = {}
        self.streams = {}

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

    def hdel(self, key, field):
        return int(self.hashes.get(key, {}).pop(field, None) is not None)

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


if __name__ == "__main__":
    unittest.main()
