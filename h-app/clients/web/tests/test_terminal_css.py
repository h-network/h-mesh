"""terminal.css must be valid enough to load: balanced, no leftovers, no undefined variables."""

import re
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent.parent


def _strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def test_terminal_css_braces_are_balanced_and_never_close_early():
    depth = 0
    for number, line in enumerate(_strip_comments((WEB_DIR / "terminal.css").read_text()).splitlines(), 1):
        depth += line.count("{") - line.count("}")
        assert depth >= 0, f"terminal.css:{number}: closing brace with nothing open"
    assert depth == 0, "terminal.css: unbalanced braces"


def test_terminal_css_rule_blocks_are_not_repeated_verbatim():
    # A bad merge once left the tail of .term-tab-bar twice; an identical
    # declaration block under two different selectors is fine, the same
    # selector repeated with the same body is not.
    css = _strip_comments((WEB_DIR / "terminal.css").read_text())
    seen = {}
    for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        key = (" ".join(selector.split()), " ".join(body.split()))
        assert key not in seen, f"duplicate rule: {key[0]}"
        seen[key] = True


def test_every_term_variable_terminal_css_uses_is_defined():
    css = _strip_comments((WEB_DIR / "terminal.css").read_text())
    defined = set(re.findall(r"(--term-[a-z-]+)\s*:", css))
    used = set(re.findall(r"var\((--term-[a-z-]+)", css))
    assert used and used <= defined, f"undefined --term-* variables: {sorted(used - defined)}"
