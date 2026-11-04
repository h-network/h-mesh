"""Per-cooperation API tokens: one token per (sending office, this office) pair.

A cooperation token lets ONE remote office act as ONE enrolled api participant
of this office, and nothing else. It replaces the office-wide ``API_TOKEN`` for
that remote office, which would otherwise be able to send ``Command`` (pasted
raw into a terminal) or ``StartAgent`` to anything.

Shape of a token: ``coop.<token-id>.<secret>`` -- ``token-id`` is 16 lowercase
hex characters (public, used for lookup, listing and revocation), ``secret`` is
43 url-safe characters (``secrets.token_urlsafe(32)``). The full token is
returned by :func:`grant` exactly once and is never stored: the receiving side
keeps only the SHA-256 of the secret in the ``coop-tokens`` hash
(``token-id -> JSON``). A token is bound to exactly one participant and a
closed set of envelope kinds (``Message`` only in v0).

Scopes are enforced by ``modules/api/server.py``; this module only mints,
revokes, lists and verifies. :func:`verify` reads Redis on every call, so a
revocation takes effect on the next request.

Honest limit: this is policy, not storage security. All agents of an office
share one Redis with no ACL, so a hostile co-resident agent could write the
hash. Hashing protects against a leaked Redis dump or backup, not against it.
"""

import hashlib
import hmac
import json
import re
import secrets
import time

from core.keys import prefix, validate_segment

TOKEN_PREFIX = "coop"
ALLOWED_KINDS = frozenset({"Message"})
# Participants a cooperation can never be bound to: fixed doors, not clients.
RESERVED_PARTICIPANTS = frozenset({"api", "host"})

_TOKEN_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")


def coop_tokens_key(pod: str, tenant: str) -> str:
    return prefix(pod, tenant, resource="coop-tokens")


def _decode(value):
    return value.decode() if isinstance(value, bytes) else value


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _load(raw) -> dict | None:
    if raw is None:
        return None
    try:
        record = json.loads(_decode(raw))
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
        return None
    return record if isinstance(record, dict) else None


def grant(r, pod: str, tenant: str, participant: str, kinds=("Message",)) -> tuple[str, str]:
    """Mint a token bound to ``participant``. Returns ``(token_id, token)``.

    The token is the only copy of the secret; callers must show it once and
    drop it. Enrolling the participant (port_type ``api``) is the caller's
    job -- the server re-checks the registry on every use.
    """
    try:
        validate_segment(participant)
    except KeyError as exc:
        raise ValueError("participant must be a valid agent name") from exc
    if participant in RESERVED_PARTICIPANTS:
        raise ValueError(f"participant {participant!r} cannot be bound to a cooperation token")
    kinds = tuple(kinds)
    if not kinds or not set(kinds) <= ALLOWED_KINDS:
        raise ValueError(f"kinds must be a non-empty subset of {sorted(ALLOWED_KINDS)}")
    key = coop_tokens_key(pod, tenant)
    token_id = secrets.token_hex(8)
    secret = secrets.token_urlsafe(32)
    record = {
        "participant": participant,
        "sha256": _digest(secret),
        "kinds": sorted(set(kinds)),
        "created_ts": time.time(),
        "revoked": False,
    }
    r.hset(key, token_id, json.dumps(record, separators=(",", ":")))
    return token_id, f"{TOKEN_PREFIX}.{token_id}.{secret}"


def revoke(r, pod: str, tenant: str, token_id: str) -> bool:
    """Revoke a token. True if it existed and was not already revoked.

    The record is kept (marked revoked) for audit; verify() refuses it at once.
    """
    if not isinstance(token_id, str) or not _TOKEN_ID_RE.fullmatch(token_id):
        return False
    key = coop_tokens_key(pod, tenant)
    record = _load(r.hget(key, token_id))
    if record is None or record.get("revoked") is True:
        return False
    record["revoked"] = True
    record["revoked_ts"] = time.time()
    r.hset(key, token_id, json.dumps(record, separators=(",", ":")))
    return True


def list_tokens(r, pod: str, tenant: str) -> list[dict]:
    """Every token's public metadata. Never includes the digest or a secret."""
    rows = []
    for raw_id, raw in (r.hgetall(coop_tokens_key(pod, tenant)) or {}).items():
        record = _load(raw)
        if record is None:
            continue
        rows.append(
            {
                "token_id": _decode(raw_id),
                "participant": record.get("participant"),
                "kinds": list(record.get("kinds") or []),
                "created_ts": record.get("created_ts"),
                "revoked": bool(record.get("revoked")),
                "revoked_ts": record.get("revoked_ts"),
            }
        )
    return sorted(rows, key=lambda row: (row["created_ts"] or 0, row["token_id"]))


def verify(r, pod: str, tenant: str, bearer: str) -> dict | None:
    """Resolve a bearer string to ``{token_id, participant, kinds}`` or None.

    None for anything wrong -- malformed, unknown id, wrong secret, revoked,
    corrupt record -- with no indication of which. Reads Redis every call.
    """
    if not isinstance(bearer, str):
        return None
    parts = bearer.split(".")
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX:
        return None
    _, token_id, secret = parts
    if not _TOKEN_ID_RE.fullmatch(token_id) or not _SECRET_RE.fullmatch(secret):
        return None
    record = _load(r.hget(coop_tokens_key(pod, tenant), token_id))
    # Compare even when the record is missing so the unknown-id path costs the
    # same as the wrong-secret path.
    expected = (record or {}).get("sha256")
    if not isinstance(expected, str):
        expected = "0" * 64
        record = None
    matches = hmac.compare_digest(_digest(secret), expected)
    if record is None or not matches or record.get("revoked") is not False:
        return None
    participant = record.get("participant")
    kinds = record.get("kinds")
    if not isinstance(participant, str) or not isinstance(kinds, list):
        return None
    return {"token_id": token_id, "participant": participant, "kinds": kinds}


def diagnose(r, pod: str, tenant: str, bearer: str) -> str:
    """Why :func:`verify` refused ``bearer``: ``"revoked"`` or ``"unknown_token"``.

    ``"revoked"`` is only reported to a caller that proved the secret (digest
    matches a revoked record); a wrong secret or unknown id is always
    ``"unknown_token"``, so this cannot be used to probe which ids exist.
    Meant for ``GET /whoami`` only. Never returns anything derived from the token.
    """
    if not isinstance(bearer, str):
        return "unknown_token"
    parts = bearer.split(".")
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX:
        return "unknown_token"
    _, token_id, secret = parts
    if not _TOKEN_ID_RE.fullmatch(token_id) or not _SECRET_RE.fullmatch(secret):
        return "unknown_token"
    record = _load(r.hget(coop_tokens_key(pod, tenant), token_id))
    expected = (record or {}).get("sha256")
    if not isinstance(expected, str):
        return "unknown_token"
    if hmac.compare_digest(_digest(secret), expected) and record.get("revoked") is True:
        return "revoked"
    return "unknown_token"
