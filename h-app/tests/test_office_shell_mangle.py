"""Shell-mangled text: warn on the hole an expansion leaves, reject blank text.

The shell expands `x`, $(x) and $X inside a double-quoted argument before
office runs, so office can only see the leftover double space (or nothing).
Text from --stdin/--file is never shell-parsed and is never checked.
"""

import io
import subprocess
from unittest.mock import patch

import pytest

from test_office_cli import POD, TENANT, FakeRedis, _env
from modules.office.cli import main as office_main

WARNING = "the shell removed it"


def _run(argv, monkeypatch, *, stdin=None, registry=None):
    _env(monkeypatch)
    r = FakeRedis(registry=registry or {"architect": "tmux", "backend": "tmux"})
    patches = [patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")),
               patch("modules.office.cli.send", return_value="s" * 32)]
    if stdin is not None:
        patches.append(patch("modules.office.cli.sys.stdin", io.StringIO(stdin)))
    with patches[0], patches[1] as send:
        if stdin is not None:
            with patches[2]:
                office_main(argv)
        else:
            office_main(argv)
    return send


def _shell_expanded(command: str) -> str:
    """What a real shell hands office for `command` (not mocked)."""
    return subprocess.run(["bash", "-c", f"printf %s {command}"], capture_output=True,
                          text=True, check=True).stdout


def test_the_hole_a_real_shell_leaves_is_what_we_detect():
    # The #16 report: a backticked name inside a double-quoted argument vanishes.
    assert _shell_expanded('"the `ghost-name` setting"') == "the  setting"


@pytest.mark.parametrize("argv", [
    ["send", "-a", "backend"],
    ["broadcast"],
])
def test_double_space_from_argv_warns_but_still_sends(argv, monkeypatch, capsys):
    send = _run([*argv, "the  setting"], monkeypatch)
    assert WARNING in capsys.readouterr().err
    assert send.call_args.kwargs["payload"] == {"text": "the  setting"}


def test_add_warns_for_title_and_description_but_still_adds(monkeypatch, capsys):
    send = _run(["add", "-a", "backend", "-t", "fix the  thing", "-d", "see  below"], monkeypatch)
    err = capsys.readouterr().err
    assert "the title" in err and "the description" in err
    assert send.call_args_list[0].kwargs["payload"]["title"] == "fix the  thing"


@pytest.mark.parametrize("text", [
    "plain text", "Two sentences.  Second one", "   indented only", "line one\n    code",
    "trailing   ", "a\nb",
])
def test_ordinary_text_does_not_warn(text, monkeypatch, capsys):
    _run(["send", "-a", "backend", text], monkeypatch)
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("argv", [["send", "-a", "backend", "--stdin"], ["broadcast", "--stdin"]])
def test_stdin_text_is_never_checked(argv, monkeypatch, capsys):
    send = _run(argv, monkeypatch, stdin="the  setting\n")
    assert capsys.readouterr().err == ""
    assert send.call_args.kwargs["payload"] == {"text": "the  setting\n"}


@pytest.mark.parametrize("argv", [["send", "-a", "backend"], ["broadcast"]])
@pytest.mark.parametrize("blank", ["", "   ", "\n"])
def test_blank_argv_text_is_refused_not_sent(argv, blank, monkeypatch, capsys):
    with pytest.raises(SystemExit) as info:
        send = _run([*argv, blank], monkeypatch)
    assert info.value.code == 1
    assert "use --stdin or --file" in capsys.readouterr().err


def test_a_real_shell_substitution_that_empties_the_body_is_refused(monkeypatch, capsys):
    body = _shell_expanded('"$(true)"')
    assert body == ""
    with pytest.raises(SystemExit):
        _run(["send", "-a", "backend", body], monkeypatch)


def test_add_description_from_stdin_and_file(monkeypatch, capsys, tmp_path):
    send = _run(["add", "-a", "backend", "-t", "t", "--description-stdin"], monkeypatch,
                stdin="full  text\nwith `code`\n")
    assert capsys.readouterr().err == ""
    assert send.call_args_list[0].kwargs["payload"]["description"] == "full  text\nwith `code`\n"

    path = tmp_path / "d.txt"
    path.write_text("from a file  here", encoding="utf-8")
    send = _run(["add", "-a", "backend", "-t", "t", "--description-file", str(path)], monkeypatch)
    assert send.call_args_list[0].kwargs["payload"]["description"] == "from a file  here"


def test_add_requires_exactly_one_description_source(monkeypatch, capsys, tmp_path):
    for extra in ([], ["-d", "x", "--description-stdin"]):
        with pytest.raises(SystemExit) as info:
            _run(["add", "-a", "backend", "-t", "t", *extra], monkeypatch, stdin="x")
        assert info.value.code == 2


def test_add_blank_inputs_are_refused(monkeypatch, capsys):
    for argv, stdin in ((["-t", "t", "-d", " "], None), (["-t", " ", "-d", "d"], None),
                        (["-t", "t", "--description-stdin"], "  ")):
        with pytest.raises(SystemExit) as info:
            _run(["add", "-a", "backend", *argv], monkeypatch, stdin=stdin)
        assert info.value.code == 1
