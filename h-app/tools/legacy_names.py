import re
import subprocess
from pathlib import Path
from typing import Iterable

LEGACY_NAME = ("f" + "lock").encode()
LEGACY_ALLOW_MARKER = "# legacy-name-" + "allow:"
MARKER_BYTES = LEGACY_ALLOW_MARKER.encode()
ALLOW_NAME = re.compile(rb"[A-Z_][A-Z0-9_]*\Z")

def _continues_identifier(value: int) -> bool:
    return value >= 128 or value == 95 or 48 <= value <= 57 or 65 <= value <= 90 or 97 <= value <= 122


def _identifier_spans(text: bytes, identifier: bytes) -> list[tuple[int, int]]:
    spans, start = [], 0
    while (found := text.find(identifier, start)) != -1:
        end = found + len(identifier)
        if not (found and _continues_identifier(text[found - 1])) and not (
            end < len(text) and _continues_identifier(text[end])
        ):
            spans.append((found, end))
        start = found + 1
    return spans


def _remove_allowed(text: bytes, identifiers: list[bytes]) -> bytes:
    spans = sorted({span for name in identifiers for span in _identifier_spans(text, name)})
    output, cursor = [], 0
    for start, end in spans:
        output.append(text[cursor:start])
        cursor = end
    return b"".join((*output, text[cursor:]))

def tracked_files(root: Path) -> list[Path]:
    names = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"], check=True, capture_output=True
    ).stdout
    return [root / name.decode("utf-8") for name in names.split(b"\0") if name]


def legacy_name_violations(root: Path, paths: Iterable[Path] | None = None) -> tuple[int, list[str]]:
    violations, checked = [], 0
    for path in sorted(paths if paths is not None else root.rglob("*")):
        if not path.is_file():
            continue
        relative, checked = path.relative_to(root), checked + 1
        for number, line in enumerate(path.read_bytes().splitlines(), 1):
            code, marker, allowance = line.partition(MARKER_BYTES)
            folded = code.lower()
            if marker:
                values = [value.strip() for value in allowance.split(b",") if value.strip()]
                invalid = [value for value in values if not ALLOW_NAME.fullmatch(value) or not _identifier_spans(code, value)]
                if not values or invalid:
                    violations.append(f"{relative}:{number}: invalid legacy allowance")
                folded = _remove_allowed(code, [value for value in values if value not in invalid]).lower()
            if LEGACY_NAME in folded:
                violations.append(f"{relative}:{number}: {LEGACY_NAME.decode()}")
    return checked, violations

def legacy_name_diagnostics(root: Path, paths: Iterable[Path] | None = None) -> list[str]:
    _, violations = legacy_name_violations(root, paths)
    diagnostics = []
    for violation in violations:
        relative, number, _ = violation.rsplit(":", 2)
        line = (root / relative).read_bytes().splitlines()[int(number) - 1]
        rendered = line.decode("utf-8", errors="backslashreplace")
        diagnostics.append(f"{relative}:{number}: {rendered}")
    return list(dict.fromkeys(diagnostics))


def main(root: Path | None = None, paths: Iterable[Path] | None = None) -> int:
    root = root or Path(__file__).resolve().parents[2]
    diagnostics = legacy_name_diagnostics(root, paths or tracked_files(root))
    if diagnostics:
        print("Unreviewed legacy-name references found:\n" + "\n".join(diagnostics))
        return 1
    print("Legacy-name guard passed: no unreviewed references found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
