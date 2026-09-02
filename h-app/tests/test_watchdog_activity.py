import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

H_APP = Path(__file__).resolve().parents[1]
if str(H_APP) not in sys.path:
    sys.path.insert(0, str(H_APP))

from core.keys import prefix
from lib.paths import get_agent_workdir
from modules.watchdog.activity import ActivityTailer


POD = "acme"
TENANT = "hq"


class FakeRedis:
    def __init__(self, agents=("sme-2",), fail_eval=False):
        self.values = {}
        self.hashes = defaultdict(dict)
        self.streams = defaultdict(list)
        self.fail_eval = fail_eval
        registry_key = prefix(POD, TENANT, resource="registry")
        self.hashes[registry_key] = {agent: "tmux" for agent in agents}

    def eval(self, script, numkeys, *args):
        # Only ever configured to fail in these tests -- stands in for a
        # real preflight rejection (see the real-Redis verification for the
        # actual WRONGTYPE repro; this just exercises the Python-side
        # exception handling around the eval() call).
        if self.fail_eval:
            raise Exception("simulated: wrong type for attributed_key")
        raise NotImplementedError("this FakeRedis only simulates eval failure")

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value

    def hkeys(self, key):
        return list(self.hashes.get(key, {}).keys())

    def xadd(self, key, fields, maxlen=None, approximate=True):
        stream = self.streams[key]
        stream.append((f"{len(stream) + 1}-0", dict(fields)))
        return stream[-1][0]

    def xrange(self, key, min="-", max="+", count=None):
        return list(self.streams.get(key, []))

    def sismember(self, key, member):
        return member in self.hashes.get(key, {})


def _events(r, agent="sme-2"):
    key = prefix(POD, TENANT, agent, "activity")
    return [json.loads(entry[1]["event"]) for entry in r.streams.get(key, [])]


def _wd(agent: str) -> str:
    """The real cwd/workspace attribution logic (activity.py) now compares
    against get_agent_workdir(), not a hardcoded literal -- build fixture
    values through the same function so these tests match whatever the
    ambient environment actually resolves it to, in any environment."""
    return get_agent_workdir(agent)


def _write_lines(path: Path, records: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records))


def _history_path(tmp_path: Path) -> Path:
    return tmp_path / ".gemini" / "antigravity-cli" / "history.jsonl"


def _ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp() * 1000)


class ActivityTailerTests(unittest.TestCase):
    def setUp(self):
        self.tmp_path = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp_path, ignore_errors=True)

    def test_claude_tailer_reads_only_new_bytes_and_never_emits_content(self):
        r = FakeRedis()
        session = self.tmp_path / ".claude" / "projects" / "-workdir-sme-2" / "one.jsonl"
        _write_lines(
            session,
            [
                {"type": "user", "timestamp": "2026-08-09T10:00:00Z", "message": "private prompt"},
                {
                    "type": "assistant",
                    "timestamp": "2026-08-09T10:00:01Z",
                    "message": {
                        "content": [
                            {"type": "text", "text": "private response"},
                            {
                                "type": "tool_use",
                                "name": "Bash",
                                "input": {"command": "cat /workdir/sme-2/secrets.env"},
                            },
                        ]
                    },
                },
                {
                    "type": "user",
                    "message": {"content": [{"type": "tool_result", "content": "private tool output"}]},
                },
            ],
        )
        tailer = ActivityTailer(r, pod=POD, tenant=TENANT, home_root=self.tmp_path)

        tailer.poll()
        self.assertEqual(_events(r), [
            {"v": 1, "agent": "sme-2", "ts": "2026-08-09T10:00:00Z", "kind": "input"},
            {"v": 1, "agent": "sme-2", "ts": "2026-08-09T10:00:01Z", "kind": "output"},
            {"v": 1, "agent": "sme-2", "ts": "2026-08-09T10:00:01Z", "kind": "tool", "tool": "Bash"},
        ])
        serialized = json.dumps(r.streams)
        for secret in ("private prompt", "private response", "private tool output", "cat ", "secrets.env", "/workdir"):
            self.assertNotIn(secret, serialized)

        offset_key = prefix(POD, TENANT, "sme-2", "activity.offset")
        self.assertEqual(
            json.loads(r.values[offset_key])["offsets"][str(session)], session.stat().st_size
        )
        tailer.poll()
        self.assertEqual(len(_events(r)), 3)

        with session.open("a") as output:
            output.write('{"type":"assistant","message":{"content":[{"type":"tool_use",')
        tailer.poll()
        self.assertEqual(len(_events(r)), 3)

        with session.open("a") as output:
            output.write('"name":"Read","input":{"file_path":"/private"}}]}}\n')
        tailer.poll()
        self.assertEqual(_events(r)[-1]["kind"], "tool")
        self.assertEqual(_events(r)[-1]["tool"], "Read")
        self.assertNotIn("/private", json.dumps(r.streams))

    def test_newest_session_starts_at_zero_instead_of_reusing_old_offset(self):
        r = FakeRedis()
        directory = self.tmp_path / ".claude" / "projects" / "-workdir-sme-2"
        old = directory / "old.jsonl"
        new = directory / "new.jsonl"
        _write_lines(old, [{"type": "user", "timestamp": "old"}])
        old.touch()
        tailer = ActivityTailer(r, pod=POD, tenant=TENANT, home_root=self.tmp_path)
        tailer.poll()

        _write_lines(new, [{"type": "assistant", "timestamp": "new", "message": {"content": "answer"}}])
        new.touch()
        tailer.poll()

        self.assertEqual([event["kind"] for event in _events(r)], ["input", "output"])
        state = json.loads(r.values[prefix(POD, TENANT, "sme-2", "activity.offset")])
        self.assertEqual(state, {
            "offsets": {
                str(old): old.stat().st_size,
                str(new): new.stat().st_size,
            }
        })

    def test_switching_back_to_prior_session_resumes_its_saved_offset(self):
        r = FakeRedis()
        directory = self.tmp_path / ".claude" / "projects" / "-workdir-sme-2"
        old = directory / "old.jsonl"
        new = directory / "new.jsonl"
        _write_lines(old, [{"type": "user", "timestamp": "old-first"}])
        tailer = ActivityTailer(r, pod=POD, tenant=TENANT, home_root=self.tmp_path)
        tailer.poll()

        _write_lines(new, [{"type": "assistant", "timestamp": "new", "message": {"content": "answer"}}])
        tailer.poll()

        with old.open("a") as output:
            output.write(json.dumps({"type": "user", "timestamp": "old-second"}) + "\n")
        old.touch()
        tailer.poll()

        self.assertEqual([event["ts"] for event in _events(r)], ["old-first", "new", "old-second"])

    def test_activity_offset_migrates_original_single_path_shape(self):
        r = FakeRedis()
        session = self.tmp_path / ".claude" / "projects" / "-workdir-sme-2" / "one.jsonl"
        first = json.dumps({"type": "user", "timestamp": "already-read"}) + "\n"
        second = json.dumps({"type": "user", "timestamp": "new"}) + "\n"
        session.parent.mkdir(parents=True, exist_ok=True)
        session.write_text(first + second)
        r.values[prefix(POD, TENANT, "sme-2", "activity.offset")] = json.dumps(
            {"path": str(session), "offset": len(first.encode())}
        )

        ActivityTailer(r, pod=POD, tenant=TENANT, home_root=self.tmp_path).poll()

        self.assertEqual([event["ts"] for event in _events(r)], ["new"])
        state = json.loads(r.values[prefix(POD, TENANT, "sme-2", "activity.offset")])
        self.assertEqual(state, {"offsets": {str(session): session.stat().st_size}})

    def test_codex_profile_session_reduces_messages_and_tool_calls(self):
        r = FakeRedis()
        r.values[prefix(POD, TENANT, "sme-2", "profile")] = "work"
        session = self.tmp_path / ".codex-work" / "sessions" / "2026" / "08" / "rollout-one.jsonl"
        _write_lines(
            session,
            [
                {"type": "session_meta", "payload": {"cwd": _wd("sme-2")}},
                {"type": "event_msg", "timestamp": "one", "payload": {"type": "user_message", "message": "secret"}},
                {"type": "event_msg", "timestamp": "two", "payload": {"type": "agent_message", "message": "secret"}},
                {
                    "type": "response_item",
                    "timestamp": "three",
                    "payload": {"type": "function_call", "name": "exec_command", "arguments": "private args"},
                },
            ],
        )

        ActivityTailer(r, pod=POD, tenant=TENANT, home_root=self.tmp_path).poll()

        self.assertEqual([event["kind"] for event in _events(r)], ["input", "output", "tool"])
        self.assertEqual(_events(r)[-1]["tool"], "exec_command")
        self.assertNotIn("secret", json.dumps(r.streams))
        self.assertNotIn("private args", json.dumps(r.streams))

    def test_codex_shared_account_attributes_each_session_by_workspace(self):
        r = FakeRedis(agents=("frontend", "backend"))
        shared = self.tmp_path / ".codex" / "sessions" / "2026" / "08"
        _write_lines(
            shared / "rollout-frontend.jsonl",
            [
                {"type": "session_meta", "payload": {"cwd": _wd("frontend")}},
                {"type": "event_msg", "timestamp": "front", "payload": {"type": "user_message"}},
            ],
        )
        _write_lines(
            shared / "rollout-backend.jsonl",
            [
                {"type": "session_meta", "payload": {"cwd": _wd("backend")}},
                {"type": "event_msg", "timestamp": "back", "payload": {"type": "agent_message"}},
            ],
        )
        r.values[prefix(POD, TENANT, "frontend", "launch")] = "codex"
        r.values[prefix(POD, TENANT, "backend", "launch")] = "codex"

        ActivityTailer(r, pod=POD, tenant=TENANT, home_root=self.tmp_path).poll()

        self.assertEqual(_events(r, "frontend"), [
            {"v": 1, "agent": "frontend", "ts": "front", "kind": "input"}
        ])
        self.assertEqual(_events(r, "backend"), [
            {"v": 1, "agent": "backend", "ts": "back", "kind": "output"}
        ])

    def test_agy_agent_has_empty_stream_even_when_an_old_claude_session_exists(self):
        r = FakeRedis()
        r.values[prefix(POD, TENANT, "sme-2", "launch")] = "agy"
        stale = self.tmp_path / ".claude" / "projects" / "-workdir-sme-2" / "stale.jsonl"
        _write_lines(stale, [{"type": "user", "message": "must not appear"}])
        ActivityTailer(r, pod=POD, tenant=TENANT, home_root=self.tmp_path).poll()
        self.assertEqual(_events(r), [])
        self.assertNotIn(prefix(POD, TENANT, "sme-2", "activity.offset"), r.values)

    def test_agy_reads_history_jsonl_filtered_by_workspace(self):
        """One shared file, two agy agents -- each sees only its own lines."""
        r = FakeRedis(agents=("frontend", "backend"))
        r.values[prefix(POD, TENANT, "frontend", "launch")] = "agy"
        r.values[prefix(POD, TENANT, "backend", "launch")] = "agy"
        _write_lines(
            _history_path(self.tmp_path),
            [
                {"display": "hi", "timestamp": _ms("2026-08-09T10:00:00"), "workspace": _wd("frontend"), "conversationId": "a"},
                {"display": "hi", "timestamp": _ms("2026-08-09T10:00:01"), "workspace": _wd("backend"), "conversationId": "b"},
                {"display": "/model", "timestamp": _ms("2026-08-09T10:00:02"), "workspace": _wd("frontend"), "type": "slash_command"},
                {"display": "not ours", "timestamp": _ms("2026-08-09T10:00:03"), "workspace": _wd("someone-else")},
            ],
        )

        ActivityTailer(r, pod=POD, tenant=TENANT, home_root=self.tmp_path).poll()

        self.assertEqual(_events(r, "frontend"), [
            {"v": 1, "agent": "frontend", "ts": "2026-08-09T10:00:00.000Z", "kind": "input"},
            {"v": 1, "agent": "frontend", "ts": "2026-08-09T10:00:02.000Z", "kind": "input"},
        ])
        self.assertEqual(_events(r, "backend"), [
            {"v": 1, "agent": "backend", "ts": "2026-08-09T10:00:01.000Z", "kind": "input"}
        ])
        # Privacy: the submitted text itself never rides into the reduced stream.
        self.assertNotIn("hi", json.dumps(r.streams))
        self.assertNotIn("not ours", json.dumps(r.streams))

    def test_agy_emits_no_usage_records(self):
        """No token/cost source exists for agy -- history.jsonl carries none."""
        r = FakeRedis()
        r.values[prefix(POD, TENANT, "sme-2", "launch")] = "agy"
        _write_lines(
            _history_path(self.tmp_path),
            [{"display": "hi", "timestamp": _ms("2026-08-09T10:00:00"), "workspace": _wd("sme-2")}],
        )
        ActivityTailer(r, pod=POD, tenant=TENANT, home_root=self.tmp_path).poll()
        self.assertNotIn(prefix(POD, TENANT, resource="usage"), r.streams)

    def test_usage_emit_failure_is_logged_not_swallowed(self):
        """A rejected eval() (preflight or otherwise) used to vanish with a
        bare `except: return` -- a systemic problem (e.g. something writing
        the wrong type to one of the usage keys) could silently stop all
        usage tracking for an agent forever, with nothing to find it by.
        Must be observable."""
        r = FakeRedis(fail_eval=True)
        session = self.tmp_path / ".claude" / "projects" / "-workdir-sme-2" / "one.jsonl"
        _write_lines(session, [
            {
                "type": "assistant",
                "timestamp": "2026-08-09T10:00:01Z",
                "message": {
                    "id": "req-1",
                    "model": "claude-x",
                    "content": [{"type": "text", "text": "hi"}],
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            },
        ])

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ActivityTailer(r, pod=POD, tenant=TENANT, home_root=self.tmp_path).poll()

        lines = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
        failures = [line for line in lines if line.get("event") == "usage_emit_failed"]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["destination"], "sme-2")
        self.assertIn("wrong type for attributed_key", failures[0]["reason"])
        # The activity event itself (privacy-reduced, no usage) still
        # emits independently -- a usage-emission failure must not swallow
        # the rest of the pass.
        self.assertEqual(
            [event["kind"] for event in _events(r)],
            ["output"],
        )

    def test_agy_shared_file_keeps_independent_offsets_per_agent(self):
        r = FakeRedis(agents=("frontend", "backend"))
        r.values[prefix(POD, TENANT, "frontend", "launch")] = "agy"
        r.values[prefix(POD, TENANT, "backend", "launch")] = "agy"
        history = _history_path(self.tmp_path)
        _write_lines(
            history,
            [{"display": "hi", "timestamp": _ms("2026-08-09T10:00:00"), "workspace": _wd("frontend")}],
        )
        tailer = ActivityTailer(r, pod=POD, tenant=TENANT, home_root=self.tmp_path)
        tailer.poll()

        with history.open("a") as output:
            output.write(json.dumps({"display": "hi again", "timestamp": _ms("2026-08-09T10:00:05"), "workspace": _wd("backend")}) + "\n")
        tailer.poll()

        self.assertEqual(len(_events(r, "frontend")), 1)
        self.assertEqual(len(_events(r, "backend")), 1)
        frontend_offset = json.loads(r.values[prefix(POD, TENANT, "frontend", "activity.offset")])["offsets"][str(history)]
        backend_offset = json.loads(r.values[prefix(POD, TENANT, "backend", "activity.offset")])["offsets"][str(history)]
        self.assertEqual(frontend_offset, history.stat().st_size)
        self.assertEqual(backend_offset, history.stat().st_size)

    def test_agy_ignores_a_line_with_no_matching_workspace(self):
        r = FakeRedis()
        r.values[prefix(POD, TENANT, "sme-2", "launch")] = "agy"
        _write_lines(
            _history_path(self.tmp_path),
            [
                {"display": "welcome", "timestamp": _ms("2026-08-09T10:00:00")},  # no workspace at all yet
                {"display": "hi", "timestamp": _ms("2026-08-09T10:00:01"), "workspace": _wd("sme-2")},
            ],
        )
        ActivityTailer(r, pod=POD, tenant=TENANT, home_root=self.tmp_path).poll()
        self.assertEqual(_events(r), [
            {"v": 1, "agent": "sme-2", "ts": "2026-08-09T10:00:01.000Z", "kind": "input"}
        ])


if __name__ == "__main__":
    unittest.main()
