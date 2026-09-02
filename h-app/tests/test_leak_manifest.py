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

# A real daemon this reaper must stop, named "switch" -- stop_daemons()
# only ever looks at pidfiles named after a KNOWN daemon key in
# ALL_DAEMON_MODULES, not an arbitrary glob, so an invented name here would
# be silently skipped rather than exercise the real authenticated-stop
# path. Real .pid.identity sidecar, same shape start_daemons() writes.
daemon = subprocess.Popen(["sleep", "300"])
pidfile = os.path.join(tmpdir, "run", "switch.pid")
with open(pidfile, "w") as f:
    f.write(str(daemon.pid))
with open(pidfile + ".identity", "w") as f:
    json.dump({{
        "v": 1, "pid": daemon.pid, "name": "switch", "module": "core.service",
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
        daemon_pid = int((victim_tmpdir / "run" / "switch.pid").read_text().strip())
        socket_path = victim_tmpdir / "isolated.sock"
        assert socket_path.exists(), "victim's tmux server never came up"
        assert _alive(proc.pid), "victim died before we could kill it -- test is not exercising the failure mode"
        assert _alive(daemon_pid), "victim's daemon (switch) is not alive before the kill"

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
        assert not _alive(daemon_pid), "daemon (switch) survived the reaper"
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


def test_unverifiable_owner_at_registration_is_stuck_not_silently_running(tmp_path):
    """Reviewer's second finding: `register()` persisting `owner_start_time:
    null` (registration-time authentication failure) used to make
    `_owner_still_running` return True forever -- indistinguishable from a
    genuinely live entry, contradicting the mandatory-visibility contract
    for permanently retained entries. Proves the corrected behavior: an
    entry whose owner could never be authenticated at registration time is
    counted as `stuck`, not silently treated as running, and stays on disk
    (never reaped -- correct, since it genuinely cannot tell if the owner
    is alive) across repeated attempts."""
    real_tmpdir = tmp_path / "h_mesh_test_leak_harm_unverifiable_owner"
    real_tmpdir.mkdir()
    entry = _leak_manifest_entry_for(real_tmpdir)
    entry.write_text(json.dumps({
        "tmpdir": str(real_tmpdir),
        "owner_pid": 999999999,  # any value; start_time None is what matters
        "owner_start_time": None,
        "registered_at": time.time(),
    }))

    try:
        for attempt in range(2):
            reaped, stuck = reap_all_orphans(log=lambda *_: None)
            assert reaped == 0, f"attempt {attempt}: an unverifiable owner was treated as dead and reaped"
            assert stuck == 1, f"attempt {attempt}: unverifiable owner was not counted as stuck"
            assert real_tmpdir.exists(), f"attempt {attempt}: tmpdir removed despite unverifiable owner"
            assert entry.exists(), f"attempt {attempt}: manifest entry cleared despite unverifiable owner"
    finally:
        entry.unlink(missing_ok=True)


def test_malformed_manifest_entry_is_left_visible_not_deleted(tmp_path):
    """Reviewer's third finding: a manifest entry with unreadable/invalid
    JSON, or missing/wrong-typed required fields, used to be deleted
    outright by reap_all_orphans -- destroying the only surviving record of
    whatever it pointed at, the exact evidence-loss problem this module
    exists to prevent one level earlier. Proves both malformed shapes are
    now left in place and counted as stuck instead."""
    bad_json_entry = MANIFEST_DIR / "h_mesh_test_leak_harm_bad_json.json"
    bad_json_entry.write_text("{not valid json")

    missing_fields_entry = MANIFEST_DIR / "h_mesh_test_leak_harm_missing_fields.json"
    missing_fields_entry.write_text(json.dumps({"owner_pid": "not-an-int", "tmpdir": None}))

    try:
        reaped, stuck = reap_all_orphans(log=lambda *_: None)

        assert reaped == 0
        assert stuck >= 2, "malformed entries were not both counted as stuck"
        assert bad_json_entry.exists(), "unreadable manifest entry was deleted rather than left visible"
        assert missing_fields_entry.exists(), "malformed manifest entry was deleted rather than left visible"
    finally:
        bad_json_entry.unlink(missing_ok=True)
        missing_fields_entry.unlink(missing_ok=True)


def test_tmux_kill_does_not_match_a_socket_path_that_is_only_a_prefix(tmp_path):
    """Reviewer's fourth finding: matching was substring membership in a
    flattened cmdline string, so a manifest socket path that is a PREFIX of
    a different, unrelated tmux server's actual socket path would also
    match and get killed. Proves the fix: a real tmux server bound to
    `<socket>-unrelated-extra` is left alone when reaping is asked to kill
    the server bound to exactly `<socket>`."""
    from _leak_manifest import _kill_tmux_server_by_socket_path

    target_socket = str(tmp_path / "isolated.sock")
    prefix_colliding_socket = target_socket + "-extra"

    real_server = subprocess.Popen(
        ["tmux", "-S", prefix_colliding_socket, "new-session", "-d", "-s", "sess", "-x", "80", "-y", "24"],
    )
    real_server.wait(timeout=5)

    def _tmux_server_pid(socket_path: str) -> int | None:
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                argv = (entry / "cmdline").read_bytes().split(b"\0")
            except OSError:
                continue
            if socket_path.encode() in argv and b"tmux" in argv[0]:
                return int(entry.name)
        return None

    try:
        server_pid = _tmux_server_pid(prefix_colliding_socket)
        assert server_pid is not None, "test setup: real tmux server for the colliding socket never came up"

        _kill_tmux_server_by_socket_path(target_socket, log=lambda *_: None)

        assert _tmux_server_pid(prefix_colliding_socket) == server_pid, (
            "a tmux server bound to a DIFFERENT socket path (only a string prefix match) was killed"
        )
    finally:
        subprocess.run(["tmux", "-S", prefix_colliding_socket, "kill-server"], capture_output=True, timeout=5)


def _alive2(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def test_pidfd_kill_never_signals_a_live_process_that_fails_verification(tmp_path):
    """The core guarantee of `_pidfd_kill_if_matches`, tested directly: a
    real, alive, unrelated process is left completely untouched when
    `verify()` returns False -- proving the decision to signal is bound to
    what `verify` says WHILE the pidfd is held, not to an earlier scan's
    result. This is what closes the concurrent-reaper pid-reuse race: a
    lagging reaper whose earlier scan matched a pid that has since been
    reused calls `verify()` again itself, fresh, with its OWN pidfd open on
    whatever now holds that number -- and a genuinely different process's
    current identity will not satisfy it."""
    from _leak_manifest import _pidfd_kill_if_matches

    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        result = _pidfd_kill_if_matches(unrelated.pid, verify=lambda pid: False, log=lambda *_: None)

        assert result == "no-match"
        assert _alive2(unrelated.pid), "a process was signalled despite verify() returning False"
    finally:
        if unrelated.poll() is None:
            unrelated.kill()
        unrelated.wait(timeout=5)


def test_pidfd_kill_signals_through_the_fd_when_verification_passes(tmp_path):
    """The mirror of the test above: when `verify()` genuinely matches, the
    process IS killed -- proving the fix does not simply refuse to act."""
    from _leak_manifest import _pidfd_kill_if_matches

    target = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        result = _pidfd_kill_if_matches(target.pid, verify=lambda pid: True, log=lambda *_: None)

        assert result == "killed"
        target.wait(timeout=5)
        assert not _alive2(target.pid)
    finally:
        if target.poll() is None:
            target.kill()
            target.wait(timeout=5)


def test_pidfd_kill_detects_a_process_that_exits_between_open_and_signal_as_stale(tmp_path):
    """The other half of the race: a process that exits in the window
    between the pidfd being opened and the signal being sent (simulated
    here inside `verify()` itself, the same position in the call sequence a
    real race would land in) is detected via the pidfd's own readability,
    not signalled as if still present -- proving detection does not depend
    on a fresh `/proc` read at signal time, which would itself be exactly
    the re-introduced TOCTOU gap."""
    from _leak_manifest import _pidfd_kill_if_matches

    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])

    def verify_then_die(pid: int) -> bool:
        os.kill(pid, signal.SIGKILL)
        victim.wait(timeout=5)
        return True  # a stale verify result -- the pidfd's own check must catch this regardless

    try:
        result = _pidfd_kill_if_matches(victim.pid, verify=verify_then_die, log=lambda *_: None)

        assert result == "stale"
    finally:
        if victim.poll() is None:
            victim.kill()
            victim.wait(timeout=5)
