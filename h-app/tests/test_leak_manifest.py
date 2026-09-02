"""Harm test for `_leak_manifest.py`'s reaper: the guard this whole
mechanism exists for is "a test process is killed before its own `finally`
runs." A test that only ever exercises the guard's happy path (register,
clean up normally) never proves it survives the one failure mode it was
built for -- see conftest.py's `pytest_sessionstart` docstring and the
standing containment-layer expectation.

This test simulates that failure directly: spawn a real subprocess that
registers a tmpdir (with a real tmux server and a real dummy daemon
running under it) exactly the way `managed_tmpdir` does, then SIGKILL it
before it reaches its own cleanup -- the same shape as an external
tool-call timeout or a cancelled CI job. Then run the reaper from a
DIFFERENT process (this test's own), matching how it actually runs in
practice: at the start of the NEXT pytest session, not the one that died.
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from _leak_manifest import MANIFEST_DIR, _process_start_time, reap_all_orphans, register

REPO_ROOT = Path(__file__).resolve().parents[2]

_VICTIM_SCRIPT = """
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, {tests_dir!r})
from _leak_manifest import _process_start_time, register

tmpdir = sys.argv[1]
os.makedirs(os.path.join(tmpdir, "run"), exist_ok=True)

# A real dummy daemon this reaper must kill -- with a real .pid.identity
# sidecar, the same shape services.daemons.start_daemons() writes, so this
# proves the AUTHENTICATED kill path, not an artificially-unauthenticated one.
daemon = subprocess.Popen(["sleep", "300"])
pidfile = os.path.join(tmpdir, "run", "dummy.pid")
with open(pidfile, "w") as f:
    f.write(str(daemon.pid))
with open(pidfile + ".identity", "w") as f:
    json.dump({{
        "v": 1, "pid": daemon.pid, "name": "dummy", "module": "dummy.module",
        "start_time": _process_start_time(daemon.pid),
    }}, f)

# A real tmux server this reaper must kill, exactly like the session-watch
# tests use, including a socket file inside the tmpdir being reaped.
socket_path = os.path.join(tmpdir, "isolated.sock")
subprocess.run(
    ["tmux", "-S", socket_path, "new-session", "-d", "-s", "victim", "-x", "80", "-y", "24"],
    check=True,
)

register(tmpdir)

# Signal readiness, then hang -- the parent SIGKILLs from here, before any
# cleanup below this point ever runs. This IS the failure mode.
print("READY", flush=True)
time.sleep(300)
"""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def test_orphan_from_a_killed_process_is_fully_reaped_by_the_next_session(tmp_path):
    script_path = tmp_path / "victim.py"
    script_path.write_text(_VICTIM_SCRIPT.format(tests_dir=str(REPO_ROOT / "h-app" / "tests")))

    victim_tmpdir = tmp_path / "h_mesh_test_leak_harm_victim"
    victim_tmpdir.mkdir()

    proc = subprocess.Popen(
        [sys.executable, str(script_path), str(victim_tmpdir)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    daemon_pid: int | None = None
    manifest_entry = MANIFEST_DIR / f"{victim_tmpdir.name}.json"
    try:
        deadline = time.monotonic() + 10.0
        ready = False
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                time.sleep(0.05)
                continue
            if "READY" in line:
                ready = True
                break
        assert ready, "victim process never signalled readiness"

        assert manifest_entry.exists(), "victim never registered before we killed it"
        daemon_pid = int((victim_tmpdir / "run" / "dummy.pid").read_text().strip())
        socket_path = victim_tmpdir / "isolated.sock"
        assert socket_path.exists(), "victim's tmux server never came up"
        assert _alive(proc.pid), "victim died before we could kill it -- test is not exercising the failure mode"
        assert _alive(daemon_pid), "victim's dummy daemon is not alive before the kill"

        # THE FAILURE MODE: external SIGKILL, no chance for the victim's own
        # cleanup to run -- same as a tool-call timeout or a cancelled job.
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)
        assert not _alive(proc.pid)

        # Confirm the damage the guard exists to prevent: without the
        # reaper, both of these survive the kill.
        assert _alive(daemon_pid), "sanity: daemon should still be alive right after the kill, pre-reap"
        assert victim_tmpdir.exists(), "sanity: tmpdir should still exist right after the kill, pre-reap"

        # THE GUARD: this is what pytest_sessionstart runs automatically at
        # the start of the next session. Called directly here so this test
        # doesn't depend on spawning a whole second pytest process.
        reaped, stuck = reap_all_orphans(log=lambda *_: None)
        assert reaped >= 1
        assert stuck == 0

        deadline = time.monotonic() + 5.0
        while _alive(daemon_pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not _alive(daemon_pid), "dummy daemon survived the reaper"
        assert not victim_tmpdir.exists(), "tmpdir survived the reaper"
        assert not manifest_entry.exists(), "manifest entry survived the reaper"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        # This harm test's own leftovers, in case an assertion above failed
        # before the reaper ran (or the reaper itself is broken) -- don't
        # let testing the leak-cleanup mechanism become a leak of its own.
        if daemon_pid is not None and _alive(daemon_pid):
            try:
                os.kill(daemon_pid, signal.SIGKILL)
            except OSError:
                pass
        manifest_entry.unlink(missing_ok=True)
        if victim_tmpdir.exists():
            import shutil
            shutil.rmtree(victim_tmpdir, ignore_errors=True)


def test_reaper_does_not_touch_a_tmpdir_whose_owner_is_genuinely_still_running(tmp_path):
    """The mirror of the harm test above, and the one that matters most: a
    reaper that reaps too eagerly is worse than one that reaps too little.
    On a shared sandbox where several agents run this suite concurrently, an
    over-eager reaper means one agent's session start kills another agent's
    LIVE test -- indistinguishable from a flaky test to the victim. This
    proves a manifest entry whose registering process is still alive and
    still the SAME process (authenticated by start time, not bare pid
    liveness) is left completely untouched."""
    real_tmpdir = tmp_path / "h_mesh_test_leak_harm_still_running"
    real_tmpdir.mkdir()
    (real_tmpdir / "run").mkdir()
    (real_tmpdir / "run" / "dummy.pid").write_text(str(os.getpid()))
    marker = real_tmpdir / "still-here"
    marker.write_text("do not touch")

    entry = register(str(real_tmpdir))
    try:
        assert entry.exists()

        reaped, stuck = reap_all_orphans(log=lambda *_: None)

        assert reaped == 0, "reaper touched a tmpdir whose owner is this live test process"
        assert stuck == 0
        assert real_tmpdir.exists()
        assert marker.exists()
        assert entry.exists(), "reaper deleted a live entry's manifest record"
    finally:
        entry.unlink(missing_ok=True)


def test_reaper_ignores_bare_pid_reuse_and_requires_start_time_to_match(tmp_path):
    """The specific regression this guards against: a manifest entry whose
    recorded pid is (coincidentally) still in use by SOME live process today
    must not be mistaken for "the same process still running" just because
    that pid answers `kill(pid, 0)`. Simulated directly: register normally,
    then corrupt the recorded start_time to a value that cannot match any
    real process, the same shape a genuine pid-reuse produces. The reaper
    must treat this as dead and reap it -- proving liveness alone, without
    authentication, would have wrongly skipped it forever."""
    real_tmpdir = tmp_path / "h_mesh_test_leak_harm_pid_reuse"
    real_tmpdir.mkdir()

    entry = register(str(real_tmpdir))
    data = json.loads(entry.read_text())
    assert data["owner_pid"] == os.getpid()
    assert data["owner_start_time"] == _process_start_time(os.getpid())

    # Simulate reuse: same pid (still very much alive, this test's own
    # process), but a start_time that cannot belong to it -- exactly the
    # shape a genuinely different process now holding this pid would have.
    data["owner_start_time"] = "0"
    entry.write_text(json.dumps(data))

    reaped, stuck = reap_all_orphans(log=lambda *_: None)

    assert reaped == 1, "reaper trusted bare pid liveness instead of the authenticated start time"
    assert stuck == 0
    assert not real_tmpdir.exists()
    assert not entry.exists()


def test_reaper_leaves_an_unauthenticatable_daemon_pid_running_and_retries_forever(tmp_path):
    """The daemon-pid mirror of the pid-reuse test above, and the more
    dangerous case: a manifest entry whose owner is genuinely dead, but
    whose recorded DAEMON pid has no (or a mismatched) `.identity` sidecar.
    A daemon pid recorded hours or days ago has had a far larger window for
    its number to be recycled than the owner does -- the exposure this
    guards against is real, not theoretical.

    Proves the deliberate design: nothing is killed, the tmpdir is NOT
    removed, the manifest entry is NOT cleared -- and calling the reaper
    again reaches the exact same outcome, forever, rather than the entry
    quietly expiring after some number of attempts. A stuck entry is meant
    to accumulate visibly (see conftest.py's own per-session log line for
    the count), not resolve itself."""
    real_tmpdir = tmp_path / "h_mesh_test_leak_harm_unauth_daemon"
    (real_tmpdir / "run").mkdir(parents=True)
    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    pidfile = real_tmpdir / "run" / "switch.pid"
    pidfile.write_text(str(unrelated.pid))
    # Deliberately no .switch.pid.identity sidecar at all -- the shape of a
    # legacy daemon pidfile, or one whose identity file was lost/truncated.

    # A dead, non-running owner so the reaper actually reaches the daemon
    # authentication step rather than skipping this entry as still-live.
    dead_owner = subprocess.Popen([sys.executable, "-c", "pass"])
    dead_owner.wait()
    entry = _leak_manifest_entry_for(real_tmpdir)
    entry.write_text(json.dumps({
        "tmpdir": str(real_tmpdir),
        "owner_pid": dead_owner.pid,
        "owner_start_time": "0",  # cannot match any current process
        "registered_at": time.time(),
    }))

    try:
        for attempt in range(2):
            reaped, stuck = reap_all_orphans(log=lambda *_: None)
            assert reaped == 0, f"attempt {attempt}: reaper killed/removed an unauthenticatable entry"
            assert stuck == 1, f"attempt {attempt}: entry was not reported as stuck"
            assert unrelated.poll() is None, f"attempt {attempt}: an unauthenticated pid was signalled"
            assert real_tmpdir.exists(), f"attempt {attempt}: tmpdir removed despite unauthenticated daemon"
            assert entry.exists(), f"attempt {attempt}: manifest entry cleared despite unauthenticated daemon"
    finally:
        if unrelated.poll() is None:
            unrelated.kill()
        unrelated.wait(timeout=5)
        entry.unlink(missing_ok=True)
        import shutil
        shutil.rmtree(real_tmpdir, ignore_errors=True)


def _leak_manifest_entry_for(tmpdir: Path) -> Path:
    return MANIFEST_DIR / f"{tmpdir.name}.json"
