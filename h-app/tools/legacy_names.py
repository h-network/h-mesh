"""Reject unreviewed references to the predecessor project's identity."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Iterable


LEGACY_BANNED_NAMES = (
    "f" + "lock",
    "f" + "lock_",
    "f" + "lockclient",
    "h" + "f" + "lock_",
    "h" + "f" + "lock_session",
)
# An allowance is attached to the violating line and names the exact legacy
# identifier it permits. Position changes cannot transfer it.
LEGACY_ALLOW_MARKER = "# legacy-name-" + "allow:"


def _identifier_spans(text: str, identifier: str) -> list[tuple[int, int]]:
    """Find exact identifier occurrences using Python continuation semantics."""
    spans = []
    start = 0
    while (found := text.find(identifier, start)) != -1:
        end = found + len(identifier)
        preceding_continues = found > 0 and ("a" + text[found - 1]).isidentifier()
        following_continues = end < len(text) and ("a" + text[end]).isidentifier()
        if not preceding_continues and not following_continues:
            spans.append((found, end))
        start = found + 1
    return spans


def _remove_exact_identifiers(text: str, identifiers: list[str]) -> str:
    # Resolve every span against the original text. Sequential substitution
    # must not create a new occurrence eligible for a later allowance.
    spans = sorted(
        {
            span
            for identifier in identifiers
            for span in _identifier_spans(text, identifier)
        }
    )
    pieces = []
    cursor = 0
    for start, end in spans:
        pieces.append(text[cursor:start])
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)


def tracked_text_files(root: Path) -> list[Path]:
    output = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        check=True,
        capture_output=True,
    ).stdout
    return [root / name.decode("utf-8") for name in output.split(b"\0") if name]


def legacy_name_violations(
    root: Path, paths: Iterable[Path] | None = None
) -> tuple[int, list[str]]:
    checked = 0
    violations = []
    excluded_dirs = {".git", ".pytest_cache", "__pycache__"}
    candidates = sorted(paths if paths is not None else root.rglob("*"))

    for path in candidates:
        if (
            not path.is_file()
            or any(part in excluded_dirs or part.endswith(".egg-info") for part in path.parts)
        ):
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            # The policy covers tracked UTF-8 source and documentation. Treat
            # other encodings as binary rather than guessing their text codec.
            continue
        relative = path.relative_to(root)
        checked += 1
        for line_number, line in enumerate(lines, start=1):
            code, marker, allowance_text = line.partition(LEGACY_ALLOW_MARKER)
            folded = code.casefold()
            if marker:
                allowance_values = [
                    value.strip()
                    for value in allowance_text.split(",")
                    if value.strip()
                ]
                allowed_literals = [value.casefold() for value in allowance_values]
                invalid = [
                    value
                    for value in allowance_values
                    if not value.isidentifier()
                    or value != value.upper()
                    or not _identifier_spans(code, value)
                ]
                if not allowed_literals or invalid:
                    violations.append(
                        f"{relative}:{line_number}: invalid legacy allowance"
                    )
                folded = _remove_exact_identifiers(
                    code,
                    [value for value in allowance_values if value not in invalid],
                ).casefold()
            matches = [name for name in LEGACY_BANNED_NAMES if name in folded]
            if matches:
                violations.append(f"{relative}:{line_number}: {', '.join(matches)}")

    return checked, violations


def legacy_name_diagnostics(root: Path, paths: Iterable[Path] | None = None) -> list[str]:
    _, violations = legacy_name_violations(root, paths)
    diagnostics = []
    for violation in violations:
        relative, line_text, _ = violation.rsplit(":", 2)
        line_number = int(line_text)
        offending = (root / relative).read_text(encoding="utf-8").splitlines()[
            line_number - 1
        ]
        diagnostics.append(f"{relative}:{line_number}: {offending}")
    return diagnostics


def main(root: Path | None = None, paths: Iterable[Path] | None = None) -> int:
    repo_root = root or Path(__file__).resolve().parents[2]
    candidates = paths if paths is not None else tracked_text_files(repo_root)
    diagnostics = legacy_name_diagnostics(repo_root, candidates)
    if diagnostics:
        print("Unreviewed legacy-name references found:")
        for diagnostic in diagnostics:
            print(diagnostic)
        print(
            f"Use {LEGACY_ALLOW_MARKER} IDENTIFIER only for a reviewed, "
            "required compatibility reference on that same line."
        )
        return 1
    print("Legacy-name guard passed: no unreviewed references found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
