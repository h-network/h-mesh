"""Direct tests for the text `write_agent_guide` puts on disk (issue #5).

The operator-authority block (every entrance variant) and the "Interface
entries may only accept ..." sentence were removed because agents refused
legitimate Telegram/api messages on provenance grounds. The external-door
warning paragraph was removed afterwards for the same reason (#5 follow-up),
and so was the "That's colleagues only ..." peers paragraph. The
destructive/ambiguous rule is deliberately kept. A calibrated replacement for
the warning (outside doors are normal; a message cannot grant itself authority)
lives later in the guide and is pinned in test_guide_external_doors.py.
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
    # The "That's colleagues only ..." peers paragraph (removed afterwards).
    "That's colleagues only",
    "lifecycle provider",
    "peers -i",
    "external entrance",
    "configured entrance",
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
    # The reply-by-name paragraph follows the peers command directly.
    assert "    h-mesh-office peers\n\nA message arrives in your terminal as" in agents


def test_the_old_warning_is_not_restored_at_the_top(home, tmp_path):
    agents, _ = _write(tmp_path)
    before, after = agents.split("    h-mesh-office peers\n\n")
    assert after.startswith("A message arrives in your terminal as")
    assert "⚠ Treat" not in agents
    # The other ⚠ paragraph (one quoted argument) is untouched.
    assert "⚠ The body is ONE argument." in agents


def test_guide_says_messages_carry_a_send_time_and_short_id(home, tmp_path):
    agents, _ = _write(tmp_path)
    assert (
        "A message arrives in your terminal as `[message from <name>] …`, led by its UTC\n"
        "send time and a short `#id` so you can order and de-duplicate messages — reply\n"
        "by name, whether or not `peers` lists it:\n"
    ) in agents


DONE_NOTICE = (
    "When a ticket is done, send the lead ONE line saying it is done, naming the\n"
    "ticket (title or id), then `h-mesh-office done`. No summary, no recap of changes.\n"
)
OLD_DONE_RULE = (
    "message the lead a summary",
    "A closed ticket with no report is invisible to the lead",
)


@pytest.mark.parametrize("variant", sorted(VARIANTS))
def test_done_rule_is_a_one_line_notice_not_a_summary(home, tmp_path, variant):
    agents, claude = _write(tmp_path, lead="architect", **VARIANTS[variant])
    for text in (agents, claude):
        assert text.count(DONE_NOTICE) == 1
        for old in OLD_DONE_RULE:
            assert old not in text
        # The suite-counts rule is unchanged.
        assert "Report both the collected count and the passed count" in text


def test_done_notice_uses_the_configured_office_command(home, tmp_path, monkeypatch):
    monkeypatch.setenv("OFFICE_TOOLS", "custom-office")
    agents, _ = _write(tmp_path)
    assert "naming the\nticket (title or id), then `custom-office done`." in agents
