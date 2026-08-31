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
    def test_core_contains_no_old_project_names(self):
        core = H_APP / "core"
        checked = [*core.glob("*.py"), *core.glob("*.md")]
        for path in checked:
            text = path.read_text(encoding="utf-8").casefold()
            with self.subTest(path=path.name):
                self.assertNotIn("flock", text)
                self.assertNotIn("roster", text)

    def test_tree_contains_no_flock_imports(self):
        """Guard against any module, client, service or test importing from flock."""
        py_files = list(H_APP.rglob("*.py"))
        self.assertTrue(len(py_files) > 20, "Expected at least 20 python files across h-app")
        for path in py_files:
            text = path.read_text(encoding="utf-8")
            lines = text.splitlines()
            for idx, line in enumerate(lines, start=1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                with self.subTest(path=str(path.relative_to(H_APP)), line=idx):
                    self.assertFalse(
                        stripped.startswith("import flock") or stripped.startswith("from flock"),
                        f"Found flock import at {path.relative_to(H_APP)}:{idx}: {line}",
                    )

    def test_tree_clients_and_services_use_h_mesh_convention(self):
        """Ensure clients and services define and prioritize H_MESH_ env vars and classes."""
        # 1. Telegram bot client and launcher
        tg_bot = (H_APP / "clients" / "telegram" / "bot.py").read_text(encoding="utf-8")
        self.assertIn("class MeshClient:", tg_bot)
        self.assertIn("mesh_telegram", tg_bot)
        self.assertIn("H_MESH_API_URL", tg_bot)
        self.assertIn("H_MESH_SESSION_URL", tg_bot)
        self.assertIn("H_MESH_API_TOKEN", tg_bot)
        self.assertNotIn("FLOCK_", tg_bot)
        self.assertNotIn("FlockClient", tg_bot)

        # 2. Web console server
        web_server = (H_APP / "clients" / "web" / "server.py").read_text(encoding="utf-8")
        self.assertIn('server_version = "h-mesh-web/1"', web_server)
        self.assertIn("hmesh_session", web_server)
        self.assertIn("H_MESH_SECRET", web_server)
        self.assertIn("H_MESH_API", web_server)
        self.assertIn("H_MESH_SESSION", web_server)
        self.assertNotIn("HFLOCK_", web_server)
        self.assertNotIn("hflock_session", web_server)

        # 3. Services launchers
        svc_tg = (H_APP / "services" / "telegram_bot.py").read_text(encoding="utf-8")
        self.assertIn("MeshClient", svc_tg)
        self.assertIn("H_MESH_API_URL", svc_tg)
        self.assertNotIn("FLOCK_", svc_tg)
        self.assertNotIn("FlockClient", svc_tg)

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
