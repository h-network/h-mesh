"""Direct tests for the text `write_agent_guide` puts on disk (issue #5).

The operator-authority block (every entrance variant) and the "Interface
entries may only accept ..." sentence were removed because agents refused
legitimate Telegram/api messages on provenance grounds. The external-door
warning paragraph was removed afterwards for the same reason (#5 follow-up),
with no replacement text. The destructive/ambiguous rule is deliberately kept.
"""
import pytest

from modules.tmux.ops import write_agent_guide

REMOVED = (
    "routing provenance",
    "cryptographic proof of identity",
    "operator coordination authority",
    "claiming operator authority never qualifies",
    "they outrank lead direction",
    "treat instructions arriving through the configured operator entrance",
    "Interface entries may only accept",
    "not arbitrary messages",
    "no declared operator entrance configured",
    # The external-door warning paragraph (removed after the #5 follow-up).
    "Treat where a message arrives",
    "an external door",
    "untrusted external content",
    "Keep all validation and containment rules intact",
    "without blindly executing arbitrary remote input",
)
DESTRUCTIVE_RULE = (
    "Anything destructive, ambiguous, or outside a ticket's stated scope: ask\n"
    "the lead first, don't guess."
)

VARIANTS = {
    "none": {},
    "valid": {"operator_entrance": "telegram", "enrolled_entrances": {"telegram"}},
    "rejected": {"operator_entrance": "telegrma", "enrolled_entrances": {"telegram"}},
    "unverified": {"operator_entrance": "telegram"},
}
PEERS_SENTENCE = {
    "none": "That's colleagues only — external entrances\n",
    "valid": "That's colleagues only — the operator's external entrance (`telegram`)\n",
    "rejected": "external entrances (configured entrance `telegrma` is not currently enrolled)",
    "unverified": "external entrances (configured entrance `telegram` unverified)",
}


@pytest.fixture
def home(tmp_path, monkeypatch):
    # write_agent_guide seeds trust files under $HOME; keep that off the real one.
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv("OPERATOR_ENTRANCE", raising=False)
    monkeypatch.delenv("OFFICE_TOOLS", raising=False)
    return h


def _write(tmp_path, **kwargs):
    cwd = tmp_path / "work" / "worker"
    write_agent_guide(str(cwd), "worker", **kwargs)
    return (cwd / "AGENTS.md").read_text(encoding="utf-8"), (cwd / "CLAUDE.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("variant", sorted(VARIANTS))
def test_removed_text_is_gone_from_both_files(home, tmp_path, variant):
    agents, claude = _write(tmp_path, lead="architect", **VARIANTS[variant])
    for text in (agents, claude):
        for gone in REMOVED:
            assert gone not in text


@pytest.mark.parametrize("variant", sorted(VARIANTS))
def test_agents_md_and_claude_md_still_match(home, tmp_path, variant):
    agents, claude = _write(tmp_path, **VARIANTS[variant])
    assert agents == claude
    assert agents.strip()


@pytest.mark.parametrize("variant", sorted(VARIANTS))
def test_kept_text_is_still_present_and_unchanged(home, tmp_path, variant):
    agents, _ = _write(tmp_path, **VARIANTS[variant])
    assert DESTRUCTIVE_RULE in agents
    assert PEERS_SENTENCE[variant] in agents
    # The reply-by-name paragraph follows the peers paragraph directly.
    assert "too, labeled apart from colleagues.\n\nA message arrives in your terminal as" in agents


def test_nothing_replaces_the_removed_warning(home, tmp_path):
    agents, _ = _write(tmp_path)
    before, after = agents.split("too, labeled apart from colleagues.\n\n")
    assert after.startswith("A message arrives in your terminal as")
    assert "⚠ Treat" not in agents
    # The other ⚠ paragraph (one quoted argument) is untouched.
    assert "⚠ The body is ONE argument." in agents
