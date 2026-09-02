import json
import os
import sys
import unittest
from collections import defaultdict, deque
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest
import redis


H_APP = Path(__file__).resolve().parents[1]
if str(H_APP) not in sys.path:
    sys.path.insert(0, str(H_APP))

from core.channels import DeadLetter, receive, send
from core.envelope import build, encode, parse
from core.keys import delivery_lock_key, prefix
from core.service import Switch


POD = "mesh"
TENANT = "office"


class FakeRedis:
    """The Redis list/hash and Lua semantics exercised by core channels."""

    def __init__(self):
        self.lists = defaultdict(deque)
        self.hashes = defaultdict(dict)
        self.values = {}
        self.hget_calls = []
        self.hgetall_calls = []
        self.hexists_calls = []

    def rpush(self, key, *values):
        self.lists[key].extend(values)
        return len(self.lists[key])

    def lpop(self, key):
        return self.lists[key].popleft() if self.lists[key] else None

    def lmove(self, source, destination, src="LEFT", dest="RIGHT"):
        if not self.lists[source]:
            return None
        value = self.lists[source].popleft()
        self.lists[destination].append(value)
        return value

    def blmove(self, source, destination, timeout, src="LEFT", dest="RIGHT"):
        return self.lmove(source, destination, src=src, dest=dest)

    def lrem(self, key, count, value):
        try:
            self.lists[key].remove(value)
        except ValueError:
            return 0
        return 1

    def llen(self, key):
        return len(self.lists[key])

    def lindex(self, key, index):
        try:
            return self.lists[key][index]
        except IndexError:
            return None

    def get(self, key):
        return self.values.get(key)

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
        if "core receive processing-to-dead" in script:
            processing, dead = keys
            raw = argv[0]
            if self.lrem(processing, 1, raw) != 1:
                return 0
            self.rpush(dead, raw)
            return 1
        if "core receive custody transfer" in script:
            source, destination = keys
            old, new = argv[0], argv[1]
            if self.lrem(source, 1, old) != 1:
                return 0
            self.rpush(destination, new)
            cap = int(argv[2])
            while cap > 0 and len(self.lists[destination]) > cap:
                self.lists[destination].popleft()
            return 1
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

    def test_send_threads_in_reply_to_onto_the_wire(self):
        self.register(alice="tmux", bob="tmux")
        target = "c" * 32
        send(
            self.redis, pod=POD, tenant=TENANT, source="alice",
            destination="bob", payload={"text": "reply"}, in_reply_to=target,
        )
        raw = self.redis.lpop(prefix(POD, TENANT, "alice", "egress"))
        self.assertEqual(parse(raw)["in_reply_to"], target)

    def test_send_without_in_reply_to_leaves_it_absent(self):
        self.register(alice="tmux", bob="tmux")
        send(
            self.redis, pod=POD, tenant=TENANT, source="alice",
            destination="bob", payload={"text": "hello"},
        )
        raw = self.redis.lpop(prefix(POD, TENANT, "alice", "egress"))
        self.assertNotIn("in_reply_to", parse(raw))

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

    def test_receive_process_death_never_removes_identifiable_custody(self):
        stream_id = send(
            self.redis, pod=POD, tenant=TENANT, source="alice",
            destination="bob", payload={"text": "must survive process death"},
        )
        raw = self.redis.lpop(prefix(POD, TENANT, "alice", "egress"))
        ingress = prefix(POD, TENANT, "bob", "ingress")
        processing = prefix(POD, TENANT, "bob", "processing")
        opening = prefix(POD, TENANT, "bob", "opening")
        dead = prefix(POD, TENANT, "bob", "dead")
        self.redis.rpush(ingress, raw)

        def terminate_after_claim(_envelope):
            raise SystemExit("simulated process death")

        with self.assertRaises(SystemExit):
            receive(
                self.redis, pod=POD, tenant=TENANT, agent="bob",
                openers={"Message": terminate_after_claim}, timeout=0, blocking=False,
            )

        surviving_ids = [
            parse(candidate)["stream_id"]
            for key in (ingress, processing, opening, dead)
            for candidate in self.redis.lists[key]
        ]
        self.assertEqual(
            surviving_ids.count(stream_id), 1,
            "the admitted envelope identity must remain in exactly one durable custody location",
        )

    def test_receive_dead_letter_failure_never_removes_identifiable_custody(self):
        stream_id = send(
            self.redis, pod=POD, tenant=TENANT, source="alice",
            destination="bob", payload={"text": "must survive dead write failure"},
        )
        raw = self.redis.lpop(prefix(POD, TENANT, "alice", "egress"))
        ingress = prefix(POD, TENANT, "bob", "ingress")
        processing = prefix(POD, TENANT, "bob", "processing")
        opening = prefix(POD, TENANT, "bob", "opening")
        dead = prefix(POD, TENANT, "bob", "dead")
        self.redis.rpush(ingress, raw)
        original_eval = self.redis.eval

        def fail_dead_write(script, key_count, *args):
            if "core receive custody transfer" in script and args[1] == dead:
                raise RuntimeError("injected dead-letter write failure")
            return original_eval(script, key_count, *args)

        self.redis.eval = fail_dead_write
        with self.assertRaises(RuntimeError):
            receive(
                self.redis, pod=POD, tenant=TENANT, agent="bob",
                openers={"Message": lambda envelope: (_ for _ in ()).throw(DeadLetter("reject"))},
                timeout=0, blocking=False,
            )

        surviving_ids = [
            parse(candidate)["stream_id"]
            for key in (ingress, processing, opening, dead)
            for candidate in self.redis.lists[key]
        ]
        self.assertEqual(
            surviving_ids.count(stream_id), 1,
            "a failed dead-letter handoff must not erase the rejected envelope identity",
        )

    def test_receive_ack_failure_retains_opened_identity_for_recovery(self):
        stream_id = send(
            self.redis, pod=POD, tenant=TENANT, source="alice",
            destination="bob", payload={"text": "opened but not acknowledged"},
        )
        raw = self.redis.lpop(prefix(POD, TENANT, "alice", "egress"))
        ingress = prefix(POD, TENANT, "bob", "ingress")
        processing = prefix(POD, TENANT, "bob", "processing")
        self.redis.rpush(ingress, raw)
        opened = []
        opening = prefix(POD, TENANT, "bob", "opening")
        opened_key = prefix(POD, TENANT, "bob", "opened")
        original_eval = self.redis.eval
        def fail_ack(script, key_count, *args):
            if "core receive custody transfer" in script and args[0] == opening and args[1] == opened_key:
                raise RuntimeError("injected acknowledgement failure")
            return original_eval(script, key_count, *args)
        self.redis.eval = fail_ack

        with self.assertRaises(RuntimeError):
            receive(
                self.redis, pod=POD, tenant=TENANT, agent="bob",
                openers={"Message": opened.append}, timeout=0, blocking=False,
            )

        self.assertEqual([item["stream_id"] for item in opened], [stream_id])
        self.redis.eval = original_eval
        receive(self.redis, pod=POD, tenant=TENANT, agent="bob", openers={"Message": opened.append}, timeout=0, blocking=False)
        unresolved = prefix(POD, TENANT, resource="unresolved")
        records = [json.loads(value) for value in self.redis.lists[unresolved]]
        self.assertEqual([parse(record["envelope"])["stream_id"] for record in records], [stream_id])
        self.assertEqual([item["stream_id"] for item in opened], [stream_id], "unknown outcome must not be reopened")

    def test_receive_successor_recovers_claimed_identity_before_new_ingress(self):
        old_id = send(
            self.redis, pod=POD, tenant=TENANT, source="alice",
            destination="bob", payload={"text": "claimed by dead predecessor"},
        )
        new_id = send(
            self.redis, pod=POD, tenant=TENANT, source="alice",
            destination="bob", payload={"text": "new ingress"},
        )
        egress = prefix(POD, TENANT, "alice", "egress")
        processing = prefix(POD, TENANT, "bob", "processing")
        ingress = prefix(POD, TENANT, "bob", "ingress")
        self.redis.rpush(processing, self.redis.lpop(egress))
        self.redis.rpush(ingress, self.redis.lpop(egress))
        opened = []

        receive(
            self.redis, pod=POD, tenant=TENANT, agent="bob",
            openers={"Message": opened.append}, timeout=0, blocking=False,
        )

        self.assertEqual([item["stream_id"] for item in opened], [old_id, new_id])
        self.assertEqual(list(self.redis.lists[processing]), [])
        self.assertEqual(list(self.redis.lists[ingress]), [])

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

    def test_backlog_reconciliation_rekicks_queue_after_delivery_lease_is_gone(self):
        self.register(host="office")
        stream_id = send(
            self.redis, pod=POD, tenant=TENANT, source="host",
            destination="host", kind="StartAgent", payload={"agent": "worker"},
        )
        raw = self.redis.lpop(prefix(POD, TENANT, "host", "egress"))
        self.redis.rpush(prefix(POD, TENANT, "host", "ingress"), raw)
        kicks = []
        switch = Switch(
            self.redis, pod=POD, tenant=TENANT,
            kick=lambda agent, port_type, envelope: kicks.append(
                (agent, port_type, envelope["stream_id"])
            ),
        )

        with patch("core.service._log_observation") as log:
            switch._reconcile_ingress()

        self.assertEqual(kicks, [("host", "office", stream_id)])
        self.assertEqual(log.call_args.args, ("kick_restarted",))
        self.assertIn("non-empty ingress", log.call_args.kwargs["reason"])

    def test_backlog_reconciliation_waits_for_live_delivery_lease(self):
        self.register(host="office")
        raw = encode(build(
            "StartAgent", "host", "host", {"agent": "worker"},
            pod=POD, tenant=TENANT,
        ))
        self.redis.rpush(prefix(POD, TENANT, "host", "ingress"), raw)
        self.redis.values[delivery_lock_key(POD, TENANT, "host")] = "live-holder"
        kicks = []

        Switch(
            self.redis, pod=POD, tenant=TENANT,
            kick=lambda *args: kicks.append(args),
        )._reconcile_ingress()

        self.assertEqual(kicks, [])

    def test_backlog_reconciliation_leaves_paused_agent_queued(self):
        self.register(host="office")
        self.redis.rpush(prefix(POD, TENANT, "host", "ingress"), "queued")
        self.redis.values[prefix(POD, TENANT, "host", "paused")] = "1"
        kicks = []

        Switch(
            self.redis, pod=POD, tenant=TENANT,
            kick=lambda *args: kicks.append(args),
        )._reconcile_ingress()

        self.assertEqual(kicks, [])


if __name__ == "__main__":
    unittest.main()


def test_receive_wrongtype_dead_key_preserves_claimed_identity_on_real_redis():
    r = redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"))
    try:
        r.ping()
    except Exception:
        pytest.skip("real Redis server not available at REDIS_URL")
    tenant = f"receive-{uuid4().hex[:12]}"
    agent = "recipient"
    processing = prefix(POD, tenant, agent, "processing")
    dead = prefix(POD, tenant, agent, "dead")
    envelope = build("Message", "sender", agent, {"text": "wrong type"}, pod=POD, tenant=tenant)
    raw = encode(envelope)
    r.rpush(processing, raw)
    r.set(dead, "hostile-wrong-type")
    try:
        with pytest.raises(redis.ResponseError, match="receive custody key is not a list"):
            receive(
                r, pod=POD, tenant=tenant, agent=agent,
                openers={}, timeout=0, blocking=False,
            )

        assert r.lrange(processing, 0, -1) == [raw.encode()]
        assert r.get(dead) == b"hostile-wrong-type"
    finally:
        r.delete(processing, dead)
