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
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

MANIFEST_DIR = Path("/tmp/h_mesh_test_manifests")


def register(tmpdir: str) -> Path:
    """Record `tmpdir` as live, owned by this process. Call before spawning
    anything under it. Returns the manifest entry's own path."""
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    entry = MANIFEST_DIR / f"{os.path.basename(tmpdir)}.json"
    entry.write_text(json.dumps({
        "tmpdir": tmpdir,
        "owner_pid": os.getpid(),
        "registered_at": time.time(),
    }))
    return entry


def clear(entry: Path) -> None:
    """Remove a manifest entry. Call only after `tmpdir` itself is already
    gone -- this is the signal a reaper trusts to mean "nothing to clean up
    here," so it must not be removed first."""
    entry.unlink(missing_ok=True)


def _owner_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


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
    """Reap every manifest entry whose owner process is no longer alive.

    An entry whose owner IS alive belongs to a test genuinely still running
    -- on this shared sandbox that may be a different agent's concurrent
    pytest invocation, not this one, so it is left alone regardless of age.
    Returns the number of orphans reaped."""
    if not MANIFEST_DIR.is_dir():
        return 0
    reaped = 0
    for entry in MANIFEST_DIR.glob("*.json"):
        try:
            data = json.loads(entry.read_text())
        except (OSError, json.JSONDecodeError):
            entry.unlink(missing_ok=True)
            continue
        owner_pid = data.get("owner_pid")
        tmpdir = data.get("tmpdir")
        if not isinstance(owner_pid, int) or not isinstance(tmpdir, str):
            entry.unlink(missing_ok=True)
            continue
        if _owner_alive(owner_pid):
            continue
        log(f"  • reaping orphaned test tmpdir {tmpdir} (owner pid {owner_pid} is dead)")
        reap_orphan(tmpdir)
        entry.unlink(missing_ok=True)
        reaped += 1
    return reaped
