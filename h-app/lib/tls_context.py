"""TLS client context with an optional private CA -- verification is never off.

One place for "talk https to another h-mesh door": the office CLI (office-add,
send --office, offices --check) and the relay daemon both build their
`ssl.SSLContext` here, so they trust the same things.

Rules, deliberately not configurable:
  * certificate AND host-name verification are always on (CERT_REQUIRED,
    check_hostname) -- there is no insecure switch in this module;
  * with no CA file the system trust store is used;
  * with a CA file ONLY that CA is trusted (the system store is not added), so a
    per-office CA pins that office to its own issuer;
  * nothing here downgrades to plain http.

The environment default is H_MESH_CA_CERT (same name the Telegram client
reads); a per-office value, when there is one, wins over it.
"""

from __future__ import annotations

import os
import ssl
from collections.abc import Mapping

ENV_CA_CERT = "H_MESH_CA_CERT"
_MAX_CA_FILE_BYTES = 4 * 1024 * 1024


class TlsConfigError(ValueError):
    """A CA file that cannot be used; the message is plain and secret-free."""


def check_ca_file(path: str) -> str:
    """Validate `path` as a readable PEM CA bundle; return it unchanged.

    Raises TlsConfigError saying what is wrong. Loads it for real, so a file that
    merely looks like PEM but holds no usable certificate is refused too.
    """
    if not isinstance(path, str) or not path.strip():
        raise TlsConfigError("the CA certificate path is empty")
    try:
        with open(path, "rb") as handle:
            head = handle.read(_MAX_CA_FILE_BYTES + 1)
    except FileNotFoundError:
        raise TlsConfigError(f"CA certificate {path!r} does not exist") from None
    except IsADirectoryError:
        raise TlsConfigError(f"CA certificate {path!r} is a directory, not a PEM file") from None
    except OSError as exc:
        raise TlsConfigError(f"cannot read CA certificate {path!r}: {exc.strerror or exc}") from None
    if len(head) > _MAX_CA_FILE_BYTES:
        raise TlsConfigError(f"CA certificate {path!r} is too large to be a CA bundle")
    if b"-----BEGIN CERTIFICATE-----" not in head:
        raise TlsConfigError(f"CA certificate {path!r} is not PEM (no BEGIN CERTIFICATE block)")
    try:
        ssl.create_default_context(cafile=path)
    except (ssl.SSLError, OSError) as exc:
        raise TlsConfigError(f"CA certificate {path!r} holds no usable certificate: {exc}") from None
    return path


def resolve_ca_file(per_office: str | None, environ: Mapping[str, str] | None = None) -> str | None:
    """The CA file to use: the per-office value, else H_MESH_CA_CERT, else None."""
    if per_office:
        return per_office
    value = (os.environ if environ is None else environ).get(ENV_CA_CERT, "").strip()
    return value or None


def build_context(ca_file: str | None = None) -> ssl.SSLContext:
    """A verifying client context. `ca_file` None -> the system trust store."""
    if ca_file:
        check_ca_file(ca_file)
        return ssl.create_default_context(cafile=ca_file)
    return ssl.create_default_context()
