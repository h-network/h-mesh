import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


H_APP = Path(__file__).resolve().parents[1]
if str(H_APP) not in sys.path:
    sys.path.insert(0, str(H_APP))

from core.config import state_dir, state_path
from core.registry import is_member, members, port_type
from core.service import Switch
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
    def test_core_contains_no_old_project_names(self):
        core = H_APP / "core"
        checked = [*core.glob("*.py"), *core.glob("*.md")]
        for path in checked:
            text = path.read_text(encoding="utf-8").casefold()
            with self.subTest(path=path.name):
                self.assertNotIn("flock", text)
                self.assertNotIn("roster", text)

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
        switch = Switch(object(), pod="mesh", tenant="office", kick=lambda agent, env: calls.append((agent, env)))
        envelope = {"stream_id": "stream", "l2": {"source": "alice"}}
        with patch("core.service._log_observation") as log:
            switch._kick("bob", envelope)
        self.assertEqual(calls, [("bob", envelope)])
        self.assertEqual(log.call_args.args, ("kick_started",))

    def test_switch_records_callback_failure_without_raising(self):
        def fail(agent, envelope):
            raise RuntimeError("edge unavailable")

        switch = Switch(object(), pod="mesh", tenant="office", kick=fail)
        envelope = {"stream_id": "stream", "l2": {"source": "alice"}}
        with patch("core.service._log_observation") as log:
            switch._kick("bob", envelope)
        self.assertEqual(log.call_args.args, ("kick_unknown",))
        self.assertIn("edge unavailable", log.call_args.kwargs["reason"])

    def test_switch_without_kick_records_deferred(self):
        switch = Switch(object(), pod="mesh", tenant="office")
        envelope = {"stream_id": "stream", "l2": {"source": "alice"}}
        with patch("core.service._log_observation") as log:
            switch._kick("bob", envelope)
        self.assertEqual(log.call_args.args, ("kick_deferred",))
        self.assertEqual(log.call_args.kwargs["destination"], "bob")

    def test_state_paths_follow_configured_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"H_MESH_STATE_DIR": directory}):
                self.assertEqual(state_dir(), Path(directory))
                self.assertEqual(state_path("tasks.jsonl"), Path(directory) / "tasks.jsonl")
                tailer = WindowLogTailer(object(), pod="mesh", tenant="office")
                self.assertEqual(tailer.path, Path(directory) / "window.log.jsonl")


if __name__ == "__main__":
    unittest.main()
