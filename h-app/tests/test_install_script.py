import os
import shutil
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = REPO_ROOT / "install.sh"
CLONE_URL = f"file://{REPO_ROOT}/.git"


def _env(install_dir: str, home_dir: str) -> dict:
    env = dict(os.environ)
    env["H_MESH_INSTALL_DIR"] = install_dir
    env["H_MESH_CLONE_URL"] = CLONE_URL
    env["HOME"] = home_dir
    return env


def test_install_sh_is_valid_posix_sh():
    # `curl | sh` ignores the shebang and runs under whatever /bin/sh is --
    # dash on Debian/Ubuntu, which rejects bash-only syntax outright.
    res = subprocess.run(["sh", "-n", str(INSTALL_SH)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


def test_install_sh_clones_and_hands_off_to_setup_sh():
    tmpdir = tempfile.mkdtemp(prefix="h_mesh_test_install_")
    try:
        install_dir = os.path.join(tmpdir, "h-mesh-clone")
        home_dir = os.path.join(tmpdir, "home")
        os.makedirs(home_dir, exist_ok=True)

        res = subprocess.run(
            ["sh", str(INSTALL_SH), "--help"],
            env=_env(install_dir, home_dir),
            capture_output=True, text=True, timeout=60,
            stdin=subprocess.DEVNULL,
        )
        assert res.returncode == 0, f"stdout: {res.stdout}\nstderr: {res.stderr}"
        assert "Usage: ./setup.sh" in res.stdout
        assert (Path(install_dir) / "setup.sh").exists()
        assert (Path(install_dir) / ".git").is_dir()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_install_sh_updates_an_existing_checkout():
    tmpdir = tempfile.mkdtemp(prefix="h_mesh_test_install_")
    try:
        install_dir = os.path.join(tmpdir, "h-mesh-clone")
        home_dir = os.path.join(tmpdir, "home")
        os.makedirs(home_dir, exist_ok=True)
        env = _env(install_dir, home_dir)

        first = subprocess.run(
            ["sh", str(INSTALL_SH), "--help"],
            env=env, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
        )
        assert first.returncode == 0, first.stderr

        second = subprocess.run(
            ["sh", str(INSTALL_SH), "--help"],
            env=env, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
        )
        assert second.returncode == 0, second.stderr
        assert "Updating existing h-mesh checkout" in second.stdout
        assert "Usage: ./setup.sh" in second.stdout
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_install_sh_refuses_a_non_git_existing_directory():
    tmpdir = tempfile.mkdtemp(prefix="h_mesh_test_install_")
    try:
        install_dir = os.path.join(tmpdir, "notgit")
        os.makedirs(install_dir, exist_ok=True)
        (Path(install_dir) / "somefile").write_text("not a git checkout\n")
        home_dir = os.path.join(tmpdir, "home")
        os.makedirs(home_dir, exist_ok=True)

        res = subprocess.run(
            ["sh", str(INSTALL_SH)],
            env=_env(install_dir, home_dir),
            capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
        )
        assert res.returncode != 0
        assert "already exists and is not a git checkout" in res.stderr
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_install_sh_passes_extra_args_through_to_setup_sh():
    tmpdir = tempfile.mkdtemp(prefix="h_mesh_test_install_")
    try:
        install_dir = os.path.join(tmpdir, "h-mesh-clone")
        home_dir = os.path.join(tmpdir, "home")
        os.makedirs(home_dir, exist_ok=True)

        res = subprocess.run(
            ["sh", str(INSTALL_SH), "--bogus-flag-xyz"],
            env=_env(install_dir, home_dir),
            capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
        )
        # setup.sh rejects unknown flags -- proves the arg actually reached it.
        assert "Unknown option: --bogus-flag-xyz" in res.stderr
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
