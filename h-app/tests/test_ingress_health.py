import json
import sys
import unittest
from collections import defaultdict, deque
from pathlib import Path
from unittest.mock import MagicMock, patch

import redis

H_APP = Path(__file__).resolve().parents[1]
if str(H_APP) not in sys.path:
    sys.path.insert(0, str(H_APP))

from core.envelope import build, encode
from core.ingress_health import IngressStallMonitor
from core.keys import delivery_lock_key, ingress_stalled_key, prefix
from core.service import Switch


POD = "mesh"
TENANT = "office"


class HealthRedis:
    def __init__(self):
        self.lists = defaultdict(deque)
        self.hashes = defaultdict(dict)
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def lindex(self, key, index):
        try:
            return self.lists[key][index]
        except IndexError:
            return None

    def llen(self, key):
        return len(self.lists[key])

    def hget(self, key, field):
        return self.hashes[key].get(field)

    def hset(self, key, field, value):
        self.hashes[key][field] = value
        return 1

    def hdel(self, key, field):
        return int(self.hashes[key].pop(field, None) is not None)


def frame(text="hello", destination="bob"):
    envelope = build("Message", "alice", destination, {"text": text}, pod=POD, tenant=TENANT)
    return envelope["stream_id"], encode(envelope).encode()


class IngressStallMonitorTests(unittest.TestCase):
    def setUp(self):
        self.r = HealthRedis()
        self.now = 1000.0
        self.monitor = IngressStallMonitor(
            self.r, pod=POD, tenant=TENANT, stall_seconds=60, clock=lambda: self.now
        )
        self.ingress = prefix(POD, TENANT, "bob", "ingress")
        self.key = ingress_stalled_key(POD, TENANT)
        self.members = {"alice": "tmux", "bob": "tmux"}
        patcher = patch("core.ingress_health._log_record")
        self.log = patcher.start()
        self.addCleanup(patcher.stop)

    def events(self):
        return [call.args[1] for call in self.log.call_args_list]

    def tick(self, seconds):
        self.now += seconds
        self.monitor.poll(self.members)

    def test_unchanged_head_below_threshold_is_not_a_stall(self):
        self.r.lists[self.ingress].append(frame()[1])
        self.tick(0)
        self.tick(59)
        self.assertEqual(self.r.hashes[self.key], {})
        self.assertEqual(self.events(), [])

    def test_unchanged_head_past_threshold_is_flagged_once_with_evidence(self):
        stream_id, raw = frame()
        self.r.lists[self.ingress].extend([raw, frame("second")[1]])
        self.tick(0)
        self.tick(61)
        self.tick(30)  # still stalled: updated in place, not announced again

        record = json.loads(self.r.hashes[self.key]["bob"])
        self.assertEqual(record["head_stream_id"], stream_id)
        self.assertEqual(record["depth"], 2)
        self.assertEqual(record["lease"], "absent")
        self.assertEqual(record["port_type"], "tmux")
        self.assertTrue(record["since"].endswith("Z"))
        self.assertEqual(self.events(), ["ingress_stalled"])
        kwargs = self.log.call_args.kwargs
        self.assertEqual(kwargs["destination"], "bob")
        self.assertEqual(kwargs["stream_id"], stream_id)
        self.assertEqual(kwargs["count"], 2)
        self.assertIn("lease absent", kwargs["reason"])

    def test_held_lease_is_reported_as_a_hung_consumer(self):
        self.r.lists[self.ingress].append(frame()[1])
        self.r.values[delivery_lock_key(POD, TENANT, "bob")] = b"token"
        self.tick(0)
        self.tick(61)
        self.assertEqual(json.loads(self.r.hashes[self.key]["bob"])["lease"], "held")
        self.assertIn("lease held", self.log.call_args.kwargs["reason"])

    def test_since_is_kept_across_polls(self):
        self.r.lists[self.ingress].append(frame()[1])
        self.tick(0)
        self.tick(61)
        first = json.loads(self.r.hashes[self.key]["bob"])["since"]
        self.tick(5)
        self.assertEqual(json.loads(self.r.hashes[self.key]["bob"])["since"], first)

    def test_progress_resets_the_clock_and_clears_a_flag(self):
        self.r.lists[self.ingress].extend([frame("one")[1], frame("two")[1]])
        self.tick(0)
        self.tick(61)
        self.assertIn("bob", self.r.hashes[self.key])

        self.r.lists[self.ingress].popleft()  # the consumer took the head
        self.tick(1)
        self.assertNotIn("bob", self.r.hashes[self.key])
        self.assertEqual(self.events(), ["ingress_stalled", "ingress_recovered"])

        self.tick(59)  # new head, clock restarted: not yet a stall
        self.assertNotIn("bob", self.r.hashes[self.key])

    def test_drained_queue_clears_the_flag(self):
        self.r.lists[self.ingress].append(frame()[1])
        self.tick(0)
        self.tick(61)
        self.r.lists[self.ingress].clear()
        self.tick(1)
        self.assertEqual(self.r.hashes[self.key], {})
        self.assertEqual(self.events(), ["ingress_stalled", "ingress_recovered"])

    def test_paused_agent_is_never_flagged(self):
        self.r.lists[self.ingress].append(frame()[1])
        self.r.values[prefix(POD, TENANT, "bob", "paused")] = b"1"
        self.tick(0)
        self.tick(600)
        self.assertEqual(self.r.hashes[self.key], {})

    def test_retired_agent_entry_is_removed(self):
        self.r.lists[self.ingress].append(frame()[1])
        self.tick(0)
        self.tick(61)
        del self.members["bob"]
        self.tick(1)
        self.assertEqual(self.r.hashes[self.key], {})
        self.assertEqual(self.events(), ["ingress_stalled", "ingress_recovered"])

    def test_detection_reads_only_the_header_never_the_body(self):
        stream_id, raw = frame()
        corrupt = raw[:256] + b"{this is not json and must never be parsed"
        self.r.lists[self.ingress].append(corrupt)
        with patch("core.ingress_health.json.loads", wraps=json.loads) as loads:
            self.tick(0)
            self.tick(61)
        self.assertEqual(json.loads(self.r.hashes[self.key]["bob"])["head_stream_id"], stream_id)
        for call in loads.call_args_list:
            self.assertNotIn("must never be parsed", str(call.args[0]))

    def test_malformed_head_without_a_header_still_stalls(self):
        self.r.lists[self.ingress].append(b"garbage")
        self.tick(0)
        self.tick(61)
        record = json.loads(self.r.hashes[self.key]["bob"])
        self.assertIsNone(record["head_stream_id"])

    def test_logging_failure_does_not_undo_the_flag(self):
        self.log.side_effect = RuntimeError("stdout closed")
        self.r.lists[self.ingress].append(frame()[1])
        self.tick(0)
        self.tick(61)
        self.assertIn("bob", self.r.hashes[self.key])

    def test_nonpositive_threshold_is_rejected(self):
        with self.assertRaises(ValueError):
            IngressStallMonitor(self.r, pod=POD, tenant=TENANT, stall_seconds=0)


class SwitchStallWiringTests(unittest.TestCase):
    def _run_once(self, monitor):
        r = MagicMock()
        r.hgetall.return_value = {}
        switch = Switch(r, pod=POD, tenant=TENANT, poll_seconds=1)
        switch._reconcile_ingress = MagicMock()
        switch.step = MagicMock(side_effect=KeyboardInterrupt)
        with (
            patch("core.service.member_types", return_value={"bob": "tmux"}),
            patch("core.service._emit_observation") as emit,
            self.assertRaises(KeyboardInterrupt),
        ):
            switch.run(stall_monitor=monitor)
        return emit

    def test_maintenance_pass_polls_the_monitor_with_the_registry_snapshot(self):
        monitor = MagicMock()
        self._run_once(monitor)
        monitor.poll.assert_called_once_with({"bob": "tmux"})

    def test_a_failing_monitor_is_reported_and_does_not_stop_the_loop(self):
        monitor = MagicMock()
        monitor.poll.side_effect = redis.exceptions.ConnectionError("down")
        emit = self._run_once(monitor)
        reasons = [c.kwargs.get("reason", "") for c in emit.call_args_list]
        self.assertTrue(any("ingress stall pass failed: ConnectionError" in x for x in reasons))


if __name__ == "__main__":
    unittest.main()
