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


def _clone_url(tmpdir: str) -> str:
    """Build a self-contained source with the branch install.sh requests.

    Actions checks out a detached, shallow commit, while install.sh correctly
    asks git for a named release branch. The fixture owns that precondition;
    the surrounding checkout depth and local branch names are irrelevant.
    """
    source = Path(tmpdir) / "source.git"
    if not source.exists():
        head = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "clone", "--bare", "--no-hardlinks", str(REPO_ROOT), str(source)],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "--git-dir", str(source), "update-ref", "refs/heads/main", head],
            check=True,
        )
        subprocess.run(
            ["git", "--git-dir", str(source), "symbolic-ref", "HEAD", "refs/heads/main"],
            check=True,
        )
    return f"file://{source}"


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
    env["H_MESH_CLONE_URL"] = _clone_url(tmpdir)
    env["HOME"] = home_dir
    env["TMUX_TMPDIR"] = tmpdir
    env["TMUX_SESSION"] = f"sess-{os.urandom(4).hex()}"
    env["TMUX_SOCKET"] = os.path.join(tmpdir, "isolated.sock")
    # setup.sh otherwise inherits VIRTUAL_ENV and installs into it. The test
    # owns only tmpdir, so make any interpreter outside that cleanup boundary
    # unreachable rather than relying on callers to sanitize their shell.
    env.pop("VIRTUAL_ENV", None)
    env["VENV_PATH"] = os.path.join(tmpdir, "venv")
    return env


def test_install_fixture_never_targets_an_ambient_virtualenv(monkeypatch):
    external_venv = "/environment-this-test-does-not-own"
    monkeypatch.setenv("VIRTUAL_ENV", external_venv)
    monkeypatch.setenv("VENV_PATH", external_venv)

    env = _env("/tmp/checkout", "/tmp/home", "/tmp/fixture-owned")

    assert env.get("VIRTUAL_ENV") != external_venv, (
        "an install test must not select an ambient interpreter it does not own"
    )
    assert env["VENV_PATH"] == "/tmp/fixture-owned/venv", (
        "the install target must be contained by the fixture's cleanup boundary"
    )


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


def test_install_sh_defaults_to_local_share_leaving_h_mesh_free_for_workdirs():
    # Real regression: every OTHER test here sets H_MESH_INSTALL_DIR, so a
    # revert of install.sh's default back to $HOME/h-mesh would leave every
    # one of them green. This is the only test that omits the override --
    # proves the actual default landed where the relocation ticket asked
    # (~/.local/share/h-mesh, hidden/machine-managed) and, just as
    # importantly, that ~/h-mesh itself is left untouched -- that's the
    # directory agent workdirs now use, and the whole point of the
    # relocation is that installing the app must not touch it.
    tmpdir = tempfile.mkdtemp(prefix="h_mesh_test_install_")
    try:
        home_dir = os.path.join(tmpdir, "home")
        os.makedirs(home_dir, exist_ok=True)
        env = dict(os.environ)
        env.pop("H_MESH_INSTALL_DIR", None)
        env["H_MESH_CLONE_URL"] = _clone_url(tmpdir)
        env["HOME"] = home_dir
        env["TMUX_TMPDIR"] = tmpdir
        env["TMUX_SESSION"] = f"sess-{os.urandom(4).hex()}"
        env["TMUX_SOCKET"] = os.path.join(tmpdir, "isolated.sock")

        res = subprocess.run(
            ["sh", str(INSTALL_SH), "--help"],
            env=env, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
        )
        assert res.returncode == 0, f"stdout: {res.stdout}\nstderr: {res.stderr}"

        expected_checkout = Path(home_dir) / ".local" / "share" / "h-mesh"
        assert expected_checkout.is_dir() and (expected_checkout / ".git").is_dir(), (
            f"checkout did not land at the new default {expected_checkout}"
        )
        assert not (Path(home_dir) / "h-mesh").exists(), (
            "installing the app wrote into ~/h-mesh -- that directory is "
            "reserved for agent workdirs, not the source checkout"
        )
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
                # Dependency installation is not this test's subject. On a
                # clean runner, live network/package work consumed nearly the
                # entire prompt deadline before setup.sh could exercise tty
                # recovery at all. Pass the real installer's supported flag
                # through the piped-sh argv and keep the tty assertion exact.
                os.execvp("sh", ["sh", "-s", "--", "--skip-deps"])
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
