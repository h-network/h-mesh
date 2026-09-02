"""Filesystem paths and workdir resolution for h-mesh."""

import os
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
    checkout from before this default moved -- operator's explicit call:
    we are the only people running h-mesh, there are no third-party
    installs to protect, and our own boxes get reinstalled, not migrated.
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
