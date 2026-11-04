"""Content-addressed document store: attach a document once, refer to it by hash.

A document is stored once per tenant under its SHA-256 and referred to by that
hash (or any unique prefix of it, at least ``MIN_PREFIX`` hex characters, or a
mutable ``name`` that points at the newest version). Messages and tickets then
carry ``doc:<hash>`` instead of re-pasting the body.

This is direct Redis state owned by the caller, like ``office status``; no
envelope crosses the switch, so payloads stay opaque to it. Three tenant-scoped
hashes: ``docs`` (hash -> bytes), ``docs.meta`` (hash -> JSON metadata) and
``docs.names`` (name -> newest hash).
"""

import hashlib
import json
import re
from datetime import datetime, timezone

from core.keys import prefix
from lib.attachment_schema import ATTACHMENT_MAX_BYTES

MIN_PREFIX = 8
REF_SCHEME = "doc:"
DISPLAY_LEN = 12
_HEX = re.compile(r"^[0-9a-f]+$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class DocError(ValueError):
    """A user-facing document-store error."""


def _text(value) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def _keys(pod: str, tenant: str) -> tuple[str, str, str]:
    return (
        prefix(pod, tenant, resource="docs"),
        prefix(pod, tenant, resource="docs.meta"),
        prefix(pod, tenant, resource="docs.names"),
    )


def short(doc_hash: str) -> str:
    return doc_hash[:DISPLAY_LEN]


def validate_name(name: str) -> str:
    if not _NAME.fullmatch(name):
        raise DocError(
            f"invalid doc name {name!r}: 1-128 chars of letters, digits, '.', '_' or '-', "
            "starting with a letter or digit"
        )
    if len(name) >= MIN_PREFIX and _HEX.fullmatch(name):
        raise DocError(f"doc name {name!r} would be indistinguishable from a hash prefix")
    return name


def put(
    r, *, pod: str, tenant: str, author: str, data: bytes, filename: str,
    mime_type: str, name: str | None = None,
) -> tuple[str, bool]:
    """Store ``data``; return ``(sha256_hex, newly_stored)``. Same bytes dedupe."""
    if len(data) > ATTACHMENT_MAX_BYTES:
        raise DocError(f"document size ({len(data)} bytes) exceeds maximum allowed size of {ATTACHMENT_MAX_BYTES} bytes")
    if name is not None:
        validate_name(name)
    docs, metas, names = _keys(pod, tenant)
    doc_hash = hashlib.sha256(data).hexdigest()
    created = bool(r.hsetnx(docs, doc_hash, data))
    if created:
        meta = {
            "filename": filename,
            "mime_type": mime_type,
            "size": len(data),
            "author": author,
            "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        r.hset(metas, doc_hash, json.dumps(meta))
    if name is not None:
        r.hset(names, name, doc_hash)
    return doc_hash, created


def resolve(r, *, pod: str, tenant: str, ref: str) -> str:
    """Resolve a name, ``doc:``-prefixed or bare hash, or unique hash prefix to a full hash."""
    ref = ref.strip()
    if ref.startswith(REF_SCHEME):
        ref = ref[len(REF_SCHEME):]
    if not ref:
        raise DocError("empty document reference")
    docs, _, names = _keys(pod, tenant)
    named = r.hget(names, ref)
    if named is not None:
        return _text(named)
    ref = ref.lower()
    if len(ref) < MIN_PREFIX or not _HEX.fullmatch(ref):
        raise DocError(f"no document named {ref!r} (a hash reference needs at least {MIN_PREFIX} hex characters)")
    matches = sorted(h for h in map(_text, r.hkeys(docs)) if h.startswith(ref))
    if not matches:
        raise DocError(f"no document matches {ref!r}")
    if len(matches) > 1:
        raise DocError(f"{ref!r} is ambiguous ({len(matches)} documents match); use more characters")
    return matches[0]


def get(r, *, pod: str, tenant: str, ref: str) -> tuple[str, dict, bytes]:
    doc_hash = resolve(r, pod=pod, tenant=tenant, ref=ref)
    docs, metas, _ = _keys(pod, tenant)
    data = r.hget(docs, doc_hash)
    if data is None:
        raise DocError(f"no document matches {ref!r}")
    raw_meta = r.hget(metas, doc_hash)
    meta = json.loads(_text(raw_meta)) if raw_meta else {}
    return doc_hash, meta, bytes(data)


def listing(r, *, pod: str, tenant: str) -> list[dict]:
    """Every document's metadata (no bodies), oldest first, with the names pointing at it."""
    _, metas, names = _keys(pod, tenant)
    by_hash: dict[str, list[str]] = {}
    for name, doc_hash in r.hgetall(names).items():
        by_hash.setdefault(_text(doc_hash), []).append(_text(name))
    rows = []
    for doc_hash, raw in r.hgetall(metas).items():
        doc_hash = _text(doc_hash)
        rows.append({"hash": doc_hash, **json.loads(_text(raw)), "names": sorted(by_hash.get(doc_hash, []))})
    return sorted(rows, key=lambda row: (row.get("created", ""), row["hash"]))
