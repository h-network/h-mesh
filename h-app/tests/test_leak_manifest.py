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

import errno
import json
import multiprocessing
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from _leak_manifest import MANIFEST_DIR, _process_start_time, reap_all_orphans, register

REPO_ROOT = Path(__file__).resolve().parents[2]


def _concurrent_destructive_reap_worker(
    entry_str: str,
    tmpdir_str: str,
    sentinel_str: str,
    barrier,
) -> None:
    """Force two real processes through the schedulable pre-action window."""
    import _leak_manifest

    entry = Path(entry_str)
    tmpdir = Path(tmpdir_str)
    sentinel = Path(sentinel_str)
    real_validate = _leak_manifest._validated_tmpdir
    waited = False

    def validate_then_wait(candidate, tmpdir):
        nonlocal waited
        validated = real_validate(candidate, tmpdir)
        if candidate == entry and validated is not None and not waited:
            waited = True
            barrier.wait(timeout=5)
        return validated

    def destructive_action(candidate_tmpdir, log=print):
        if candidate_tmpdir == tmpdir:
            sentinel.unlink()
            return True
        return False

    _leak_manifest._validated_tmpdir = validate_then_wait
    _leak_manifest.reap_orphan = destructive_action
    _leak_manifest.reap_all_orphans(log=lambda *_: None)


def _owned_scratch_tmpdir(prefix: str) -> Path:
    """A REAL direct child of /tmp with the expected prefix, matching what
    `managed_tmpdir` actually creates via `tempfile.mkdtemp` -- not a
    nested path under pytest's own `tmp_path` fixture, which
    `_validated_tmpdir`'s ownership check correctly refuses (not a direct
    child of the owned root) and would make these tests fail for the wrong
    reason if used here instead."""
    return Path(tempfile.mkdtemp(prefix=prefix))


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


def _describe_exit(proc: subprocess.Popen) -> str:
    """Distinguish "still running" from "exited" from "killed by a signal" --
    the three outcomes a bare `assert ready` used to collapse into one
    indistinguishable message."""
    code = proc.returncode
    if code is None:
        return "still running"
    if code < 0:
        try:
            return f"killed by signal {signal.Signals(-code).name}"
        except ValueError:
            return f"killed by signal {-code}"
    return f"exited with code {code}"


def _wait_for_ready_line(proc: subprocess.Popen, timeout: float) -> tuple[bool, str]:
    """Wait for a "READY" line without blocking past `timeout` even if the
    child never writes anything, or writes a partial, unterminated line and
    then goes on being silently alive.

    Reviewer's finding on an earlier version of this helper: `select`
    reports the pipe readable as soon as ANY bytes exist -- it answers "is
    there data", not "is there a line". A text-mode `proc.stdout.readline()`
    called right after still blocks until a newline or EOF, so a child that
    writes a partial line and then keeps running silently could hang this
    wait past its own deadline anyway, the exact "deadline that never fires"
    harm this helper exists to close. Fixed by reading raw, available bytes
    directly off the fd (`os.read`, which -- unlike a buffered readline --
    returns as soon as `select` says there's anything, never blocking for a
    newline that hasn't arrived yet) into our own incremental buffer, and
    detecting "READY" from that buffer rather than from the stream. Partial,
    unterminated output is retained in the buffer either way, since that's
    the other half of the evidence a bare pass/fail throws away."""
    deadline = time.monotonic() + timeout
    fd = proc.stdout.fileno()
    buffer = b""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        readable, _, _ = select.select([fd], [], [], min(0.2, remaining))
        if not readable:
            continue
        chunk = os.read(fd, 4096)
        if chunk == b"":
            break  # EOF: the child's stdout closed, which only happens on exit.
        buffer += chunk
        if b"READY" in buffer:
            return True, buffer.decode("utf-8", errors="replace")
    proc.poll()
    return False, buffer.decode("utf-8", errors="replace")


def test_orphan_from_a_killed_process_is_fully_reaped_by_the_next_session(tmp_path):
    script_path = tmp_path / "victim.py"
    script_path.write_text(_VICTIM_SCRIPT.format(tests_dir=str(REPO_ROOT / "h-app" / "tests")))

    victim_tmpdir = _owned_scratch_tmpdir("h_mesh_test_leak_harm_victim_")

    proc = subprocess.Popen(
        [sys.executable, str(script_path), str(victim_tmpdir)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    daemon_pid: int | None = None
    manifest_entry = MANIFEST_DIR / f"{victim_tmpdir.name}.json"
    try:
        ready, captured = _wait_for_ready_line(proc, timeout=10.0)
        assert ready, (
            f"victim process never signalled readiness -- {_describe_exit(proc)}; "
            f"captured output: {captured!r}"
        )

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
        #
        # Not `assert reaped >= 1`: a genuinely dead victim, once its
        # manifest entry is visible in the shared MANIFEST_DIR, is just as
        # reapable by a DIFFERENT concurrent process's own ordinary sweep
        # as by this one -- if that process wins the race, THIS call's own
        # `reaped` can legitimately be 0 even though the victim WAS reaped
        # (just not by this invocation). Which process did it doesn't
        # matter to this test's actual claim; the victim's own outcome
        # (daemon dead, tmpdir gone, entry gone) is what's asserted below,
        # by its own identity, regardless of who did the reaping.
        reap_all_orphans(log=lambda *_: None)

        deadline = time.monotonic() + 5.0
        while (
            _alive(daemon_pid)
            or victim_tmpdir.exists()
            or _manifest_evidence_exists(manifest_entry)
        ) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not _alive(daemon_pid), "daemon (switch) survived the reaper"
        assert not victim_tmpdir.exists(), "tmpdir survived the reaper"
        assert not _manifest_evidence_exists(manifest_entry), "manifest evidence survived the reaper"
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
        shutil.rmtree(victim_tmpdir, ignore_errors=True)


def test_wait_for_ready_line_does_not_block_past_its_deadline_on_a_partial_line():
    """Reviewer's finding on an earlier version of `_wait_for_ready_line`:
    `select` reporting a pipe readable only means SOME bytes are there -- a
    text-mode `readline()` called right after still blocks until a newline
    or EOF, so a child that writes a PARTIAL, unterminated line and then
    goes on being silently alive (not exiting) could still hang the wait
    well past its own deadline, exactly the "deadline that never fires"
    harm this helper exists to close. Reviewer's exact reproduction,
    reused directly: a child writes and flushes "PARTIAL" with no trailing
    newline, then sleeps well past the given timeout. Asserts BOTH the
    message (ready is False, the partial text was captured, not lost) AND
    the elapsed wall-clock time -- a message-only assertion would pass
    identically whether this returned in 0.2s or 2s, which is exactly the
    cannot-fail-in-the-direction-of-the-harm gap a bound on elapsed time
    closes."""
    proc = subprocess.Popen(
        [sys.executable, "-c",
         "import sys, time; sys.stdout.write('PARTIAL'); sys.stdout.flush(); time.sleep(2)"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        start = time.monotonic()
        ready, captured = _wait_for_ready_line(proc, timeout=0.2)
        elapsed = time.monotonic() - start

        assert ready is False, "a partial, unterminated line was mistaken for READY"
        assert "PARTIAL" in captured, "the partial, unterminated line was lost rather than captured"
        assert elapsed < 1.0, (
            f"wait blocked well past its own 0.2s deadline on a partial line: {elapsed:.3f}s elapsed"
        )
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


def test_reaper_does_not_touch_a_tmpdir_whose_owner_is_genuinely_still_running(tmp_path):
    """The mirror of the harm test above, and the one that matters most: a
    reaper that reaps too eagerly is worse than one that reaps too little.
    On a shared sandbox where several agents run this suite concurrently, an
    over-eager reaper means one agent's session start kills another agent's
    LIVE test -- indistinguishable from a flaky test to the victim. This
    proves a manifest entry whose registering process is still alive and
    still the SAME process (authenticated by start time, not bare pid
    liveness) is left completely untouched."""
    real_tmpdir = _owned_scratch_tmpdir("h_mesh_test_leak_harm_still_running_")
    (real_tmpdir / "run").mkdir()
    (real_tmpdir / "run" / "dummy.pid").write_text(str(os.getpid()))
    marker = real_tmpdir / "still-here"
    marker.write_text("do not touch")

    entry = register(str(real_tmpdir))
    try:
        assert _manifest_evidence_eventually_exists(entry)

        # NOT `assert reaped == 0` / `assert stuck == 0`: on a shared
        # sandbox, reap_all_orphans() sweeps the WHOLE manifest directory,
        # not just this test's own entry -- a different agent's concurrent
        # session registering (and later genuinely dying) at the same time
        # legitimately makes these aggregate counts nonzero without this
        # test's own entry ever being touched. What this test actually
        # claims is scoped to its OWN entry/tmpdir, asserted directly below.
        reap_all_orphans(log=lambda *_: None)

        assert real_tmpdir.exists()
        assert marker.exists()
        assert entry.exists(), "reaper deleted a live entry's manifest record"
    finally:
        entry.unlink(missing_ok=True)
        shutil.rmtree(real_tmpdir, ignore_errors=True)


def test_reaper_ignores_bare_pid_reuse_and_requires_start_time_to_match(tmp_path):
    """The specific regression this guards against: a manifest entry whose
    recorded pid is (coincidentally) still in use by SOME live process today
    must not be mistaken for "the same process still running" just because
    that pid answers `kill(pid, 0)`. Simulated directly: register normally,
    then corrupt the recorded start_time to a value that cannot match any
    real process, the same shape a genuine pid-reuse produces. The reaper
    must treat this as dead and reap it -- proving liveness alone, without
    authentication, would have wrongly skipped it forever."""
    real_tmpdir = _owned_scratch_tmpdir("h_mesh_test_leak_harm_pid_reuse_")
    try:
        entry = register(str(real_tmpdir))
        data = json.loads(entry.read_text())
        assert data["owner_pid"] == os.getpid()
        assert data["owner_start_time"] == _process_start_time(os.getpid())

        # Simulate reuse: same pid (still very much alive, this test's own
        # process), but a start_time that cannot belong to it -- exactly the
        # shape a genuinely different process now holding this pid would have.
        data["owner_start_time"] = "0"
        entry.write_text(json.dumps(data))

        # Not `assert reaped == 1` / `assert stuck == 0`: a concurrent
        # agent's own entries in the same shared manifest directory can
        # move these aggregate counts independently of this test's own
        # entry. This test's own claim -- that its OWN entry was reaped
        # despite the pid-reuse shape -- is scoped to it directly below.
        reap_all_orphans(log=lambda *_: None)

        assert _wait_until_reaped(real_tmpdir, entry), (
            "reaper trusted bare pid liveness instead of the authenticated start time"
        )
    finally:
        shutil.rmtree(real_tmpdir, ignore_errors=True)


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
    real_tmpdir = _owned_scratch_tmpdir("h_mesh_test_leak_harm_unauth_daemon_")
    (real_tmpdir / "run").mkdir()
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
        # Not `assert reaped == 0` / `assert stuck == 1`: a concurrent
        # agent's own entries in the shared manifest directory move these
        # aggregate counts independently of this test's own entry, which is
        # asserted directly below by its own identity instead.
        for attempt in range(2):
            reap_all_orphans(log=lambda *_: None)
            assert unrelated.poll() is None, f"attempt {attempt}: an unauthenticated pid was signalled"
            assert real_tmpdir.exists(), f"attempt {attempt}: tmpdir removed despite unauthenticated daemon"
            assert _manifest_evidence_eventually_exists(entry), f"attempt {attempt}: manifest evidence vanished despite unauthenticated daemon"
    finally:
        if unrelated.poll() is None:
            unrelated.kill()
        unrelated.wait(timeout=5)
        entry.unlink(missing_ok=True)
        shutil.rmtree(real_tmpdir, ignore_errors=True)


def _leak_manifest_entry_for(tmpdir: Path) -> Path:
    return MANIFEST_DIR / f"{tmpdir.name}.json"


def _manifest_evidence_exists(entry: Path) -> bool:
    """True while work is public or held by an authenticated claimant."""
    return entry.exists() or any(
        entry.parent.glob(f".claim.*.{entry.name}")
    )


def _claimed_evidence_exists(entry: Path) -> bool:
    return any(entry.parent.glob(f".claim.*.{entry.name}"))


def _different_live_claim_exists(entry: Path) -> bool:
    """Evidence of a claim held by a different, authenticated live process."""
    import _leak_manifest

    for claim in entry.parent.glob(f".claim.*.{entry.name}"):
        identity = _leak_manifest._claim_identity(claim)
        if identity is None or identity[0] == os.getpid():
            continue
        if _leak_manifest._owner_status(identity[0], identity[1]) == "running":
            return True
    return False


def _manifest_evidence_eventually_exists(entry: Path, timeout: float = 1.0) -> bool:
    """Tolerate a directory scan concurrent with an atomic claim rename."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _manifest_evidence_exists(entry):
            return True
        time.sleep(0.01)
    return _manifest_evidence_exists(entry)


def _wait_until_reaped(tmpdir: Path, entry: Path, timeout: float = 5.0) -> bool:
    """Wait for whichever authenticated claimant won to finish cleanup."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not tmpdir.exists() and not _manifest_evidence_exists(entry):
            return True
        time.sleep(0.01)
    return not tmpdir.exists() and not _manifest_evidence_exists(entry)


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
    real_tmpdir = _owned_scratch_tmpdir("h_mesh_test_leak_harm_unverifiable_owner_")
    entry = _leak_manifest_entry_for(real_tmpdir)
    entry.write_text(json.dumps({
        "tmpdir": str(real_tmpdir),
        "owner_pid": 999999999,  # any value; start_time None is what matters
        "owner_start_time": None,
        "registered_at": time.time(),
    }))

    try:
        # Not `assert reaped == 0` / `assert stuck == 1`: see the identical
        # note in the unauthenticatable-daemon test above -- these are
        # whole-shared-directory aggregates, not this entry's own outcome.
        for attempt in range(2):
            reap_all_orphans(log=lambda *_: None)
            assert real_tmpdir.exists(), f"attempt {attempt}: tmpdir removed despite unverifiable owner"
            assert _manifest_evidence_eventually_exists(entry), f"attempt {attempt}: manifest evidence vanished despite unverifiable owner"
    finally:
        entry.unlink(missing_ok=True)
        shutil.rmtree(real_tmpdir, ignore_errors=True)


def test_malformed_manifest_entry_is_left_visible_not_deleted(tmp_path):
    """Reviewer's third finding: a manifest entry with unreadable/invalid
    JSON, or missing/wrong-typed required fields, used to be deleted
    outright by reap_all_orphans -- destroying the only surviving record of
    whatever it pointed at, the exact evidence-loss problem this module
    exists to prevent one level earlier. Proves both malformed shapes are
    now left in place and counted as stuck instead."""
    # Filenames include this process's own pid: a hardcoded, shared name
    # here collides with a concurrent copy of THIS SAME test running in a
    # different process against the same shared MANIFEST_DIR.
    bad_json_entry = MANIFEST_DIR / f"h_mesh_test_leak_harm_bad_json_{os.getpid()}.json"
    bad_json_entry.write_text("{not valid json")

    missing_fields_entry = MANIFEST_DIR / f"h_mesh_test_leak_harm_missing_fields_{os.getpid()}.json"
    missing_fields_entry.write_text(json.dumps({"owner_pid": "not-an-int", "tmpdir": None}))

    try:
        # `reaped` and `stuck` are whole-shared-directory aggregates, not
        # scoped to these two entries -- `reaped` is not asserted directly,
        # see the ticket. `stuck >= 2` stays: a lower bound is safe under
        # concurrent pollution (it can only grow, never shrink).
        _reaped, stuck = reap_all_orphans(log=lambda *_: None)

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


def test_tmux_kill_does_not_match_a_program_whose_name_merely_contains_tmux(tmp_path):
    """Reviewer's fourth finding, same shape one field over: a REAL,
    unrelated executable named `notmux`, invoked with the EXACT `-S
    <socket>` argument pair a genuine tmux server would use, must not be
    killed just because its name contains the substring "tmux". A pidfd
    proves an action is delivered to one specific process lifetime; it
    proves nothing about what program that lifetime is running -- program
    identity has to be an exact comparison, checked separately."""
    from _leak_manifest import _kill_tmux_server_by_socket_path

    target_socket = str(tmp_path / "isolated.sock")
    fake_tmux = tmp_path / "notmux"
    fake_tmux.write_text("#!/usr/bin/env bash\nsleep 60\n")
    fake_tmux.chmod(0o755)

    impostor = subprocess.Popen([str(fake_tmux), "-S", target_socket, "new-session", "-d", "-s", "sess"])
    try:
        deadline = time.monotonic() + 5.0
        found = False
        while time.monotonic() < deadline and not found:
            for entry in Path("/proc").iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    argv = (entry / "cmdline").read_bytes().split(b"\0")
                except OSError:
                    continue
                if target_socket.encode() in argv and int(entry.name) == impostor.pid:
                    found = True
                    break
            time.sleep(0.05)
        assert found, "test setup: impostor process never showed up in /proc with the expected argv"

        _kill_tmux_server_by_socket_path(target_socket, log=lambda *_: None)

        assert impostor.poll() is None, (
            "a program whose name merely CONTAINS 'tmux' was killed for an exact -S argument match"
        )
    finally:
        if impostor.poll() is None:
            impostor.kill()
        impostor.wait(timeout=5)


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


def test_forged_entry_pointing_outside_tmp_is_never_touched(tmp_path):
    """Reviewer's most severe finding, 2026-09-02: reap_all_orphans() used
    to trust ANY string read from a manifest JSON file as a shutil.rmtree
    target -- MANIFEST_DIR is world-writable, so a stale, corrupt, or
    forged entry could name a git checkout or any other writable tree, and
    every pytest session on the box would delete it. Proves the target
    directly: a manifest entry with a dead owner naming a REAL, existing
    directory outside /tmp entirely (standing in for "a git checkout") is
    left completely untouched -- confirmed to still exist, with its own
    content intact, and the entry counted as stuck rather than acted on."""
    victim = tmp_path / "not_ours" / "important_work"
    victim.mkdir(parents=True)
    marker = victim / "do-not-delete"
    marker.write_text("this is not h-mesh's to touch")

    dead_owner = subprocess.Popen([sys.executable, "-c", "pass"])
    dead_owner.wait()

    # A pid-suffixed name, not a hardcoded one: a shared literal filename
    # collides with a concurrent copy of THIS SAME test in a different
    # process against the same shared MANIFEST_DIR.
    entry = MANIFEST_DIR / f"forged_out_of_scope_{os.getpid()}.json"
    entry.write_text(json.dumps({
        "tmpdir": str(victim),
        "owner_pid": dead_owner.pid,
        "owner_start_time": "0",
        "registered_at": time.time(),
    }))

    try:
        # `reaped == 0` removed: a whole-shared-directory aggregate, not
        # scoped to this entry (see the ticket). `victim.exists()` below is
        # the correctly-scoped equivalent -- if it were reaped, it wouldn't.
        _reaped, stuck = reap_all_orphans(log=lambda *_: None)

        assert stuck >= 1
        assert victim.exists(), "the out-of-scope directory was deleted"
        assert marker.exists(), "content inside the out-of-scope directory was destroyed"
        assert entry.exists(), "the forged entry itself was silently deleted rather than left stuck"
    finally:
        entry.unlink(missing_ok=True)


def test_forged_entry_with_wrong_prefix_under_tmp_is_never_touched():
    """Same finding, narrower case: a real directory that IS a direct child
    of /tmp but does not carry the h_mesh_test_ prefix this whole scheme
    uses -- must not be reachable either, even though it satisfies the
    "direct child of /tmp" check alone."""
    victim = Path(tempfile.mkdtemp(prefix="not_h_mesh_related_"))
    marker = victim / "do-not-delete"
    marker.write_text("unrelated tmp directory")

    dead_owner = subprocess.Popen([sys.executable, "-c", "pass"])
    dead_owner.wait()

    # Pid-suffixed for the same reason as the sibling test above.
    entry = MANIFEST_DIR / f"forged_wrong_prefix_{os.getpid()}.json"
    entry.write_text(json.dumps({
        "tmpdir": str(victim),
        "owner_pid": dead_owner.pid,
        "owner_start_time": "0",
        "registered_at": time.time(),
    }))

    try:
        # `reaped == 0` removed: whole-shared-directory aggregate, not
        # scoped to this entry (see the ticket). `victim.exists()` below is
        # the correctly-scoped equivalent.
        _reaped, stuck = reap_all_orphans(log=lambda *_: None)

        assert stuck >= 1
        assert victim.exists()
        assert marker.exists()
    finally:
        entry.unlink(missing_ok=True)
        shutil.rmtree(victim, ignore_errors=True)


def test_forged_entry_whose_tmpdir_does_not_match_its_own_filename_is_never_touched():
    """Same finding, another narrow case: a manifest file NAMED after one
    tmpdir but whose JSON content CLAIMS a different one -- register()
    itself never produces this shape (it always names the file after the
    tmpdir it's registering), so an entry like this is either corruption or
    a forgery, and either way must not be trusted to redirect the target."""
    real = _owned_scratch_tmpdir("h_mesh_test_leak_harm_real_")
    decoy = _owned_scratch_tmpdir("h_mesh_test_leak_harm_decoy_")
    marker = decoy / "do-not-delete"
    marker.write_text("this is the decoy, not the entry's own name")

    dead_owner = subprocess.Popen([sys.executable, "-c", "pass"])
    dead_owner.wait()

    # Named after `real`, but claims `decoy` as its tmpdir -- a mismatch
    # that could never come from a genuine register() call.
    entry = MANIFEST_DIR / f"{real.name}.json"
    entry.write_text(json.dumps({
        "tmpdir": str(decoy),
        "owner_pid": dead_owner.pid,
        "owner_start_time": "0",
        "registered_at": time.time(),
    }))

    try:
        # `reaped == 0` removed: whole-shared-directory aggregate, not
        # scoped to this entry (see the ticket). `decoy.exists()` below is
        # the correctly-scoped equivalent.
        _reaped, stuck = reap_all_orphans(log=lambda *_: None)

        assert stuck >= 1
        assert decoy.exists(), "the mismatched tmpdir was deleted despite not matching its own entry's filename"
        assert marker.exists()
    finally:
        entry.unlink(missing_ok=True)
        shutil.rmtree(real, ignore_errors=True)
        shutil.rmtree(decoy, ignore_errors=True)


def test_a_symlink_standing_in_for_the_tmpdir_is_never_touched():
    """Architect's follow-up, same day: naming checks alone are not
    ownership checks. A symlink at the expected, correctly-prefixed,
    correctly-named path -- pointing anywhere -- would pass every naming
    check. Proves it's rejected anyway: lstat must show a real directory,
    not a symlink, however correctly it's named."""
    real_target = Path(tempfile.mkdtemp(prefix="h_mesh_test_symlink_target_"))
    marker = real_target / "do-not-delete"
    marker.write_text("pointed to via a symlink standing in for the manifest's claimed tmpdir")

    link_name = f"h_mesh_test_leak_harm_symlink_{os.getpid()}"
    link_path = Path("/tmp") / link_name
    link_path.symlink_to(real_target)

    dead_owner = subprocess.Popen([sys.executable, "-c", "pass"])
    dead_owner.wait()

    entry = MANIFEST_DIR / f"{link_name}.json"
    entry.write_text(json.dumps({
        "tmpdir": str(link_path),
        "owner_pid": dead_owner.pid,
        "owner_start_time": "0",
        "registered_at": time.time(),
    }))

    try:
        # `reaped == 0` removed: whole-shared-directory aggregate, not
        # scoped to this entry (see the ticket). The symlink/target checks
        # below are the correctly-scoped equivalent.
        _reaped, stuck = reap_all_orphans(log=lambda *_: None)

        assert stuck >= 1
        assert link_path.is_symlink(), "the symlink itself was removed"
        assert real_target.exists(), "the symlink's real target was deleted"
        assert marker.exists()
    finally:
        entry.unlink(missing_ok=True)
        link_path.unlink(missing_ok=True)
        shutil.rmtree(real_target, ignore_errors=True)


def test_an_entry_owned_by_a_different_uid_is_never_touched(monkeypatch):
    """Same finding: naming and real-directory checks alone are still not
    OWNERSHIP -- a conforming, real, correctly-named /tmp directory planted
    by a different user must also be rejected. Simulated by making this
    process's own reported uid differ from the real directory's owner
    (rather than requiring an actual second system user, impractical to
    set up in this environment) -- from _validated_tmpdir's point of view
    this is indistinguishable from a genuine cross-user directory."""
    real_tmpdir = _owned_scratch_tmpdir("h_mesh_test_leak_harm_wrong_uid_")
    marker = real_tmpdir / "do-not-delete"
    marker.write_text("owned by 'someone else' as far as this test can simulate")

    dead_owner = subprocess.Popen([sys.executable, "-c", "pass"])
    dead_owner.wait()

    entry = register(str(real_tmpdir))
    data = json.loads(entry.read_text())
    data["owner_pid"] = dead_owner.pid
    data["owner_start_time"] = "0"
    entry.write_text(json.dumps(data))

    import _leak_manifest
    real_uid = os.getuid()
    monkeypatch.setattr(_leak_manifest.os, "getuid", lambda: real_uid + 1)

    try:
        # `reaped == 0` removed: whole-shared-directory aggregate, not
        # scoped to this entry (see the ticket). `real_tmpdir.exists()`
        # below is the correctly-scoped equivalent.
        _reaped, stuck = reap_all_orphans(log=lambda *_: None)

        assert stuck >= 1
        if not real_tmpdir.exists():
            pytest.skip(
                "a concurrent unpatched reaper removed this entry before "
                "this process could establish the simulated uid guard"
            )
        assert real_tmpdir.exists()
        assert marker.exists()
    finally:
        entry.unlink(missing_ok=True)
        shutil.rmtree(real_tmpdir, ignore_errors=True)


def test_an_unrecognized_pidfile_name_blocks_the_whole_entry_before_any_kill(tmp_path):
    """Reviewer's finding: _daemon_pidfile_preflight authenticated ANY
    pidfile, but stop_daemons() only ever processes names it recognizes
    (ALL_DAEMON_MODULES) -- an authenticated-but-unrecognized pidfile
    (e.g. custom.pid) used to pass preflight, stop_daemons would then
    stop/remove the REAL, recognized daemon anyway while never touching
    the unrecognized one, and only the after-the-fact leftover check
    caught it -- after the partial teardown this preflight exists to
    prevent had already happened. Proves the fix: a run_dir with one
    recognized daemon (switch) and one unrecognized one (custom), both
    individually authenticated, leaves BOTH untouched -- not just the
    unrecognized one."""
    real_tmpdir = _owned_scratch_tmpdir("h_mesh_test_leak_harm_unknown_name_")
    (real_tmpdir / "run").mkdir()

    known = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    unknown = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])

    def _write_pidfile_and_identity(name: str, proc: subprocess.Popen) -> None:
        pidfile = real_tmpdir / "run" / f"{name}.pid"
        pidfile.write_text(str(proc.pid))
        pidfile.with_suffix(".pid.identity").write_text(json.dumps({
            "v": 1, "pid": proc.pid, "name": name, "module": f"{name}.module",
            "start_time": _process_start_time(proc.pid),
        }))

    _write_pidfile_and_identity("switch", known)
    _write_pidfile_and_identity("custom", unknown)

    dead_owner = subprocess.Popen([sys.executable, "-c", "pass"])
    dead_owner.wait()
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    entry = _leak_manifest_entry_for(real_tmpdir)
    entry.write_text(json.dumps({
        "tmpdir": str(real_tmpdir),
        "owner_pid": dead_owner.pid,
        "owner_start_time": "0",
        "registered_at": time.time(),
    }))

    try:
        # `reaped == 0` removed: whole-shared-directory aggregate, not
        # scoped to this entry (see the ticket). `real_tmpdir`/`entry`
        # existence below is the correctly-scoped equivalent.
        _reaped, stuck = reap_all_orphans(log=lambda *_: None)

        assert stuck >= 1
        assert known.poll() is None, "the RECOGNIZED daemon was killed despite the unrecognized sibling blocking the entry"
        assert unknown.poll() is None, "the unrecognized process was killed"
        assert real_tmpdir.exists()
        assert _manifest_evidence_eventually_exists(entry)
    finally:
        for proc in (known, unknown):
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
        entry.unlink(missing_ok=True)
        shutil.rmtree(real_tmpdir, ignore_errors=True)


def test_a_provably_stale_daemon_pidfile_does_not_block_reaping(tmp_path):
    """Reviewer's finding: a nonexistent pid is not the same as
    unverified-live ownership -- it is the ordinary, safe stale case
    stop_daemons() already handles correctly (removes the pidfile,
    signals nothing, since nothing is alive to misidentify). An earlier
    version collapsed this into the same "do not proceed" bucket as a
    genuinely ambiguous mismatch, which would leave every ordinary
    already-exited daemon's entry stuck forever -- exactly what a daemon
    from a killed pytest process naturally becomes before the next
    session even runs. Proves the fix: an entry whose only daemon pidfile
    names a pid that has already, genuinely exited is fully reaped."""
    real_tmpdir = _owned_scratch_tmpdir("h_mesh_test_leak_harm_stale_daemon_")
    (real_tmpdir / "run").mkdir()

    already_exited = subprocess.Popen([sys.executable, "-c", "pass"])
    already_exited.wait()
    start_time_before_exit = _process_start_time(already_exited.pid)
    # A pid this old/reaped may already read as unreadable via /proc; if so
    # this specific test can't distinguish "gone" from "never existed" --
    # still exercises the same code path (_process_start_time returns None).

    pidfile = real_tmpdir / "run" / "switch.pid"
    pidfile.write_text(str(already_exited.pid))
    pidfile.with_suffix(".pid.identity").write_text(json.dumps({
        "v": 1, "pid": already_exited.pid, "name": "switch", "module": "core.service",
        "start_time": start_time_before_exit or "0",
    }))

    dead_owner = subprocess.Popen([sys.executable, "-c", "pass"])
    dead_owner.wait()
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    entry = _leak_manifest_entry_for(real_tmpdir)
    entry.write_text(json.dumps({
        "tmpdir": str(real_tmpdir),
        "owner_pid": dead_owner.pid,
        "owner_start_time": "0",
        "registered_at": time.time(),
    }))

    try:
        # Not `assert reaped == 1` / `assert stuck == 0`: whole-shared-
        # directory aggregates, not scoped to this entry (see the ticket).
        # The existence checks below are the correctly-scoped equivalent.
        reap_all_orphans(log=lambda *_: None)

        assert _wait_until_reaped(real_tmpdir, entry), (
            "a provably-stale daemon pidfile blocked reaping instead of being treated as safe"
        )
    finally:
        entry.unlink(missing_ok=True)
        shutil.rmtree(real_tmpdir, ignore_errors=True)


def test_finalize_removal_refuses_when_the_name_now_points_at_a_different_inode():
    """Reviewer's third finding, the most severe: an earlier version
    validated a tmpdir once and then called shutil.rmtree on its pathname
    -- which re-resolves every component itself when it actually runs. A
    same-uid process could rename the validated directory away and rename
    a DIFFERENT real, same-owned directory into that exact pathname in the
    gap, and the substitute -- not the original -- would be what got
    removed. This is a STRUCTURAL proof rather than a race against a
    background thread (the same kind of proof this office already
    preferred over a manufactured timing test tonight): construct the
    exact END STATE the race would produce -- the original's fd-bound stat
    captured, then the pathname reassigned to a different real directory
    -- and confirm _finalize_directory_removal's own recheck refuses
    rather than delete the substitute."""
    from _leak_manifest import _finalize_directory_removal, _open_verified_directory_fd

    original = _owned_scratch_tmpdir("h_mesh_test_leak_harm_swap_original_")
    verified = _open_verified_directory_fd(original)
    assert verified is not None, "test setup: could not open/verify the original directory"
    original_fd, original_st = verified
    os.close(original_fd)  # simulating _remove_tree_via_fd having already emptied and released it

    moved_aside = original.parent / f"{original.name}-moved-aside"
    original.rename(moved_aside)

    substitute = _owned_scratch_tmpdir("h_mesh_test_leak_harm_swap_substitute_")
    (substitute / "do-not-delete").write_text("the substitute directory, not the one that was validated")
    substitute.rename(original)  # now `original`'s pathname points at a DIFFERENT real inode
    marker = original / "do-not-delete"  # the marker's CURRENT location, post-rename

    try:
        result = _finalize_directory_removal(original, original_st, log=lambda *_: None)

        assert result is False, "the recheck did not refuse a swapped-in substitute directory"
        assert original.exists(), "the substitute directory was removed despite the inode mismatch"
        assert marker.exists(), "content inside the substitute directory was destroyed"
    finally:
        shutil.rmtree(original, ignore_errors=True)
        shutil.rmtree(moved_aside, ignore_errors=True)


def test_an_unreadable_but_not_confirmed_gone_pid_is_unverifiable_not_stale(monkeypatch):
    """Architect's follow-up to the stale-daemon fix, same day: "the pid no
    longer exists" must mean CONFIRMED absent, not merely "a read failed."
    _process_start_time returns None on ANY read failure (permission
    denied, a malformed stat line, any other I/O error), not only a
    confirmed-gone one -- collapsing those into "stale" would let anything
    that makes /proc/<pid>/stat transiently unreadable masquerade as the
    safe, provably-gone case, reopening the exact TOCTOU/misidentification
    class finding 3 closed, from the read side instead of the deletion
    side. Simulated directly: a pid whose start-time read fails but whose
    existence is NOT confirmed-gone (as a permission error or a transient
    I/O error would look) must classify as "unverifiable", never "stale"."""
    import _leak_manifest

    monkeypatch.setattr(_leak_manifest, "_process_start_time", lambda pid: None)
    monkeypatch.setattr(_leak_manifest, "_pid_confirmed_gone", lambda pid: False)

    real_tmpdir = _owned_scratch_tmpdir("h_mesh_test_leak_harm_ambiguous_read_")
    (real_tmpdir / "run").mkdir()
    pidfile = real_tmpdir / "run" / "switch.pid"
    pidfile.write_text("123456789")
    pidfile.with_suffix(".pid.identity").write_text(json.dumps({
        "v": 1, "pid": 123456789, "name": "switch", "module": "core.service",
        "start_time": "1",
    }))

    try:
        outcome, reason = _leak_manifest._daemon_pidfile_preflight(pidfile)

        assert outcome == "unverifiable", (
            f"an ambiguous (not confirmed-gone) read classified as {outcome!r} instead of unverifiable"
        )
    finally:
        shutil.rmtree(real_tmpdir, ignore_errors=True)


def test_a_failed_nested_removal_does_not_report_success_or_lose_content(monkeypatch):
    """Reviewer's finding: _remove_tree_via_fd used to catch and discard
    every error at every level (listdir, stat, child open, unlink, rmdir)
    and return nothing -- so a permission error, an unexpected I/O error,
    or a concurrent mutation deep in the tree could leave part of it
    behind while the caller went on to report success anyway. "Cleanup
    becoming hiding": the only surviving record (the manifest entry) would
    get cleared while real content survived, invisible from a bare
    directory-existence check. Proves the fix directly on the function
    that actually walks the tree: a nested file that cannot be removed
    makes the whole removal report failure, and the file survives."""
    import _leak_manifest

    real_tmpdir = _owned_scratch_tmpdir("h_mesh_test_leak_harm_failed_unlink_")
    subdir = real_tmpdir / "subdir"
    subdir.mkdir()
    leaf = subdir / "leaf.txt"
    leaf.write_text("must survive a failed removal")

    real_unlink = os.unlink

    def failing_unlink(*args, **kwargs):
        raise PermissionError("simulated: cannot remove this entry")

    monkeypatch.setattr(_leak_manifest.os, "unlink", failing_unlink)

    try:
        result = _leak_manifest._fd_safe_remove_owned_tmpdir(real_tmpdir, log=lambda *_: None)

        assert result is False, "a failed nested unlink was reported as successful removal"
        assert real_tmpdir.exists(), "the tmpdir was removed despite a failed nested unlink"
        assert leaf.exists(), "content that failed to unlink was lost anyway"
    finally:
        monkeypatch.setattr(_leak_manifest.os, "unlink", real_unlink)
        shutil.rmtree(real_tmpdir, ignore_errors=True)


def test_a_failed_top_level_rmdir_does_not_report_success_or_lose_the_directory(monkeypatch):
    """Reviewer's finding, mirror of the one above at the top level:
    _finalize_directory_removal used to catch every OSError from the final
    os.rmdir and return True regardless -- so a permission error, or the
    directory turning out non-empty because contents failed to remove
    upstream, was reported as success, and the manifest entry (the only
    surviving record) got cleared while the directory itself was still
    there. Proves the fix directly: a failing top-level rmdir makes the
    whole operation report failure, and the directory survives."""
    import _leak_manifest
    from _leak_manifest import _finalize_directory_removal, _open_verified_directory_fd

    real_tmpdir = _owned_scratch_tmpdir("h_mesh_test_leak_harm_failed_rmdir_")
    verified = _open_verified_directory_fd(real_tmpdir)
    assert verified is not None, "test setup: could not open/verify the directory"
    fd, st = verified
    os.close(fd)

    real_rmdir = os.rmdir

    def failing_rmdir(*args, **kwargs):
        raise PermissionError("simulated: cannot remove this directory")

    monkeypatch.setattr(_leak_manifest.os, "rmdir", failing_rmdir)

    try:
        result = _finalize_directory_removal(real_tmpdir, st, log=lambda *_: None)

        assert result is False, "a failed top-level rmdir was reported as successful removal"
        assert real_tmpdir.exists(), "the directory was removed despite a failed rmdir call"
    finally:
        monkeypatch.setattr(_leak_manifest.os, "rmdir", real_rmdir)
        shutil.rmtree(real_tmpdir, ignore_errors=True)


def test_a_transient_nonempty_directory_is_stuck_then_cleanly_reaped_next_session(monkeypatch):
    """Architect's explicit ask, following the ENOTEMPTY investigation:
    the stuck-then-reaped-next-session cycle IS the mechanism now (not a
    settle/retry, which would be a fail-open timeout against an unbounded
    event -- see this behavior's own known-benign-class documentation in
    _remove_tree_via_fd). Proves the full cycle deterministically, the
    same structural-proof-over-race precedent as the earlier TOCTOU test:
    a monkeypatched os.rmdir simulates something writing a new file into a
    subdirectory exactly once (matching a killed pane's shell finishing
    its SIGHUP-triggered history flush a few milliseconds late), then
    behaves normally on every later call -- so the FIRST reap attempt must
    leave the entry stuck with surviving content, and a SECOND, later
    attempt (nothing new races the second time) must fully reap it.

    ⚠ This entry looks genuinely dead (`owner_start_time: "0"`) the moment
    it's written -- indistinguishable, from a DIFFERENT concurrent
    process's own unrelated `reap_all_orphans()` sweep of the same shared
    MANIFEST_DIR, from any other reapable orphan. That other process's
    `os.rmdir` is real, not monkeypatched, so if it wins the race it
    reaps this entry cleanly with no ENOTEMPTY at all -- THIS process's
    own `racing_rmdir` is then never called for "home", and the claim
    this test exists to prove (the stuck-then-reaped cycle) was never
    actually exercised by this attempt. `raced_once["done"]` is the
    ground truth for whether that happened; when it's False this test
    says so and stops rather than asserting a claim it did not establish."""
    import _leak_manifest

    real_tmpdir = _owned_scratch_tmpdir("h_mesh_test_leak_harm_transient_race_")
    subdir = real_tmpdir / "home"
    subdir.mkdir()

    dead_owner = subprocess.Popen([sys.executable, "-c", "pass"])
    dead_owner.wait()
    entry = register(str(real_tmpdir))
    data = json.loads(entry.read_text())
    data["owner_pid"] = dead_owner.pid
    data["owner_start_time"] = "0"
    entry.write_text(json.dumps(data))

    real_rmdir = os.rmdir
    raced_once = {"done": False}

    def racing_rmdir(name, *args, dir_fd=None, **kwargs):
        if not raced_once["done"] and name == "home":
            raced_once["done"] = True
            # Simulate a process still writing into this directory a few
            # milliseconds after it was found empty -- exactly the shape
            # of a killed pane's shell finishing its SIGHUP-triggered
            # history flush. Open "home" itself (not its parent, which is
            # what dir_fd refers to here) to create the file INSIDE it.
            home_fd = os.open(name, os.O_DIRECTORY, dir_fd=dir_fd)
            try:
                fd = os.open("race-injected.txt", os.O_CREAT | os.O_WRONLY, dir_fd=home_fd)
                os.close(fd)
            finally:
                os.close(home_fd)
            raise OSError(errno.ENOTEMPTY, "Directory not empty", name)
        return real_rmdir(name, *args, dir_fd=dir_fd, **kwargs)

    monkeypatch.setattr(_leak_manifest.os, "rmdir", racing_rmdir)

    try:
        # Not `assert reaped_N == ...` / `assert stuck_N == ...`: whole-
        # shared-directory aggregates, not scoped to this entry (see the
        # ticket). The existence checks below are the correctly-scoped
        # equivalent for each attempt. Log lines are captured (not
        # discarded) so a failure below can show real evidence of what
        # reap_all_orphans actually did with THIS entry, not just infer it.
        captured_log: list[str] = []
        reap_all_orphans(log=captured_log.append)
        own_log_lines = [line for line in captured_log if real_tmpdir.name in line]
        # Two distinct windows where a DIFFERENT concurrent process's own,
        # entirely ordinary (non-monkeypatched) sweep of the same shared
        # MANIFEST_DIR can finish this entry's job before -- or right after
        # -- this process's own attempt: (a) it reaps the entry outright
        # before THIS process's racing_rmdir is ever called for "home", so
        # `raced_once["done"]` stays False; or (b) THIS process's own
        # attempt correctly leaves it stuck (raced_once IS True), but the
        # entry is still visible in the shared directory afterward, and a
        # different concurrent sweep finishes removing it (a real,
        # unmonkeypatched rmdir has no trouble with a directory that
        # genuinely, ordinarily contains one leftover file) before this
        # process gets to check.
        #
        # ⚠ Reviewer's finding: `not raced_once["done"]` is NOT by itself a
        # safe skip condition. A concurrent process finishing the job
        # requires the tmpdir to be GONE -- if it's still present, no
        # process (this one or a concurrent one) has completed removing
        # it, so "someone else finished the job" cannot explain this
        # state. That state-based reasoning is what justifies failing
        # rather than skipping here; it does not by itself say WHY this
        # process's own attempt didn't complete (see the diagnostic
        # below, which states what's known and doesn't guess the rest).
        # Skip ONLY when the tmpdir is gone -- otherwise fall through and
        # let `raced_once` itself be asserted, which fails loudly instead
        # of turning the only test for this recovery path into
        # permanently green noise.
        if not real_tmpdir.exists():
            pytest.skip(
                "a concurrent process's ordinary sweep of the same shared "
                "MANIFEST_DIR reaped this entry before this attempt could "
                "establish the stuck-then-reaped claim itself (this "
                f"process's own race fired: {raced_once['done']})"
            )
        if not raced_once["done"]:
            if _different_live_claim_exists(entry):
                pytest.skip(
                    "a different live process owns this entry's stamped claim; "
                    "this process did not establish the injected-rmdir path"
                )
            if not own_log_lines:
                # Reviewer's finding: absence of a matching captured line is
                # NOT proof this entry was never reached -- only proof there
                # is no captured evidence that it was. A matching line
                # would positively prove reaching a logged point; the
                # absence of one cannot positively prove the opposite (the
                # sweep could have reached the entry and failed before its
                # first matching log call, the wording could have stopped
                # including this entry's name, or logging on this path
                # could have been removed/moved). State the absence, list
                # what it could mean, and pick none of them.
                cause = (
                    "no captured log line mentions this entry at all -- this "
                    "could mean reap_all_orphans never reached it in its "
                    "sweep, OR that it reached it and failed/stopped before "
                    "any matching log call, OR that a matching line exists "
                    "but no longer mentions this entry's name/path. Absence "
                    "of a captured line proves absence of evidence, not "
                    "absence of the attempt."
                )
            else:
                cause = (
                    "reap_all_orphans reached this entry (log line(s) below) "
                    "but never called rmdir('home') for it -- it failed or "
                    "stopped somewhere on the way to the injected race, not "
                    f"at it: {own_log_lines!r}"
                )
            raise AssertionError(
                "the tmpdir is still here and nothing reaped it, with no "
                "concurrent process having touched it either -- this is a "
                f"genuine failure to complete the owned orphan's removal: {cause}"
            )
        assert real_tmpdir.exists(), "the tmpdir was removed despite the transient race"
        injected = subdir / "race-injected.txt"
        if not injected.exists():
            # This process established the transient ENOTEMPTY state, then
            # published a retryable claim.  A concurrent session is allowed
            # to win that NEXT claim immediately.  Once the injected file is
            # gone, wait for the observable outcome of that later ownership:
            # complete removal is recovery, while a surviving tmpdir is lost
            # content and remains a failure.  The wait does not decide when a
            # claim is stale; it only avoids sampling another reaper halfway
            # through its already-authorised removal.
            deadline = time.monotonic() + 5.0
            while real_tmpdir.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert not real_tmpdir.exists(), (
                "the injected content vanished but the orphan survived the "
                "concurrent retry that removed it"
            )
            assert not _manifest_evidence_exists(entry), (
                "the orphan was removed but its manifest evidence survived"
            )
            return
        assert injected.exists(), "the injected content was lost anyway"
        assert _manifest_evidence_eventually_exists(entry), "the manifest evidence vanished despite the transient race"

        # Second attempt, later "session" -- nothing races this time.
        reap_all_orphans(log=lambda *_: None)
        deadline = time.monotonic() + 5.0
        while real_tmpdir.exists() and time.monotonic() < deadline:
            reap_all_orphans(log=lambda *_: None)
            time.sleep(0.05)
        assert not real_tmpdir.exists(), "the tmpdir survived the second, unraced attempt"
        assert not _manifest_evidence_exists(entry), "the manifest evidence survived the second attempt"
    finally:
        monkeypatch.setattr(_leak_manifest.os, "rmdir", real_rmdir)
        entry.unlink(missing_ok=True)
        shutil.rmtree(real_tmpdir, ignore_errors=True)


def test_concurrent_reapers_execute_destructive_cleanup_once_per_entry(
    monkeypatch, tmp_path,
):
    """Two schedulable reapers must never act on one orphan concurrently.

    Both real child processes validate the same dead entry, then wait at the
    genuine preemption point between validation and destructive cleanup.  The
    cleanup consumes one sentinel without ``missing_ok``: double action makes
    one child crash with FileNotFoundError, the same harm observed for daemon
    pidfiles under repeated concurrent pytest invocations.
    """
    import _leak_manifest

    isolated_manifest = tmp_path / "shared-manifest"
    monkeypatch.setattr(_leak_manifest, "MANIFEST_DIR", isolated_manifest)
    monkeypatch.setattr(_leak_manifest, "CLAIM_DIR", isolated_manifest)
    real_tmpdir = _owned_scratch_tmpdir("h_mesh_test_leak_harm_double_reap_")
    sentinel = real_tmpdir / "single-use-cleanup-target"
    sentinel.write_text("remove exactly once")
    dead_owner = subprocess.Popen([sys.executable, "-c", "pass"])
    dead_owner.wait()
    entry = register(str(real_tmpdir))
    data = json.loads(entry.read_text())
    data["owner_pid"] = dead_owner.pid
    data["owner_start_time"] = "0"
    entry.write_text(json.dumps(data))

    context = multiprocessing.get_context("fork")
    barrier = context.Barrier(2)
    workers = [
        context.Process(
            target=_concurrent_destructive_reap_worker,
            args=(str(entry), str(real_tmpdir), str(sentinel), barrier),
        )
        for _ in range(2)
    ]
    try:
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)

        assert all(not worker.is_alive() for worker in workers), "a concurrent reaper hung"
        assert [worker.exitcode for worker in workers] == [0, 0], (
            "two reapers executed destructive cleanup for the same manifest entry"
        )
        assert not sentinel.exists()
        assert not entry.exists()
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.kill()
            worker.join(timeout=5)
        entry.unlink(missing_ok=True)
        shutil.rmtree(real_tmpdir, ignore_errors=True)


def test_dead_claimant_is_recovered_without_a_timeout():
    import _leak_manifest

    real_tmpdir = _owned_scratch_tmpdir("h_mesh_test_leak_harm_dead_claim_")
    entry = register(str(real_tmpdir))

    script = (
        "import json,os,sys; sys.path.insert(0, sys.argv[1]); "
        "import _leak_manifest as m; from pathlib import Path; "
        "claim=m._take_claim(Path(sys.argv[2]), Path(sys.argv[2]), log=lambda *_:None); "
        "data=json.loads(claim.read_text()); data['owner_pid']=os.getpid(); "
        "data['owner_start_time']=m._process_start_time(os.getpid()); "
        "claim.write_text(json.dumps(data)); "
        "print(claim, flush=True)"
    )
    claimer = subprocess.run(
        [sys.executable, "-c", script, str(REPO_ROOT / "h-app" / "tests"), str(entry)],
        capture_output=True,
        text=True,
        check=True,
    )
    claim = Path(claimer.stdout.strip())
    try:
        assert claim.exists() and not entry.exists()

        reap_all_orphans(log=lambda *_: None)

        assert not real_tmpdir.exists(), "an abandoned claimed orphan was never recovered"
        assert not claim.exists()
        assert not entry.exists()
    finally:
        claim.unlink(missing_ok=True)
        entry.unlink(missing_ok=True)
        shutil.rmtree(real_tmpdir, ignore_errors=True)


def test_live_claimant_is_not_stolen():
    import _leak_manifest

    real_tmpdir = _owned_scratch_tmpdir("h_mesh_test_leak_harm_live_claim_")
    entry = register(str(real_tmpdir))
    script = (
        "import sys,time; sys.path.insert(0, sys.argv[1]); "
        "import _leak_manifest as m; from pathlib import Path; "
        "claim=m._take_claim(Path(sys.argv[2]), Path(sys.argv[2]), log=lambda *_:None); "
        "print(claim, flush=True); time.sleep(60)"
    )
    claimer = subprocess.Popen(
        [sys.executable, "-c", script, str(REPO_ROOT / "h-app" / "tests"), str(entry)],
        stdout=subprocess.PIPE,
        text=True,
    )
    claim = Path(claimer.stdout.readline().strip())
    try:
        assert claim.exists() and not entry.exists()

        reap_all_orphans(log=lambda *_: None)

        assert claimer.poll() is None, "the live claimant was disturbed"
        assert claim.exists(), "a live claimant's entry was stolen"
        assert real_tmpdir.exists(), "a live claimant's tmpdir was reaped"
    finally:
        if claimer.poll() is None:
            claimer.kill()
        claimer.wait(timeout=5)
        claim.unlink(missing_ok=True)
        entry.unlink(missing_ok=True)
        shutil.rmtree(real_tmpdir, ignore_errors=True)


def test_incomplete_cleanup_retains_one_recoverable_claim(monkeypatch):
    import _leak_manifest

    _leak_manifest.CLAIM_DIR.mkdir(parents=True, exist_ok=True)
    tmpdir = _owned_scratch_tmpdir("h_mesh_test_leak_harm_incomplete_claim_")
    entry = MANIFEST_DIR / f"{tmpdir.name}.json"
    claim = _leak_manifest.CLAIM_DIR / f".claim.{os.getpid()}.1.token.{entry.name}"
    claim.write_text(json.dumps({
        "tmpdir": str(tmpdir),
        "owner_pid": 999999999,
        "owner_start_time": "0",
    }))
    logs = []
    monkeypatch.setattr(_leak_manifest, "reap_orphan", lambda *_args, **_kwargs: False)
    try:
        assert _leak_manifest._reap_claimed_entry(claim, entry, log=logs.append) == (0, 1)
        assert claim.exists()
        assert not entry.exists()
        assert any("orphan cleanup incomplete" in line and "STUCK" in line for line in logs)
    finally:
        claim.unlink(missing_ok=True)
        entry.unlink(missing_ok=True)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_completed_cleanup_with_unremovable_claim_reports_stuck_and_keeps_evidence(
    monkeypatch,
):
    import _leak_manifest

    tmpdir = _owned_scratch_tmpdir("h_mesh_test_leak_harm_completed_claim_")
    entry = MANIFEST_DIR / f"{tmpdir.name}.json"
    claim = _leak_manifest.CLAIM_DIR / f".claim.{os.getpid()}.1.token.{entry.name}"
    claim.write_text(json.dumps({
        "tmpdir": str(tmpdir),
        "owner_pid": 999999999,
        "owner_start_time": "0",
    }))
    real_unlink = Path.unlink
    logs = []

    def refuse_claim_unlink(path, *args, **kwargs):
        if path == claim:
            raise PermissionError("denied")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(_leak_manifest, "reap_orphan", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(Path, "unlink", refuse_claim_unlink)
    try:
        assert _leak_manifest._reap_claimed_entry(claim, entry, log=logs.append) == (0, 1)
        assert claim.exists()
        assert any("retaining completed claim" in line and "STUCK" in line for line in logs)
    finally:
        monkeypatch.setattr(Path, "unlink", real_unlink)
        claim.unlink(missing_ok=True)
        entry.unlink(missing_ok=True)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_failed_retry_restamp_retains_live_claim_and_reports_stuck(monkeypatch):
    import _leak_manifest

    entry = MANIFEST_DIR / "retry-restamp.json"
    claim = MANIFEST_DIR / f".claim.{os.getpid()}.1.token.{entry.name}"
    claim.write_text("evidence")
    real_rename = Path.rename
    logs = []

    def refuse_claim_rename(path, target):
        if path == claim:
            raise PermissionError("denied")
        return real_rename(path, target)

    monkeypatch.setattr(Path, "rename", refuse_claim_rename)
    try:
        assert _leak_manifest._retire_claim(claim, entry, log=logs.append) is None
        assert claim.read_text() == "evidence"
        assert any("cannot mark it retryable" in line and "STUCK" in line for line in logs)
    finally:
        monkeypatch.setattr(Path, "rename", real_rename)
        claim.unlink(missing_ok=True)
        entry.unlink(missing_ok=True)


def test_entry_claimed_after_glob_snapshot_is_not_reported_stuck(
    monkeypatch, tmp_path,
):
    """Losing the atomic claim race is normal, not an unresolved orphan."""
    import _leak_manifest

    isolated_manifest = tmp_path / "claim-loser-manifest"
    monkeypatch.setattr(_leak_manifest, "MANIFEST_DIR", isolated_manifest)
    monkeypatch.setattr(_leak_manifest, "CLAIM_DIR", isolated_manifest)
    real_tmpdir = _owned_scratch_tmpdir("h_mesh_test_leak_harm_claim_loser_")
    entry = register(str(real_tmpdir))
    data = json.loads(entry.read_text())
    data["owner_start_time"] = "0"
    entry.write_text(json.dumps(data))
    real_read_text = Path.read_text
    claimed = None

    def claim_before_read(path, *args, **kwargs):
        nonlocal claimed
        if path == entry and claimed is None:
            claimed = _leak_manifest._take_claim(entry, entry, log=lambda *_: None)
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", claim_before_read)
    logs = []
    try:
        reap_all_orphans(log=logs.append)

        assert claimed is not None and claimed.exists()
        assert not any("STUCK" in line and entry.name in line for line in logs)
    finally:
        if claimed is not None:
            claimed.unlink(missing_ok=True)
        entry.unlink(missing_ok=True)
        shutil.rmtree(real_tmpdir, ignore_errors=True)
