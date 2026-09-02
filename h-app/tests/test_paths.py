import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

H_APP = Path(__file__).resolve().parents[1]
if str(H_APP) not in sys.path:
    sys.path.insert(0, str(H_APP))

from lib.paths import build_pane_path, get_agent_workdir, get_workdir_root


class PathsTests(unittest.TestCase):
    def test_h_mesh_workdir_env_precedence(self):
        with patch.dict(os.environ, {"H_MESH_WORKDIR": "/custom/workdir", "H_MESH_STATE_DIR": "/state"}):
            self.assertEqual(get_workdir_root(), "/custom/workdir")
            self.assertEqual(get_agent_workdir("alice"), "/custom/workdir/alice")

    def test_h_mesh_state_dir_env(self):
        with patch.dict(os.environ, {"H_MESH_STATE_DIR": "/var/lib/h-mesh"}, clear=True):
            self.assertEqual(get_workdir_root(), "/var/lib/h-mesh/workdir")
            self.assertEqual(get_agent_workdir("bob"), "/var/lib/h-mesh/workdir/bob")

    def test_writable_workdir_container_fallback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Mock /workdir as existing and writable
            with patch("os.path.isdir", side_effect=lambda p: p == "/workdir" or os.path.isdir(p)), \
                 patch("os.access", side_effect=lambda p, m: True if p == "/workdir" else os.access(p, m)), \
                 patch.dict(os.environ, {}, clear=True):
                self.assertEqual(get_workdir_root(), "/workdir")
                self.assertEqual(get_agent_workdir("carol"), "/workdir/carol")

    def test_non_writable_workdir_falls_back_to_home(self):
        with tempfile.TemporaryDirectory() as fake_home:
            # Mock /workdir as not existing or not writable
            _real_isdir = os.path.isdir
            with patch("os.path.isdir", side_effect=lambda p: False if p == "/workdir" else _real_isdir(p)), \
                 patch.dict(os.environ, {"HOME": fake_home}, clear=True):
                self.assertEqual(get_workdir_root(), os.path.join(fake_home, "h-mesh"))
                self.assertEqual(get_agent_workdir("dave"), os.path.join(fake_home, "h-mesh", "dave"))

    def test_get_agent_workdir_explicit_cwd(self):
        self.assertEqual(get_agent_workdir("eve", cwd="/custom/dir"), "/custom/dir")

    def test_build_pane_path_includes_venv_and_user_and_system_locations(self):
        with patch.dict(os.environ, {"HOME": "/home/tester", "PATH": "/usr/bin:/bin", "VIRTUAL_ENV": "/venv/root"}):
            path_str = build_pane_path()
            entries = path_str.split(":")
            # venv bin is first
            self.assertEqual(entries[0], "/venv/root/bin")
            # ~/.local/bin and ~/bin are included
            self.assertIn("/home/tester/.local/bin", entries)
            self.assertIn("/home/tester/bin", entries)
            # standard system directories are included
            self.assertIn("/usr/local/bin", entries)
            self.assertIn("/usr/bin", entries)
            self.assertIn("/bin", entries)

    def test_build_pane_path_with_stripped_ambient_env(self):
        # Even with an empty ambient PATH (e.g. non-interactive launch / cron / systemd),
        # ~/.local/bin and system paths are always guaranteed
        with patch.dict(os.environ, {"HOME": "/home/tester"}, clear=True):
            path_str = build_pane_path(venv_bin="/custom/bin", ambient_path="")
            entries = path_str.split(":")
            self.assertEqual(entries[0], "/custom/bin")
            self.assertEqual(entries[1], "/home/tester/.local/bin")
            self.assertEqual(entries[2], "/home/tester/bin")
            self.assertIn("/usr/local/bin", entries)
            self.assertIn("/usr/bin", entries)
            self.assertIn("/bin", entries)

    def test_build_pane_path_with_prefix_env(self):
        with patch.dict(os.environ, {"HOME": "/home/tester", "PREFIX": "/opt/custom"}, clear=True):
            path_str = build_pane_path(venv_bin="/custom/bin")
            entries = path_str.split(":")
            self.assertEqual(entries[0], "/custom/bin")
            self.assertEqual(entries[1], "/opt/custom/bin")
            self.assertEqual(entries[2], "/home/tester/.local/bin")

    def test_build_pane_path_deduplication_and_order(self):
        with patch.dict(os.environ, {"HOME": "/home/tester"}, clear=True):
            ambient = "/usr/bin:/home/tester/.local/bin:/custom/bin:/bin"
            path_str = build_pane_path(venv_bin="/custom/bin", ambient_path=ambient)
            entries = path_str.split(":")
            # No duplicates
            self.assertEqual(len(entries), len(set(entries)))
            # /custom/bin moved to front
            self.assertEqual(entries[0], "/custom/bin")
            # /home/tester/.local/bin present before ambient /usr/bin
            self.assertEqual(entries[1], "/home/tester/.local/bin")


if __name__ == "__main__":
    unittest.main()
