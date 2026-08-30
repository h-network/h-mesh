"""Shared runtime paths for h-mesh core."""

import os
from pathlib import Path


def state_dir() -> Path:
    """Return the configurable per-user directory for durable local state."""
    configured = os.environ.get("H_MESH_STATE_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".h-mesh"


def state_path(name: str) -> Path:
    """Resolve a core state filename beneath :func:`state_dir`."""
    return state_dir() / name
