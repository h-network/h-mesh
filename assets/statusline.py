#!/usr/bin/env python3
"""Claude Code statusLine: context usage as a percentage bar.

Reads the hook JSON Claude Code passes on stdin, finds the transcript file
it points at, and sums the most recent assistant turn's token usage
(input + cache_read + cache_creation) as the current context size --
that's the size of everything sent on the last API call, which is the
actual context window occupancy right now. Divided against the model's
known context window to get a percentage.

Installed into each claude agent's config dir by services.claude_statusline
(setup.sh, h-mesh upgrade, and at hire time for a profiled account) --
never edited in place there; this repo copy is the source of truth.
"""

import json
import sys

# Observed live (cache_read_input_tokens alone hit ~456k in a real long
# session), well past the usual 200k figure -- this office's deployment
# runs an extended-context tier. Using 1M as the default since guessing
# an exact per-model figure has already proven wrong once; percentage is
# clamped at 100% either way so an under-estimate just shows full, never
# a number over 100.
DEFAULT_WINDOW = 1_000_000
BAR_WIDTH = 20


def _latest_usage(transcript_path: str) -> int | None:
    try:
        with open(transcript_path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return None
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        usage = entry.get("message", {}).get("usage")
        if not usage:
            continue
        return (
            usage.get("input_tokens", 0)
            + usage.get("cache_read_input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0)
        )
    return None


def _bar(pct: float) -> str:
    filled = round(BAR_WIDTH * min(pct, 100) / 100)
    return "█" * filled + "░" * (BAR_WIDTH - filled)


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        data = {}

    model = data.get("model", {})
    model_id = model.get("id", "")
    model_name = model.get("display_name", model_id or "claude")
    transcript_path = data.get("transcript_path", "")
    cwd = data.get("workspace", {}).get("current_dir", "")

    tokens = _latest_usage(transcript_path) if transcript_path else None

    if tokens is None:
        print(f"{model_name} · [{'░' * BAR_WIDTH}] --% · {cwd}")
        return

    pct = min((tokens / DEFAULT_WINDOW) * 100, 100)
    print(f"{model_name} · [{_bar(pct)}] {pct:.0f}% · {cwd}")


if __name__ == "__main__":
    main()
