import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import redis

H_APP = Path(__file__).resolve().parents[1]
if str(H_APP) not in sys.path:
    sys.path.insert(0, str(H_APP))

from core.channels import send
from core.keys import prefix
from core.registry import port_type
from modules.webui.port import deliver_webui

POD = "testpod"


class WebuiPortTests(unittest.TestCase):
    def setUp(self):
        self.redis = redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"))
        try:
            self.redis.ping()
        except Exception:
            self.skipTest("real Redis server not available at REDIS_URL")
        self.tenant = f"webui-{uuid4().hex[:12]}"
        registry = prefix(POD, self.tenant, resource="registry")
        self.redis.hset(registry, "claude1", "claude_sdk")
        self.redis.hset(registry, "webui1", "webui")

    def queue(self, kind, payload, source="claude1", destination="webui1"):
        stream_id = send(
            self.redis,
            pod=POD,
            tenant=self.tenant,
            source=source,
            destination=destination,
            kind=kind,
            payload=payload,
        )
        raw = self.redis.lpop(prefix(POD, self.tenant, source, "egress"))
        self.redis.rpush(prefix(POD, self.tenant, destination, "ingress"), raw)
        return stream_id

    def test_registers_cleanly_as_a_generic_port_type(self):
        self.assertEqual(port_type(self.redis, pod=POD, tenant=self.tenant, agent="webui1"), "webui")

    def test_progress_envelope_is_relayed_into_the_inbox_stream(self):
        stream_id = self.queue("Progress", {"event": "claude_sdk_turn", "detail": "stop_reason=end_turn"})
        deliver_webui(self.redis, pod=POD, tenant=self.tenant, agent="webui1")

        inbox_key = prefix(POD, self.tenant, "webui1", "inbox")
        entries = self.redis.xrange(inbox_key, min="-", max="+")
        self.assertEqual(len(entries), 1)
        _entry_id, fields = entries[0]
        relayed = json.loads(fields[b"envelope"])
        self.assertEqual(relayed["kind"], "Progress")
        self.assertEqual(relayed["stream_id"], stream_id)
        self.assertEqual(relayed["payload"], {"event": "claude_sdk_turn", "detail": "stop_reason=end_turn"})

    def test_message_envelope_is_relayed_too(self):
        self.queue("Message", {"text": "final reply"})
        deliver_webui(self.redis, pod=POD, tenant=self.tenant, agent="webui1")

        inbox_key = prefix(POD, self.tenant, "webui1", "inbox")
        entries = self.redis.xrange(inbox_key, min="-", max="+")
        self.assertEqual(len(entries), 1)
        _entry_id, fields = entries[0]
        relayed = json.loads(fields[b"envelope"])
        self.assertEqual(relayed["kind"], "Message")
        self.assertEqual(relayed["payload"], {"text": "final reply"})

    def test_multiple_envelopes_are_relayed_in_order(self):
        self.queue("Progress", {"event": "claude_sdk_query_started", "detail": "subtype=init"})
        self.queue("Progress", {"event": "claude_sdk_query_finished", "detail": "subtype=success"})
        deliver_webui(self.redis, pod=POD, tenant=self.tenant, agent="webui1")

        inbox_key = prefix(POD, self.tenant, "webui1", "inbox")
        entries = self.redis.xrange(inbox_key, min="-", max="+")
        self.assertEqual(len(entries), 2)
        events = [json.loads(fields[b"envelope"])["payload"]["event"] for _id, fields in entries]
        self.assertEqual(events, ["claude_sdk_query_started", "claude_sdk_query_finished"])

    def test_unfamiliar_kind_is_dead_lettered_cleanly_not_crashed(self):
        """The ticket's own explicit test: a destination without an opener
        for a given kind must dead-letter cleanly, never crash the delivery
        subprocess. claude1 (port_type claude_sdk) has openers for Message/
        ListContexts only -- sending it a Progress envelope (this ticket's
        new kind) exercises core.channels' generic "unknown kind" handling,
        the same mechanism every port module already relies on, with nothing
        webui- or claude_sdk-specific built to make this particular pairing
        work."""
        from modules.claude_sdk.port import deliver_claude_sdk

        self.queue("Progress", {"event": "irrelevant", "detail": "irrelevant"}, source="webui1", destination="claude1")

        with patch("modules.claude_sdk.port._run_query") as mock_query:
            deliver_claude_sdk(self.redis, pod=POD, tenant=self.tenant, agent="claude1")

        mock_query.assert_not_called()
        dead = self.redis.lpop(prefix(POD, self.tenant, "claude1", "dead"))
        self.assertIsNotNone(dead)


if __name__ == "__main__":
    unittest.main()
