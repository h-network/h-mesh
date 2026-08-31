import contextlib
import io
import json
import sys
import unittest
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

H_APP = Path(__file__).resolve().parents[1]
if str(H_APP) not in sys.path:
    sys.path.insert(0, str(H_APP))

from core.keys import prefix
from modules.watchdog import verification
from modules.watchdog.verification import DeliveryVerifier


POD = "acme"
TENANT = "hq"
NOW = datetime(2026, 8, 9, 12, 0, 20, tzinfo=timezone.utc)


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.hashes = defaultdict(dict)
        self.streams = defaultdict(list)
        self.deleted = []
        self.xrange_calls = []

    def xrange(self, key, min="-", max="+", count=None):
        self.xrange_calls.append((key, min, max))
        entries = self.streams.get(key, [])
        result = []
        exclusive = False
        min_str = min
        if isinstance(min_str, str) and min_str.startswith("("):
            exclusive = True
            min_str = min_str[1:]
        for entry_id, fields in entries:
            eid = entry_id.decode() if isinstance(entry_id, bytes) else str(entry_id)
            if min_str != "-":
                if exclusive and eid <= min_str:
                    continue
                if not exclusive and eid < min_str:
                    continue
            result.append((entry_id, fields))
            if count and len(result) >= count:
                break
        return result

    def xdel(self, key, *ids):
        stream = self.streams.get(key, [])
        id_set = set(ids)
        self.streams[key] = [entry for entry in stream if entry[0] not in id_set]
        self.deleted.extend((key, i) for i in ids)
        return len(ids)

    def xlen(self, key):
        return len(self.streams.get(key, []))

    def exists(self, *keys):
        return sum(1 for k in keys if k in self.values or k in self.hashes or k in self.streams)

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def hset(self, key, field=None, value=None, mapping=None):
        if mapping is not None:
            self.hashes[key].update(mapping)
            return len(mapping)
        self.hashes[key][field] = value
        return 1

    def delete(self, *keys):
        count = 0
        for key in keys:
            if key in self.hashes:
                del self.hashes[key]
                count += 1
        return count


def _key(resource):
    return prefix(POD, TENANT, "sme-2", resource)


def _marker(stream_id, timestamp, entry_id=b"1-0"):
    return entry_id, {b"stream_id": stream_id.encode(), b"ts": timestamp.encode()}


def _activity(kind, timestamp, entry_id=b"2-0"):
    event = json.dumps({"v": 1, "agent": "sme-2", "ts": timestamp, "kind": kind})
    return entry_id, {b"event": event.encode()}


def _capture(fn):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn()
    return buf.getvalue()


class DeliveryVerifierTests(unittest.TestCase):
    def test_later_input_verifies_and_drops_marker_without_log(self):
        r = FakeRedis()
        r.streams[_key("pending.verify")] = [_marker("delivered", "2026-08-09T12:00:00Z")]
        r.streams[_key("activity")] = [_activity("input", "2026-08-09T12:00:01Z")]
        r.hashes[_key("blocked")] = {"since": "old", "stream_id": "old"}

        out = _capture(lambda: DeliveryVerifier(
            r, pod=POD, tenant=TENANT, verify_after_seconds=10
        ).poll({"sme-2"}, now=NOW))

        self.assertEqual(r.streams[_key("pending.verify")], [])
        self.assertNotIn(_key("blocked"), r.hashes)
        self.assertEqual(out, "")

    def test_later_output_verifies_and_drops_marker_without_log(self):
        r = FakeRedis()
        r.streams[_key("pending.verify")] = [_marker("delivered", "2026-08-09T12:00:00Z")]
        r.streams[_key("activity")] = [_activity("output", "2026-08-09T12:00:05Z")]
        r.hashes[_key("blocked")] = {"since": "old", "stream_id": "old"}

        out = _capture(lambda: DeliveryVerifier(
            r, pod=POD, tenant=TENANT, verify_after_seconds=10
        ).poll({"sme-2"}, now=NOW))

        self.assertEqual(r.streams[_key("pending.verify")], [])
        self.assertNotIn(_key("blocked"), r.hashes)
        self.assertEqual(out, "")

    def test_no_activity_after_marker_is_surfaced_and_not_retried(self):
        r = FakeRedis()
        r.streams[_key("pending.verify")] = [_marker("not-confirmed", "2026-08-09T12:00:00Z")]
        r.values[_key("activity.offset")] = "observed"

        out = _capture(lambda: DeliveryVerifier(
            r, pod=POD, tenant=TENANT, verify_after_seconds=10
        ).poll({"sme-2"}, now=NOW))

        record = json.loads(out)
        self.assertEqual(record["module"], "switch")
        self.assertEqual(record["event"], "delivery_unverified")
        self.assertEqual(record["stream_id"], "not-confirmed")
        self.assertEqual(record["destination"], "sme-2")
        self.assertEqual(record["waited"], 20)
        self.assertEqual(record["reason"], (
            "not confirmed by a later CLI activity event; "
            "not retried because verification cannot distinguish loss from a landed paste"
        ))
        self.assertNotIn("lost", json.dumps(record))
        self.assertEqual(r.streams[_key("pending.verify")], [])
        self.assertEqual(r.hashes[_key("blocked")], {
            "since": "2026-08-09T12:00:00Z",
            "stream_id": "not-confirmed",
        })

    def test_activity_before_marker_does_not_verify(self):
        r = FakeRedis()
        r.streams[_key("pending.verify")] = [_marker("ordered", "2026-08-09T12:00:00Z")]
        r.streams[_key("activity")] = [_activity("tool", "2026-08-09T11:59:59Z")]

        out = _capture(lambda: DeliveryVerifier(
            r, pod=POD, tenant=TENANT, verify_after_seconds=10
        ).poll({"sme-2"}, now=NOW))

        self.assertEqual(json.loads(out)["event"], "delivery_unverified")
        self.assertEqual(r.hashes[_key("blocked")]["stream_id"], "ordered")

    def test_activity_read_starts_at_earliest_eligible_marker(self):
        r = FakeRedis()
        marker_time = datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc)
        r.streams[_key("activity")] = [_activity("output", "2026-08-09T12:00:01Z")]

        DeliveryVerifier(r, pod=POD, tenant=TENANT)._input_times("sme-2", marker_time)

        self.assertEqual(r.xrange_calls, [(_key("activity"), "1786276800000-0", "+")])

    def test_input_only_negative_control_flips_output_evidence(self):
        """The widened-evidence control fails at the evidence reader's locus."""
        r = FakeRedis()
        marker_time = datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc)
        r.streams[_key("activity")] = [_activity("output", "2026-08-09T12:00:01Z")]
        verifier = DeliveryVerifier(r, pod=POD, tenant=TENANT)

        self.assertEqual(
            verifier._input_times("sme-2", marker_time),
            [datetime(2026, 8, 9, 12, 0, 1, tzinfo=timezone.utc)],
        )
        with patch.object(verification, "VERIFICATION_ACTIVITY_KINDS", frozenset(("input",))):
            self.assertEqual(verifier._input_times("sme-2", marker_time), [])

            r.streams[_key("pending.verify")] = [_marker("control", "2026-08-09T12:00:00Z")]
            verifier.verify_after_seconds = 10
            out = _capture(lambda: verifier.poll({"sme-2"}, now=NOW))
        self.assertEqual(json.loads(out)["event"], "delivery_unverified")

    def test_default_verification_window_is_two_minutes(self):
        self.assertEqual(
            DeliveryVerifier(object(), pod=POD, tenant=TENANT).verify_after_seconds, 120.0
        )

    def test_first_unverified_delivery_preserves_blocked_since_and_stream_id(self):
        r = FakeRedis()
        r.values[_key("activity.offset")] = "observed"
        r.hashes[_key("blocked")] = {"since": "2026-08-09T11:00:00Z", "stream_id": "first"}
        r.streams[_key("pending.verify")] = [_marker("second", "2026-08-09T12:00:00Z")]

        _capture(lambda: DeliveryVerifier(
            r, pod=POD, tenant=TENANT, verify_after_seconds=10
        ).poll({"sme-2"}, now=NOW))

        self.assertEqual(
            r.hashes[_key("blocked")], {"since": "2026-08-09T11:00:00Z", "stream_id": "first"}
        )

    def test_first_delivery_without_activity_history_is_dropped_unjudged(self):
        r = FakeRedis()
        r.streams[_key("pending.verify")] = [_marker("first", "2026-08-09T12:00:00Z")]

        out = _capture(lambda: DeliveryVerifier(
            r, pod=POD, tenant=TENANT, verify_after_seconds=10
        ).poll({"sme-2"}, now=NOW))

        record = json.loads(out)
        self.assertEqual(record, {
            "ts": record["ts"],
            "module": "switch",
            "event": "delivery_unjudged",
            "writer": "switch",
            "stream_id": "first",
            "destination": "sme-2",
            "reason": "agent has no activity history; first delivery is not judged",
            "waited": 20,
        })
        self.assertEqual(r.streams[_key("pending.verify")], [])
        self.assertNotIn(_key("blocked"), r.hashes)

    def test_marker_younger_than_threshold_remains_pending(self):
        r = FakeRedis()
        marker = _marker("young", "2026-08-09T12:00:15Z")
        r.streams[_key("pending.verify")] = [marker]
        r.streams[_key("activity")] = [_activity("input", "2026-08-09T12:00:16Z")]

        out = _capture(lambda: DeliveryVerifier(
            r, pod=POD, tenant=TENANT, verify_after_seconds=10
        ).poll({"sme-2"}, now=NOW))

        self.assertEqual(r.streams[_key("pending.verify")], [marker])
        self.assertEqual(r.deleted, [])
        self.assertEqual(out, "")

    def test_pending_verify_key_follows_the_dotted_resource_convention(self):
        """Resources compose with a dot, like tasks.todo and activity.offset.

        Pinned here because the adapter (`modules.tmux.port.mark_delivery_pending`)
        writes this key and the watchdog reads it -- two lanes, two files.
        """
        self.assertEqual(_key("pending.verify"), "pod:acme:tenant:hq:agent:sme-2:pending.verify")


if __name__ == "__main__":
    unittest.main()
