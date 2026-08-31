import os
import pty
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = REPO_ROOT / "install.sh"
CLONE_URL = f"file://{REPO_ROOT}/.git"


def _env(install_dir: str, home_dir: str, tmpdir: str) -> dict:
    # ⚠ install.sh can hand off to a REAL setup.sh run (including starting
    # real daemons non-interactively when the tty-recovery path isn't
    # exercised), which is exactly the shape that has driven a real
    # tmux_reconciler against this office's own live tmux server before --
    # see [[hmesh-tmux-safety]]. TMUX_TMPDIR/TMUX_SESSION/TMUX_SOCKET must
    # always be pinned to something private here, unconditionally, not just
    # for the tests that look tmux-related at a glance.
    env = dict(os.environ)
    env["H_MESH_INSTALL_DIR"] = install_dir
    env["H_MESH_CLONE_URL"] = CLONE_URL
    env["HOME"] = home_dir
    env["TMUX_TMPDIR"] = tmpdir
    env["TMUX_SESSION"] = f"sess-{os.urandom(4).hex()}"
    env["TMUX_SOCKET"] = os.path.join(tmpdir, "isolated.sock")
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
            env=_env(install_dir, home_dir, tmpdir),
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
        env = _env(install_dir, home_dir, tmpdir)

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
            env=_env(install_dir, home_dir, tmpdir),
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
            env=_env(install_dir, home_dir, tmpdir),
            capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
        )
        # setup.sh rejects unknown flags -- proves the arg actually reached it.
        assert "Unknown option: --bogus-flag-xyz" in res.stderr
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_install_sh_recovers_a_tty_for_setup_sh_when_piped_to_sh():
    # Regression for a real bug: `curl -fsSL url | sh` consumes stdin as
    # sh's own script source, so by the time install.sh execs setup.sh, fd
    # 0 is the exhausted pipe -- setup.sh's own `[ -t 0 ]` then (correctly)
    # sees a non-tty and silently skips the wizard, even at a real
    # terminal. install.sh must redirect setup.sh's stdin from /dev/tty
    # first.
    #
    # subprocess.Popen (used everywhere else in this file) never gives a
    # real *controlling* terminal -- it dups a pty fd onto stdin/stdout but
    # doesn't setsid()+open-by-path the way a real interactive shell
    # session does, so /dev/tty wouldn't resolve to anything inside the
    # child either way and this exact bug wouldn't reproduce. pty.fork()
    # does that setup correctly; only the plain-pipe swap onto fd 0 below
    # (not stdout/stderr) simulates the piped-stdin part of `curl | sh`.
    tmpdir = tempfile.mkdtemp(prefix="h_mesh_test_install_")
    pid = None
    try:
        install_dir = os.path.join(tmpdir, "h-mesh-clone")
        home_dir = os.path.join(tmpdir, "home")
        os.makedirs(home_dir, exist_ok=True)
        env = _env(install_dir, home_dir, tmpdir)

        script = INSTALL_SH.read_bytes()

        pid, master_fd = pty.fork()
        if pid == 0:
            try:
                os.chdir(str(REPO_ROOT))
                for key, value in env.items():
                    os.environ[key] = value
                r, w = os.pipe()
                os.write(w, script)
                os.close(w)
                os.dup2(r, 0)
                os.close(r)
                os.execvp("sh", ["sh"])
            except Exception:
                os._exit(127)

        output = b""
        deadline = time.monotonic() + 30
        found = False
        while time.monotonic() < deadline:
            try:
                chunk = os.read(master_fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            output += chunk
            if b"Pod name [" in output:
                found = True
                break

        assert found, f"wizard never prompted; output so far:\n{output.decode(errors='replace')}"
    finally:
        if pid:
            try:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            except OSError:
                pass
        shutil.rmtree(tmpdir, ignore_errors=True)
