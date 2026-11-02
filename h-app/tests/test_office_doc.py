import hashlib
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

H_APP = Path(__file__).resolve().parents[1]
if str(H_APP) not in sys.path:
    sys.path.insert(0, str(H_APP))

from lib import doc_store
from modules.office.cli import main as office_main


class HashRedis:
    """In-memory double for the hash calls lib/doc_store.py makes."""

    def __init__(self):
        self.hashes: dict[str, dict] = {}

    def _h(self, key):
        return self.hashes.setdefault(key, {})

    def hsetnx(self, key, field, value):
        h = self._h(key)
        if field in h:
            return 0
        h[field] = value if isinstance(value, bytes) else str(value).encode()
        return 1

    def hset(self, key, field, value):
        self._h(key)[field] = value if isinstance(value, bytes) else str(value).encode()

    def hget(self, key, field):
        return self._h(key).get(field)

    def hkeys(self, key):
        return [k.encode() for k in self._h(key)]

    def hgetall(self, key):
        return {k.encode(): v for k, v in self._h(key).items()}


@pytest.fixture
def store():
    r = HashRedis()
    with patch("modules.office.cli._context", return_value=(r, "acme", "hq", "alice")):
        yield r


def _file(tmp_path, name="checklist.md", body=b"# v1\n- one\n"):
    path = tmp_path / name
    path.write_bytes(body)
    return path


def test_put_prints_short_ref_and_get_roundtrips_bytes(store, tmp_path, capsysbinary):
    body = b"\x00binary\xffdoc"
    path = _file(tmp_path, "blob.bin", body)
    office_main(["doc", "put", str(path)])
    out = capsysbinary.readouterr().out.decode()
    digest = hashlib.sha256(body).hexdigest()
    assert out.startswith(f"stored: doc:{digest[:12]} {len(body)} bytes")

    office_main(["doc", "get", f"doc:{digest[:12]}"])
    assert capsysbinary.readouterr().out == body


def test_put_same_bytes_dedupes_and_keeps_first_metadata(store, tmp_path, capsys):
    path = _file(tmp_path)
    office_main(["doc", "put", str(path)])
    capsys.readouterr()
    other = _file(tmp_path, "copy.md")
    office_main(["doc", "put", str(other)])
    assert capsys.readouterr().out.startswith("already stored: doc:")
    rows = doc_store.listing(store, pod="acme", tenant="hq")
    assert len(rows) == 1 and rows[0]["filename"] == "checklist.md" and rows[0]["author"] == "alice"


def test_name_points_at_newest_version_and_old_hash_still_resolves(store, tmp_path, capsysbinary):
    v1 = _file(tmp_path, body=b"v1")
    office_main(["doc", "put", str(v1), "--name", "checklist"])
    v2 = _file(tmp_path, body=b"v2")
    office_main(["doc", "put", str(v2), "--name", "checklist"])
    capsysbinary.readouterr()

    office_main(["doc", "get", "checklist"])
    assert capsysbinary.readouterr().out == b"v2"
    office_main(["doc", "get", hashlib.sha256(b"v1").hexdigest()[:10]])
    assert capsysbinary.readouterr().out == b"v1"


def test_get_writes_output_file(store, tmp_path, capsys):
    office_main(["doc", "put", str(_file(tmp_path, body=b"hello"))])
    target = tmp_path / "out.txt"
    office_main(["doc", "get", hashlib.sha256(b"hello").hexdigest()[:8], "-o", str(target)])
    assert target.read_bytes() == b"hello"
    assert "wrote 5 bytes" in capsys.readouterr().out


def test_info_and_ls_show_metadata_without_body(store, tmp_path, capsys):
    office_main(["doc", "put", str(_file(tmp_path)), "--name", "checklist"])
    capsys.readouterr()
    office_main(["doc", "ls"])
    listed = capsys.readouterr().out
    assert "checklist.md" in listed and "alice" in listed and "[checklist]" in listed
    office_main(["doc", "info", "checklist"])
    info = capsys.readouterr().out
    assert "mime_type: " in info and "size: 11" in info
    assert "filename: checklist.md" in info and "author: alice" in info


def test_unknown_short_and_ambiguous_refs_are_rejected(store, tmp_path, capsys):
    for ref in ("deadbeef", "abc", "nosuchname"):
        with pytest.raises(SystemExit):
            office_main(["doc", "get", ref])
        assert "office: error:" in capsys.readouterr().err
    # force an ambiguous prefix without grinding hashes
    docs = doc_store._keys("acme", "hq")[0]
    store.hsetnx(docs, "ab" * 4 + "1" * 56, b"x")
    store.hsetnx(docs, "ab" * 4 + "2" * 56, b"y")
    with pytest.raises(SystemExit):
        office_main(["doc", "get", "ab" * 4])
    assert "ambiguous" in capsys.readouterr().err


def test_hash_lookalike_and_malformed_names_are_rejected(store, tmp_path, capsys):
    path = _file(tmp_path)
    for bad in ("deadbeef", "has space", ".dot"):
        with pytest.raises(SystemExit):
            office_main(["doc", "put", str(path), "--name", bad])
        err = capsys.readouterr().err
        assert "invalid doc name" in err or "hash prefix" in err
    assert doc_store.listing(store, pod="acme", tenant="hq") == []


def test_put_rejects_missing_file_and_oversize(store, tmp_path, capsys, monkeypatch):
    with pytest.raises(SystemExit):
        office_main(["doc", "put", str(tmp_path / "nope.md")])
    assert "does not exist" in capsys.readouterr().err
    monkeypatch.setattr("modules.office.cli.ATTACHMENT_MAX_BYTES", 3)
    with pytest.raises(SystemExit):
        office_main(["doc", "put", str(_file(tmp_path, body=b"toolong"))])
    assert "exceeds maximum" in capsys.readouterr().err
