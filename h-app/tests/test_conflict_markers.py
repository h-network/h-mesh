"""No tracked file may contain merge-conflict markers (see tools/conflict_markers.py)."""

import sys
from pathlib import Path

import pytest

H_APP = Path(__file__).resolve().parents[1]
if str(H_APP) not in sys.path:
    sys.path.insert(0, str(H_APP))

from tools.conflict_markers import conflict_marker_violations, main as guard_main
from tools.legacy_names import tracked_blobs

# Built from parts so this file itself never has a marker at the start of a line.
START, SEP, END = "<" * 7, "=" * 7, ">" * 7


def _scan(tmp_path: Path, name: str, text: str):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode())
    return conflict_marker_violations(tmp_path, [(path, path.read_bytes())])


def test_the_whole_tracked_tree_has_no_conflict_markers():
    root = H_APP.parent
    checked, violations = conflict_marker_violations(root, tracked_blobs(root))
    assert checked > 100, "Expected to scan the complete repository"
    assert violations == [], "Conflict markers found:\n" + "\n".join(violations)


@pytest.mark.parametrize("name, text", [
    ("a.css", f"a {{}}\n{START} HEAD\nx\n{SEP}\ny\n{END} 51d32b6 (msg)\n"),
    ("a.py", f"x = 1\n{START}\n"),
    ("a.md", f"text\n{END} branch\n"),
    ("orphan.css", f".a {{ b: c; }}\n{SEP}\n.d {{ e: f; }}\n"),
    ("crlf.js", f"a;\r\n{SEP}\r\nb;\r\n"),
    ("noext", f"{START} HEAD\n"),
], ids=["css-hunk", "python-start", "markdown-end", "orphan-separator", "crlf", "no-extension"])
def test_markers_are_reported_with_file_and_line(tmp_path, name, text):
    checked, violations = _scan(tmp_path, name, text)
    assert checked == 1 and violations and violations[0].startswith(f"{name}:")


@pytest.mark.parametrize("name, text", [
    ("title.md", f"Title\n{SEP}\nbody\n"),            # setext heading underline
    ("doc.rst", f"Title\n{SEP}\n"),
    ("a.css", f"/* {START} not at line start */\n.a {{}}\n"),
    ("a.py", f'x = "{SEP}"\nprint("{START} HEAD")\n'),
    ("a.txt", f"{SEP}\n"),
    ("six.css", "<<<<<<\n======\n>>>>>>\n"),
    ("long.css", f"{SEP}=\n{START}<\n"),               # eight characters: not a marker
], ids=["markdown-underline", "rst-underline", "indented-in-comment", "inside-a-string", "text-underline",
        "six-characters", "eight-characters"])
def test_legitimate_lines_are_not_flagged(tmp_path, name, text):
    assert _scan(tmp_path, name, text)[1] == []


def test_binary_files_are_skipped(tmp_path):
    checked, violations = _scan(tmp_path, "blob.bin", f"\0\n{START} HEAD\n")
    assert (checked, violations) == (0, [])


def test_guard_main_reports_and_fails(tmp_path, capsys):
    path = tmp_path / "bad.css"
    path.write_text(f"{START} HEAD\n")
    assert guard_main(tmp_path, [(path, path.read_bytes())]) == 1
    assert "bad.css:1" in capsys.readouterr().out
    path.write_text(".ok {}\n")
    assert guard_main(tmp_path, [(path, path.read_bytes())]) == 0
