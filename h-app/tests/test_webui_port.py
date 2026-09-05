import json
import sys
import unittest
from pathlib import Path

H_APP = Path(__file__).resolve().parents[1]
if str(H_APP) not in sys.path:
    sys.path.insert(0, str(H_APP))
TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

from core.channels import send
from core.keys import prefix
from core.registry import port_type
from modules.webui.port import WebSocketRelay, deliver_webui
from test_tmux_port import FakeRedis

POD = "testpod"
TENANT = "testtenant"


class FakeWebSocket:
    def __init__(self):
        self.frames = []

    def send(self, frame):
        self.frames.append(frame)


class WebuiPortTests(unittest.TestCase):
    def setUp(self):
        self.redis = FakeRedis()
        registry = prefix(POD, TENANT, resource="registry")
        self.redis.hset(registry, "sdk", "claude_sdk")
        self.redis.hset(registry, "browser", "webui")
        self.relay = WebSocketRelay()

    def queue(self, kind, payload):
        stream_id = send(
            self.redis,
            pod=POD,
            tenant=TENANT,
            source="sdk",
            destination="browser",
            kind=kind,
            payload=payload,
        )
        raw = self.redis.lpop(prefix(POD, TENANT, "sdk", "egress"))
        self.redis.rpush(prefix(POD, TENANT, "browser", "ingress"), raw)
        return stream_id

    def test_progress_envelope_is_relayed_to_connected_client(self):
        stream_id = self.queue(
            "Progress",
            {
                "event": "claude_sdk_turn",
                "reason": "stop_reason=tool_use tools=Read",
            },
        )
        client = FakeWebSocket()

        with self.relay.connected(client):
            deliver_webui(
                self.redis, pod=POD, tenant=TENANT, agent="browser", relay=self.relay
            )

        self.assertEqual(len(client.frames), 1)
        envelope = json.loads(client.frames[0])
        self.assertEqual(envelope["kind"], "Progress")
        self.assertEqual(envelope["stream_id"], stream_id)
        self.assertEqual(
            envelope["payload"],
            {
                "event": "claude_sdk_turn",
                "reason": "stop_reason=tool_use tools=Read",
            },
        )

    def test_unknown_kind_is_dead_lettered_without_raising(self):
        self.queue("Command", {"text": "not supported"})

        deliver_webui(
            self.redis, pod=POD, tenant=TENANT, agent="browser", relay=self.relay
        )

        dead = self.redis.lpop(prefix(POD, TENANT, "browser", "dead"))
        self.assertIsNotNone(dead)

    def test_registers_cleanly_as_a_generic_port_type(self):
        self.assertEqual(
            port_type(self.redis, pod=POD, tenant=TENANT, agent="browser"), "webui"
        )


if __name__ == "__main__":
    unittest.main()
