"""Filesystem paths and workdir resolution for h-mesh."""

import os


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
