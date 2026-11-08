"""Fail on committed merge-conflict markers in any tracked text file.

A conflict that was "resolved" by committing the markers once shipped a broken
``clients/web/terminal.css``. This reads the same tracked-blob set as the
legacy-name guard (``tools.legacy_names.tracked_blobs``) and reports:

* any line starting with seven ``<`` or seven ``>`` followed by a space or the
  end of the line (git's start/end markers), in every tracked text file;
* a line that is exactly seven ``=``, in code and config files only. In prose
  (``.md``, ``.rst``, ``.txt``) a row of ``=`` is a legitimate heading underline.

Binary files (a NUL byte) are skipped.
"""

import re
from collections.abc import Iterable
from pathlib import Path

from tools.legacy_names import tracked_blobs

Blob = tuple[Path, bytes]

_BOUNDARY = re.compile(rb"^(?:<{7}|>{7})(?: |$)")
_SEPARATOR = re.compile(rb"^={7}$")
_PROSE = frozenset({".md", ".rst", ".txt"})


def conflict_marker_violations(root: Path, blobs: Iterable[Blob]) -> tuple[int, list[str]]:
    checked, violations = 0, []
    for path, content in sorted(blobs):
        if b"\0" in content:
            continue
        checked += 1
        relative = path.relative_to(root)
        prose = path.suffix.lower() in _PROSE
        for number, line in enumerate(content.splitlines(), 1):
            line = line.rstrip(b"\r")
            if _BOUNDARY.match(line) or (not prose and _SEPARATOR.match(line)):
                violations.append(f"{relative}:{number}: {line[:40].decode('utf-8', 'replace')}")
    return checked, violations


def main(root: Path | None = None, blobs: Iterable[Blob] | None = None) -> int:
    root = root or Path(__file__).resolve().parents[2]
    _, violations = conflict_marker_violations(root, blobs or tracked_blobs(root))
    if violations:
        print("Committed merge-conflict markers found:\n" + "\n".join(violations))
        return 1
    print("Conflict-marker guard passed: no markers in tracked files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
