"""#27 action audit: lib/audit.py and `office audit`."""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

H_APP = Path(__file__).resolve().parents[1]
if str(H_APP) not in sys.path:
    sys.path.insert(0, str(H_APP))

from core.keys import prefix
from lib import audit
from modules.office.cli import main as office_main
from tests.test_api import FakeRedis as ApiFakeRedis
from test_office_cli import POD, TENANT, FakeRedis, _env

NOW = datetime(2026, 11, 5, 12, 0, 0, tzinfo=timezone.utc)
COOP = "coop.0123456789abcdef." + "Zq3_-" * 8 + "abc"          # 43-char secret part
BEARER = "s3cr3tBearerValue99"
PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA1\nzzzPEMBODYzzz\n-----END RSA PRIVATE KEY-----"
SECRETS = (BEARER, COOP.rsplit(".", 1)[1], "zzzPEMBODYzzz", "MIIEowIBAAKCAQEA1")


def test_a_record_with_bearer_coop_and_pem_is_stored_masked():
    r = ApiFakeRedis()
    command = (
        f'curl -H "Authorization: Bearer {BEARER}" -H "X-Coop: {COOP}" '
        f"--data '{PEM}' https://other.example/agents/x/envelopes"
    )
    entry_id = audit.append(r, "p", "t", "alice", {"tool": "Bash", "args": command, "exit": 0}, now=NOW)
    stored = json.dumps(r.streams[audit.audit_key("p", "t", "alice")])
    for secret in SECRETS:
        assert secret not in stored
    (row,) = audit.read(r, "p", "t", "alice")
    assert row["cursor"] == entry_id
    assert "curl" in row["args"] and audit.REDACTED in row["args"]
    assert row["tool"] == "Bash" and row["exit"] == 0 and row["agent"] == "alice"
    assert row["recv_ts"] == "2026-11-05T12:00:00.000Z"


def test_secrets_in_structured_args_are_masked_and_digest_is_of_the_masked_text():
    r = ApiFakeRedis()
    args = {"command": f"deploy --token {BEARER}", "env": {"API_SECRET": "hunter2"}, "password": "hunter2"}
    audit.append(r, "p", "t", "alice", {"tool": "Bash", "args": args}, now=NOW)
    (row,) = audit.read(r, "p", "t", "alice")
    assert "hunter2" not in row["args"] and BEARER not in row["args"]
    import hashlib
    assert row["args_sha256"] == hashlib.sha256(row["args"].encode()).hexdigest()   # short enough: not truncated


@pytest.mark.parametrize("text, secret", [
    ("API_KEY=abcd1234 run", "abcd1234"),
    ("export GITHUB_TOKEN='gh p'; x", "gh p"),
    ("tool --password hunter2 --verbose", "hunter2"),
    ("tool --api-key=hunter2", "hunter2"),
    ("git clone https://bob:hunter2@host/r.git", "hunter2"),
    ('{"password": "hunter two"}', "hunter two"),
    ("curl -H 'X-Api-Key: hunter2' u", "hunter2"),
    ("echo sk-" + "a" * 30, "a" * 30),
    ("echo ghp_" + "b" * 30, "b" * 30),
    ("echo AKIAABCDEFGHIJKLMNOP", "AKIAABCDEFGHIJKLMNOP"),
    ("echo eyJhbGciOiJI.eyJzdWIiOiIx.SflKxwRJSMeKKF2QT4", "SflKxwRJSMeKKF2QT4"),
], ids=["name-equals-value", "quoted-env-value", "flag-space-value", "flag-equals-value", "url-credentials",
        "json-password", "api-key-header", "sk-key", "github-token", "aws-key-id", "jwt"])
def test_redaction_table(text, secret):
    masked = audit.redact(text)
    assert secret not in masked and audit.REDACTED in masked
    assert audit.redact(masked) == masked                      # idempotent


@pytest.mark.parametrize("text", ["ls -la /tmp", "git log --author bob", "tool --no-password --verbose", "cat README.md"],
                         ids=["ls", "author-flag", "negated-flag", "cat"])
def test_ordinary_commands_are_untouched(text):
    assert audit.redact(text) == text


def test_a_secret_cut_by_truncation_is_masked_before_cutting():
    pad = "x" * (audit.ARGS_MAX_BYTES - 5)
    row = audit.build_record("alice", {"tool": "Bash", "args": f"{pad} --token {BEARER}"}, now=NOW)
    assert row["args_truncated"] is True and BEARER not in row["args"] and BEARER[:4] not in row["args"][-20:]
    assert len(row["args"].encode()) <= audit.ARGS_MAX_BYTES


@pytest.mark.parametrize("body, message", [
    ({}, "tool is required"),
    ({"tool": "Bash", "extra": 1}, "unknown field"),
    ({"tool": "Bash", "prev_hash": "x"}, "unknown field"),        # reserved, not client-settable
    ({"tool": "Bash", "v": 2}, "version"),
    ({"tool": "bad tool!"}, "tool must be"),
    ({"tool": "Bash", "phase": "during"}, "phase"),
    ({"tool": "Bash", "ts": "yesterday"}, "ts must be"),
    ({"tool": "Bash", "exit": "0"}, "exit must be"),
    ({"tool": "Bash", "exit": True}, "exit must be"),
    ({"tool": "Bash", "out_bytes": -1}, "out_bytes"),
    ({"tool": "Bash", "session": "s" * 65}, "session must be"),
    ("not an object", "JSON object"),
], ids=["no-tool", "unknown-field", "prev-hash-reserved", "bad-version", "bad-tool", "bad-phase", "bad-ts",
        "exit-string", "exit-bool", "out-bytes-negative", "session-too-long", "not-an-object"])
def test_invalid_records_are_refused(body, message):
    with pytest.raises(audit.AuditError, match=message):
        audit.build_record("alice", body, now=NOW)


def test_the_record_format_reserves_prev_hash_and_keeps_client_ts_apart_from_recv_ts():
    row = audit.build_record(
        "alice",
        {"v": 1, "tool": "Bash", "ts": "2026-11-05T11:59:00Z", "cli": "claude", "session": "s-1",
         "call_id": "c1", "phase": "post", "args": "ls", "exit": 2, "out_bytes": 10},
        now=NOW,
    )
    assert row["prev_hash"] is None and row["v"] == 1
    assert row["ts"] == "2026-11-05T11:59:00Z" and row["recv_ts"] == "2026-11-05T12:00:00.000Z"


def test_the_stream_is_capped_per_agent():
    r = ApiFakeRedis()
    for n in range(audit.MAX_ENTRIES + 3):
        audit.append(r, "p", "t", "alice", {"tool": "Bash", "args": str(n)}, now=NOW)
    audit.append(r, "p", "t", "bob", {"tool": "Bash"}, now=NOW)
    assert len(r.streams[audit.audit_key("p", "t", "alice")]) == audit.MAX_ENTRIES
    assert len(r.streams[audit.audit_key("p", "t", "bob")]) == 1


def test_the_optional_mirror_file_gets_one_masked_line_per_record(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    monkeypatch.setenv(audit.MIRROR_ENV, str(path))
    r = ApiFakeRedis()
    audit.append(r, "p", "t", "alice", {"tool": "Bash", "args": f"x --token {BEARER}"}, now=NOW)
    (line,) = path.read_text().splitlines()
    assert BEARER not in line and json.loads(line)["id"] == "1-0"


def test_a_broken_mirror_never_fails_the_record(tmp_path, monkeypatch):
    monkeypatch.setenv(audit.MIRROR_ENV, str(tmp_path / "missing-dir" / "audit.jsonl"))
    r = ApiFakeRedis()
    assert audit.append(r, "p", "t", "alice", {"tool": "Bash"}, now=NOW) == "1-0"


def test_no_mirror_without_the_env_var(tmp_path, monkeypatch):
    monkeypatch.delenv(audit.MIRROR_ENV, raising=False)
    audit.append(ApiFakeRedis(), "p", "t", "alice", {"tool": "Bash"}, now=NOW)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("text, delta", [("90m", timedelta(minutes=90)), ("2h", timedelta(hours=2)),
                                          ("1d", timedelta(days=1)), ("30s", timedelta(seconds=30))],
                         ids=["minutes", "hours", "days", "seconds"])
def test_parse_since_relative(text, delta):
    assert audit.parse_since(text, now=NOW) == NOW - delta


def test_parse_since_iso_and_garbage():
    assert audit.parse_since("2026-11-05T10:00:00Z", now=NOW) == datetime(2026, 11, 5, 10, tzinfo=timezone.utc)
    with pytest.raises(audit.AuditError):
        audit.parse_since("last tuesday", now=NOW)


# --- office audit ----------------------------------------------------------


def _seed(r, agent, when, **body):
    record = audit.build_record(agent, {"tool": "Bash", **body}, now=when)
    ms = int(when.timestamp() * 1000)
    n = len(r.streams[audit.audit_key(POD, TENANT, agent)])
    r.streams[audit.audit_key(POD, TENANT, agent)].append((f"{ms}-{n}", {"record": json.dumps(record)}))


def _audit(monkeypatch, capsys, r, *argv, agent="architect"):
    from unittest.mock import patch
    _env(monkeypatch, agent=agent)
    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, agent)):
        office_main(["audit", *argv])
    return capsys.readouterr().out


def _office_with_records():
    r = FakeRedis(registry={"architect": "tmux", "alice": "tmux", "bob": "tmux"})
    _seed(r, "alice", NOW - timedelta(hours=3), args="old", exit=0)
    _seed(r, "alice", NOW - timedelta(minutes=30), args="ls -la", exit=0)
    _seed(r, "bob", NOW - timedelta(minutes=10), args=f"deploy --token {BEARER}", exit=1)
    _seed(r, "bob", NOW - timedelta(minutes=5), tool="Edit", args="file.py")
    return r


def test_audit_lists_all_agents_in_time_order_masked(monkeypatch, capsys):
    out = _audit(monkeypatch, capsys, _office_with_records())
    lines = out.strip().splitlines()
    assert [line.split("\t")[1] for line in lines] == ["alice", "alice", "bob", "bob"]
    assert BEARER not in out and audit.REDACTED in out
    assert "exit=1" in lines[2] and "exit=-" in lines[3]


def test_audit_filters_by_agent_since_tool_and_limit(monkeypatch, capsys):
    r = _office_with_records()
    only_bob = _audit(monkeypatch, capsys, r, "--agent", "bob").strip().splitlines()
    assert len(only_bob) == 2 and all("\tbob\t" in line for line in only_bob)
    recent = _audit(monkeypatch, capsys, r, "--since", "1h").strip().splitlines()
    assert len(recent) == 3 and not any("old" in line for line in recent)
    edits = _audit(monkeypatch, capsys, r, "--tool", "Edit").strip().splitlines()
    assert len(edits) == 1 and "file.py" in edits[0]
    newest = _audit(monkeypatch, capsys, r, "--limit", "1").strip().splitlines()
    assert len(newest) == 1 and "file.py" in newest[0]


def test_audit_json_and_empty(monkeypatch, capsys):
    r = _office_with_records()
    rows = [json.loads(line) for line in _audit(monkeypatch, capsys, r, "--json", "-a", "alice").splitlines()]
    assert [row["args"] for row in rows] == ["old", "ls -la"] and rows[0]["prev_hash"] is None
    assert _audit(monkeypatch, capsys, FakeRedis(registry={"architect": "tmux"})).strip() == "no audit records"


def test_audit_reads_a_retired_agent_by_name(monkeypatch, capsys):
    r = _office_with_records()
    _seed(r, "ghost", NOW - timedelta(minutes=1), args="was here")           # not enrolled any more
    assert "was here" not in _audit(monkeypatch, capsys, r)
    assert "was here" in _audit(monkeypatch, capsys, r, "-a", "ghost")


def test_audit_one_line_per_record_even_with_newlines(monkeypatch, capsys):
    r = FakeRedis(registry={"architect": "tmux", "alice": "tmux"})
    _seed(r, "alice", NOW, args="echo a\necho b")
    assert len(_audit(monkeypatch, capsys, r).strip().splitlines()) == 1


@pytest.mark.parametrize("argv", [["--since", "never"], ["--agent", "Bad Name"], ["--limit", "0"]],
                         ids=["bad-since", "bad-agent", "bad-limit"])
def test_audit_bad_arguments_fail_cleanly(argv, monkeypatch, capsys):
    with pytest.raises(SystemExit) as exc:
        _audit(monkeypatch, capsys, _office_with_records(), *argv)
    assert exc.value.code in (1, 2)
