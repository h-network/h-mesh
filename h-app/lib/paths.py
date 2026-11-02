"""Filesystem paths and workdir resolution for h-mesh."""

import os
import pwd
import sys
from pathlib import Path


def get_workdir_root() -> str:
    """Resolve the base directory for agent workspaces.

    Resolution hierarchy:
    1. H_MESH_WORKDIR environment variable if explicitly set.
    2. $H_MESH_STATE_DIR/workdir if H_MESH_STATE_DIR is set.
    3. /workdir if it exists and is writable (container environment --
       unaffected by the ~/h-mesh relocation below; a real container mount
       is a different concept from a host install's fallback location).
    4. ~/h-mesh (host/non-root fallback) -- VISIBLE, the operator's own
       directory, on purpose: this is where an operator would look for an
       agent's actual working files. Not to be confused with the h-mesh
       SOURCE checkout itself, which installs to ~/.local/share/h-mesh
       (see install.sh) precisely so the two don't collide -- code and
       state (and now workdirs) are kept apart on purpose; an app
       reinstall must not touch a live workdir, and vice versa.

    ⚠ No migration or collision handling for a pre-existing ~/h-mesh
    checkout from before this default moved -- operator's explicit
    support policy: a legacy-default checkout is unsupported, and a
    managed box picks up the new layout by reinstall, not migration.
    """
    if "H_MESH_WORKDIR" in os.environ:
        return os.environ["H_MESH_WORKDIR"]
    if "H_MESH_STATE_DIR" in os.environ:
        return os.path.join(os.environ["H_MESH_STATE_DIR"], "workdir")
    if os.path.isdir("/workdir") and os.access("/workdir", os.W_OK):
        return "/workdir"
    home = os.environ.get("HOME", os.path.expanduser("~"))
    return os.path.join(home, "h-mesh")


def get_agent_workdir(agent_name: str, cwd: str | None = None) -> str:
    """Resolve the working directory for a specific agent."""
    if cwd:
        return cwd
    return os.path.join(get_workdir_root(), agent_name)


def resolve_workdir_owner(
    target: str | Path,
    workdir_root: str | Path | None = None,
) -> tuple[int, int] | None:
    """Return the non-root (uid, gid) that should own workspace paths when running as root.

    When a workspace provisioning step (`h-mesh-office clone-to-all`, a root
    setup/hire invocation, or `docker exec -u 0`) runs as root (`euid == 0`),
    creating directories or cloning a repository leaves them owned by `0:0`.
    Agents run as the unprivileged workspace user (e.g. `ubuntu`), so a
    root-owned clone triggers git's `detected dubious ownership` error and
    prevents creating `.venv` or editing files in-repo (issue #28).

    Resolution order when `os.geteuid() == 0`:
    1. Existing non-root owner of the nearest existing directory on the chain
       from `target` up through `workdir_root` (and its parent, e.g. `$HOME`).
    2. `SUDO_UID` / `SUDO_GID` environment variables if set to a non-root uid.
    3. The `ubuntu` user in the passwd database if present and non-root.
    """
    if os.geteuid() != 0:
        return None

    root_path = Path(workdir_root) if workdir_root is not None else Path(get_workdir_root())
    target_path = Path(target)

    candidates: list[Path] = []
    cur = target_path
    while True:
        candidates.append(cur)
        if cur == root_path or cur.parent == cur:
            break
        cur = cur.parent
    if root_path not in candidates:
        candidates.append(root_path)
    if root_path.parent != root_path:
        candidates.append(root_path.parent)

    for candidate in candidates:
        try:
            if os.path.islink(candidate) or not os.path.exists(candidate):
                continue
            st = os.lstat(candidate)
            if st.st_uid != 0:
                return (st.st_uid, st.st_gid)
        except OSError:
            continue

    raw_sudo_uid = os.environ.get("SUDO_UID", "").strip()
    if raw_sudo_uid.isdigit():
        sudo_uid = int(raw_sudo_uid)
        if sudo_uid > 0:
            raw_sudo_gid = os.environ.get("SUDO_GID", "").strip()
            if raw_sudo_gid.isdigit():
                return (sudo_uid, int(raw_sudo_gid))
            try:
                return (sudo_uid, pwd.getpwuid(sudo_uid).pw_gid)
            except KeyError:
                return (sudo_uid, sudo_uid)

    try:
        pw = pwd.getpwnam("ubuntu")
        if pw.pw_uid != 0:
            return (pw.pw_uid, pw.pw_gid)
    except KeyError:
        pass

    return None


def ensure_workdir_ownership(
    target: str | Path,
    workdir_root: str | Path | None = None,
) -> bool:
    """Ensure `target` (and any root-owned workspace ancestors) is owned by the workspace user.

    No-op (returns False) when not running as root or when no non-root
    workspace owner can be resolved. Uses `os.lchown` with `followlinks=False`
    so symlinks inside a repository are never followed outside `target`.
    """
    owner = resolve_workdir_owner(target, workdir_root=workdir_root)
    if owner is None:
        return False
    uid, gid = owner

    root_path = Path(workdir_root) if workdir_root is not None else Path(get_workdir_root())
    target_path = Path(target)

    ancestors: list[Path] = []
    cur = target_path.parent
    while cur != cur.parent:
        ancestors.append(cur)
        if cur == root_path:
            break
        cur = cur.parent
    for anc in reversed(ancestors):
        try:
            if not os.path.islink(anc) and os.path.exists(anc) and os.lstat(anc).st_uid == 0:
                os.lchown(anc, uid, gid)
        except OSError:
            pass

    if not os.path.lexists(target_path):
        return False

    changed = False
    try:
        os.lchown(target_path, uid, gid)
        changed = True
    except OSError:
        return False

    if os.path.isdir(target_path) and not os.path.islink(target_path):
        for dirpath, dirnames, filenames in os.walk(target_path, followlinks=False):
            for name in (*dirnames, *filenames):
                entry = os.path.join(dirpath, name)
                try:
                    os.lchown(entry, uid, gid)
                except OSError:
                    pass

    return changed


def resolve_venv_bin(venv_dir: str | Path | None = None) -> str:
    """Resolve the directory containing venv executables (e.g. h-mesh-office, python).

    Resolution hierarchy:
    1. Explicit venv_dir argument (either the venv root or venv's bin dir directly).
    2. VIRTUAL_ENV environment variable if set ($VIRTUAL_ENV/bin).
    3. Parent directory of sys.executable if running under a virtualenv/custom python.
    4. Repo-level .venv/bin if it exists.
    5. Fallback to sys.executable's parent directory.
    """
    if venv_dir:
        p = Path(venv_dir)
        if (p / "bin").is_dir():
            return str(p / "bin")
        return str(p)
    if os.environ.get("VIRTUAL_ENV"):
        return str(Path(os.environ["VIRTUAL_ENV"]) / "bin")

    candidate = Path(sys.executable).parent
    if str(candidate) in ("/usr/bin", "/bin", "/usr/local/bin"):
        repo_root = Path(__file__).resolve().parents[2]
        repo_venv_bin = repo_root / ".venv" / "bin"
        if repo_venv_bin.is_dir():
            return str(repo_venv_bin)
    return str(candidate)


def build_pane_path(
    venv_bin: str | Path | None = None,
    ambient_path: str | None = None,
) -> str:
    """Construct a complete, deterministic PATH for agent panes from known-required locations.

    Agent panes spawned by tmux run non-interactively without sourcing shell rc
    files (~/.bashrc / ~/.profile). A daemon started from a minimal shell (e.g.
    reboot, systemd, h-mesh start) has a stripped PATH that lacks user-level
    install locations like ~/.local/bin where h-agent is installed.

    Instead of blindly inheriting the daemon's ambient PATH, we assemble PATH
    from known-required locations in priority order:
    1. Virtualenv bin directory (where h-mesh-office, h-mesh, and repo tools live)
    2. User binary directories ($PREFIX/bin, ~/.local/bin, ~/bin where h-agent and user tools live)
    3. Any additional entries from the caller's ambient PATH
    4. Standard system binary directories (/usr/local/bin, /usr/bin, /bin, etc.)
    """
    resolved_bin = resolve_venv_bin(venv_bin)
    home_dir = os.environ.get("HOME", os.path.expanduser("~"))
    prefix_dir = os.environ.get("PREFIX")

    candidates: list[str] = []
    if resolved_bin:
        candidates.append(str(resolved_bin))

    if prefix_dir:
        candidates.append(os.path.join(prefix_dir, "bin"))

    if home_dir:
        candidates.append(os.path.join(home_dir, ".local", "bin"))
        candidates.append(os.path.join(home_dir, "bin"))

    raw_ambient = ambient_path if ambient_path is not None else os.environ.get("PATH", "")
    if raw_ambient:
        candidates.extend(raw_ambient.split(":"))

    candidates.extend([
        "/usr/local/bin",
        "/usr/local/sbin",
        "/usr/bin",
        "/usr/sbin",
        "/bin",
        "/sbin",
    ])

    seen: set[str] = set()
    deduped: list[str] = []
    for entry in candidates:
        if not entry:
            continue
        norm = os.path.normpath(entry)
        if norm not in seen:
            seen.add(norm)
            deduped.append(norm)

    return ":".join(deduped)
