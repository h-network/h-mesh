import json
import sys
import unittest
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


H_APP = Path(__file__).resolve().parents[1]
if str(H_APP) not in sys.path:
    sys.path.insert(0, str(H_APP))

from core.channels import DeadLetter, receive, send
from core.envelope import parse
from core.keys import prefix
from core.service import Switch


POD = "mesh"
TENANT = "office"


class FakeRedis:
    """The Redis list/hash and Lua semantics exercised by core channels."""

    def __init__(self):
        self.lists = defaultdict(deque)
        self.hashes = defaultdict(dict)

    def rpush(self, key, *values):
        self.lists[key].extend(values)
        return len(self.lists[key])

    def lpop(self, key):
        return self.lists[key].popleft() if self.lists[key] else None

    def blpop(self, keys, timeout=0):
        if isinstance(keys, str):
            keys = [keys]
        for key in keys:
            if self.lists[key]:
                return key, self.lists[key].popleft()
        return None

    def hset(self, key, field, value):
        self.hashes[key][field] = value
        return 1

    def hget(self, key, field):
        return self.hashes[key].get(field)

    def hdel(self, key, field):
        return int(self.hashes[key].pop(field, None) is not None)

    def hkeys(self, key):
        return list(self.hashes[key])

    def hexists(self, key, field):
        return field in self.hashes[key]

    def eval(self, script, key_count, *args):
        keys = args[:key_count]
        argv = args[key_count:]
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
        if "core ack streak" in script:
            key, destination, now_ts, cutoff_ts = keys[0], argv[0], argv[1], argv[2]
            existing = self.hget(key, destination)
            data = json.loads(existing) if existing else None
            within_window = (
                isinstance(data, dict)
                and isinstance(data.get("streak"), (int, float))
                and isinstance(data.get("last_ts"), str)
                and cutoff_ts <= data["last_ts"] <= now_ts
            )
            value = {"streak": data["streak"] + 1 if within_window else 1, "last_ts": now_ts}
            self.hset(key, destination, json.dumps(value, separators=(",", ":")))
            return value["streak"]
        if "core ingress admission" in script:
            limit, raw = int(argv[0]), argv[1]
            for index, key in enumerate(keys, start=1):
                depth = len(self.lists[key])
                if depth >= limit:
                    return [0, index, depth]
            return [1, *(self.rpush(key, raw) for key in keys)]
        raise AssertionError("unexpected Lua script")


class ChannelTests(unittest.TestCase):
    def setUp(self):
        self.redis = FakeRedis()
        self.registry = prefix(POD, TENANT, resource="registry")
        self.observations = patch("core.channels._emit_observation")
        self.recipient_observations = patch("core.channels._emit_for_recipient")
        self.emit_observation = self.observations.start()
        self.emit_for_recipient = self.recipient_observations.start()
        self.addCleanup(self.observations.stop)
        self.addCleanup(self.recipient_observations.stop)

    def register(self, **agents):
        for agent, port_type in agents.items():
            self.redis.hset(self.registry, agent, port_type)

    def test_send_and_receive_round_trip(self):
        self.register(alice="tmux", bob="tmux")
        payload = {"text": "hello", "sequence": 7}
        stream_id = send(
            self.redis, pod=POD, tenant=TENANT, source="alice",
            destination="bob", payload=payload,
        )
        egress = prefix(POD, TENANT, "alice", "egress")
        raw = self.redis.lpop(egress)
        self.assertEqual(parse(raw)["stream_id"], stream_id)
        self.redis.rpush(prefix(POD, TENANT, "bob", "ingress"), raw)
        opened = []
        receive(
            self.redis, pod=POD, tenant=TENANT, agent="bob",
            openers={"Message": opened.append}, timeout=0, blocking=False,
        )
        self.assertEqual(opened[0]["payload"], payload)

    def test_unreplied_count_opens_increments_and_reply_clears(self):
        self.register(client="api", worker="tmux")
        send(
            self.redis, pod=POD, tenant=TENANT, source="client",
            destination="worker", payload={"text": "first"},
        )
        key = prefix(POD, TENANT, "worker", "unreplied")
        first_since = json.loads(self.redis.hget(key, "client"))["since"]
        send(
            self.redis, pod=POD, tenant=TENANT, source="client",
            destination="worker", payload={"text": "second"},
        )
        tracked = json.loads(self.redis.hget(key, "client"))
        self.assertEqual(tracked["count"], 2)
        self.assertEqual(tracked["since"], first_since)
        send(
            self.redis, pod=POD, tenant=TENANT, source="worker",
            destination="client", payload={"text": "reply"},
        )
        self.assertIsNone(self.redis.hget(key, "client"))

    def test_ack_streak_increments_resets_after_window_and_clears_on_non_ack(self):
        self.register(alice="tmux", bob="tmux")
        key = prefix(POD, TENANT, "alice", "acks")
        for _ in range(2):
            send(
                self.redis, pod=POD, tenant=TENANT, source="alice",
                destination="bob", payload={"text": "Thanks!"},
            )
        self.assertEqual(json.loads(self.redis.hget(key, "bob"))["streak"], 2)

        expired = datetime.now(timezone.utc) - timedelta(seconds=121)
        self.redis.hset(
            key, "bob",
            json.dumps({"streak": 9, "last_ts": expired.isoformat(timespec="milliseconds").replace("+00:00", "Z")}),
        )
        send(
            self.redis, pod=POD, tenant=TENANT, source="alice",
            destination="bob", payload={"text": "noted"},
        )
        self.assertEqual(json.loads(self.redis.hget(key, "bob"))["streak"], 1)
        send(
            self.redis, pod=POD, tenant=TENANT, source="alice",
            destination="bob", payload={"text": "Here is substantive work"},
        )
        self.assertIsNone(self.redis.hget(key, "bob"))

    def test_receive_dead_letters_opener_rejection(self):
        envelope_id = send(
            self.redis, pod=POD, tenant=TENANT, source="alice",
            destination="bob", payload={"text": "reject me"},
        )
        raw = self.redis.lpop(prefix(POD, TENANT, "alice", "egress"))
        self.redis.rpush(prefix(POD, TENANT, "bob", "ingress"), raw)

        def reject(envelope):
            raise DeadLetter("recipient rejected payload")

        receive(
            self.redis, pod=POD, tenant=TENANT, agent="bob",
            openers={"Message": reject}, timeout=0, blocking=False,
        )
        dead = self.redis.lpop(prefix(POD, TENANT, "bob", "dead"))
        self.assertEqual(parse(dead)["stream_id"], envelope_id)
        self.assertIsNone(self.redis.lpop(prefix(POD, TENANT, "bob", "ingress")))
        self.assertEqual(self.emit_for_recipient.call_args.args[1], "dead_lettered")
        self.assertEqual(self.emit_for_recipient.call_args.args[3], "bob")
        self.assertEqual(self.emit_for_recipient.call_args.args[4], "recipient rejected payload")

    def test_send_switch_receive_integration(self):
        self.register(alice="tmux", bob="tmux")
        payload = {"text": "through the switch", "nested": {"ok": True}}
        stream_id = send(
            self.redis, pod=POD, tenant=TENANT, source="alice",
            destination="bob", payload=payload,
        )
        kicks = []
        switch = Switch(
            self.redis, pod=POD, tenant=TENANT,
            kick=lambda agent, envelope: kicks.append((agent, envelope["stream_id"])),
        )
        with patch("core.service._emit_observation"), patch("core.service._log_observation"):
            self.assertTrue(switch.step(timeout=0))

        opened = []
        receive(
            self.redis, pod=POD, tenant=TENANT, agent="bob",
            openers={"Message": opened.append}, timeout=0, blocking=False,
        )
        self.assertEqual(kicks, [("bob", stream_id)])
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0]["stream_id"], stream_id)
        self.assertEqual(opened[0]["l2"], {"source": "alice", "destination": "bob"})
        self.assertEqual(opened[0]["payload"], payload)
        self.assertEqual(opened[0]["ttl"], 15)
        self.assertEqual(opened[0]["hops"], 1)


if __name__ == "__main__":
    unittest.main()
