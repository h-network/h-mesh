import json
import sys
import unittest
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

H_APP = Path(__file__).resolve().parents[1]
if str(H_APP) not in sys.path:
    sys.path.insert(0, str(H_APP))

from core.keys import prefix
from modules.watchdog.presence import PresenceSampler


POD = "acme"
TENANT = "hq"
NOW = datetime(2026, 8, 9, 12, 1, 0, tzinfo=timezone.utc)


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.hashes = defaultdict(dict)
        self.streams = defaultdict(list)
        self.reverse_counts = []

    def get(self, key):
        return self.values.get(key)

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def hset(self, key, field=None, value=None, mapping=None):
        if mapping is not None:
            self.hashes[key].update(mapping)
            return len(mapping)
        self.hashes[key][field] = value
        return 1

    def xrevrange(self, key, max="+", min="-", count=None):
        entries = list(reversed(self.streams.get(key, [])))
        if count is not None:
            self.reverse_counts.append(count)
            return entries[:count]
        return entries


def _activity(agent, timestamp, entry_id):
    event = json.dumps({"v": 1, "agent": agent, "ts": timestamp, "kind": "tool", "tool": "Read"})
    return entry_id, {"event": event}


def _presence(r, agent):
    return r.hashes[prefix(POD, TENANT, agent, "presence")]


class PresenceSamplerTests(unittest.TestCase):
    def test_presence_samples_working_idle_and_unknown(self):
        r = FakeRedis()
        r.values[prefix(POD, TENANT, "working", "launch")] = "claude"
        r.values[prefix(POD, TENANT, "idle", "launch")] = "codex"
        r.streams[prefix(POD, TENANT, "working", "activity")] = [
            _activity("working", "2026-08-09T12:00:50Z", "1-0")
        ]
        r.streams[prefix(POD, TENANT, "idle", "activity")] = [
            _activity("idle", "2026-08-09T11:59:00Z", "1-0")
        ]

        PresenceSampler(r, pod=POD, tenant=TENANT, working_seconds=30).poll(
            {"working", "idle", "unknown"}, now=NOW
        )

        self.assertEqual(_presence(r, "working"), {
            "state": "working",
            "since": "2026-08-09T12:00:50.000Z",
            "last_activity": "2026-08-09T12:00:50.000Z",
        })
        self.assertEqual(_presence(r, "idle"), {
            "state": "idle",
            "since": "2026-08-09T11:59:30.000Z",
            "last_activity": "2026-08-09T11:59:00.000Z",
        })
        self.assertEqual(_presence(r, "unknown"), {
            "state": "unknown",
            "since": "2026-08-09T12:01:00.000Z",
            "last_activity": "",
        })

    def test_presence_since_changes_only_on_state_transition(self):
        r = FakeRedis()
        r.values[prefix(POD, TENANT, "sme-2", "launch")] = "claude"
        key = prefix(POD, TENANT, "sme-2", "activity")
        r.streams[key] = [_activity("sme-2", "2026-08-09T12:00:50Z", "1-0")]
        sampler = PresenceSampler(r, pod=POD, tenant=TENANT, working_seconds=30)
        sampler.poll({"sme-2"}, now=NOW)

        r.streams[key].append(_activity("sme-2", "2026-08-09T12:01:05Z", "2-0"))
        sampler.poll({"sme-2"}, now=datetime(2026, 8, 9, 12, 1, 10, tzinfo=timezone.utc))

        self.assertEqual(_presence(r, "sme-2")["since"], "2026-08-09T12:00:50.000Z")
        self.assertEqual(_presence(r, "sme-2")["last_activity"], "2026-08-09T12:01:05.000Z")

    def test_malformed_latest_activity_falls_back_to_last_valid_event(self):
        r = FakeRedis()
        r.values[prefix(POD, TENANT, "sme-2", "launch")] = "claude"
        key = prefix(POD, TENANT, "sme-2", "activity")
        r.streams[key] = [
            _activity("sme-2", "2026-08-09T12:00:50Z", "1-0"),
            ("2-0", {"event": "not-json"}),
        ]
        PresenceSampler(r, pod=POD, tenant=TENANT).poll({"sme-2"}, now=NOW)
        self.assertEqual(_presence(r, "sme-2")["state"], "working")
        self.assertEqual(r.reverse_counts, [10])

    def test_agy_reads_working_from_its_own_activity_stream(self):
        """agy joined `_tailable`'s CLI set once history.jsonl was confirmed live
        and wired into ActivityTailer -- an agy agent now reads real presence off
        the same activity stream claude/codex populate, not a permanent `unknown`.
        """
        r = FakeRedis()
        r.values[prefix(POD, TENANT, "sme-2", "launch")] = "agy"
        r.streams[prefix(POD, TENANT, "sme-2", "activity")] = [
            _activity("sme-2", "2026-08-09T12:00:59Z", "1-0")
        ]
        PresenceSampler(r, pod=POD, tenant=TENANT).poll({"sme-2"}, now=NOW)
        self.assertEqual(_presence(r, "sme-2")["state"], "working")
        self.assertEqual(_presence(r, "sme-2")["last_activity"], "2026-08-09T12:00:59.000Z")

    def test_agy_with_no_activity_yet_is_idle_not_unknown(self):
        r = FakeRedis()
        r.values[prefix(POD, TENANT, "sme-2", "launch")] = "agy"
        PresenceSampler(r, pod=POD, tenant=TENANT).poll({"sme-2"}, now=NOW)
        self.assertEqual(_presence(r, "sme-2")["state"], "idle")

    def test_a_fresh_tailable_agent_is_idle_not_unknown(self):
        """A freshly hired claude agent has no activity yet -- that is idle.

        Only an agent whose activity could never be seen is unknown. Without the
        distinction a ready agent and a bare shell give a client the same answer,
        and it cannot tell "nothing seen yet" from "nothing to see".
        """
        r = FakeRedis()
        r.values[prefix(POD, TENANT, "fresh", "launch")] = "claude"
        # 'shell' has no launch key at all

        PresenceSampler(r, pod=POD, tenant=TENANT).poll({"fresh", "shell"}, now=NOW)

        self.assertEqual(r.hashes[prefix(POD, TENANT, "fresh", "presence")]["state"], "idle")
        self.assertEqual(r.hashes[prefix(POD, TENANT, "shell", "presence")]["state"], "unknown")


if __name__ == "__main__":
    unittest.main()
