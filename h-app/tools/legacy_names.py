import re
import subprocess
from pathlib import Path
from typing import Iterable

LEGACY_NAME = ("f" + "lock").encode()
LEGACY_ALLOW_MARKER = "# legacy-name-" + "allow:"
MARKER_BYTES = LEGACY_ALLOW_MARKER.encode()
ALLOW_NAME = re.compile(rb"[A-Z_][A-Z0-9_]*\Z")
Blob = tuple[Path, bytes]
def _continues_identifier(value: int) -> bool:
    return value >= 128 or value == 95 or chr(value).isalnum()
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
def tracked_blobs(root: Path) -> list[Blob]:
    records = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-s", "-z"], check=True, capture_output=True
    ).stdout.split(b"\0")
    blobs = []
    for record in filter(None, records):
        metadata, name = record.split(b"\t", 1)
        content = subprocess.run(
            ["git", "-C", str(root), "cat-file", "blob", metadata.split()[1].decode()],
            check=True, capture_output=True,
        ).stdout
        blobs.append((root / name.decode(), content))
    return blobs
def legacy_name_violations(root: Path, blobs: Iterable[Blob]) -> tuple[int, list[str]]:
    blobs, violations = list(blobs), []
    for path, content in sorted(blobs):
        relative = path.relative_to(root)
        for number, line in enumerate(content.splitlines(), 1):
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
    return len(blobs), violations
def legacy_name_diagnostics(root: Path, blobs: Iterable[Blob]) -> list[str]:
    blobs = list(blobs)
    content = {str(path.relative_to(root)): value for path, value in blobs}
    _, violations = legacy_name_violations(root, blobs)
    diagnostics = []
    for violation in violations:
        relative, number, _ = violation.rsplit(":", 2)
        line = content[relative].splitlines()[int(number) - 1]
        diagnostics.append(
            f"{relative}:{number}: {line.decode('utf-8', errors='backslashreplace')}"
        )
    return list(dict.fromkeys(diagnostics))
def main(root: Path | None = None, blobs: Iterable[Blob] | None = None) -> int:
    root = root or Path(__file__).resolve().parents[2]
    diagnostics = legacy_name_diagnostics(root, blobs or tracked_blobs(root))
    if diagnostics:
        print("Unreviewed legacy-name references found:\n" + "\n".join(diagnostics))
        return 1
    print("Legacy-name guard passed: no unreviewed references found.")
    return 0
if __name__ == "__main__":
    raise SystemExit(main())
