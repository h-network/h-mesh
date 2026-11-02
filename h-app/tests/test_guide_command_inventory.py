"""The generated guide lists every office command, from the command table (#4)."""

import re
import sys
from pathlib import Path

import pytest

H_APP = Path(__file__).resolve().parents[1]
if str(H_APP) not in sys.path:
    sys.path.insert(0, str(H_APP))

from modules.office import cli as office_cli
from modules.tmux import ops
from modules.tmux.ops import GUIDE_CURATED_COMMANDS, generate_agents_md, office_command_inventory

CMD = "h-mesh-office"


def _guide(**kw):
    return generate_agents_md("worker", "t", office_cmd=CMD, **kw)


def _rows(text):
    """name -> description for the indented rows of the inventory block."""
    block = text.split("Other commands (", 1)[1].split("\n\n", 2)[1]
    rows = {}
    for line in block.splitlines():
        name, _, description = line.strip().partition("  ")
        rows[name] = description.strip()
    return rows


@pytest.mark.parametrize("lead", [
    pytest.param(None, id="no-lead"),
    pytest.param("architect", id="other-lead"),
    pytest.param("worker", id="is-lead"),
])
def test_every_table_command_name_is_in_the_guide(lead):
    guide = _guide(lead=lead)
    rows = _rows(guide)
    for names, _description, _handler in office_cli._COMMAND_TABLE:
        for name in names:
            in_prose = name in GUIDE_CURATED_COMMANDS and f"{CMD} {name}" in guide
            assert in_prose or name in rows, f"{name} is in neither the prose nor the inventory"


def test_curated_commands_are_real_and_still_in_the_prose():
    table_primaries = {names[0] for names, _, _ in office_cli._COMMAND_TABLE}
    guide = _guide()
    for name in GUIDE_CURATED_COMMANDS:
        assert name in table_primaries, f"curated {name!r} is no longer a command"
        assert f"{CMD} {name}" in guide, f"curated {name!r} is no longer explained in the prose"


def test_inventory_rows_carry_the_table_descriptions_and_skip_curated_commands():
    rows = _rows(_guide())
    for names, description, _handler in office_cli._COMMAND_TABLE:
        if names[0] in GUIDE_CURATED_COMMANDS:
            assert names[0] not in rows
        else:
            assert rows[names[0]] == description
        for alias in names[1:]:
            assert rows[alias] == f"alias of {names[0]}"


def test_a_new_table_command_reaches_the_guide_without_editing_it(monkeypatch):
    extra = (("frobnicate", "fz"), "frobnicate the widgets", lambda argv: None)
    monkeypatch.setattr(office_cli, "_COMMAND_TABLE", office_cli._COMMAND_TABLE + (extra,))
    rows = _rows(_guide())
    assert rows["frobnicate"] == "frobnicate the widgets"
    assert rows["fz"] == "alias of frobnicate"


def test_inventory_sits_between_the_board_commands_and_the_working_rules():
    guide = _guide()
    assert guide.index("`doing` is how the office\nknows what you are on.") < guide.index("Other commands (")
    assert guide.index("Other commands (") < guide.index("## Working in this office")
    assert guide.count("Other commands (") == 1


def test_inventory_uses_the_given_office_command():
    assert "(`custom-office NAME --help` for each)" in generate_agents_md("w", "t", office_cmd="custom-office")


def test_rows_are_aligned_and_one_line_each():
    block = office_command_inventory(CMD)
    lines = block.strip("\n").splitlines()[2:]
    assert lines and all(l.startswith("    ") and "\n" not in l for l in lines)
    starts = {len(l) - len(l[4:].split("  ", 1)[1].lstrip()) for l in lines}
    assert len(starts) == 1


def test_an_unreadable_command_table_omits_the_inventory_but_not_the_guide(monkeypatch):
    monkeypatch.setitem(sys.modules, "modules.office.cli", None)  # import raises
    assert office_command_inventory(CMD) == ""
    guide = _guide()
    assert "Other commands" not in guide
    assert f"{CMD} take" in guide and "## Working in this office" in guide


def test_no_previously_removed_text_comes_back_through_the_inventory():
    inventory = office_command_inventory(CMD).lower()
    for gone in ("operator", "external entrance", "peers -i", "lifecycle provider", "authority"):
        assert gone not in inventory
