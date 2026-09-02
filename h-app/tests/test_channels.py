import json
import sys
import unittest
from collections import defaultdict, deque
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
        self.hget_calls = []
        self.hgetall_calls = []
        self.hexists_calls = []

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
        self.hget_calls.append((key, field))
        return self.hashes[key].get(field)

    def hgetall(self, key):
        self.hgetall_calls.append(key)
        return dict(self.hashes[key])

    def hdel(self, key, field):
        return int(self.hashes[key].pop(field, None) is not None)

    def hkeys(self, key):
        return list(self.hashes[key])

    def hexists(self, key, field):
        self.hexists_calls.append((key, field))
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

    def test_receive_notifies_tmux_sender_when_kind_is_rejected(self):
        self.register(alice="tmux", host="control")
        envelope_id = send(
            self.redis, pod=POD, tenant=TENANT, source="alice",
            destination="host", payload={"text": "not a lifecycle command"},
        )
        raw = self.redis.lpop(prefix(POD, TENANT, "alice", "egress"))
        self.redis.rpush(prefix(POD, TENANT, "host", "ingress"), raw)

        receive(
            self.redis, pod=POD, tenant=TENANT, agent="host",
            openers={"StartAgent": lambda envelope: None}, timeout=0, blocking=False,
        )

        feedback = parse(self.redis.lpop(prefix(POD, TENANT, "host", "egress")))
        self.assertEqual(feedback["l2"], {"source": "host", "destination": "alice"})
        self.assertEqual(feedback["correlation_id"], envelope_id)
        self.assertEqual(
            feedback["payload"]["text"],
            f"Delivery to host failed for message {envelope_id}: unknown kind: Message",
        )

    def test_one_receive_drains_valid_request_behind_stale_rejected_entry(self):
        self.register(alice="tmux", host="office")
        stale_id = send(
            self.redis, pod=POD, tenant=TENANT, source="alice",
            destination="host", payload={"text": "stale broadcast"},
        )
        request_id = send(
            self.redis, pod=POD, tenant=TENANT, source="alice",
            destination="host", kind="StartAgent", payload={"agent": "worker"},
        )
        ingress = prefix(POD, TENANT, "host", "ingress")
        for _ in range(2):
            self.redis.rpush(
                ingress,
                self.redis.lpop(prefix(POD, TENANT, "alice", "egress")),
            )
        opened = []

        receive(
            self.redis, pod=POD, tenant=TENANT, agent="host",
            openers={"StartAgent": opened.append}, timeout=0, blocking=False,
        )

        self.assertEqual([envelope["stream_id"] for envelope in opened], [request_id])
        self.assertIsNone(self.redis.lpop(ingress))
        dead = parse(self.redis.lpop(prefix(POD, TENANT, "host", "dead")))
        self.assertEqual(dead["stream_id"], stale_id)

    def test_receive_does_not_notify_non_tmux_sender(self):
        self.register(client="api", host="control")
        send(
            self.redis, pod=POD, tenant=TENANT, source="client",
            destination="host", payload={"text": "wrong kind"},
        )
        raw = self.redis.lpop(prefix(POD, TENANT, "client", "egress"))
        self.redis.rpush(prefix(POD, TENANT, "host", "ingress"), raw)

        receive(
            self.redis, pod=POD, tenant=TENANT, agent="host",
            openers={}, timeout=0, blocking=False,
        )

        self.assertIsNone(self.redis.lpop(prefix(POD, TENANT, "host", "egress")))

    def test_send_switch_receive_integration(self):
        self.register(alice="tmux", bob="tmux")
        payload = {"text": "through the switch", "nested": {"ok": True}}
        stream_id = send(
            self.redis, pod=POD, tenant=TENANT, source="alice",
            destination="bob", payload=payload,
        )
        kicks = []
        hgets_before_step = len(self.redis.hget_calls)
        switch = Switch(
            self.redis, pod=POD, tenant=TENANT,
            kick=lambda agent, port_type, envelope: kicks.append(
                (agent, port_type, envelope["stream_id"])
            ),
        )
        with patch("core.service._emit_observation"), patch("core.service._log_observation"):
            self.assertTrue(switch.step(timeout=0))

        opened = []
        receive(
            self.redis, pod=POD, tenant=TENANT, agent="bob",
            openers={"Message": opened.append}, timeout=0, blocking=False,
        )
        self.assertEqual(kicks, [("bob", "tmux", stream_id)])
        self.assertEqual(
            self.redis.hget_calls[hgets_before_step:],
            [(self.registry, "bob")],
        )
        self.assertEqual(self.redis.hexists_calls, [])
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0]["stream_id"], stream_id)
        self.assertEqual(opened[0]["l2"], {"source": "alice", "destination": "bob"})
        self.assertEqual(opened[0]["payload"], payload)
        self.assertEqual(opened[0]["ttl"], 15)
        self.assertEqual(opened[0]["hops"], 1)

    def test_broadcast_resolves_each_member_type_and_kicks_all(self):
        self.register(alice="tmux", bob="tmux", carol="api")
        stream_id = send(
            self.redis, pod=POD, tenant=TENANT, source="alice",
            destination="all", payload={"text": "broadcast"},
        )
        hgets_before_step = len(self.redis.hget_calls)
        kicks = []
        switch = Switch(
            self.redis, pod=POD, tenant=TENANT,
            kick=lambda agent, port_type, envelope: kicks.append(
                (agent, port_type, envelope["stream_id"])
            ),
        )
        with patch("core.service._emit_observation"), patch("core.service._log_observation") as log:
            self.assertTrue(switch.step(timeout=0))
        self.assertEqual(kicks, [
            ("bob", "tmux", stream_id),
            ("carol", "api", stream_id),
        ])
        skipped = [call for call in log.call_args_list if call.args == ("kick_skipped",)]
        self.assertEqual(skipped, [])
        self.assertEqual(self.redis.hget_calls[hgets_before_step:], [])
        self.assertEqual(self.redis.hgetall_calls, [self.registry])
        self.assertEqual(self.redis.hexists_calls, [])

    def test_broadcast_kicks_resolved_members_and_records_unresolved_member(self):
        self.register(alice="tmux", bob="tmux", unresolved="")
        stream_id = send(
            self.redis, pod=POD, tenant=TENANT, source="alice",
            destination="all", payload={"text": "broadcast"},
        )
        kicks = []
        switch = Switch(
            self.redis, pod=POD, tenant=TENANT,
            kick=lambda agent, port_type, envelope: kicks.append(
                (agent, port_type, envelope["stream_id"])
            ),
        )

        with patch("core.service._emit_observation"), patch("core.service._log_observation") as log:
            self.assertTrue(switch.step(timeout=0))

        self.assertEqual(kicks[0], ("bob", "tmux", stream_id))
        self.assertEqual(kicks[1][0:2], ("alice", "tmux"))
        skipped = [call for call in log.call_args_list if call.args == ("kick_skipped",)]
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0].kwargs["destination"], "unresolved")
        self.assertIn("no delivery attempt started", skipped[0].kwargs["reason"])
        notice = parse(self.redis.lists[prefix(POD, TENANT, "alice", "ingress")][0])
        self.assertEqual(notice["l2"], {"source": "switch", "destination": "alice"})
        self.assertEqual(
            notice["payload"]["text"],
            "Broadcast notice: no delivery attempt was started for unresolved.",
        )
        self.assertEqual(notice["correlation_id"], stream_id)

    def test_unicast_missing_hget_value_dead_letters_without_kick(self):
        self.register(alice="tmux")
        stream_id = send(
            self.redis, pod=POD, tenant=TENANT, source="alice",
            destination="ghost", payload={"text": "nowhere"},
        )
        hgets_before_step = len(self.redis.hget_calls)
        kicks = []
        switch = Switch(
            self.redis, pod=POD, tenant=TENANT,
            kick=lambda agent, port_type, envelope: kicks.append(
                (agent, port_type, envelope["stream_id"])
            ),
        )
        with patch("core.service._emit_observation"), patch("core.service._log_observation"):
            self.assertTrue(switch.step(timeout=0))
        dead = self.redis.lpop(prefix(POD, TENANT, "alice", "dead"))
        self.assertEqual(parse(dead)["stream_id"], stream_id)
        self.assertEqual(kicks, [])
        self.assertEqual(
            self.redis.hget_calls[hgets_before_step:],
            [(self.registry, "ghost")],
        )
        self.assertEqual(self.redis.hexists_calls, [])


if __name__ == "__main__":
    unittest.main()
