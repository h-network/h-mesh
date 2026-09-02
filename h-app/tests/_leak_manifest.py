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

⚠ REVIEWER BLOCKING FAIL, 2026-09-02, MOST SEVERE FINDING: an earlier
version passed ANY string a manifest JSON file claimed as `tmpdir` straight
to `shutil.rmtree` once its owner read as dead. `MANIFEST_DIR` is
world-writable, so a stale, corrupt, or deliberately forged entry could
name a git checkout, another agent's working directory, or any writable
tree, and every pytest session on the box would delete it -- not a bug that
has to fire twice, one file is enough. See `_validated_tmpdir` for the fix
and, just as importantly, for what it does NOT close: naming checks
(prefix, direct child of `/tmp`, matching the manifest filename) are not
ownership checks, and even lstat-plus-uid is proof of conformance and
same-user trust, not proof of exclusive ownership -- read that function's
own docstring before assuming this validation means more than it does.

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

⚠ THAT DELEGATION ALONE WAS NOT ENOUGH, next reviewer pass, same day:
`stop_daemons()` processes its known daemons sequentially and stops
whichever DO authenticate even if a later one in the same run_dir fails --
so calling it directly could stop and remove evidence for some daemons
while the whole tmpdir was reported as untouched, contradicting the
whole-entry preflight-before-any-kill property this module promises. See
`reap_orphan`'s own docstring for the restored read-only preflight and,
just as importantly, for the honest residual it does NOT close (the brief
gap between that preflight and `stop_daemons`' own re-authentication) --
this paragraph describes only the TOCTOU-on-signalling half of the fix, not
the whole-entry half, and should not be read as claiming the stronger
property on its own.

⚠ The same reviewer pass, same reasons, on two smaller gaps: an entry whose
OWNER authentication itself failed at registration time (`owner_start_time`
recorded as `None`) was silently treated as "still running" forever --
correct to never reap on that basis, wrong to make it indistinguishable from
a live entry; and a manifest entry with malformed/unreadable JSON was
deleted outright, destroying the only surviving record of whatever it
pointed at. Both now count as `stuck` and stay on disk, logged, exactly like
an unauthenticatable daemon pid does -- see `reap_all_orphans`.

⚠ Two sessions starting and reaping at once: every action taken here
RE-AUTHENTICATES FRESH at the moment of that action -- via pidfd for a
signal, via a freshly-opened directory fd for a deletion (see
`_fd_safe_remove_owned_tmpdir`) -- never trusting a value read earlier in
the race, which is what actually makes concurrent reaping safe. Two
reapers independently authenticating the same genuinely-dead entry reach
the same conclusion and do the same idempotent work twice (a pidfd opened
against an already-exited pid raises `ProcessLookupError`, caught; opening
a directory fd against an already-removed tree raises `OSError`, caught;
unlinking an already-unlinked manifest entry is a no-op) -- safe. A
registration race (one process mid-`register()` while another reaps) cannot
produce a corrupt read either: `register()` writes to a temp file and
`os.replace()`s it into place, so a concurrent reader only ever sees the
manifest directory either without the entry yet, or with it fully written --
never partial.
"""

from __future__ import annotations

import errno
import json
import os
import select
import signal
import stat
import time
from pathlib import Path

from services.daemons import ALL_DAEMON_MODULES, stop_daemons

MANIFEST_DIR = Path("/tmp/h_mesh_test_manifests")
_OWNED_ROOT = Path("/tmp")
_OWNED_PREFIX = "h_mesh_test_"


def _validated_tmpdir(entry: Path, tmpdir_str: str) -> Path | None:
    """`tmpdir_str`, as an owned `Path`, ONLY if it names-and-is-proven-to-be
    a directory this scheme could plausibly have created -- otherwise
    `None`, and the caller must treat the entry as STUCK, never touching
    the claimed path.

    ⚠ REVIEWER BLOCKING FAIL, 2026-09-02: without this, `reap_orphan()`
    trusted an arbitrary string read from a JSON file inside
    `MANIFEST_DIR` -- a world-writable shared directory ANY process on this
    box can drop a file into -- as a `shutil.rmtree` target. A well-typed
    stale, corrupt, or deliberately forged entry could name a git checkout,
    another agent's working directory, or any writable tree, and every
    pytest session on the box would delete it, silently, forever.

    ⚠ ARCHITECT'S FOLLOW-UP, same day: a first version of this checked only
    NAMING facts -- direct child of `/tmp`, the expected prefix, matching
    the manifest filename. Checking the name is not checking ownership:
    `/tmp` is world-writable, so anything on this box can create a
    correctly-named directory and a matching manifest entry, and every
    naming check above would still pass. What actually establishes
    ownership, added here: the resolved target must be lstat'd as a REAL
    DIRECTORY, not a symlink (a symlink named correctly can point anywhere,
    and validating the link's resolved path before deleting through the
    link itself is a TOCTOU that checking the name harder cannot close);
    and its `st_uid` must equal this process's own uid, so a directory
    planted by a different user fails. Resolution happens once, before any
    check, and every check runs against that SAME resolved path within
    THIS function's own lifetime.

    ⚠ REVIEWER BLOCKING FAIL, 2026-09-02: an earlier version of this
    docstring claimed that resolving once here meant "a symlinked path
    component swapped in later cannot move the target underneath an
    already-passed check" -- FALSE as a description of the whole pipeline,
    and correctly rejected. This function returns a `Path`, a plain
    pathname; the caller (at the time, `reap_orphan`) went on to call
    `shutil.rmtree(path, ...)`, which is itself pathname-based and
    RE-RESOLVES every component from scratch when it actually runs, at
    whatever point later that happens to be -- nothing about validating
    here bound that later, separate resolution to what was checked in this
    function. A same-uid process could rename the validated directory away
    and rename a different, same-owned, correctly-named directory into
    that exact pathname in the gap between this function returning and the
    deletion actually starting, and the replacement -- not the original --
    is what would be removed. This function's job is now understood
    correctly as a cheap, EARLY plausibility filter (is this claim even
    shaped right, is it even worth acting on) -- not the thing that binds
    the eventual destructive action to a specific inode. That binding is
    `_fd_safe_remove_owned_tmpdir`'s job: it re-verifies fresh, at the
    actual moment of deletion, via an opened directory fd rather than a
    pathname, and its own docstring is where the honest residual (and what
    actually closes the rest of the gap) belongs -- not here.

    ⚠ THE RESIDUAL THIS FUNCTION ALONE LEAVES, stated plainly: this is
    proof of CONFORMANCE plus SAME-USER TRUST, not proof of exclusive
    ownership, and not a TOCTOU-safe binding to the eventual deletion by
    itself. Another process running as this same uid -- another agent's
    test code, or this scheme's own leaked code -- can still deliberately
    create a real, correctly-named, same-owned directory and a matching
    manifest entry; no path-level check distinguishes that from a
    directory this scheme actually created itself, because by construction
    there is nothing else to distinguish it by. What THIS function closes
    is an ACCIDENT or an OUT-OF-SCOPE target (a git checkout, a different
    user's files, a typo'd or corrupted path) being reachable at all --
    it does not, and cannot from path checks alone, close a deliberate
    same-user forgery, and it does not by itself close the gap between
    validation and deletion; see `_fd_safe_remove_owned_tmpdir` for what
    does.
    """
    try:
        tmpdir = Path(tmpdir_str)
    except (TypeError, ValueError):
        return None
    if not tmpdir.is_absolute():
        return None
    try:
        resolved = tmpdir.resolve(strict=False)
    except (OSError, RuntimeError):
        return None
    try:
        relative = resolved.relative_to(_OWNED_ROOT)
    except ValueError:
        return None
    if len(relative.parts) != 1:
        return None  # not a DIRECT child of _OWNED_ROOT -- e.g. a nested escape
    if not relative.parts[0].startswith(_OWNED_PREFIX):
        return None
    if resolved.name != entry.stem:
        return None  # claimed tmpdir doesn't match the manifest file that named it
    try:
        st = os.lstat(resolved)
    except OSError:
        return None
    if not stat.S_ISDIR(st.st_mode):
        return None  # not a real directory -- includes a symlink, which lstat never follows
    if st.st_uid != os.getuid():
        return None  # conforms in name only; owned by a different user
    return resolved


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


def _pid_confirmed_gone(pid: int) -> bool:
    """True ONLY if `pid` is confirmed to no longer exist at all (ENOENT on
    `/proc/<pid>`) -- never true for a read that merely failed for some
    OTHER reason (permission denied, a malformed/unexpected stat, any other
    I/O error).

    ⚠ ARCHITECT'S FOLLOW-UP, same day: `_process_start_time` returning
    `None` is not, by itself, proof a process is gone -- it also returns
    `None` on a read that failed for an unrelated reason. Callers that
    treated "current start time is None" as "confirmed dead" (both
    `_owner_status` and `_daemon_pidfile_preflight` did) would then let
    ANYTHING that makes `/proc/<pid>/stat` transiently unreadable
    masquerade as the safe, provably-gone case -- reopening the exact
    TOCTOU/misidentification class finding 3 closed, from the read side
    instead of the deletion side. This function is the one place that
    distinguishes "confirmed absent" from "merely unreadable," so every
    caller asks the narrower, correct question explicitly rather than
    inferring it from a `None`.
    """
    try:
        os.stat(f"/proc/{pid}")
    except FileNotFoundError:
        return True
    except OSError:
        return False  # exists but unreadable for some other reason -- NOT confirmed gone
    return False


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
    "running"; a mismatch (a live pid that isn't the one we recorded) or
    the pid being CONFIRMED gone (see `_pid_confirmed_gone` -- never merely
    "the read failed for some other reason") means the original owner is
    provably gone ("dead") regardless of who holds that pid number now. A
    read that failed WITHOUT confirming absence is "unverifiable", not
    "dead" -- collapsing those two would let anything that makes
    `/proc/<pid>/stat` transiently unreadable masquerade as the safe,
    provably-gone case.
    """
    if owner_start_time is None:
        return "unverifiable"
    current = _process_start_time(owner_pid)
    if current is None:
        return "dead" if _pid_confirmed_gone(owner_pid) else "unverifiable"
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


_TMUX_EXACT_NAME = "tmux"


def _matching_tmux_pids(socket_path: str) -> list[int]:
    """Pids whose CURRENT argv contains an exact `-S socket_path` pair AND
    whose argv[0] basename is EXACTLY `tmux` -- never a substring check on
    either field.

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

    ⚠ REVIEWER BLOCKING FAIL, 2026-09-02, on the SAME shape one field over:
    an earlier version accepted argv[0] whenever it merely CONTAINED the
    bytes "tmux" -- an executable literally named `notmux`, invoked with
    the exact `-S socket_path` pair, would satisfy that and get killed. A
    pidfd proves an action is delivered to one specific process LIFETIME;
    it proves nothing about what PROGRAM that lifetime is running. Fixed to
    an exact basename comparison against argv[0] -- not `in`, `==`.
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
        if not argv:
            continue
        try:
            flag_index = argv.index(b"-S")
        except ValueError:
            continue
        if flag_index + 1 >= len(argv):
            continue
        if argv[flag_index + 1].decode(errors="replace") != socket_path:
            continue
        argv0_name = os.path.basename(argv[0].decode(errors="replace"))
        if argv0_name != _TMUX_EXACT_NAME:
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
        # Secondary, EXACT confirmation beyond the argv match -- resolves
        # through wrapper symlinks to whatever binary actually runs (a
        # real, low-probability blind spot a wrapper chain could exploit,
        # not closed by this), but the comparison itself is exact, not a
        # substring: an executable named `notmux` must not satisfy this
        # any more than it satisfies the argv[0] check above.
        try:
            exe = os.readlink(f"/proc/{pid}/exe")
        except OSError:
            return False
        return os.path.basename(exe) == _TMUX_EXACT_NAME

    for pid in _matching_tmux_pids(socket_path):
        _pidfd_kill_if_matches(pid, verify, log=log, context=f"tmux server for {socket_path}")


def _daemon_pidfile_preflight(pidfile: Path) -> tuple[str, str]:
    """Read-only: what would `services.daemons` do with this pidfile right
    now? Returns `(outcome, reason)`, outcome one of:

      "owned"        -- authenticates; stop_daemons will signal it.
      "stale"        -- the pid is CONFIRMED to no longer exist at all
                         (see `_pid_confirmed_gone` -- never merely "the
                         read failed for some other reason"); stop_daemons
                         will safely remove the stale pidfile, no signal
                         sent. SAFE to proceed on -- there is nothing alive
                         here to misidentify.
      "unowned-name" -- the pidfile's own name isn't a key stop_daemons
                         recognizes at all (see `reap_orphan`'s enumeration
                         check) -- not decided here, callers must check
                         separately; kept out of this function because it
                         doesn't require reading the file.
      "unverifiable" -- identity missing/corrupt/mismatched pid, OR the pid
                         is alive but its CURRENT start time does not match
                         what was recorded (a live, reused number) -- the
                         one genuinely dangerous case. NOT safe to proceed.

    ⚠ REVIEWER BLOCKING FAIL, 2026-09-02: an earlier version collapsed
    "stale" (pid confirmed gone -- the safe, ordinary case a daemon from a
    killed pytest process naturally reaches on its own before the next
    session even runs) into the same "do not proceed" bucket as
    "unverifiable" (genuinely ambiguous: alive, but not the process we
    think it is). That made every ordinary already-exited daemon
    permanently stuck -- the ENTRY never reaped, noise every session,
    forever -- when `stop_daemons()` itself already handles a stale pidfile
    correctly and safely (removes it, signals nothing, since there is
    nothing alive to misidentify). Only "unverifiable" may block the whole
    entry; "stale" is a green light exactly like "owned" is.

    ⚠ ARCHITECT'S FOLLOW-UP, same day: "the pid no longer exists" must mean
    CONFIRMED absent, not "a read failed." `_process_start_time` returns
    `None` on any read failure, not only a confirmed-gone one (permission
    denied, a malformed stat line, any other I/O error) -- collapsing
    those into "stale" would let anything that makes `/proc/<pid>/stat`
    transiently unreadable masquerade as the safe, provably-gone case,
    reopening the exact TOCTOU/misidentification class finding 3 closed,
    from the read side rather than the deletion side. `_pid_confirmed_gone`
    is checked explicitly whenever `_process_start_time` returns `None`,
    rather than treating that `None` itself as proof of absence.
    """
    try:
        pid = int(pidfile.read_text().strip())
    except (ValueError, OSError):
        return "unverifiable", "pidfile unreadable or non-numeric"
    identity_path = pidfile.with_suffix(pidfile.suffix + ".identity")
    try:
        identity = json.loads(identity_path.read_text())
    except OSError:
        return "unverifiable", f"no {identity_path.name} sidecar"
    except json.JSONDecodeError:
        return "unverifiable", f"{identity_path.name} is not valid JSON"
    if not isinstance(identity, dict) or identity.get("pid") != pid:
        return "unverifiable", f"{identity_path.name} does not name pid {pid}"
    recorded = identity.get("start_time")
    if not isinstance(recorded, str):
        return "unverifiable", f"{identity_path.name} has no recorded start_time"
    current = _process_start_time(pid)
    if current is None:
        if _pid_confirmed_gone(pid):
            return "stale", f"pid {pid} confirmed no longer exists -- safe, ordinary stale pidfile"
        return "unverifiable", f"pid {pid}'s /proc entry could not be read (not confirmed gone)"
    if current != recorded:
        return "unverifiable", f"pid {pid} start_time does not match -- number was reused"
    return "owned", "ok"


def _open_verified_directory_fd(path: Path) -> tuple[int, os.stat_result] | None:
    """Open `path` as a directory fd with `O_NOFOLLOW`, re-verifying real
    directory + owned-uid at the MOMENT of opening -- not trusting an
    earlier validation to still describe reality. Returns `(fd, stat)` on
    success (caller must close the fd); `None`, nothing left open, on any
    failure."""
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
    except OSError:
        os.close(fd)
        return None
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        os.close(fd)
        return None
    return fd, st


def _remove_tree_via_fd(dir_fd: int, log=print) -> bool:
    """Recursively remove everything reachable from `dir_fd`, entirely
    through `os.*at()`-style fd-relative calls (`dir_fd=...`) -- never by
    re-resolving a pathname from the top. Does NOT remove `dir_fd`'s own
    directory entry; a directory cannot rmdir itself, the caller does that
    via the PARENT's fd once this returns.

    ⚠ REVIEWER BLOCKING FAIL, 2026-09-02: an earlier version caught and
    silently discarded every error at every level (`listdir`, `stat`,
    child `open`, `unlink`, `rmdir`) and returned nothing -- the caller had
    no way to know whether the tree was actually fully removed, and went
    on to report success regardless. "Cleanup becoming hiding": a
    permission error, an unexpected I/O error, or a concurrent mutation
    could leave part of the tree behind while the manifest entry -- the
    only surviving record of it -- got cleared anyway. Returns `True` ONLY
    if every entry was confirmed removed (or was confirmed already gone,
    `FileNotFoundError` specifically -- the same "confirmed absent, not
    merely unreadable" distinction as `_pid_confirmed_gone`); `False` if
    anything could not be removed, and the caller MUST treat that as "stop,
    do not finalize, leave this stuck and visible" rather than assume the
    directory is actually empty.

    Each child directory's removal also re-verifies, via a fresh
    `os.stat(name, dir_fd=dir_fd)` immediately before its own `rmdir`, that
    the name still identifies the SAME inode this function just finished
    recursing into and emptying -- narrowing (not eliminating; see
    `_fd_safe_remove_owned_tmpdir`'s own docstring on the residual) the
    window for a concurrent same-uid rename to substitute a different
    directory into that name while this was still working underneath it.

    ⚠ A KNOWN, BENIGN, RECURRING CAUSE OF "Directory not empty" specifically
    (`errno.ENOTEMPTY`) on this rmdir, found the same day this
    error-propagation fix landed and worth naming explicitly rather than
    re-derived by whoever sees it next: a test tmux pane's fake-agent shell
    (`bash -il`) survives its own tmux SERVER being killed -- SIGKILLing
    the server does not SIGKILL its child shell, the kernel delivers SIGHUP
    to the pty's foreground process instead, and that shell's own
    SIGHUP-triggered exit sequence can write a fresh `.bash_history` a few
    milliseconds AFTER this function's own recursive pass already found
    that directory empty, landing in the gap before the parent's `rmdir`
    call. This is not a defect in this deletion logic -- it is a real race
    this fix made OBSERVABLE for the first time (the previous,
    error-swallowing version silently hid it, the exact "cleanup becoming
    hiding" shape this whole fix exists to close) rather than one this fix
    introduced. Architect's explicit call: do not add a settle/retry here
    to paper over it -- a timeout against an unbounded event (how long a
    shell takes to flush history under load) is a fail-open path wearing a
    fail-closed appearance, the same class removed elsewhere tonight, and
    it would be indistinguishable at this layer from a directory that is
    genuinely, permanently not empty. The correct behavior is exactly what
    already happens: detected, retained (not falsely reported clean),
    counted stuck with this reason, and cleanly reaped on the NEXT session
    once the shell's own SIGHUP handling has finished -- self-resolving by
    design, not a bug needing a fix at this layer.
    """
    ok = True
    try:
        names = os.listdir(dir_fd)
    except OSError as exc:
        log(f"  • could not list directory contents ({exc}) -- treating as incomplete")
        return False
    for name in names:
        try:
            entry_st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue  # confirmed already gone -- a concurrent idempotent pass, fine
        except OSError as exc:
            log(f"  • could not stat {name!r} ({exc}) -- treating as incomplete")
            ok = False
            continue
        if stat.S_ISDIR(entry_st.st_mode):
            try:
                child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
            except FileNotFoundError:
                continue
            except OSError as exc:
                log(f"  • could not open child directory {name!r} ({exc}) -- treating as incomplete")
                ok = False
                continue
            try:
                child_st = os.fstat(child_fd)
                if (child_st.st_dev, child_st.st_ino) != (entry_st.st_dev, entry_st.st_ino):
                    log(f"  • {name!r} changed between stat and open -- not descending, treating as incomplete")
                    ok = False
                    continue
                if not _remove_tree_via_fd(child_fd, log=log):
                    ok = False
                    continue
            finally:
                os.close(child_fd)
            try:
                recheck_st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            except OSError as exc:
                log(f"  • could not re-verify {name!r} before removing it ({exc}) -- treating as incomplete")
                ok = False
                continue
            if (recheck_st.st_dev, recheck_st.st_ino) != (entry_st.st_dev, entry_st.st_ino):
                log(f"  • {name!r} no longer identifies the directory just emptied -- not removing it")
                ok = False
                continue
            try:
                os.rmdir(name, dir_fd=dir_fd)
            except FileNotFoundError:
                continue
            except OSError as exc:
                hint = ""
                if getattr(exc, "errno", None) == errno.ENOTEMPTY:
                    hint = (
                        " -- KNOWN BENIGN CLASS: a killed tmux pane's shell can flush "
                        "history via SIGHUP after the server itself is confirmed dead; "
                        "see this function's own docstring. Retries cleanly next session."
                    )
                log(f"  • could not remove directory {name!r} ({exc}){hint} -- treating as incomplete")
                ok = False
        else:
            try:
                os.unlink(name, dir_fd=dir_fd)
            except FileNotFoundError:
                continue
            except OSError as exc:
                log(f"  • could not remove {name!r} ({exc}) -- treating as incomplete")
                ok = False
    return ok


def _fd_safe_remove_owned_tmpdir(tmpdir: Path, log=print) -> bool:
    """Remove `tmpdir` entirely through fd-relative operations bound to one
    freshly-opened, freshly-verified inode -- never a bare pathname-based
    `shutil.rmtree`.

    ⚠ REVIEWER BLOCKING FAIL, 2026-09-02: an earlier version validated
    `tmpdir` once (`_validated_tmpdir`) and then called
    `shutil.rmtree(tmpdir, ...)` -- a PATHNAME-based call that re-resolves
    every path component itself, from scratch, at the moment it actually
    runs, regardless of what validation observed earlier. A same-uid
    process (another agent's test code, or this scheme's own leaked code --
    see `_validated_tmpdir`'s own residual paragraph) could rename the
    validated directory away and rename a DIFFERENT real, same-owned
    directory into that exact pathname in the gap between validation and
    the rmtree call actually starting -- the replacement, not the
    validated original, is what would get recursively removed. On an
    automatic destructive hook running at the start of every session on a
    shared box, same-uid concurrency is not a hypothetical, it is the
    actual deployment model.

    Fixed by binding the TOP-LEVEL deletion to one inode: open with
    `O_NOFOLLOW` (refuses a symlink substituted in), confirm the
    freshly-opened fd's own `fstat` is still a real directory owned by this
    uid, then walk and delete everything beneath it via `os.*at()`-family
    calls relative to that fd rather than re-resolving a name from `/tmp`
    downward. This is the fix for the arbitrary-tree escape specifically:
    traversal is CONFINED beneath the opened root fd, full stop -- nothing
    reachable from here can redirect outside the validated tree.

    ⚠ NARROWER CLAIM THAN "everything is inode-bound," per reviewer's
    follow-up: descending through the fd confines WHERE deletion can reach,
    it does not by itself bind every individual descendant ACTION to the
    inode inspected for it. `_remove_tree_via_fd` adds an explicit
    inode-recheck immediately before each child DIRECTORY's own `rmdir`
    (narrows, does not eliminate, the same class of gap this function's
    own final step has). A leaf (non-directory) entry gets a plain
    stat-then-unlink by name -- POSIX has no fd-bound unlink, so there is
    no stronger primitive available for that case; concurrent replacement
    of a single file within the already-confined, owned tree remains
    possible, and it says so rather than implying otherwise. The one step
    that cannot be fd-relative at all -- removing `tmpdir`'s own top-level
    directory entry, since a directory cannot rmdir itself and the
    PARENT's fd + name must be used -- gets the same recheck-immediately-
    before-the-syscall treatment. Every one of these final gaps (between a
    recheck and its syscall) is the narrowest achievable without the
    kernel offering an atomic "remove this exact inode, wherever its name
    currently points" primitive; stated here rather than implied closed.
    """
    verified = _open_verified_directory_fd(tmpdir)
    if verified is None:
        log(f"  • STUCK, leaving {tmpdir}: failed re-verification immediately before deletion")
        return False
    dir_fd, expected_st = verified
    try:
        contents_removed = _remove_tree_via_fd(dir_fd, log=log)
    finally:
        os.close(dir_fd)
    if not contents_removed:
        log(f"  • STUCK, leaving {tmpdir}: contents were not fully removed -- not attempting to remove the directory itself")
        return False

    return _finalize_directory_removal(tmpdir, expected_st, log=log)


def _finalize_directory_removal(tmpdir: Path, expected_st: os.stat_result, log=print) -> bool:
    """The one step in `_fd_safe_remove_owned_tmpdir` that cannot be
    fd-relative -- a directory cannot rmdir itself, so removing `tmpdir`'s
    own entry requires the PARENT's fd plus `tmpdir`'s name. Split out as
    its own function specifically so this recheck can be tested directly
    against an already-swapped end state (a structural proof that the
    recheck itself refuses correctly), rather than only via a real,
    unreliable race against a background thread.

    Re-verifies the name still refers to the SAME inode `expected_st`
    describes IMMEDIATELY before the single `rmdir` syscall -- if a
    same-uid process renamed the original away and renamed a different,
    real directory into this exact name in the meantime, this refuses
    rather than remove the substitute.

    ⚠ REVIEWER BLOCKING FAIL, 2026-09-02: an earlier version caught EVERY
    `OSError` from the pre-check stat and from the final `rmdir` itself and
    returned `True` regardless -- so a permission error, the directory
    turning out non-empty (contents that failed to remove upstream), or
    any other real failure at the FINAL syscall was reported as success.
    `reap_all_orphans` then unlinked the manifest entry -- the only
    surviving record -- while the directory could still be sitting there
    on disk: cleanup silently becoming hiding. Only a CONFIRMED-gone
    pre-check stat (`FileNotFoundError` specifically -- the same
    "confirmed absent, not merely unreadable" distinction used elsewhere in
    this module) is treated as the safe "already removed" case; every
    other stat failure, and any failure from the `rmdir` call itself, now
    returns `False` and is logged, not swallowed.
    """
    try:
        parent_fd = os.open(str(_OWNED_ROOT), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        log(f"  • STUCK, leaving {tmpdir}: could not open {_OWNED_ROOT} to remove its own directory entry ({exc})")
        return False
    try:
        try:
            current_st = os.stat(tmpdir.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            # Confirmed already gone -- a concurrent reaper finished this
            # same, genuinely dead entry first; idempotent, not an error.
            return True
        except OSError as exc:
            log(f"  • STUCK, leaving {tmpdir}: could not stat it before the final removal ({exc})")
            return False
        if (current_st.st_dev, current_st.st_ino) != (expected_st.st_dev, expected_st.st_ino):
            log(f"  • STUCK, leaving {tmpdir}: the name now refers to a different inode than the one just emptied -- not removing it")
            return False
        try:
            os.rmdir(tmpdir.name, dir_fd=parent_fd)
        except FileNotFoundError:
            return True  # confirmed already gone, e.g. a concurrent reaper won the race
        except OSError as exc:
            log(f"  • STUCK, leaving {tmpdir}: final rmdir failed ({exc}) -- NOT reporting success")
            return False
        return True
    finally:
        os.close(parent_fd)


def reap_orphan(tmpdir: Path, log=print) -> bool:
    """The same cleanup every test's own `finally` already does for its own
    tmpdir -- stop every daemon recorded under `<tmpdir>/run/*.pid` (through
    `services.daemons.stop_daemons`, the reviewed pidfd-authenticated
    implementation -- not reimplemented here), kill the isolated tmux
    server bound to `<tmpdir>/isolated.sock`, remove the tree.

    `tmpdir` MUST already be a validated `Path` (see `_validated_tmpdir`) --
    this function does not itself re-validate that the target is an owned
    directory, by design, so every call site is required to have done that
    check with its own `entry` context first.

    ⚠ REVIEWER BLOCKING FAIL, 2026-09-02, against an earlier version:
    `stop_daemons()` processes ALL_DAEMON_MODULES sequentially and stops
    whichever pidfiles DO authenticate even if a LATER one in the same
    run_dir fails -- so calling it directly and only checking for leftover
    pidfiles AFTERWARD could stop and remove evidence for some daemons
    while the whole entry is reported as untouched and stuck, exactly
    contradicting the whole-entry preflight-before-any-kill contract this
    module promises. Restored here: every pidfile under run_dir is
    preflighted (read-only, see `_daemon_pidfile_preflight`) BEFORE
    `stop_daemons` is ever called; if even one is "unverifiable",
    `stop_daemons` is never invoked for this run_dir at all, and NOTHING
    here is touched -- no daemon, no tmux server, no directory. This
    narrows the failure window from "however long stop_daemons takes to
    process the whole set" down to the brief gap between this preflight and
    stop_daemons' own independent, per-daemon pidfd-bound
    re-authentication.

    ⚠ REVIEWER BLOCKING FAIL, 2026-09-02, SAME PASS, TWO MORE GAPS in that
    preflight, both now fixed here:

    (a) It enumerated every `run/*.pid` file, but `stop_daemons()` only
    ever processes pidfiles whose NAME is a key in `ALL_DAEMON_MODULES` --
    it has no way to "own" or even notice a pidfile named anything else.
    An authenticated-but-unrecognized pidfile (e.g. `custom.pid`) used to
    pass preflight, `stop_daemons()` would then stop/remove evidence for
    every REAL daemon it does recognize while silently never touching the
    unrecognized one, and only the after-the-fact leftover check caught
    it -- by which point the partial teardown this preflight exists to
    prevent had already happened. Every pidfile's own name (its `.stem`)
    is now checked against `ALL_DAEMON_MODULES` as part of the preflight
    gate, not left to be discovered afterward.

    (b) "Stale" (the pid provably no longer exists at all) and
    "unverifiable" (identity missing/corrupt, or a LIVE pid whose identity
    doesn't match -- the genuinely dangerous, possibly-reused case) were
    collapsed into the same "do not proceed" outcome. A daemon from a
    killed pytest process routinely exits entirely on its own before the
    next session even runs -- `stop_daemons()` already handles that pidfile
    correctly and safely (removes it, signals nothing, since there is
    nothing alive to misidentify) -- so treating it as unverifiable made
    every ordinary already-exited daemon leave its ENTIRE entry stuck
    forever: the tmpdir and manifest entry never reaped, noise every
    session, permanently, for the single most common and least dangerous
    case a real orphan reaches. `_daemon_pidfile_preflight` now returns
    three outcomes and only "unverifiable" blocks the whole entry; "stale"
    is a green light exactly like "owned" is.
    """
    run_dir = tmpdir / "run"
    if run_dir.is_dir():
        pidfiles = sorted(run_dir.glob("*.pid"))
        for pidfile in pidfiles:
            if pidfile.stem not in ALL_DAEMON_MODULES:
                log(
                    f"  • STUCK, leaving {tmpdir} entirely: {pidfile.name} is not a name "
                    "stop_daemons recognizes -- it would never be touched, leaving it "
                    "unowned after other daemons here are stopped"
                )
                return False
            outcome, reason = _daemon_pidfile_preflight(pidfile)
            if outcome == "unverifiable":
                log(f"  • STUCK, leaving {tmpdir} entirely: {pidfile.name} failed preflight ({reason}) -- stop_daemons not called")
                return False
            # "owned" and "stale" are both safe to proceed on -- see
            # _daemon_pidfile_preflight's own docstring for why "stale"
            # must not be treated the same as "unverifiable".
        if pidfiles:
            stop_daemons(run_dir, log=log)
            remaining = sorted(p.name for p in run_dir.glob("*.pid"))
            if remaining:
                log(f"  • STUCK, leaving {tmpdir} entirely: still present after stop_daemons: {remaining}")
                return False
    socket_path = str(tmpdir / "isolated.sock")
    _kill_tmux_server_by_socket_path(socket_path, log=log)
    return _fd_safe_remove_owned_tmpdir(tmpdir, log=log)


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
        validated = _validated_tmpdir(entry, tmpdir)
        if validated is None:
            log(f"  • STUCK, leaving {entry}: claimed tmpdir {tmpdir!r} failed ownership validation -- not touching it")
            stuck += 1
            continue
        log(f"  • reaping orphaned test tmpdir {validated} (owner pid {owner_pid} authenticated dead)")
        if not reap_orphan(validated, log=log):
            stuck += 1
            continue  # left in place on purpose; entry stays for a later retry
        entry.unlink(missing_ok=True)
        reaped += 1
    return reaped, stuck
