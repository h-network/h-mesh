"""Action audit (#27): a bounded per-agent record of what an agent DID.

The switch logs envelopes, not the shell/tool commands an agent runs. The
capture belongs to h-agent (per-CLI tool hooks); this module is the h-mesh side:
validate one submitted record, mask secrets in it, append it to the agent's
``audit`` stream, and read it back. Design: ``docs/design/action-audit.md``.

Stored in Redis as ``pod:<p>:tenant:<t>:agent:<name>:audit`` (a stream, one
``record`` field holding the JSON), capped at ``MAX_ENTRIES`` per agent, plus an
optional append-only JSONL copy named by ``H_MESH_AUDIT_FILE`` (never raises,
unset = off; rotation is the operator's, nothing here deletes it).

⚠ **This is not tamper-evident evidence.** All agents share one OS user, one
Redis and one ``API_TOKEN``: an agent can forge a record for any agent, skip the
hook, or delete the stream and the file. ``ts`` is whatever the caller says;
only ``recv_ts`` is this process's clock. See the README for the plain statement.

⚠ **Redaction is best-effort pattern masking**, run here on every record
whatever the caller already did. A secret passed as a bare positional argument
has no shape to match and is NOT caught.
"""

import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone

from core.keys import prefix

VERSION = 1
MAX_BODY_BYTES = 8192
MAX_ENTRIES = 5000
ARGS_MAX_BYTES = 2048
MIRROR_ENV = "H_MESH_AUDIT_FILE"
REDACTED = "[redacted]"

PHASES = frozenset({"pre", "post"})
# Fields a caller may send. ``prev_hash`` is deliberately NOT accepted: it is
# reserved for a later version that chains records (the stored record carries
# ``prev_hash: null`` so the format already has the slot).
ALLOWED_FIELDS = frozenset(
    {"v", "ts", "cli", "session", "call_id", "phase", "tool", "args", "exit", "out_bytes"}
)
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,63}$")
_INT_LIMIT = 2**31


class AuditError(ValueError):
    """A record that cannot be accepted. The message is safe to return to the caller."""


# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------

_NAME = r"[A-Za-z0-9_.-]*"
_SECRET_WORDS = r"(?:token|secret|passw(?:or)?d|passwd|api[_-]?key|apikey|credential|private[_-]?key|authorization)"
_VALUE = r"""("(?:[^"\\]|\\.)*"|'[^']*'|[^\s"';&|)]+)"""

# Order matters: whole blocks first, then labelled values, then bare shapes.
_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    # PEM blocks, terminated or cut off by truncation / a short capture.
    (re.compile(r"-----BEGIN [A-Z0-9 ]+-----[\s\S]*?-----END [A-Z0-9 ]+-----"), REDACTED),
    (re.compile(r"-----BEGIN [A-Z0-9 ]+-----[\s\S]*"), REDACTED),
    # Cooperation tokens: coop.<16 hex>.<secret>
    (re.compile(r"\bcoop\.[0-9a-fA-F]{8,}\.[A-Za-z0-9_-]{16,}"), REDACTED),
    # Authorization header / assignment, with or without a scheme word.
    (
        re.compile(
            r"""(?i)(authorization["']?\s*[:=]\s*["']?)(?:(?:bearer|basic|token|digest)\s+)?[^\s"']+"""
        ),
        r"\1" + REDACTED,
    ),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{6,}"), r"\1 " + REDACTED),
    # --token abc / --password=abc / -H 'X-Api-Key: abc'
    (
        re.compile(rf"(?i)(--?{_NAME}{_SECRET_WORDS}{_NAME})(\s+(?!-)|=){_VALUE}"),
        r"\1\2" + REDACTED,
    ),
    (
        re.compile(rf"""(?i)(['"]?x-{_NAME}(?:key|token|secret|auth){_NAME}\s*:\s*)[^\s"']+"""),
        r"\1" + REDACTED,
    ),
    # NAME=value where NAME looks like a secret
    (
        re.compile(
            r"(?i)\b([A-Za-z0-9_]*(?:token|secret|passw(?:or)?d|passwd|key|credential)[A-Za-z0-9_]*)=" + _VALUE
        ),
        r"\1=" + REDACTED,
    ),
    # "password": "value" (JSON-shaped args)
    (
        re.compile(
            rf"""(?i)("[^"\\]*{_SECRET_WORDS}[^"\\]*"\s*:\s*)("(?:[^"\\]|\\.)*"|[^\s,}}\]]+)"""
        ),
        r"\1" + f'"{REDACTED}"',
    ),
    # user:password@ in a URL
    (re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@"), r"\1" + REDACTED + "@"),
    # Well-known credential shapes
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), REDACTED),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), REDACTED),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), REDACTED),
    (re.compile(r"\bxox[a-zA-Z]-[A-Za-z0-9-]{10,}"), REDACTED),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), REDACTED),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), REDACTED),
)


def redact(text: str) -> str:
    """Mask secrets in ``text``. Idempotent: redact(redact(x)) == redact(x)."""
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


# --------------------------------------------------------------------------
# Keys, record building
# --------------------------------------------------------------------------


def audit_key(pod: str, tenant: str, agent: str) -> str:
    return prefix(pod, tenant, agent, "audit")


def _now_iso(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_ts(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _label(body: dict, field: str, *, required: bool = False) -> str | None:
    value = body.get(field)
    if value is None:
        if required:
            raise AuditError(f"{field} is required")
        return None
    if not isinstance(value, str) or not _LABEL_RE.fullmatch(value):
        raise AuditError(f"{field} must be 1-64 characters of letters, digits and . _ : @ -")
    return value


def _canonical_args(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise AuditError("args must be a string or JSON-serialisable") from exc


def _truncate_bytes(text: str, limit: int) -> tuple[str, bool]:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text, False
    return raw[:limit].decode("utf-8", errors="ignore"), True


def build_record(agent: str, body, *, now: datetime | None = None) -> dict:
    """Validate ``body`` and return the record to store (secrets already masked).

    Raises :class:`AuditError` for anything that must not be stored. The caller
    supplies no ``agent`` field: it comes from where the record was posted.
    """
    if not isinstance(body, dict):
        raise AuditError("body must be a JSON object")
    unknown = sorted(set(body) - ALLOWED_FIELDS)
    if unknown:
        raise AuditError(f"unknown field(s): {', '.join(str(name) for name in unknown)}")
    if body.get("v", VERSION) != VERSION:
        raise AuditError(f"unsupported record version (this server speaks v{VERSION})")

    record: dict = {"v": VERSION, "agent": agent}
    ts = body.get("ts")
    if ts is not None:
        if not isinstance(ts, str) or len(ts) > 40 or _parse_ts(ts) is None:
            raise AuditError("ts must be an ISO-8601 timestamp")
        record["ts"] = ts
    record["recv_ts"] = _now_iso(now)
    for field in ("cli", "session", "call_id"):
        value = _label(body, field)
        if value is not None:
            record[field] = value
    phase = body.get("phase")
    if phase is not None:
        if not isinstance(phase, str) or phase not in PHASES:
            raise AuditError("phase must be 'pre' or 'post'")
        record["phase"] = phase
    record["tool"] = _label(body, "tool", required=True)

    # Mask the FULL text first, then digest it, then truncate: cutting first
    # could leave half a secret that no pattern recognises, and the digest is of
    # the masked text so it cannot be used to guess a low-entropy secret.
    masked = redact(_canonical_args(body.get("args")))
    record["args_sha256"] = hashlib.sha256(masked.encode("utf-8")).hexdigest()
    record["args"], record["args_truncated"] = _truncate_bytes(masked, ARGS_MAX_BYTES)

    for field, low in (("exit", -_INT_LIMIT), ("out_bytes", 0)):
        value = body.get(field)
        if value is None:
            if field in body:
                record[field] = None
            continue
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value < _INT_LIMIT:
            raise AuditError(f"{field} must be an integer" + (" >= 0" if low == 0 else ""))
        record[field] = value
    record["prev_hash"] = None  # reserved for chaining; always null in v1
    return record


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------


def _decode(value):
    return value.decode() if isinstance(value, bytes) else value


def _mirror(record: dict, entry_id: str) -> None:
    path = os.environ.get(MIRROR_ENV)
    if not path:
        return
    try:
        line = json.dumps({**record, "id": entry_id}, separators=(",", ":"))
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception:
        pass  # a full or read-only disk must not fail an audit POST


def append(r, pod: str, tenant: str, agent: str, body, *, now: datetime | None = None) -> str:
    """Validate, mask and store one record. Returns its stream id (the cursor)."""
    record = build_record(agent, body, now=now)
    entry_id = _decode(
        r.xadd(
            audit_key(pod, tenant, agent),
            {"record": json.dumps(record, separators=(",", ":"))},
            maxlen=MAX_ENTRIES,
            approximate=True,
        )
    )
    _mirror(record, entry_id)
    return entry_id


def _entries(raw_entries) -> list[dict]:
    rows = []
    for entry_id, fields in raw_entries:
        raw = None
        for name, value in fields.items():
            if _decode(name) == "record":
                raw = value
        try:
            record = json.loads(_decode(raw))
        except (TypeError, ValueError):
            continue
        if isinstance(record, dict):
            record["cursor"] = _decode(entry_id)
            rows.append(record)
    return rows


def read(r, pod: str, tenant: str, agent: str, *, after: str | None = None, limit: int = 100) -> list[dict]:
    """Records after cursor ``after`` (exclusive), oldest first, at most ``limit``."""
    low = f"({after}" if after else "-"
    return _entries(r.xrange(audit_key(pod, tenant, agent), min=low, max="+", count=limit))


# --------------------------------------------------------------------------
# CLI helpers
# --------------------------------------------------------------------------

_SINCE_RE = re.compile(r"^(\d+)([smhd])$")
_SINCE_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


def parse_since(text: str, *, now: datetime | None = None) -> datetime:
    """``90m`` / ``2h`` / ``1d`` (relative to now) or an ISO-8601 timestamp."""
    now = now or datetime.now(timezone.utc)
    match = _SINCE_RE.fullmatch(text.strip())
    if match:
        return now - timedelta(**{_SINCE_UNITS[match.group(2)]: int(match.group(1))})
    parsed = _parse_ts(text.strip())
    if parsed is None:
        raise AuditError("--since must be like 90m, 2h, 1d or an ISO-8601 time")
    return parsed


def since_cursor(moment: datetime) -> str:
    """The stream id at/after which entries were received at ``moment``."""
    return f"{int(moment.timestamp() * 1000)}-0"


def read_since(r, pod: str, tenant: str, agent: str, moment: datetime | None) -> list[dict]:
    """Every retained record for ``agent`` received at/after ``moment`` (None = all)."""
    low = since_cursor(moment) if moment else "-"
    rows = _entries(r.xrange(audit_key(pod, tenant, agent), min=low, max="+"))
    if moment is None:
        return rows
    kept = []
    for row in rows:
        received = _parse_ts(row.get("recv_ts", "")) if isinstance(row.get("recv_ts"), str) else None
        if received is None or received >= moment:
            kept.append(row)
    return kept
