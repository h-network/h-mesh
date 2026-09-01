"""Shared identity validation for daemon CLIs and daemon entrypoints."""

import argparse
import os
from collections.abc import Mapping

from core.keys import validate_segment


def identity_arg(value: str) -> str:
    """Argparse type for pod/tenant values, using the Redis-key contract."""
    try:
        return validate_segment(value)
    except KeyError as exc:
        raise argparse.ArgumentTypeError(
            "must be a lowercase name of 1-63 letters, digits, or hyphens"
        ) from exc


def require_daemon_identity(env: Mapping[str, str] | None = None) -> tuple[str, str]:
    """Return a valid daemon namespace or exit before daemon work begins."""
    source = os.environ if env is None else env
    pod, tenant = source.get("POD"), source.get("TENANT")
    try:
        return validate_segment(pod), validate_segment(tenant)
    except KeyError as exc:
        raise SystemExit(
            "error: POD and TENANT must both be valid non-empty names"
        ) from exc
