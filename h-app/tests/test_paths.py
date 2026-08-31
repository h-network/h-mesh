import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

H_APP = Path(__file__).resolve().parents[1]
if str(H_APP) not in sys.path:
    sys.path.insert(0, str(H_APP))

from lib.paths import get_agent_workdir, get_workdir_root


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
            with patch("os.path.isdir", side_effect=lambda p: False if p == "/workdir" else os.path.isdir(p)), \
                 patch.dict(os.environ, {"HOME": fake_home}, clear=True):
                self.assertEqual(get_workdir_root(), os.path.join(fake_home, ".h-mesh", "workdir"))
                self.assertEqual(get_agent_workdir("dave"), os.path.join(fake_home, ".h-mesh", "workdir", "dave"))

    def test_get_agent_workdir_explicit_cwd(self):
        self.assertEqual(get_agent_workdir("eve", cwd="/custom/dir"), "/custom/dir")


if __name__ == "__main__":
    unittest.main()
