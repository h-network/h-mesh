import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from unittest.mock import MagicMock


H_APP = Path(__file__).resolve().parents[1]
if str(H_APP) not in sys.path:
    sys.path.insert(0, str(H_APP))

from core.config import state_dir, state_path
from core.registry import is_member, members, port_type
from core.service import Switch, main, transmission
from core.windowlog import WindowLogTailer


class RegistryRedis:
    def __init__(self):
        self.calls = []

    def hkeys(self, key):
        self.calls.append(("hkeys", key))
        return [b"alice"]

    def hexists(self, key, agent):
        self.calls.append(("hexists", key, agent))
        return 1

    def hget(self, key, agent):
        self.calls.append(("hget", key, agent))
        return b"tmux"


class CoreAdaptationTests(unittest.TestCase):
    def test_tree_contains_no_old_project_names(self):
        banned = (
            "f" + "lock",
            "f" + "lock_",
            "f" + "lockclient",
            "h" + "f" + "lock_",
            "h" + "f" + "lock_session",
        )
        allowed = {
            (Path("tests/test_session.py"), 371),
            (Path("tests/test_session.py"), 372),
        }
        checked = 0
        violations = []
        excluded_dirs = {".git", ".pytest_cache", "__pycache__"}

        for path in H_APP.rglob("*"):
            if (
                not path.is_file()
                or any(part in excluded_dirs or part.endswith(".egg-info") for part in path.parts)
            ):
                continue
            relative = path.relative_to(H_APP)
            checked += 1
            for line_number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if (relative, line_number) in allowed:
                    continue
                folded = line.casefold()
                matches = [name for name in banned if name in folded]
                if matches:
                    violations.append(f"{relative}:{line_number}: {', '.join(matches)}")

        self.assertGreater(checked, 100, "Expected to scan the complete h-app tree")
        self.assertEqual(violations, [], "Old project names found:\n" + "\n".join(violations))

    def test_registry_uses_registry_wire_resource(self):
        client = RegistryRedis()
        self.assertEqual(members(client, pod="mesh", tenant="office"), {"alice"})
        self.assertTrue(is_member(client, pod="mesh", tenant="office", agent="alice"))
        self.assertEqual(port_type(client, pod="mesh", tenant="office", agent="alice"), "tmux")
        self.assertTrue(
            all(call[1] == "pod:mesh:tenant:office:registry" for call in client.calls)
        )

    def test_switch_invokes_injected_kick(self):
        calls = []
        switch = Switch(object(), pod="mesh", tenant="office", kick=lambda agent, port_type, env: calls.append((agent, port_type, env)))
        envelope = {"stream_id": "stream", "l2": {"source": "alice"}}
        with patch("core.service._log_observation") as log:
            switch._kick("bob", "tmux", envelope)
        self.assertEqual(calls, [("bob", "tmux", envelope)])
        self.assertEqual(log.call_args.args, ("kick_started",))

    def test_switch_records_callback_failure_without_raising(self):
        def fail(agent, port_type, envelope):
            raise RuntimeError("edge unavailable")

        switch = Switch(object(), pod="mesh", tenant="office", kick=fail)
        envelope = {"stream_id": "stream", "l2": {"source": "alice"}}
        with patch("core.service._log_observation") as log:
            switch._kick("bob", "tmux", envelope)
        self.assertEqual(log.call_args.args, ("kick_unknown",))
        self.assertIn("edge unavailable", log.call_args.kwargs["reason"])

    def test_switch_without_kick_records_deferred(self):
        switch = Switch(object(), pod="mesh", tenant="office")
        envelope = {"stream_id": "stream", "l2": {"source": "alice"}}
        with patch("core.service._log_observation") as log:
            switch._kick("bob", "tmux", envelope)
        self.assertEqual(log.call_args.args, ("kick_deferred",))
        self.assertEqual(log.call_args.kwargs["destination"], "bob")

    def test_broadcast_without_resolved_type_defers_before_callback(self):
        kick = MagicMock()
        switch = Switch(object(), pod="mesh", tenant="office", kick=kick)
        envelope = {"stream_id": "stream", "l2": {"source": "alice"}}
        with patch("core.service._log_observation") as log:
            switch._kick("bob", None, envelope)
        kick.assert_not_called()
        self.assertEqual(log.call_args.args, ("kick_deferred",))
        self.assertIn("broadcast port_type is unresolved", log.call_args.kwargs["reason"])

    def test_transmission_spawns_module_port_without_envelope_in_argv(self):
        envelope = {"stream_id": "secret", "payload": {"text": "not argv"}}
        with patch("core.service.subprocess.Popen") as popen:
            transmission("bob", "tmux", envelope)
        popen.assert_called_once_with(
            [sys.executable, "-m", "modules.tmux.port", "bob"]
        )
        self.assertNotIn("secret", popen.call_args.args[0])

    def test_transmission_rejects_unresolved_port_type(self):
        with patch("core.service.subprocess.Popen") as popen:
            with self.assertRaisesRegex(ValueError, "resolved port_type"):
                transmission("bob", None, {})
        popen.assert_not_called()

    def test_main_installs_auto_reap_and_wires_production_transmission(self):
        events = []
        switch = MagicMock(pod="mesh", tenant="office")
        switch.run.side_effect = lambda **kwargs: events.append("run")
        with (
            patch.dict(
                os.environ,
                {"REDIS_URL": "redis://example", "POD": "mesh", "TENANT": "office"},
                clear=True,
            ),
            patch("core.service.signal.signal", side_effect=lambda *args: events.append("signal")) as set_signal,
            patch("core.service.redis.Redis.from_url", return_value=object()),
            patch("core.service.Switch", return_value=switch) as switch_class,
            patch("core.service.WindowLogTailer", return_value=object()),
            patch("core.service.RetentionTrimmer", return_value=object()),
        ):
            main()
        set_signal.assert_called_once_with(signal.SIGCHLD, signal.SIG_IGN)
        self.assertIs(switch_class.call_args.kwargs["kick"], transmission)
        self.assertEqual(events, ["signal", "run"])

    def test_state_paths_follow_configured_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"H_MESH_STATE_DIR": directory}):
                self.assertEqual(state_dir(), Path(directory))
                self.assertEqual(state_path("tasks.jsonl"), Path(directory) / "tasks.jsonl")
                tailer = WindowLogTailer(object(), pod="mesh", tenant="office")
                self.assertEqual(tailer.path, Path(directory) / "window.log.jsonl")


if __name__ == "__main__":
    unittest.main()
