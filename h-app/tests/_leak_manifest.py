"""Crash-resistant registration for a test's tmpdir/tmux/daemon triple.

⚠ A test's own `finally` block only runs if the test process is still alive
to reach it. Killing the process from OUTSIDE -- an external tool-call
timeout, Ctrl-C escalated to SIGKILL, a CI job cancelled mid-run -- cannot be
caught, and skips every `finally`/`atexit`/pytest teardown in that process
unconditionally. On a shared sandbox where several agents run this suite
concurrently, that happens often enough to matter: 290+ `/tmp/h_mesh_test_*`
directories and 150+ leaked tmux/daemon processes accumulated exactly this
way (ticket a3807c5b / 2198b696). In-process cleanup, however careful, cannot
close this gap by itself -- only something OUTSIDE the killed process, run on
a LATER invocation, can.

The pattern here: register the tmpdir in a manifest file the moment it's
created (before anything is spawned under it), record this process's own pid
as the manifest's owner, and delete the manifest entry as the last step of
normal cleanup. A manifest entry that survives to the start of the NEXT
pytest session is proof its owner died before finishing -- reap it there,
regardless of which test or which agent created it.

⚠ "Owner died" is decided by more than a numeric pid. A pid recorded at
registration time can be reused by an unrelated process by the time a later
session checks it -- exactly the bug lifecycle-agent's `services/daemons.py`
authenticated-pidfile work removed from `stop_daemons` (reviewer's fail-first
there killed a real unrelated sleeper on a real box). Bare `kill(pid, 0)`
cannot tell "the same process that registered this" from "a different process
that happens to hold this pid number now." So this module authenticates the
same way: record the registering process's `/proc/<pid>/stat` start time
alongside its pid, and at reap time require an EXACT match before treating
the pid as still the same process. A mismatch (or the pid simply being gone)
is proof the original owner exited -- reaping is then correct regardless of
who or what holds that pid number today. If start-time authentication itself
is unavailable at registration (e.g. `/proc` unreadable), this fails CLOSED:
never reap an entry it could not authenticate, rather than guess.

⚠ The same bug, one level down, is worse: authenticating the OWNER pid is not
enough if the daemon pids reap_orphan() goes on to kill (switch,
tmux_reconciler, watchdog...) are still signalled by their bare recorded
number. The owner is typically freshly dead, a narrow reuse window; a daemon
pid recorded in a manifest entry that's sat orphaned for hours (entries from
31 August existed) has had an enormous window for its number to be recycled
by something else on this shared box entirely -- another agent's daemon, or
the live office. So every daemon pid is authenticated too, via the
`.pid.identity` sidecar `services.daemons.start_daemons()` already writes
(same file, same schema -- no separate recording step needed here). If even
one pidfile under a tmpdir can't be authenticated, `reap_orphan()` does
NOTHING for that entry: no kill, no tmux-server kill, no directory removal,
leaving it for a later session to retry. A surviving orphan is visible and
is the problem this module exists to fix; a wrongly killed unrelated process
is silent and lands on someone else -- asymmetric costs, so this fails
closed on the side that stays visible.

⚠ Two sessions starting and reaping at once: every action taken here
RE-AUTHENTICATES FRESH at the moment of that action rather than trusting a
value read earlier in the race, which is what actually makes concurrent
reaping safe, not just idempotence. Two reapers independently authenticating
the same genuinely-dead entry reach the same conclusion and do the same
idempotent work twice (killing an already-dead pid raises and is caught,
`shutil.rmtree` on an already-removed tree is a no-op, unlinking an
already-unlinked manifest entry is a no-op) -- safe. In the narrower case
where a reaper's authentication read lags behind an actual kill-then-reuse
by a concurrent reaper, the LAGGING reaper's own fresh start-time comparison
will not match the newly-reused pid's actual start time, so it correctly
declines to signal it -- the authentication step is what closes this race,
not merely catching already-dead-pid errors. A registration race (one
process mid-`register()` while another reaps) cannot produce a corrupt read
either: `register()` writes to a temp file and `os.replace()`s it into
place, so a concurrent reader only ever sees the manifest directory either
without the entry yet, or with it fully written -- never partial.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

MANIFEST_DIR = Path("/tmp/h_mesh_test_manifests")


def _process_start_time(pid: int) -> str | None:
    """Read Linux /proc starttime (field 22), unique to one use of a pid --
    same field, same reasoning as services/daemons.py's authenticated
    pidfile identity (reused here rather than imported: this module has no
    other reason to depend on production daemon-lifecycle code)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        # comm is parenthesized and may itself contain spaces or ')'. Fields
        # after its final ')' begin at field 3; starttime is field 22.
        return stat[stat.rfind(")") + 2:].split()[19]
    except (IndexError, OSError):
        return None


def register(tmpdir: str) -> Path:
    """Record `tmpdir` as live, owned by this process. Call before spawning
    anything under it. Returns the manifest entry's own path."""
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    pid = os.getpid()
    entry = MANIFEST_DIR / f"{os.path.basename(tmpdir)}.json"
    payload = json.dumps({
        "tmpdir": tmpdir,
        "owner_pid": pid,
        "owner_start_time": _process_start_time(pid),
        "registered_at": time.time(),
    })
    temporary = entry.with_name(f".{entry.name}.{pid}.tmp")
    temporary.write_text(payload)
    os.replace(temporary, entry)
    return entry


def clear(entry: Path) -> None:
    """Remove a manifest entry. Call only after `tmpdir` itself is already
    gone -- this is the signal a reaper trusts to mean "nothing to clean up
    here," so it must not be removed first."""
    entry.unlink(missing_ok=True)


def _owner_still_running(owner_pid: int, owner_start_time: str | None) -> bool:
    """True only if `owner_pid` is authenticated as the SAME process that
    registered this entry -- never true from pid liveness alone.

    `owner_start_time` being `None` means registration itself could not
    authenticate (fails closed: treated as still running, never reaped on
    that basis). Otherwise the pid's CURRENT start time must match exactly;
    any mismatch, or the pid no longer existing at all, means the original
    owner is provably gone regardless of who holds that pid number now.
    """
    if owner_start_time is None:
        return True
    current = _process_start_time(owner_pid)
    if current is None:
        return False
    return current == owner_start_time


def _identity_path(pidfile: Path) -> Path:
    """Same convention as services/daemons.py's own `_identity_path` --
    `<name>.pid` -> `<name>.pid.identity`. Deliberately the same suffix, not
    reimplemented differently, so identity files `start_daemons()` already
    writes for every daemon it starts (current main) are exactly what this
    reads, with no separate recording step of our own required."""
    return pidfile.with_suffix(pidfile.suffix + ".identity")


def _authenticated_daemon_pid(pidfile: Path) -> tuple[int | None, str]:
    """(pid, "authenticated") only if `pidfile`'s companion `.identity` file
    (written by `services.daemons.start_daemons`) proves the recorded pid is
    still the same process that file was written for -- never the bare
    recorded pid. Otherwise (None, reason), reason meant to be logged, not
    just branched on -- see `reap_orphan`'s "cannot ever authenticate" path.

    ⚠ This is the same bug one level down from the owner check above, and
    the more dangerous instance of it: the owner is typically freshly dead
    (a narrow reuse window), but a daemon pid recorded in a manifest entry
    that's sat orphaned for hours -- and entries from 31 August have --
    has had an enormous window for its number to be recycled by something
    else entirely: another agent's daemon, or the live office itself.
    Fails closed (do not kill) whenever authentication is missing,
    unreadable, or mismatched -- a surviving orphan is visible and was the
    problem this module exists to fix; a wrongly killed unrelated process
    is silent and lands on someone else.
    """
    try:
        pid = int(pidfile.read_text().strip())
    except (ValueError, OSError):
        return None, "pidfile unreadable or non-numeric"
    identity_path = _identity_path(pidfile)
    try:
        identity = json.loads(identity_path.read_text())
    except OSError:
        return None, f"no {identity_path.name} sidecar"
    except json.JSONDecodeError:
        return None, f"{identity_path.name} is not valid JSON"
    if not isinstance(identity, dict) or identity.get("pid") != pid:
        return None, f"{identity_path.name} does not name pid {pid}"
    recorded_start_time = identity.get("start_time")
    if not isinstance(recorded_start_time, str):
        return None, f"{identity_path.name} has no recorded start_time"
    current_start_time = _process_start_time(pid)
    if current_start_time is None:
        return None, f"pid {pid} no longer exists"
    if current_start_time != recorded_start_time:
        return None, f"pid {pid} start_time does not match -- number was reused"
    return pid, "authenticated"


def _kill_tmux_server_by_socket_path(socket_path: str, log=print) -> None:
    """Find and SIGKILL the tmux server process bound to `socket_path`.

    ⚠ Not a stored-pid trust in the first place, unlike a bare daemon
    pidfile -- the target pid is re-derived FRESH at the moment of the kill
    by scanning every live process's CURRENT cmdline for the exact
    ` -S <socket_path>` argument, never from a pid recorded earlier. A
    process that has since exited leaves no live process with that cmdline
    to match, so an already-dead server's (reused) pid number is never
    signalled on the strength of a stale recording, which is the actual
    shape of the pid-reuse bug -- trusting a NUMBER read at one point in
    time to still identify the same process at a later one. `socket_path`
    itself is also not a small, quickly-recycled identifier the way a pid
    is: it is derived from `tempfile.mkdtemp`'s randomness, so an unrelated
    process coincidentally being invoked with that exact string is not a
    realistic collision the way pid reuse is. Also checks the matched
    process's resolved `/proc/<pid>/exe` basename contains "tmux" before
    signalling, as a secondary, imperfect confirmation -- NOT the primary
    defense (the cmdline content-match is), and it has its own known blind
    spot: `exe` resolves through wrapper symlinks to whatever binary
    actually runs, the same class of surprise that made an earlier
    installed-interpreter filter in this ticket's own investigation wrongly
    resolve a wrapped python invocation through to the system interpreter
    underneath it. A real tmux reached through an unusual wrapper chain is
    a residual, low-probability gap this does not close -- called with
    `kill-server`'s own attempt already having run and, on this box, having
    been observed to silently fail under load (see the "kill-server didn't
    take" note in ticket 2198b696's report).
    """
    needle = f"-S {socket_path}"
    proc_dir = Path("/proc")
    if not proc_dir.is_dir():
        return
    for entry in proc_dir.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            continue
        if needle not in cmdline or "tmux" not in cmdline:
            continue
        try:
            exe = os.readlink(f"/proc/{entry.name}/exe")
        except OSError:
            continue
        if "tmux" not in os.path.basename(exe):
            log(f"  • not killing pid {entry.name}: cmdline matched {socket_path} but exe is {exe}, not tmux")
            continue
        try:
            os.kill(int(entry.name), 9)
        except OSError:
            pass


def reap_orphan(tmpdir: str, log=print) -> bool:
    """The same cleanup every test's own `finally` already does for its own
    tmpdir -- kill any AUTHENTICATED daemon recorded under
    `<tmpdir>/run/*.pid`, kill the isolated tmux server bound to
    `<tmpdir>/isolated.sock`, remove the tree. Safe to call on a tmpdir
    that's partially or fully gone already.

    If ANY daemon pidfile cannot be authenticated (missing or mismatched
    `.identity` -- see `_authenticated_daemon_pid`), this does NOTHING at
    all: no daemon killed, no tmux server killed, tmpdir NOT removed.
    Partial reaping -- killing what authenticates and deleting the
    directory anyway -- would destroy the very evidence (the directory,
    the manifest entry staying meaningful) that makes an unauthenticatable
    orphan visible, while leaving whatever it couldn't authenticate running
    unlabelled. Returns False in that case so the caller leaves the
    manifest entry in place for a later session to retry, rather than
    treating a partial, unsafe cleanup as done.
    """
    tmpdir_path = Path(tmpdir)
    run_dir = tmpdir_path / "run"
    to_kill: list[int] = []
    if run_dir.is_dir():
        for pidfile in run_dir.glob("*.pid"):
            pid, reason = _authenticated_daemon_pid(pidfile)
            if pid is None:
                log(f"  • STUCK, leaving entirely: {tmpdir} -- {pidfile.name} not authenticated ({reason})")
                return False
            to_kill.append(pid)
    for pid in to_kill:
        try:
            os.kill(pid, 9)
        except OSError:
            pass
    socket_path = str(tmpdir_path / "isolated.sock")
    _kill_tmux_server_by_socket_path(socket_path, log=log)
    shutil.rmtree(tmpdir, ignore_errors=True)
    return True


def reap_all_orphans(log=print) -> tuple[int, int]:
    """Reap every manifest entry whose registering process is authenticated
    as no longer running (see `_owner_still_running` -- never bare pid
    liveness). An entry whose owner IS still the same running process
    belongs to a test genuinely in progress -- on this shared sandbox that
    may be a different agent's concurrent pytest invocation, not this one --
    so it is left alone regardless of age.

    Returns `(reaped, stuck)`. `stuck` counts entries whose owner is dead
    but a daemon pidfile could not be authenticated (see `reap_orphan`) --
    these are deliberately retried, not aged out, EVERY session, forever,
    until whatever broke their `.identity` sidecar is fixed. That is a
    considered choice, not an oversight: an unauthenticatable entry that
    silently expired after some timeout would be a fail-OPEN path wearing a
    fail-closed appearance, exactly the class of bug this whole module
    exists to remove. Permanent retention is only an acceptable answer
    because it stays VISIBLE -- `reap_orphan` logs the specific pidfile and
    reason on every attempt, and this count is what a caller (`conftest.py`)
    reports plainly rather than folding into "reaped" and going quiet.
    """
    if not MANIFEST_DIR.is_dir():
        return 0, 0
    reaped = 0
    stuck = 0
    for entry in MANIFEST_DIR.glob("*.json"):
        if entry.name.startswith("."):
            continue  # a register() temp file mid-write, not a real entry
        try:
            data = json.loads(entry.read_text())
        except (OSError, json.JSONDecodeError):
            entry.unlink(missing_ok=True)
            continue
        owner_pid = data.get("owner_pid")
        owner_start_time = data.get("owner_start_time")
        tmpdir = data.get("tmpdir")
        if not isinstance(owner_pid, int) or not isinstance(tmpdir, str):
            entry.unlink(missing_ok=True)
            continue
        if _owner_still_running(owner_pid, owner_start_time):
            continue
        log(f"  • reaping orphaned test tmpdir {tmpdir} (owner pid {owner_pid} authenticated dead)")
        if not reap_orphan(tmpdir, log=log):
            stuck += 1
            continue  # left in place on purpose; entry stays for a later retry
        entry.unlink(missing_ok=True)
        reaped += 1
    return reaped, stuck
