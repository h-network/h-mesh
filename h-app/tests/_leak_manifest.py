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

⚠ REVIEWER BLOCKING FAIL, 2026-09-02, against an earlier version of this
module -- record kept because the failure mode is the point: that version
*collected* authenticated daemon pids into a list and only signalled them
afterward, after finishing authentication of every other pidfile. That gap
between "we read /proc and it matched" and "we actually call kill()" is
exactly a TOCTOU window a pid could exit and be reused inside, identifying
by an authenticated-in-the-past number rather than binding through the
signal itself -- "fresh at the moment of action" was claimed, not
implemented. Fixed by not reimplementing daemon killing here at all: every
daemon pid this module ever signals goes through
`services.daemons.stop_daemons()`, which opens a pidfd BEFORE
authentication and signals through that fd (`signal.pidfd_send_signal`),
never a numeric `os.kill(pid, ...)` -- the fd is bound to one specific
process lifetime the moment it's opened, immune to reuse after that point
by construction, not by re-checking fast enough. Reusing the already
-reviewed primitive closes this more reliably than a second, less-reviewed
implementation of the same delicate mechanism would. The isolated tmux
server (not a `services.daemons` module, no existing pidfd helper) gets the
same treatment built locally: `os.pidfd_open` immediately upon a match,
re-verified while that fd is held, signalled through the fd.

⚠ The same reviewer pass, same reasons, on two smaller gaps: an entry whose
OWNER authentication itself failed at registration time (`owner_start_time`
recorded as `None`) was silently treated as "still running" forever --
correct to never reap on that basis, wrong to make it indistinguishable from
a live entry; and a manifest entry with malformed/unreadable JSON was
deleted outright, destroying the only surviving record of whatever it
pointed at. Both now count as `stuck` and stay on disk, logged, exactly like
an unauthenticatable daemon pid does -- see `reap_all_orphans`.

⚠ Two sessions starting and reaping at once: every action taken here
RE-AUTHENTICATES FRESH at the moment of that action -- via pidfd, not a
value read earlier in the race -- which is what actually makes concurrent
reaping safe. Two reapers independently authenticating the same
genuinely-dead entry reach the same conclusion and do the same idempotent
work twice (a pidfd opened against an already-exited pid raises
`ProcessLookupError`, caught; `shutil.rmtree` on an already-removed tree is
a no-op; unlinking an already-unlinked manifest entry is a no-op) -- safe. A
registration race (one process mid-`register()` while another reaps) cannot
produce a corrupt read either: `register()` writes to a temp file and
`os.replace()`s it into place, so a concurrent reader only ever sees the
manifest directory either without the entry yet, or with it fully written --
never partial.
"""

from __future__ import annotations

import json
import os
import select
import shutil
import signal
import time
from pathlib import Path

from services.daemons import stop_daemons

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


def _owner_status(owner_pid: int, owner_start_time: str | None) -> str:
    """"running", "dead", or "unverifiable" -- never a bare bool, because
    "we don't know" and "it's still running" must not collapse into the
    same outcome the way an earlier version of this function did.

    `owner_start_time` being `None` means registration itself could not
    authenticate (`/proc` unreadable at that moment) -- fails closed (never
    reaped on that basis, "unverifiable" is not "running") but must also
    stay VISIBLE as its own distinct case, not silently indistinguishable
    from a genuinely live entry forever (reviewer's finding: it previously
    was). Otherwise the pid's CURRENT start time must match exactly for
    "running"; any mismatch, or the pid no longer existing at all, means the
    original owner is provably gone ("dead") regardless of who holds that
    pid number now.
    """
    if owner_start_time is None:
        return "unverifiable"
    current = _process_start_time(owner_pid)
    if current is None:
        return "dead"
    return "running" if current == owner_start_time else "dead"


def _pidfd_kill_if_matches(pid: int, verify, log=print, context: str = "") -> str:
    """Open a pidfd for `pid` FIRST, then call `verify(pid)` while that fd is
    held, then SIGKILL through the fd -- never through a bare numeric
    `os.kill(pid, ...)`. Returns "killed", "no-match", or "stale".

    ⚠ This is the structural fix for the exact gap reviewer found: opening
    the pidfd before re-verification, and delivering the signal through it,
    means the kernel resolves the target from the fd's bound lifetime, not
    from re-resolving the numeric pid at signal time -- so even if `pid` is
    reused by an unrelated process in between `verify()` returning and the
    signal being sent, the reused process is never touched. `os.pidfd_open`
    itself can still race an exit-and-reuse in the brief window before it
    opens (if `pid` has already been reused by the time this is even
    called, the fd binds to whatever process holds it NOW, not to the
    original) -- `verify()` running with that fd already open, plus the
    `select.select` staleness check right after, is what catches that: a
    freshly reused pid's own current identity will not satisfy `verify`.
    """
    try:
        pidfd = os.pidfd_open(pid)
    except ProcessLookupError:
        return "stale"
    except (AttributeError, OSError):
        log(f"  • cannot open pidfd for pid {pid}{(' (' + context + ')') if context else ''}; leaving it")
        return "no-match"
    try:
        if not verify(pid):
            return "no-match"
        # Already exited between pidfd_open and here, even though the
        # verify() read above happened to look consistent.
        readable, _, _ = select.select([pidfd], [], [], 0)
        if readable:
            return "stale"
        try:
            signal.pidfd_send_signal(pidfd, signal.SIGKILL)
        except ProcessLookupError:
            return "stale"
        except (AttributeError, OSError):
            log(f"  • cannot safely signal pid {pid}{(' (' + context + ')') if context else ''}; leaving it")
            return "no-match"
        return "killed"
    finally:
        os.close(pidfd)


def _matching_tmux_pids(socket_path: str) -> list[int]:
    """Pids whose CURRENT argv contains an exact `-S socket_path` pair.

    ⚠ Parses `/proc/<pid>/cmdline` as real argv (split on NUL, drop the
    trailing empty token), not a flattened-string substring check -- an
    earlier version matched `-S {socket_path}` as a plain substring of the
    joined cmdline, which a DIFFERENT socket path that happens to start
    with this one as a prefix (e.g. `/tmp/x` inside `/tmp/x-extra`) would
    also satisfy. `socket_path` itself is still not a small, quickly
    recycled identifier the way a pid is -- it is derived from
    `tempfile.mkdtemp`'s randomness -- so an unrelated process being
    invoked with the exact same argument value is not a realistic
    collision the way pid reuse is, once the comparison is actually exact.
    """
    proc_dir = Path("/proc")
    if not proc_dir.is_dir():
        return []
    matches = []
    for entry in proc_dir.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        argv = raw.split(b"\0")
        if argv and argv[-1] == b"":
            argv = argv[:-1]
        try:
            flag_index = argv.index(b"-S")
        except ValueError:
            continue
        if flag_index + 1 >= len(argv):
            continue
        if argv[flag_index + 1].decode(errors="replace") != socket_path:
            continue
        if not any(b"tmux" in part for part in argv[:1]):
            continue
        matches.append(int(entry.name))
    return matches


def _kill_tmux_server_by_socket_path(socket_path: str, log=print) -> None:
    """SIGKILL the tmux server bound to exactly `socket_path`, through a
    pidfd opened and re-verified at the moment of the kill (see
    `_pidfd_kill_if_matches`) -- never a numeric `os.kill` resolved from an
    earlier scan. Also called with `tmux kill-server`'s own attempt already
    having run and, on this box, having been observed to silently fail
    under load (see the "kill-server didn't take" note in ticket 2198b696's
    report), which is why this exists at all rather than trusting that.
    """
    def verify(pid: int) -> bool:
        if pid not in _matching_tmux_pids(socket_path):
            return False
        # Secondary, imperfect confirmation beyond the argv match -- exe
        # resolves through wrapper symlinks to whatever binary actually
        # runs, a real but low-probability blind spot, not the primary
        # defense (the exact argv match, re-checked with the pidfd held, is).
        try:
            exe = os.readlink(f"/proc/{pid}/exe")
        except OSError:
            return False
        return "tmux" in os.path.basename(exe)

    for pid in _matching_tmux_pids(socket_path):
        _pidfd_kill_if_matches(pid, verify, log=log, context=f"tmux server for {socket_path}")


def reap_orphan(tmpdir: str, log=print) -> bool:
    """The same cleanup every test's own `finally` already does for its own
    tmpdir -- stop every daemon recorded under `<tmpdir>/run/*.pid` (through
    `services.daemons.stop_daemons`, the reviewed pidfd-authenticated
    implementation -- not reimplemented here), kill the isolated tmux
    server bound to `<tmpdir>/isolated.sock`, remove the tree. Safe to call
    on a tmpdir that's partially or fully gone already.

    If ANY daemon pidfile is still present after `stop_daemons` returns --
    meaning it could not authenticate that pid and, correctly, left it
    running rather than guess -- this does NOTHING further: no tmux-server
    kill, no directory removal. Partial reaping (stopping what authenticates
    and deleting the directory anyway) would destroy the very evidence that
    makes an unauthenticatable orphan visible, while leaving whatever
    couldn't be stopped running unlabelled. Returns False in that case so
    the caller leaves the manifest entry in place for a later session to
    retry, rather than treating a partial, unsafe cleanup as done.
    """
    tmpdir_path = Path(tmpdir)
    run_dir = tmpdir_path / "run"
    if run_dir.is_dir():
        stop_daemons(run_dir, log=log)
        remaining = sorted(p.name for p in run_dir.glob("*.pid"))
        if remaining:
            log(f"  • STUCK, leaving {tmpdir} entirely: still present after stop_daemons: {remaining}")
            return False
    socket_path = str(tmpdir_path / "isolated.sock")
    _kill_tmux_server_by_socket_path(socket_path, log=log)
    shutil.rmtree(tmpdir, ignore_errors=True)
    return True


def reap_all_orphans(log=print) -> tuple[int, int]:
    """Reap every manifest entry whose registering process is authenticated
    as no longer running (see `_owner_status` -- never bare pid liveness).
    An entry whose owner IS still the same running process belongs to a
    test genuinely in progress -- on this shared sandbox that may be a
    different agent's concurrent pytest invocation, not this one -- so it
    is left alone regardless of age.

    Returns `(reaped, stuck)`. `stuck` counts every entry left in place on
    purpose rather than silently: a dead owner whose daemon pidfile could
    not be authenticated (see `reap_orphan`); an owner whose OWN liveness
    could not be authenticated at registration time (`owner_start_time`
    recorded as `None` -- reviewer's finding: this used to be silently
    treated as "running" forever, indistinguishable from a genuinely live
    entry); and a manifest entry with malformed or unreadable JSON, which
    used to be deleted outright -- destroying the only surviving record of
    whatever it pointed at, reviewer's second finding. None of these are
    aged out: an unauthenticatable entry that silently expired after some
    timeout would be a fail-OPEN path wearing a fail-closed appearance,
    exactly the class of bug this whole module exists to remove. Permanent
    retention is only an acceptable answer because it stays VISIBLE -- a
    reason is logged on every attempt, and this count is what a caller
    (`conftest.py`) reports plainly rather than folding into "reaped" and
    going quiet.
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
        except (OSError, json.JSONDecodeError) as exc:
            log(f"  • STUCK, leaving {entry}: unreadable manifest entry ({exc})")
            stuck += 1
            continue
        owner_pid = data.get("owner_pid")
        owner_start_time = data.get("owner_start_time")
        tmpdir = data.get("tmpdir")
        if not isinstance(owner_pid, int) or not isinstance(tmpdir, str):
            log(f"  • STUCK, leaving {entry}: malformed manifest entry (owner_pid={owner_pid!r} tmpdir={tmpdir!r})")
            stuck += 1
            continue
        status = _owner_status(owner_pid, owner_start_time)
        if status == "running":
            continue
        if status == "unverifiable":
            log(f"  • STUCK, leaving {tmpdir}: owner pid {owner_pid}'s liveness could not be authenticated")
            stuck += 1
            continue
        log(f"  • reaping orphaned test tmpdir {tmpdir} (owner pid {owner_pid} authenticated dead)")
        if not reap_orphan(tmpdir, log=log):
            stuck += 1
            continue  # left in place on purpose; entry stays for a later retry
        entry.unlink(missing_ok=True)
        reaped += 1
    return reaped, stuck
