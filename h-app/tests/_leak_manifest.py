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

⚠ Two sessions starting and reaping at once: every reap action here is
idempotent against a resource that's already gone -- killing an already-dead
pid raises and is caught, `shutil.rmtree` on an already-removed tree is a
no-op (`ignore_errors=True`), unlinking an already-unlinked manifest entry is
a no-op (`missing_ok=True`). A genuinely orphaned entry reaped redundantly by
two concurrent sessions does the same idempotent work twice, safely. A
registration race (one process mid-`register()` while another reaps) cannot
produce a corrupt read: `register()` writes to a temp file and `os.replace()`s
it into place, so a concurrent reader only ever sees the manifest directory
either without the entry yet, or with it fully written -- never partial.
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


def _kill_tmux_server_by_socket_path(socket_path: str) -> None:
    """Find and SIGKILL the tmux server process bound to `socket_path`
    directly, rather than trusting `tmux -S ... kill-server` alone.

    ⚠ `kill-server` asks the SERVER, over IPC, to exit -- on a loaded shared
    sandbox that call can time out or otherwise silently fail (this is
    exactly how ~150 tmux servers outlived their own already-deleted tmpdir:
    kill-server didn't take, and the unconditional rmtree right after it ran
    anyway). A raw SIGKILL by pid does not depend on the server being
    responsive to IPC. Matches by the exact ` -S <socket_path>` argument in
    each process's own cmdline, not by socket-file existence -- the socket
    file itself may already be gone while the process holding it is not.
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
        if needle in cmdline and "tmux" in cmdline:
            try:
                os.kill(int(entry.name), 9)
            except OSError:
                pass


def reap_orphan(tmpdir: str) -> None:
    """The same cleanup every test's own `finally` already does for its own
    tmpdir -- kill any daemon recorded under `<tmpdir>/run/*.pid`, kill the
    isolated tmux server bound to `<tmpdir>/isolated.sock`, remove the tree.
    Safe to call on a tmpdir that's partially or fully gone already; kills
    the tmux server by pid regardless of whether its socket file survives."""
    tmpdir_path = Path(tmpdir)
    run_dir = tmpdir_path / "run"
    if run_dir.is_dir():
        for pidfile in run_dir.glob("*.pid"):
            try:
                pid = int(pidfile.read_text().strip())
                os.kill(pid, 9)
            except (ValueError, OSError):
                pass
    socket_path = str(tmpdir_path / "isolated.sock")
    _kill_tmux_server_by_socket_path(socket_path)
    shutil.rmtree(tmpdir, ignore_errors=True)


def reap_all_orphans(log=print) -> int:
    """Reap every manifest entry whose registering process is authenticated
    as no longer running (see `_owner_still_running` -- never bare pid
    liveness). An entry whose owner IS still the same running process
    belongs to a test genuinely in progress -- on this shared sandbox that
    may be a different agent's concurrent pytest invocation, not this one --
    so it is left alone regardless of age. Returns the number reaped."""
    if not MANIFEST_DIR.is_dir():
        return 0
    reaped = 0
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
        reap_orphan(tmpdir)
        entry.unlink(missing_ok=True)
        reaped += 1
    return reaped
