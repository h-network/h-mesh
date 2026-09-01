"""Filesystem paths and workdir resolution for h-mesh."""

import os
import sys
from pathlib import Path


def get_workdir_root() -> str:
    """Resolve the base directory for agent workspaces.

    Resolution hierarchy:
    1. H_MESH_WORKDIR environment variable if explicitly set.
    2. $H_MESH_STATE_DIR/workdir if H_MESH_STATE_DIR is set.
    3. /workdir if it exists and is writable (container environment).
    4. ~/h-mesh/workdir (host/non-root fallback).
    """
    if "H_MESH_WORKDIR" in os.environ:
        return os.environ["H_MESH_WORKDIR"]
    if "H_MESH_STATE_DIR" in os.environ:
        return os.path.join(os.environ["H_MESH_STATE_DIR"], "workdir")
    if os.path.isdir("/workdir") and os.access("/workdir", os.W_OK):
        return "/workdir"
    home = os.environ.get("HOME", os.path.expanduser("~"))
    return os.path.join(home, "h-mesh", "workdir")


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
