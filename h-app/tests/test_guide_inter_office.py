"""The generated guide teaches reaching another office; only the lead gets the linking commands."""

import shlex
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

H_APP = Path(__file__).resolve().parents[1]
if str(H_APP) not in sys.path:
    sys.path.insert(0, str(H_APP))

from modules.office import cli as office_cli
from modules.office.cli import main as office_main
from modules.tmux.ops import generate_agents_md, inter_office_guide

CMD = "h-mesh-office"
START = "This directory is yours; work in it."
END = "You have a task board."
LEAD_HEADING = "Linking offices is yours alone (lead-only):"


def _guide(agent="worker", lead="architect"):
    return generate_agents_md(agent, "t", lead=lead, office_cmd=CMD)


def _section(guide):
    return guide.split(START, 1)[1].split(END, 1)[0]


def _lead_only_commands():
    return {
        names[0]
        for names, description, _ in office_cli._COMMAND_TABLE
        if "lead only" in description.lower()
    }


@pytest.mark.parametrize("agent, lead", [
    pytest.param("worker", "architect", id="other-lead"),
    pytest.param("worker", None, id="no-lead"),
    pytest.param("architect", "architect", id="is-lead"),
])
def test_every_agent_gets_the_send_office_form_and_offices(agent, lead):
    section = _section(_guide(agent, lead))
    assert f"`{CMD} offices`" in section
    assert f'{CMD} send --office <office> -a <agent> "one quoted argument"' in section
    assert "[from <office>/<agent>]" in section
    assert "do not blindly resend" in section


@pytest.mark.parametrize("agent, lead", [
    pytest.param("worker", "architect", id="other-lead"),
    pytest.param("worker", None, id="no-lead"),
])
def test_ordinary_agents_get_no_linking_syntax(agent, lead):
    # They still see the lead-only commands as labelled rows of the generated
    # command inventory (#4); what they do not get is the linking section.
    guide = _guide(agent, lead)
    assert LEAD_HEADING not in guide
    for syntax in ("coop-grant <participant>", "office-add <name> <url>", "--token-stdin",
                   "coop-revoke <token-id>", "Give a token only to"):
        assert syntax not in guide, f"{syntax!r} is lead-only syntax in an ordinary agent's guide"


def test_the_lead_gets_every_lead_only_command_the_table_marks():
    commands = _lead_only_commands()
    assert commands >= {"office-add", "office-rm", "coop-grant", "coop-revoke"}
    section = _section(_guide("architect", "architect"))
    assert LEAD_HEADING in section
    for name in commands:
        assert name in section, f"lead-only {name} is missing from the lead's inter-office text"


def test_the_lead_is_told_not_to_leak_the_token():
    section = _section(_guide("architect", "architect"))
    assert "prints a token ONCE" in section
    assert "never an argument" in section
    assert "Never put one in a message,\na file or a commit." in section


def test_the_printed_send_form_is_accepted_by_the_real_parser():
    line = next(l for l in _section(_guide()).splitlines() if "send --office" in l)
    words = shlex.split(line.strip().replace("<office>", "labb").replace("<agent>", "worker"))
    assert words[:2] == [CMD, "send"]
    with patch("modules.office.cli._send_to_office") as send_to_office:
        office_main(words[1:])
    send_to_office.assert_called_once_with("labb", "worker", "one quoted argument")


def test_the_exit_codes_in_the_guide_match_what_send_reports(capsys):
    with pytest.raises(SystemExit):
        office_main(["send", "--help"])
    help_text = " ".join(capsys.readouterr().out.split())
    assert "Exit 0 = accepted by the remote office (queued, not delivered)" in help_text
    assert "1 = refused, provably nothing sent" in help_text
    assert "3 = outcome unknown" in help_text
    section = _section(_guide())
    assert "Exit 0: accepted by that office (queued, not delivered)" in section
    assert "1: refused, nothing\nsent" in section
    assert "3: outcome unknown" in section


def test_commands_named_for_ordinary_agents_exist():
    names = {n for names, _, _ in office_cli._COMMAND_TABLE for n in names}
    assert {"offices", "send"} <= names


def test_section_sits_after_the_send_examples_and_before_the_board():
    guide = _guide()
    assert guide.index('{CMD} send -a <name> --file PATH'.format(CMD=CMD)) < guide.index(START)
    assert guide.index(START) < guide.index("Another office:") < guide.index(END)
    assert guide.count("Another office:") == 1


def test_the_custom_office_command_is_used_everywhere():
    text = inter_office_guide("custom-office", True)
    assert "custom-office send --office" in text and "custom-office coop-grant" in text
    assert CMD not in text


def test_the_section_stays_short():
    ordinary = inter_office_guide(CMD, False).count("\n")
    lead = inter_office_guide(CMD, True).count("\n")
    assert ordinary <= 12 and lead <= 26
