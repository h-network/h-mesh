import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

H_APP = Path(__file__).resolve().parents[1]
if str(H_APP) not in sys.path:
    sys.path.insert(0, str(H_APP))

from core.envelope import parse
from core.keys import prefix
from modules.watchdog import service
from modules.watchdog.service import Watchdog, run_observers


POD = "acme"
TENANT = "hq"
NOW = datetime(2026, 8, 9, 14, 0, tzinfo=timezone.utc)


class FakeRedis:
    """Minimal in-memory double covering what Watchdog touches: lists (board
    resources, ingress), hashes (presence/blocked/acks/*.alerted), a stream
    (alerts) and `eval` for core.queues.admit_ingress's Lua script.
    """

    def __init__(self, agents=("architect", "sme-2", "frontend", "backend"), fails_on=None):
        self.values = {}
        self.hashes = defaultdict(dict)
        self.lists = defaultdict(list)
        self.streams = defaultdict(list)
        self.writes = []
        self.fails_on = dict(fails_on) if fails_on else {}
        registry_key = prefix(POD, TENANT, resource="registry")
        self.hashes[registry_key] = {agent: "tmux" for agent in agents}

    def _fault(self, name):
        if name in self.fails_on:
            err = self.fails_on[name]
            raise err() if isinstance(err, type) else err

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, ex=None):
        self.values[key] = value

    def delete(self, *keys):
        count = 0
        for key in keys:
            if key in self.hashes:
                del self.hashes[key]
                count += 1
        return count

    def rpush(self, key, *values):
        self.writes.append(("rpush", key, values))
        self.lists[key].extend(values)
        return len(self.lists[key])

    def lpop(self, key):
        return self.lists[key].pop(0) if self.lists.get(key) else None

    def lrange(self, key, start, stop):
        lst = self.lists.get(key, [])
        if stop == -1:
            return list(lst[start:])
        return list(lst[start : stop + 1])

    def lindex(self, key, index):
        lst = self.lists.get(key, [])
        if lst and -len(lst) <= index < len(lst):
            return lst[index]
        return None

    def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    def hset(self, key, field=None, value=None, mapping=None):
        if mapping is not None:
            self.hashes[key].update(mapping)
            return len(mapping)
        self.hashes[key][field] = value
        return 1

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def hdel(self, key, *fields):
        h = self.hashes.get(key, {})
        count = 0
        for field in fields:
            if field in h:
                del h[field]
                count += 1
        return count

    def hkeys(self, key):
        return list(self.hashes.get(key, {}).keys())

    def hexists(self, key, field):
        return field in self.hashes.get(key, {})

    def xadd(self, key, fields, maxlen=None, approximate=True):
        stream = self.streams[key]
        entry_id = f"{len(stream) + 1}-0"
        stream.append((entry_id, dict(fields)))
        return entry_id

    def eval(self, script, numkeys, *args):
        self._fault("eval")
        keys = args[:numkeys]
        rest = args[numkeys:]
        if "core ingress admission v1" in script:
            limit = int(rest[0])
            raw = rest[1]
            for index, key in enumerate(keys, start=1):
                depth = len(self.lists.get(key, []))
                if depth >= limit:
                    return [0, index, depth]
            result = [1]
            for key in keys:
                self.lists.setdefault(key, []).append(raw)
                result.append(len(self.lists[key]))
            return result
        raise AssertionError(f"unexpected Lua script: {script}")


def _key(agent, resource):
    return prefix(POD, TENANT, agent, resource)


def _watchdog(r):
    return Watchdog(r, pod=POD, tenant=TENANT, session_name=TENANT)


def _stalled_agent(r, agent="sme-2", *, state="idle"):
    r.lists[_key(agent, "tasks.doing")] = [
        json.dumps({
            "id": "ticket-1",
            "title": "review the auth change",
            "started_ts": "2026-08-09T13:46:00Z",
        })
    ]
    r.hashes[_key(agent, "presence")] = {
        "state": state,
        "last_activity": "2026-08-09T13:51:00Z" if state != "unknown" else "",
    }


def _quiet_windows(now=NOW):
    """A list-windows reply with fresh activity, so the 3-signal stall check
    never fires and doesn't contaminate a doing/todo/hold/unreplied/ack-loop
    assertion."""
    ts = int(now.timestamp())

    def _run(*args, socket=None):
        if args[0] == "list-windows":
            return 0, f"architect\t{ts}\nsme-2\t{ts}", ""
        return 0, "", ""

    return _run


def _doing_agent(r, agent="sme-2", *, started="2026-08-09T13:45:00Z", ticket_id="ticket-1", title="review the auth change"):
    r.lists[_key(agent, "tasks.doing")] = [
        json.dumps({"id": ticket_id, "title": title, "started_ts": started})
    ]


def _lead(r, name="architect"):
    r.values[prefix(POD, TENANT, resource="lead")] = name


def _ack(r, source, destination, *, streak, last_ts="2026-08-09T13:59:00Z"):
    r.hashes.setdefault(_key(source, "acks"), {})[destination] = json.dumps(
        {"streak": streak, "last_ts": last_ts}
    )


def _todo_agent(r, agent="sme-2", *, created="2026-08-09T13:55:00Z", ticket_id="ticket-1", title="pick up the auth review", append=False):
    entry = json.dumps({"id": ticket_id, "title": title, "created_ts": created})
    key = _key(agent, "tasks.todo")
    if append:
        r.lists.setdefault(key, []).append(entry)
    else:
        r.lists[key] = [entry]


def _hold_agent(r, agent="sme-2", *, held="2026-08-09T13:00:00Z", ticket_id="ticket-1", title="wait on the vendor reply", append=False, created=None):
    entry = {"id": ticket_id, "title": title}
    if held is not None:
        entry["held_ts"] = held
    if created is not None:
        entry["created_ts"] = created
    key = _key(agent, "tasks.hold")
    if append:
        r.lists.setdefault(key, []).append(json.dumps(entry))
    else:
        r.lists[key] = [json.dumps(entry)]


def _unreplied_agent(r, agent="sme-2", *, client="telegram", since="2026-08-09T13:59:00Z", count=1):
    key = _key(agent, "unreplied")
    r.hashes.setdefault(key, {})[client] = json.dumps({"count": count, "since": since})


def _capture(fn):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn()
    return buf.getvalue()


def _kicks():
    """Return (list, fake_deliver_tmux) -- records the kicked agent name."""
    kicks = []

    def fake_deliver_tmux(r, pod, tenant, agent, session_name=None, socket=None, **kw):
        kicks.append(agent)

    return kicks, fake_deliver_tmux


def _no_kick():
    def fake_deliver_tmux(*a, **kw):
        raise AssertionError("should not kick")

    return fake_deliver_tmux


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self._live_patches = []

    def tearDown(self):
        for p in self._live_patches:
            p.stop()

    def _set(self, target, name, value):
        p = patch.object(target, name, value)
        p.start()
        self._live_patches.append(p)

    # -- stalls -----------------------------------------------------------

    def test_stall_requires_old_ticket_nonworking_presence_and_silent_window(self):
        r = FakeRedis()
        _stalled_agent(r)
        captures = []

        def tmux(*args, socket=None):
            if args[0] == "list-windows":
                return 0, "architect\t1786283999\nsme-2\t1786283580", ""
            captures.append(args[-1])
            return 0, "healthy pane", ""

        self._set(service, "run_tmux", tmux)
        watchdog = _watchdog(r)
        out = _capture(lambda: watchdog.poll(now=NOW))

        alert = json.loads(r.streams[prefix(POD, TENANT, resource="alerts")][0][1]["alert"])
        self.assertEqual(alert, {
            "v": 1,
            "ts": "2026-08-09T14:00:00.000Z",
            "kind": "stalled",
            "writer": "watchdog",
            "agent": "sme-2",
            "ticket": "review the auth change",
            "doing_age_s": 840,
            "no_activity_s": 540,
            "no_output_s": 420,
            "unchecked": [],
        })
        self.assertEqual(json.loads(out), alert)
        self.assertEqual(captures, [])

        watchdog.poll(now=NOW)
        self.assertEqual(len(r.streams[prefix(POD, TENANT, resource="alerts")]), 1)

    def test_printing_window_or_working_presence_suppresses_stall(self):
        r = FakeRedis()
        _stalled_agent(r)
        self._set(service, "run_tmux", lambda *args, socket=None: (
            (0, "architect\t1786283999\nsme-2\t1786283990", "") if args[0] == "list-windows" else (0, "", "")
        ))
        watchdog = _watchdog(r)
        watchdog.poll(now=NOW)
        self.assertNotIn(prefix(POD, TENANT, resource="alerts"), r.streams)

        r.hashes[_key("sme-2", "presence")]["state"] = "working"
        self._set(service, "run_tmux", lambda *args, socket=None: (
            (0, "architect\t1786283999\nsme-2\t1786283580", "") if args[0] == "list-windows" else (0, "", "")
        ))
        watchdog.poll(now=NOW)
        self.assertNotIn(prefix(POD, TENANT, resource="alerts"), r.streams)

    def test_unknown_activity_is_named_as_unchecked(self):
        r = FakeRedis()
        _stalled_agent(r, state="unknown")
        self._set(service, "run_tmux", lambda *args, socket=None: (
            (0, "architect\t1786283999\nsme-2\t1786283580", "") if args[0] == "list-windows" else (0, "", "")
        ))
        _watchdog(r).poll(now=NOW)
        alert = json.loads(r.streams[prefix(POD, TENANT, resource="alerts")][0][1]["alert"])
        self.assertIsNone(alert["no_activity_s"])
        self.assertEqual(alert["unchecked"], ["activity"])

    def test_missing_window_is_reported_for_otherwise_stalled_agent(self):
        r = FakeRedis()
        _stalled_agent(r)
        self._set(service, "run_tmux", lambda *args, socket=None: (0, "architect\t1786283999", ""))

        _watchdog(r).poll(now=NOW)

        alert = json.loads(r.streams[prefix(POD, TENANT, resource="alerts")][0][1]["alert"])
        self.assertEqual(alert["kind"], "stalled")
        self.assertEqual(alert["agent"], "sme-2")
        self.assertIsNone(alert["no_output_s"])
        self.assertTrue(alert["window_missing"])

    # -- blocked ------------------------------------------------------------

    def test_blocked_alert_reads_router_verdict_without_scraping(self):
        r = FakeRedis()
        r.hashes[_key("sme-2", "blocked")] = {
            "since": "2026-08-09T13:53:00Z",
            "stream_id": "delivery-1",
        }
        calls = []
        self._set(service, "run_tmux", lambda *args, socket=None: (
            calls.append(args) or (0, "architect\t1786284000\nsme-2\t1786284000", "")
        ))
        watchdog = _watchdog(r)
        watchdog.poll(now=NOW)

        self.assertTrue(all(call[0] == "list-windows" for call in calls))
        alerts = r.streams[prefix(POD, TENANT, resource="alerts")]
        alert = json.loads(alerts[0][1]["alert"])
        self.assertEqual(alert, {
            "v": 1,
            "ts": "2026-08-09T14:00:00.000Z",
            "kind": "blocked",
            "writer": "watchdog",
            "agent": "sme-2",
            "since": "2026-08-09T13:53:00Z",
            "stream_id": "delivery-1",
            "unconsumed_s": 420,
        })
        self.assertFalse(any("egress" in str(write) for write in r.writes))

        watchdog.poll(now=NOW)
        self.assertEqual(len(r.streams[prefix(POD, TENANT, resource="alerts")]), 1)

    def test_stall_alert_includes_blocked_verdict(self):
        r = FakeRedis()
        _stalled_agent(r)
        r.hashes[_key("sme-2", "blocked")] = {
            "since": "2026-08-09T13:53:00Z",
            "stream_id": "delivery-1",
        }
        self._set(service, "run_tmux", lambda *args, socket=None: (
            0, "architect\t1786283999\nsme-2\t1786283580", ""
        ))
        _watchdog(r).poll(now=NOW)
        alert = json.loads(r.streams[prefix(POD, TENANT, resource="alerts")][0][1]["alert"])
        self.assertEqual(alert["kind"], "stalled")
        self.assertEqual(alert["blocked"], {
            "since": "2026-08-09T13:53:00Z",
            "stream_id": "delivery-1",
            "unconsumed_s": 420,
        })

    # -- doing duration -------------------------------------------------------

    def test_doing_duration_messages_the_lead_directly_not_the_alerts_stream(self):
        r = FakeRedis()
        _doing_agent(r)
        _lead(r)
        kicks, fake = _kicks()
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", fake)

        _watchdog(r).poll(now=NOW)

        self.assertNotIn(prefix(POD, TENANT, resource="alerts"), r.streams)
        ingress = r.lists[_key("architect", "ingress")]
        self.assertEqual(len(ingress), 1)
        envelope = parse(ingress[0])
        self.assertEqual(envelope["kind"], "Message")
        self.assertEqual(envelope["l2"]["source"], "watchdog")
        self.assertEqual(envelope["l2"]["destination"], "architect")
        self.assertEqual(envelope["payload"]["text"], (
            '[alert from watchdog] sme-2 has been working on '
            '"review the auth change" for 15 min, request an update'
        ))
        self.assertEqual(kicks, ["architect"])

    def test_doing_duration_does_not_fire_before_fifteen_minutes(self):
        r = FakeRedis()
        _doing_agent(r, started="2026-08-09T13:46:00Z")  # 840s old, under the 900s default
        _lead(r)
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", _no_kick())

        _watchdog(r).poll(now=NOW)

        self.assertNotIn(_key("architect", "ingress"), r.lists)

    def test_doing_duration_does_not_repeat_within_the_same_threshold_crossing(self):
        r = FakeRedis()
        _doing_agent(r)
        _lead(r)
        kicks, fake = _kicks()
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", fake)
        watchdog = _watchdog(r)

        watchdog.poll(now=NOW)
        watchdog.poll(now=NOW)
        watchdog.poll(now=NOW + timedelta(seconds=60))

        self.assertEqual(len(kicks), 1)
        self.assertEqual(len(r.lists[_key("architect", "ingress")]), 1)

    def test_doing_duration_re_alerts_at_the_next_threshold_crossing(self):
        r = FakeRedis()
        _doing_agent(r)
        _lead(r)
        kicks, fake = _kicks()
        watchdog = _watchdog(r)

        self._set(service, "run_tmux", _quiet_windows(NOW))
        self._set(service, "deliver_tmux", fake)
        watchdog.poll(now=NOW)
        self.assertEqual(len(kicks), 1)

        later = NOW + timedelta(seconds=900)
        self._set(service, "run_tmux", _quiet_windows(later))
        watchdog.poll(now=later)
        self.assertEqual(len(kicks), 2)
        envelope = parse(r.lists[_key("architect", "ingress")][-1])
        self.assertIn("30 min", envelope["payload"]["text"])

    def test_doing_duration_different_ticket_resets_and_re_alerts(self):
        r = FakeRedis()
        _doing_agent(r)
        _lead(r)
        kicks, fake = _kicks()
        watchdog = _watchdog(r)
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", fake)
        watchdog.poll(now=NOW)
        self.assertEqual(len(kicks), 1)

        _doing_agent(r, started="2026-08-09T13:45:00Z", ticket_id="ticket-2", title="fix the flaky test")
        watchdog.poll(now=NOW)
        self.assertEqual(len(kicks), 2)

    def test_doing_duration_does_nothing_without_a_configured_lead(self):
        r = FakeRedis()
        _doing_agent(r)
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", _no_kick())

        _watchdog(r).poll(now=NOW)

        self.assertNotIn(_key("architect", "ingress"), r.lists)

    def test_notify_lead_drops_the_alert_when_the_lead_ingress_is_full(self):
        r = FakeRedis()
        _doing_agent(r)
        _lead(r)
        r.lists[_key("architect", "ingress")] = ["x"] * 300  # INGRESS_MAX default
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", _no_kick())

        out = _capture(lambda: _watchdog(r).poll(now=NOW))

        self.assertEqual(len(r.lists[_key("architect", "ingress")]), 300)
        events = [json.loads(line) for line in out.splitlines()]
        self.assertTrue(any(event.get("event") == "lead_alert_capacity" for event in events))
        self.assertFalse(any(event.get("event") == "lead_alert_sent" for event in events))

    def test_notify_lead_logs_unknown_and_does_not_kick_on_a_redis_fault(self):
        r = FakeRedis(fails_on={"eval": ConnectionError})
        _doing_agent(r)
        _lead(r)
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", _no_kick())

        out = _capture(lambda: _watchdog(r).poll(now=NOW))

        events = [json.loads(line) for line in out.splitlines()]
        self.assertTrue(any(event.get("event") == "lead_alert_unknown" for event in events))
        self.assertFalse(any(event.get("event") == "lead_alert_sent" for event in events))

    def test_notify_lead_logs_a_record_when_the_lead_is_not_a_registered_agent(self):
        """A dangling `lead` key pointing at a retired agent must not vanish
        silently -- there is currently no way to transfer leadership, so this
        is a reachable state, not a hypothetical."""
        r = FakeRedis()
        _doing_agent(r)
        _lead(r, name="retired-lead")  # never registered in this FakeRedis
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", _no_kick())

        out = _capture(lambda: _watchdog(r).poll(now=NOW))

        self.assertNotIn(_key("retired-lead", "ingress"), r.lists)
        events = [json.loads(line) for line in out.splitlines()]
        no_lead = [e for e in events if e.get("event") == "lead_alert_no_lead"]
        self.assertEqual(len(no_lead), 1)
        self.assertEqual(no_lead[0]["destination"], "retired-lead")
        self.assertIn("not a registered agent", no_lead[0]["reason"])
        self.assertTrue(no_lead[0].get("stream_id"))
        self.assertFalse(any(e.get("event") == "lead_alert_sent" for e in events))

    def test_notify_lead_logs_a_record_when_the_lead_is_not_a_tmux_agent(self):
        r = FakeRedis()
        _doing_agent(r)
        _lead(r, name="api")  # registered, but as the api client, not tmux
        r.hashes[prefix(POD, TENANT, resource="registry")]["api"] = "api"
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", _no_kick())

        out = _capture(lambda: _watchdog(r).poll(now=NOW))

        self.assertNotIn(_key("api", "ingress"), r.lists)
        events = [json.loads(line) for line in out.splitlines()]
        no_lead = [e for e in events if e.get("event") == "lead_alert_no_lead"]
        self.assertEqual(len(no_lead), 1)
        self.assertEqual(no_lead[0]["destination"], "api")
        self.assertIn("port_type is not tmux", no_lead[0]["reason"])
        self.assertFalse(any(e.get("event") == "lead_alert_sent" for e in events))

    def test_lead_window_missing_dead_letters_with_a_real_record_not_replayed(self):
        """A registered tmux lead whose window is merely missing right now
        (recreate in progress) still gets a real custody record: admitted to
        ingress, then dead-lettered by modules.tmux.port's own DeadLetter
        handling when the pop finds no window. Deliberately not replayed
        when the window returns -- see _notify_lead's docstring."""
        r = FakeRedis()
        _doing_agent(r)
        _lead(r)
        self._set(service, "run_tmux", _quiet_windows())
        with patch("modules.tmux.port.list_windows", return_value=set()):
            out = _capture(lambda: _watchdog(r).poll(now=NOW))

        self.assertEqual(r.lists[_key("architect", "ingress")], [])
        dead = r.lists[_key("architect", "dead")]
        self.assertEqual(len(dead), 1)
        events = [json.loads(line) for line in out.splitlines()]
        dead_lettered = [e for e in events if e.get("event") == "dead_lettered"]
        self.assertEqual(len(dead_lettered), 1)
        self.assertEqual(dead_lettered[0]["reason"], "window_missing")
        self.assertEqual(dead_lettered[0]["destination"], "architect")
        self.assertTrue(any(e.get("event") == "lead_alert_sent" for e in events))

    # -- ack loop -------------------------------------------------------------

    def test_ack_loop_fires_when_both_directions_cross_the_threshold(self):
        r = FakeRedis()
        _lead(r)
        _ack(r, "frontend", "backend", streak=3)
        _ack(r, "backend", "frontend", streak=3)
        kicks, fake = _kicks()
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", fake)

        _watchdog(r).poll(now=NOW)

        self.assertEqual(len(kicks), 1)
        envelope = parse(r.lists[_key("architect", "ingress")][0])
        self.assertEqual(envelope["l2"]["source"], "watchdog")
        self.assertIn("backend", envelope["payload"]["text"])
        self.assertIn("frontend", envelope["payload"]["text"])
        self.assertIn("ack-looping", envelope["payload"]["text"])
        self.assertIn("3 closing replies", envelope["payload"]["text"])

    def test_ack_loop_does_not_fire_when_only_one_direction_crosses_the_threshold(self):
        r = FakeRedis()
        _lead(r)
        _ack(r, "frontend", "backend", streak=5)
        _ack(r, "backend", "frontend", streak=1)
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", _no_kick())

        _watchdog(r).poll(now=NOW)

        self.assertNotIn(_key("architect", "ingress"), r.lists)

    def test_ack_loop_ignores_a_stale_streak(self):
        r = FakeRedis()
        _lead(r)
        stale = (NOW - timedelta(seconds=300)).isoformat().replace("+00:00", "Z")
        _ack(r, "frontend", "backend", streak=3, last_ts=stale)
        _ack(r, "backend", "frontend", streak=3, last_ts=stale)
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", _no_kick())

        _watchdog(r).poll(now=NOW)

        self.assertNotIn(_key("architect", "ingress"), r.lists)

    def test_ack_loop_does_not_repeat_within_the_same_crossing(self):
        r = FakeRedis()
        _lead(r)
        _ack(r, "frontend", "backend", streak=3)
        _ack(r, "backend", "frontend", streak=3)
        kicks, fake = _kicks()
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", fake)
        watchdog = _watchdog(r)

        watchdog.poll(now=NOW)
        watchdog.poll(now=NOW)

        self.assertEqual(len(kicks), 1)

    def test_ack_loop_re_alerts_after_the_streak_doubles(self):
        r = FakeRedis()
        _lead(r)
        _ack(r, "frontend", "backend", streak=3)
        _ack(r, "backend", "frontend", streak=3)
        kicks, fake = _kicks()
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", fake)
        watchdog = _watchdog(r)
        watchdog.poll(now=NOW)
        self.assertEqual(len(kicks), 1)

        _ack(r, "frontend", "backend", streak=6)
        _ack(r, "backend", "frontend", streak=6)
        watchdog.poll(now=NOW)
        self.assertEqual(len(kicks), 2)

        # Still below the next crossing (12) -- no third alert.
        _ack(r, "frontend", "backend", streak=7)
        _ack(r, "backend", "frontend", streak=7)
        watchdog.poll(now=NOW)
        self.assertEqual(len(kicks), 2)

    def test_ack_loop_clears_state_once_a_side_sends_real_content(self):
        r = FakeRedis()
        _lead(r)
        _ack(r, "frontend", "backend", streak=3)
        _ack(r, "backend", "frontend", streak=3)
        kicks, fake = _kicks()
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", fake)
        watchdog = _watchdog(r)
        watchdog.poll(now=NOW)
        self.assertEqual(len(kicks), 1)

        # backend sent something substantive -- send() would have hdel'd this edge.
        del r.hashes[_key("backend", "acks")]["frontend"]
        _ack(r, "frontend", "backend", streak=4)
        watchdog.poll(now=NOW)
        self.assertEqual(len(kicks), 1)
        self.assertFalse(r.hashes.get(_key("frontend", "ack-loop.alerted")))

    def test_ack_loop_does_nothing_without_a_configured_lead(self):
        r = FakeRedis()
        _ack(r, "frontend", "backend", streak=3)
        _ack(r, "backend", "frontend", streak=3)
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", _no_kick())

        _watchdog(r).poll(now=NOW)

    # -- todo duration --------------------------------------------------------

    def test_todo_duration_messages_the_lead_directly_not_the_alerts_stream(self):
        r = FakeRedis()
        _todo_agent(r)
        _lead(r)
        kicks, fake = _kicks()
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", fake)

        _watchdog(r).poll(now=NOW)

        self.assertNotIn(prefix(POD, TENANT, resource="alerts"), r.streams)
        ingress = r.lists[_key("architect", "ingress")]
        self.assertEqual(len(ingress), 1)
        envelope = parse(ingress[0])
        self.assertEqual(envelope["l2"]["source"], "watchdog")
        self.assertEqual(envelope["l2"]["destination"], "architect")
        self.assertEqual(envelope["payload"]["text"], (
            '[alert from watchdog] sme-2 has an unpicked ticket '
            '"pick up the auth review" waiting 5 min'
        ))
        self.assertEqual(kicks, ["architect"])

    def test_todo_duration_does_not_fire_before_five_minutes(self):
        r = FakeRedis()
        _todo_agent(r, created="2026-08-09T13:56:00Z")  # 240s old, under the 300s default
        _lead(r)
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", _no_kick())

        _watchdog(r).poll(now=NOW)

        self.assertNotIn(_key("architect", "ingress"), r.lists)

    def test_todo_duration_does_not_repeat_within_the_same_threshold_crossing(self):
        r = FakeRedis()
        _todo_agent(r)
        _lead(r)
        kicks, fake = _kicks()
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", fake)
        watchdog = _watchdog(r)

        watchdog.poll(now=NOW)
        watchdog.poll(now=NOW)
        watchdog.poll(now=NOW + timedelta(seconds=60))

        self.assertEqual(len(kicks), 1)
        self.assertEqual(len(r.lists[_key("architect", "ingress")]), 1)

    def test_todo_duration_re_alerts_at_the_next_threshold_crossing(self):
        r = FakeRedis()
        _todo_agent(r)
        _lead(r)
        kicks, fake = _kicks()
        watchdog = _watchdog(r)

        self._set(service, "run_tmux", _quiet_windows(NOW))
        self._set(service, "deliver_tmux", fake)
        watchdog.poll(now=NOW)
        self.assertEqual(len(kicks), 1)

        later = NOW + timedelta(seconds=300)
        self._set(service, "run_tmux", _quiet_windows(later))
        watchdog.poll(now=later)
        self.assertEqual(len(kicks), 2)
        envelope = parse(r.lists[_key("architect", "ingress")][-1])
        self.assertIn("10 min", envelope["payload"]["text"])

    def test_todo_duration_different_ticket_resets_and_re_alerts(self):
        r = FakeRedis()
        _todo_agent(r)
        _lead(r)
        kicks, fake = _kicks()
        watchdog = _watchdog(r)
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", fake)
        watchdog.poll(now=NOW)
        self.assertEqual(len(kicks), 1)

        _todo_agent(r, created="2026-08-09T13:55:00Z", ticket_id="ticket-2", title="fix the flaky test")
        watchdog.poll(now=NOW)
        self.assertEqual(len(kicks), 2)

    def test_todo_duration_tracks_each_queued_ticket_independently(self):
        r = FakeRedis()
        _todo_agent(r, created="2026-08-09T13:55:00Z", ticket_id="ticket-1", title="old enough")
        _todo_agent(r, created="2026-08-09T13:58:00Z", ticket_id="ticket-2", title="too new", append=True)
        _lead(r)
        kicks, fake = _kicks()
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", fake)

        _watchdog(r).poll(now=NOW)

        self.assertEqual(len(kicks), 1)
        envelope = parse(r.lists[_key("architect", "ingress")][-1])
        self.assertIn("old enough", envelope["payload"]["text"])

    def test_todo_duration_drops_state_for_a_ticket_no_longer_in_todo(self):
        r = FakeRedis()
        _todo_agent(r)
        _lead(r)
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", lambda *a, **kw: None)
        watchdog = _watchdog(r)
        watchdog.poll(now=NOW)
        self.assertEqual(r.hashes[_key("sme-2", "todo.alerted")], {"ticket-1": "1"})

        r.lists[_key("sme-2", "tasks.todo")] = []  # taken, cancelled, or deleted
        watchdog.poll(now=NOW)
        self.assertEqual(r.hashes[_key("sme-2", "todo.alerted")], {})

    def test_todo_duration_does_nothing_without_a_configured_lead(self):
        r = FakeRedis()
        _todo_agent(r)
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", _no_kick())

        _watchdog(r).poll(now=NOW)

        self.assertNotIn(_key("architect", "ingress"), r.lists)

    # -- hold duration --------------------------------------------------------

    def test_hold_duration_messages_the_lead_directly_not_the_alerts_stream(self):
        r = FakeRedis()
        _hold_agent(r)
        _lead(r)
        kicks, fake = _kicks()
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", fake)

        _watchdog(r).poll(now=NOW)

        self.assertNotIn(prefix(POD, TENANT, resource="alerts"), r.streams)
        ingress = r.lists[_key("architect", "ingress")]
        self.assertEqual(len(ingress), 1)
        envelope = parse(ingress[0])
        self.assertEqual(envelope["l2"]["source"], "watchdog")
        self.assertEqual(envelope["l2"]["destination"], "architect")
        self.assertEqual(envelope["payload"]["text"], (
            '[alert from watchdog] sme-2 has had '
            '"wait on the vendor reply" on hold for 60 min'
        ))
        self.assertEqual(kicks, ["architect"])

    def test_hold_duration_does_not_fire_before_sixty_minutes(self):
        r = FakeRedis()
        _hold_agent(r, held="2026-08-09T13:01:00Z")  # 3540s old, under the 3600s default
        _lead(r)
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", _no_kick())

        _watchdog(r).poll(now=NOW)

        self.assertNotIn(_key("architect", "ingress"), r.lists)

    def test_hold_duration_falls_back_to_created_ts_when_held_ts_is_missing(self):
        """Same precedent as office/cli.py's own `_ticket_age` for `hold`."""
        r = FakeRedis()
        _hold_agent(r, held=None, created="2026-08-09T13:00:00Z")
        _lead(r)
        kicks, fake = _kicks()
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", fake)

        _watchdog(r).poll(now=NOW)

        self.assertEqual(len(kicks), 1)

    def test_hold_duration_does_not_repeat_within_the_same_threshold_crossing(self):
        r = FakeRedis()
        _hold_agent(r)
        _lead(r)
        kicks, fake = _kicks()
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", fake)
        watchdog = _watchdog(r)

        watchdog.poll(now=NOW)
        watchdog.poll(now=NOW)
        watchdog.poll(now=NOW + timedelta(seconds=60))

        self.assertEqual(len(kicks), 1)
        self.assertEqual(len(r.lists[_key("architect", "ingress")]), 1)

    def test_hold_duration_re_alerts_at_the_next_threshold_crossing(self):
        r = FakeRedis()
        _hold_agent(r)
        _lead(r)
        kicks, fake = _kicks()
        watchdog = _watchdog(r)

        self._set(service, "run_tmux", _quiet_windows(NOW))
        self._set(service, "deliver_tmux", fake)
        watchdog.poll(now=NOW)
        self.assertEqual(len(kicks), 1)

        later = NOW + timedelta(seconds=3600)
        self._set(service, "run_tmux", _quiet_windows(later))
        watchdog.poll(now=later)
        self.assertEqual(len(kicks), 2)
        envelope = parse(r.lists[_key("architect", "ingress")][-1])
        self.assertIn("120 min", envelope["payload"]["text"])

    def test_hold_duration_different_ticket_resets_and_re_alerts(self):
        r = FakeRedis()
        _hold_agent(r)
        _lead(r)
        kicks, fake = _kicks()
        watchdog = _watchdog(r)
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", fake)
        watchdog.poll(now=NOW)
        self.assertEqual(len(kicks), 1)

        _hold_agent(r, held="2026-08-09T13:00:00Z", ticket_id="ticket-2", title="wait on the other vendor")
        watchdog.poll(now=NOW)
        self.assertEqual(len(kicks), 2)

    def test_hold_duration_tracks_each_held_ticket_independently(self):
        r = FakeRedis()
        _hold_agent(r, held="2026-08-09T13:00:00Z", ticket_id="ticket-1", title="old enough")
        _hold_agent(r, held="2026-08-09T13:30:00Z", ticket_id="ticket-2", title="too new", append=True)
        _lead(r)
        kicks, fake = _kicks()
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", fake)

        _watchdog(r).poll(now=NOW)

        self.assertEqual(len(kicks), 1)
        envelope = parse(r.lists[_key("architect", "ingress")][-1])
        self.assertIn("old enough", envelope["payload"]["text"])

    def test_hold_duration_drops_state_for_a_ticket_no_longer_on_hold(self):
        r = FakeRedis()
        _hold_agent(r)
        _lead(r)
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", lambda *a, **kw: None)
        watchdog = _watchdog(r)
        watchdog.poll(now=NOW)
        self.assertEqual(r.hashes[_key("sme-2", "hold.alerted")], {"ticket-1": "1"})

        r.lists[_key("sme-2", "tasks.hold")] = []  # resumed, cancelled, or deleted
        watchdog.poll(now=NOW)
        self.assertEqual(r.hashes[_key("sme-2", "hold.alerted")], {})

    def test_hold_duration_does_nothing_without_a_configured_lead(self):
        r = FakeRedis()
        _hold_agent(r)
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", _no_kick())

        _watchdog(r).poll(now=NOW)

        self.assertNotIn(_key("architect", "ingress"), r.lists)

    # -- unreplied duration -----------------------------------------------------

    def test_unreplied_duration_messages_the_lead_directly_not_the_alerts_stream(self):
        r = FakeRedis()
        _unreplied_agent(r)  # 60s old, at the 60s default
        _lead(r)
        kicks, fake = _kicks()
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", fake)

        _watchdog(r).poll(now=NOW)

        self.assertNotIn(prefix(POD, TENANT, resource="alerts"), r.streams)
        ingress = r.lists[_key("architect", "ingress")]
        self.assertEqual(len(ingress), 1)
        envelope = parse(ingress[0])
        self.assertEqual(envelope["l2"]["source"], "watchdog")
        self.assertEqual(envelope["l2"]["destination"], "architect")
        self.assertEqual(envelope["payload"]["text"],
            "[alert from watchdog] sme-2 has 1 unanswered message from telegram, oldest 1 min old"
        )
        self.assertEqual(kicks, ["architect"])

    def test_unreplied_duration_pluralizes_the_count(self):
        r = FakeRedis()
        _unreplied_agent(r, count=3)
        _lead(r)
        kicks, fake = _kicks()
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", fake)

        _watchdog(r).poll(now=NOW)

        envelope = parse(r.lists[_key("architect", "ingress")][-1])
        self.assertIn("3 unanswered messages from telegram", envelope["payload"]["text"])

    def test_unreplied_duration_does_not_fire_before_one_minute(self):
        r = FakeRedis()
        _unreplied_agent(r, since="2026-08-09T13:59:30Z")  # 30s old, under the 60s default
        _lead(r)
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", _no_kick())

        _watchdog(r).poll(now=NOW)

        self.assertNotIn(_key("architect", "ingress"), r.lists)

    def test_unreplied_duration_does_not_repeat_before_the_backoff_doubles(self):
        r = FakeRedis()
        _unreplied_agent(r)  # 60s old at NOW
        _lead(r)
        kicks, fake = _kicks()
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", fake)
        watchdog = _watchdog(r)

        watchdog.poll(now=NOW)
        watchdog.poll(now=NOW)
        watchdog.poll(now=NOW + timedelta(seconds=30))  # 90s old, under the next 120s threshold

        self.assertEqual(len(kicks), 1)
        self.assertEqual(len(r.lists[_key("architect", "ingress")]), 1)

    def test_unreplied_duration_re_alerts_back_off_exponentially(self):
        """A quick miss still nags fast; a long one does not nag every minute."""
        r = FakeRedis()
        _unreplied_agent(r)  # 60s old at NOW
        _lead(r)
        kicks, fake = _kicks()
        watchdog = _watchdog(r)
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", fake)

        watchdog.poll(now=NOW)  # age 60s: first nag, next threshold 120s
        self.assertEqual(len(kicks), 1)

        watchdog.poll(now=NOW + timedelta(seconds=59))  # age 119s: still under 120s
        self.assertEqual(len(kicks), 1)

        watchdog.poll(now=NOW + timedelta(seconds=60))  # age 120s: second nag, next threshold 240s
        self.assertEqual(len(kicks), 2)
        envelope = parse(r.lists[_key("architect", "ingress")][-1])
        self.assertIn("2 min", envelope["payload"]["text"])

        watchdog.poll(now=NOW + timedelta(seconds=179))  # age 239s: still under 240s
        self.assertEqual(len(kicks), 2)

        watchdog.poll(now=NOW + timedelta(seconds=180))  # age 240s: third nag, next threshold 480s
        self.assertEqual(len(kicks), 3)
        envelope = parse(r.lists[_key("architect", "ingress")][-1])
        self.assertIn("4 min", envelope["payload"]["text"])

    def test_unreplied_duration_tracks_each_client_independently(self):
        r = FakeRedis()
        _unreplied_agent(r, client="telegram", since="2026-08-09T13:57:00Z")  # old enough
        _unreplied_agent(r, client="signal", since="2026-08-09T13:59:30Z")  # too new
        _lead(r)
        kicks, fake = _kicks()
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", fake)

        _watchdog(r).poll(now=NOW)

        self.assertEqual(len(kicks), 1)
        envelope = parse(r.lists[_key("architect", "ingress")][-1])
        self.assertIn("telegram", envelope["payload"]["text"])

    def test_unreplied_duration_drops_state_once_the_agent_replies(self):
        r = FakeRedis()
        _unreplied_agent(r)
        _lead(r)
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", lambda *a, **kw: None)
        watchdog = _watchdog(r)
        watchdog.poll(now=NOW)
        self.assertEqual(r.hashes[_key("sme-2", "unreplied.alerted")], {"telegram": "60"})

        del r.hashes[_key("sme-2", "unreplied")]["telegram"]  # sme-2 replied
        watchdog.poll(now=NOW)
        self.assertEqual(r.hashes[_key("sme-2", "unreplied.alerted")], {})

    def test_unreplied_duration_does_nothing_without_a_configured_lead(self):
        r = FakeRedis()
        _unreplied_agent(r)
        self._set(service, "run_tmux", _quiet_windows())
        self._set(service, "deliver_tmux", _no_kick())

        _watchdog(r).poll(now=NOW)

        self.assertNotIn(_key("architect", "ingress"), r.lists)

    # -- run_observers ----------------------------------------------------------

    def test_watchdog_observers_each_get_their_own_try(self):
        """One failing observer must not silence the others.

        In the switch all five used to share a single try, so a throw in the
        first skipped the rest of the pass, and the record named only the
        exception class -- from a five-job block, which was close to
        undiagnosable.
        """
        calls, errors = [], []

        class Boom:
            def poll(self, agents):
                calls.append("boom")
                raise RuntimeError("nope")

        class Fine:
            def __init__(self, name):
                self.name = name

            def poll(self, agents):
                calls.append(self.name)

        class Recorder:
            def _error(self, job, exc):
                errors.append((job, str(exc)))

        failed = run_observers(
            Recorder(),
            (("activity", Boom()), ("presence", Fine("presence")), ("verification", Fine("verify"))),
            {"sme-2"},
        )

        self.assertEqual(calls, ["boom", "presence", "verify"], "a throw must not skip the rest")
        self.assertEqual(failed, ["activity"])
        self.assertEqual(errors, [("activity", "nope")], "the failing job is named, not just its class")


class WatchdogCredentialTests(unittest.TestCase):
    def setUp(self):
        self._live_patches = []
        self.tmp_path = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp_path, ignore_errors=True)

    def tearDown(self):
        for p in self._live_patches:
            p.stop()

    def _set(self, target, name, value):
        p = patch.object(target, name, value)
        p.start()
        self._live_patches.append(p)

    def _delenv(self, name):
        p = patch.dict(os.environ)
        p.start()
        self._live_patches.append(p)
        os.environ.pop(name, None)

    def _setenv(self, name, value):
        p = patch.dict(os.environ)
        p.start()
        self._live_patches.append(p)
        os.environ[name] = value

    def test_credentials_warn_on_claude_refresh_expiry_and_codex_is_unknown(self):
        r = FakeRedis()
        self._delenv("CLAUDE_OAUTH_TOKEN_DEFAULT")
        r.values[_key("architect", "launch")] = "claude"
        r.values[_key("sme-2", "launch")] = "codex"
        claude = self.tmp_path / ".claude"
        claude.mkdir()
        (claude / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"refreshTokenExpiresAt": "2026-08-12T14:00:00Z"}})
        )
        codex = self.tmp_path / ".codex"
        codex.mkdir()
        (codex / "auth.json").write_text("{}")

        watchdog = Watchdog(r, pod=POD, tenant=TENANT, session_name=TENANT, home_root=self.tmp_path)
        out = _capture(lambda: watchdog.check_credentials(now=NOW))

        alerts = [json.loads(fields["alert"]) for _, fields in r.streams[prefix(POD, TENANT, resource="alerts")]]
        self.assertEqual([(alert["cli"], alert["status"]) for alert in alerts], [
            ("claude", "expiring"),
            ("codex", "unknown"),
        ])
        self.assertTrue(all(alert["account"] == "default" for alert in alerts))
        self.assertEqual(len(out.splitlines()), 2)

    def test_profile_codex_is_unknown_even_without_an_auth_file(self):
        r = FakeRedis()
        r.values[_key("sme-2", "launch")] = "codex"
        r.values[_key("sme-2", "profile")] = "work"
        (self.tmp_path / ".codex-work").mkdir()
        _capture(lambda: Watchdog(
            r, pod=POD, tenant=TENANT, session_name=TENANT, home_root=self.tmp_path
        ).check_credentials(now=NOW))
        alerts = [json.loads(fields["alert"]) for _, fields in r.streams[prefix(POD, TENANT, resource="alerts")]]
        self.assertTrue(any(
            alert["account"] == "work" and alert["cli"] == "codex" and alert["status"] == "absent"
            for alert in alerts
        ))

    def test_claude_profile_token_is_authenticated_without_credentials_file(self):
        r = FakeRedis()
        r.values[_key("architect", "launch")] = "claude"
        r.values[_key("architect", "profile")] = "work"
        self._setenv("CLAUDE_OAUTH_TOKEN_WORK", "token-authenticated")

        out = _capture(lambda: Watchdog(
            r, pod=POD, tenant=TENANT, session_name=TENANT, home_root=self.tmp_path
        ).check_credentials(now=NOW))

        self.assertNotIn(prefix(POD, TENANT, resource="alerts"), r.streams)
        self.assertEqual(r.hashes.get(prefix(POD, TENANT, resource="credential.alerted"), {}), {})
        self.assertEqual(out, "")

    def test_claude_without_token_or_credentials_still_alerts_absent(self):
        r = FakeRedis()
        r.values[_key("architect", "launch")] = "claude"
        self._delenv("CLAUDE_OAUTH_TOKEN_DEFAULT")

        _capture(lambda: Watchdog(
            r, pod=POD, tenant=TENANT, session_name=TENANT, home_root=self.tmp_path
        ).check_credentials(now=NOW))

        alert = json.loads(r.streams[prefix(POD, TENANT, resource="alerts")][0][1]["alert"])
        self.assertEqual(alert["status"], "absent")

    def test_provider_agent_needs_no_vendor_credential_and_clears_stale_status(self):
        r = FakeRedis()
        r.values[_key("architect", "launch")] = "claude"
        r.values[_key("architect", "provider")] = "local-vllm"
        alerted_key = prefix(POD, TENANT, resource="credential.alerted")
        r.hashes[alerted_key] = {"default:claude": "absent"}

        Watchdog(
            r, pod=POD, tenant=TENANT, session_name=TENANT, home_root=self.tmp_path
        ).check_credentials(now=NOW)

        self.assertNotIn(prefix(POD, TENANT, resource="alerts"), r.streams)
        self.assertEqual(r.hashes[alerted_key], {})

    def test_credential_alert_retracted_when_credential_recovers(self):
        """Build 105 Sec.1: when a credential recovers, watchdog emits status=present and clears alerted hash."""
        r = FakeRedis()
        r.values[_key("architect", "launch")] = "claude"
        self._delenv("CLAUDE_OAUTH_TOKEN_DEFAULT")

        watchdog = Watchdog(r, pod=POD, tenant=TENANT, session_name=TENANT, home_root=self.tmp_path)
        alerted_key = prefix(POD, TENANT, resource="credential.alerted")
        alerts_key = prefix(POD, TENANT, resource="alerts")

        _capture(lambda: watchdog.check_credentials(now=NOW))
        alerts = [json.loads(fields["alert"]) for _, fields in r.streams[alerts_key]]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["status"], "absent")
        self.assertEqual(alerts[0]["cli"], "claude")
        self.assertEqual(alerts[0]["account"], "default")
        self.assertEqual(r.hashes[alerted_key], {"default:claude": "absent"})

        claude = self.tmp_path / ".claude"
        claude.mkdir()
        (claude / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"refreshTokenExpiresAt": "2026-09-12T14:00:00Z"}})
        )

        _capture(lambda: watchdog.check_credentials(now=NOW))
        alerts = [json.loads(fields["alert"]) for _, fields in r.streams[alerts_key]]
        self.assertEqual(len(alerts), 2)
        self.assertEqual(alerts[1]["status"], "present")
        self.assertEqual(alerts[1]["cli"], "claude")
        self.assertEqual(alerts[1]["account"], "default")
        self.assertEqual(alerts[1]["expires_ts"], "2026-09-12T14:00:00.000Z")
        self.assertEqual(r.hashes.get(alerted_key, {}), {})

        _capture(lambda: watchdog.check_credentials(now=NOW))
        alerts = [json.loads(fields["alert"]) for _, fields in r.streams[alerts_key]]
        self.assertEqual(len(alerts), 2)
        self.assertEqual(r.hashes.get(alerted_key, {}), {})

    def test_credential_alert_retracted_from_expiring_when_token_refreshed(self):
        """Build 105 Sec.1: when an expiring credential is refreshed, watchdog emits status=present."""
        r = FakeRedis()
        self._delenv("CLAUDE_OAUTH_TOKEN_DEFAULT")
        r.values[_key("architect", "launch")] = "claude"
        claude = self.tmp_path / ".claude"
        claude.mkdir()
        (claude / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"refreshTokenExpiresAt": "2026-08-12T14:00:00Z"}})
        )

        watchdog = Watchdog(r, pod=POD, tenant=TENANT, session_name=TENANT, home_root=self.tmp_path)
        alerted_key = prefix(POD, TENANT, resource="credential.alerted")
        alerts_key = prefix(POD, TENANT, resource="alerts")

        _capture(lambda: watchdog.check_credentials(now=NOW))
        alerts = [json.loads(fields["alert"]) for _, fields in r.streams[alerts_key]]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["status"], "expiring")
        self.assertEqual(r.hashes[alerted_key], {"default:claude": "expiring"})

        (claude / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"refreshTokenExpiresAt": "2026-09-09T14:00:00Z"}})
        )
        _capture(lambda: watchdog.check_credentials(now=NOW))
        alerts = [json.loads(fields["alert"]) for _, fields in r.streams[alerts_key]]
        self.assertEqual(len(alerts), 2)
        self.assertEqual(alerts[1]["status"], "present")
        self.assertEqual(alerts[1]["expires_ts"], "2026-09-09T14:00:00.000Z")
        self.assertEqual(r.hashes.get(alerted_key, {}), {})

        _capture(lambda: watchdog.check_credentials(now=NOW))
        self.assertEqual(len(r.streams[alerts_key]), 2)

    def test_agy_is_unknown_because_its_expiry_is_an_access_token(self):
        """Only claude records a refresh-token expiry.

        agy's `token.expiry` tracks its ACCESS token. Measured: the same file
        read hours apart showed the value moved forward while the login
        stayed valid -- the CLI refreshes it itself. Alerting on it fires
        constantly and correctly, which is the cry-wolf failure the
        credential check exists to avoid.
        """
        agy = self.tmp_path / ".gemini" / "antigravity-cli"
        agy.mkdir(parents=True)
        (agy / "antigravity-oauth-token").write_text(
            json.dumps({"token": {"access_token": "x", "refresh_token": "y",
                                  "expiry": "2020-01-01T00:00:00Z"}})
        )

        r = FakeRedis()
        r.values[_key("architect", "launch")] = "agy"
        _capture(lambda: Watchdog(
            r, pod=POD, tenant=TENANT, session_name=TENANT, home_root=self.tmp_path
        ).check_credentials(now=NOW))

        alerts = [json.loads(f["alert"]) for _, f in r.streams.get(prefix(POD, TENANT, resource="alerts"), [])]
        agy_alerts = [a for a in alerts if a.get("cli") == "agy"]
        self.assertTrue(agy_alerts, "agy should still be reported")
        self.assertEqual(agy_alerts[0]["status"], "unknown", "never 'expiring' from an access token")

    def test_missing_credentials_alert_once_per_account_in_use_and_clear_on_reseed(self):
        r = FakeRedis()
        r.values[_key("architect", "launch")] = "claude"
        r.values[_key("architect", "profile")] = "work"
        r.values[_key("sme-2", "launch")] = "claude"
        r.values[_key("sme-2", "profile")] = "work"
        watchdog = Watchdog(r, pod=POD, tenant=TENANT, session_name=TENANT, home_root=self.tmp_path)

        watchdog.check_credentials(now=NOW)
        watchdog.check_credentials(now=NOW)
        alerts_key = prefix(POD, TENANT, resource="alerts")
        alerts = [json.loads(fields["alert"]) for _, fields in r.streams[alerts_key]]
        self.assertEqual([(alert["account"], alert["cli"], alert["status"]) for alert in alerts], [
            ("work", "claude", "absent")
        ])

        credentials = self.tmp_path / ".claude-work" / ".credentials.json"
        credentials.parent.mkdir()
        credentials.write_text(
            json.dumps({"claudeAiOauth": {"refreshTokenExpiresAt": "2026-09-12T14:00:00Z"}})
        )
        watchdog.check_credentials(now=NOW)
        watchdog.check_credentials(now=NOW)
        alerts = [json.loads(fields["alert"]) for _, fields in r.streams[alerts_key]]
        self.assertEqual([(alert["account"], alert["cli"], alert["status"]) for alert in alerts], [
            ("work", "claude", "absent"),
            ("work", "claude", "present"),
        ])
        self.assertEqual(r.hashes[prefix(POD, TENANT, resource="credential.alerted")], {})

    def test_missing_credentials_alert_for_each_cli_account_in_use(self):
        r = FakeRedis()
        self._delenv("CLAUDE_OAUTH_TOKEN_DEFAULT")
        r.hashes[prefix(POD, TENANT, resource="registry")]["sme-3"] = "tmux"
        r.values[_key("architect", "launch")] = "claude"
        r.values[_key("sme-2", "launch")] = "codex"
        r.values[_key("sme-3", "launch")] = "agy"

        Watchdog(
            r, pod=POD, tenant=TENANT, session_name=TENANT, home_root=self.tmp_path
        ).check_credentials(now=NOW)

        alerts = [
            json.loads(fields["alert"])
            for _, fields in r.streams[prefix(POD, TENANT, resource="alerts")]
        ]
        self.assertEqual({(alert["cli"], alert["status"]) for alert in alerts}, {
            ("agy", "absent"),
            ("claude", "absent"),
            ("codex", "absent"),
        })

    def test_unused_profile_directory_does_not_alert(self):
        r = FakeRedis()
        r.values[_key("architect", "launch")] = "claude"
        default = self.tmp_path / ".claude"
        default.mkdir()
        (default / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"refreshTokenExpiresAt": "2026-09-12T14:00:00Z"}})
        )
        (self.tmp_path / ".claude-unused").mkdir()

        Watchdog(
            r, pod=POD, tenant=TENANT, session_name=TENANT, home_root=self.tmp_path
        ).check_credentials(now=NOW)

        self.assertNotIn(prefix(POD, TENANT, resource="alerts"), r.streams)


class WatchdogFailureIsolationTests(unittest.TestCase):
    def setUp(self):
        self._live_patches = []

    def tearDown(self):
        for p in self._live_patches:
            p.stop()

    def _set(self, target, name, value):
        p = patch.object(target, name, value)
        p.start()
        self._live_patches.append(p)

    def test_stall_failure_does_not_disable_blocked_check(self):
        r = FakeRedis()
        r.hashes[_key("sme-2", "blocked")] = {
            "since": "2026-08-09T13:53:00Z",
            "stream_id": "delivery-1",
        }
        watchdog = _watchdog(r)
        watchdog._window_activity = lambda: {}
        watchdog._check_stalls = lambda *args: (_ for _ in ()).throw(RuntimeError("bad board"))

        out = _capture(lambda: watchdog.poll(now=NOW))

        output = [json.loads(line) for line in out.splitlines()]
        self.assertEqual(output[0], {
            "module": "watchdog",
            "event": "error",
            "writer": "watchdog",
            "job": "stalls",
            "reason": "RuntimeError: bad board",
        })
        self.assertTrue(any(record.get("kind") == "blocked" for record in output))


class WatchdogMainLoopTests(unittest.TestCase):
    def setUp(self):
        self._live_patches = []

    def tearDown(self):
        for p in self._live_patches:
            p.stop()

    def _set(self, target, name, value):
        p = patch.object(target, name, value)
        p.start()
        self._live_patches.append(p)

    def _env(self, **values):
        p = patch.dict(os.environ, values)
        p.start()
        self._live_patches.append(p)

    def _delenv(self, name):
        p = patch.dict(os.environ)
        p.start()
        self._live_patches.append(p)
        os.environ.pop(name, None)

    def test_observation_failure_does_not_disable_due_credential_check(self):
        calls = []

        class FailingWatchdog:
            def __init__(self, *args, **kwargs):
                pass

            def poll(self):
                calls.append("poll")
                raise RuntimeError("bad observations")

            def check_credentials(self):
                calls.append("credentials")

            def _agents(self):
                return set()

            _error = staticmethod(Watchdog._error)

        self._delenv("WATCHDOG_ENABLED")
        self._env(WATCHDOG_INTERVAL="0", REDIS_URL="redis://unused", POD=POD, TENANT=TENANT)
        self._set(service, "Watchdog", FailingWatchdog)
        self._set(service.redis.Redis, "from_url", lambda url: object())
        self._set(service.time, "monotonic", lambda: 0)

        def _boom_sleep(interval):
            raise StopIteration

        self._set(service.time, "sleep", _boom_sleep)

        with self.assertRaises(StopIteration):
            _capture_bytes = io.StringIO()
            with contextlib.redirect_stdout(_capture_bytes):
                service.main()
        out = _capture_bytes.getvalue()

        self.assertEqual(calls, ["poll", "credentials"])
        error = json.loads(out)
        self.assertEqual(error["job"], "observations")
        self.assertEqual(error["reason"], "RuntimeError: bad observations")

    def test_disabled_alerting_still_connects_because_observers_need_redis(self):
        """WATCHDOG_ENABLED=0 must still connect: the observers live in this
        process and read Redis, so exiting early would silence telemetry
        rather than alerts. The connection is the evidence that they still
        run even with alerting off.
        """
        connected = []
        self._env(WATCHDOG_ENABLED="0", REDIS_URL="redis://unused", POD=POD, TENANT=TENANT)
        self._set(service.redis.Redis, "from_url", lambda url: connected.append(url) or object())

        def _boom_sleep(s):
            raise StopIteration

        self._set(service.time, "sleep", _boom_sleep)
        self._set(service.time, "monotonic", lambda: 0)
        with self.assertRaises(StopIteration):
            service.main()
        self.assertTrue(connected, "observers need Redis even with alerting off")

    def test_alerting_disabled_still_runs_the_observers(self):
        """WATCHDOG_ENABLED silences ALERTS, not telemetry.

        Returning early would also stop ActivityTailer, PresenceSampler and
        DeliveryVerifier -- presence would read `unknown` forever, the
        activity stream would stay empty, and a client's progress indicator
        would break.
        """
        polled = []

        class Observer:
            def __init__(self, name):
                self.name = name

            def poll(self, agents):
                polled.append(self.name)
                raise StopIteration  # one pass, then out of the loop

        class QuietWatchdog:
            def __init__(self, *a, **kw):
                pass

            def poll(self):
                polled.append("ALERT")  # must never appear

            def check_credentials(self):
                polled.append("CREDENTIALS")

            def _agents(self):
                return {"sme-2"}

            _error = staticmethod(Watchdog._error)

        self._env(WATCHDOG_ENABLED="0", REDIS_URL="redis://unused", POD=POD, TENANT=TENANT)
        self._set(service, "Watchdog", QuietWatchdog)
        self._set(service.redis.Redis, "from_url", lambda url: object())
        self._set(service, "ActivityTailer", lambda *a, **kw: Observer("activity"))
        self._set(service, "PresenceSampler", lambda *a, **kw: Observer("presence"))
        self._set(service, "DeliveryVerifier", lambda *a, **kw: Observer("verify"))
        self._set(service.time, "monotonic", lambda: 0)

        def _boom_sleep(s):
            raise StopIteration

        self._set(service.time, "sleep", _boom_sleep)

        buf = io.StringIO()
        with self.assertRaises(StopIteration):
            with contextlib.redirect_stdout(buf):
                service.main()
        out = buf.getvalue()

        self.assertIn("activity", polled, "observers must run with alerting off")
        self.assertNotIn("ALERT", polled, "alerting must be silent")
        self.assertNotIn("CREDENTIALS", polled)
        self.assertIn('"event":"alerting_disabled"', out)


if __name__ == "__main__":
    unittest.main()
