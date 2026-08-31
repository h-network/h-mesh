from unittest.mock import Mock

import pytest

from modules.office import dispatcher


def test_no_args_prints_unified_help(capsys):
    dispatcher.main([])
    out = capsys.readouterr().out
    assert "usage: h-mesh" in out
    assert "hire" in out
    assert "api" in out
    assert "start" in out
    assert "upgrade" in out
    assert "tmux-reconciler" in out


def test_office_command_delegates_with_original_argv(monkeypatch):
    target = Mock()
    monkeypatch.setattr(dispatcher.office_cli, "main", target)

    dispatcher.main(["send", "-a", "worker", "hello"])

    target.assert_called_once_with(["send", "-a", "worker", "hello"])


@pytest.mark.parametrize(
    ("command", "module"),
    [
        ("openshell-port", "modules.openshell.port"),
        ("start", "services.daemons"),
        ("tmux-port", "modules.tmux.port"),
        ("upgrade", "services.upgrade"),
    ],
)
def test_argv_service_delegates_remainder(monkeypatch, command, module):
    target = Mock()
    loader = Mock(return_value=target)
    monkeypatch.setattr(dispatcher, "_load", loader)

    dispatcher.main([command, "--verbose"])

    assert loader.call_args.args[0].module == module
    target.assert_called_once_with(["--verbose"])


@pytest.mark.parametrize(
    ("command", "module"),
    [
        ("api", "services.api"),
        ("session", "services.session"),
        ("switch", "core.service"),
        ("tmux-reconciler", "services.tmux_reconciler"),
        ("watchdog", "services.watchdog"),
    ],
)
def test_zero_argument_service_calls_existing_entrypoint(monkeypatch, command, module):
    target = Mock()
    loader = Mock(return_value=target)
    monkeypatch.setattr(dispatcher, "_load", loader)

    dispatcher.main([command])

    assert loader.call_args.args[0].module == module
    target.assert_called_once_with()


def test_zero_argument_service_rejects_extra_arguments(monkeypatch, capsys):
    target = Mock()
    monkeypatch.setattr(dispatcher, "_load", Mock(return_value=target))

    with pytest.raises(SystemExit) as exc:
        dispatcher.main(["switch", "--unexpected"])

    assert exc.value.code == 2
    assert "switch does not accept arguments" in capsys.readouterr().err
    target.assert_not_called()


def test_unknown_command_is_an_argparse_error(capsys):
    with pytest.raises(SystemExit) as exc:
        dispatcher.main(["no-such-command"])

    assert exc.value.code == 2
    assert "unknown command: no-such-command" in capsys.readouterr().err


def test_service_modules_are_loaded_only_when_dispatched(monkeypatch):
    imported = []
    target = Mock()

    def import_module(name):
        imported.append(name)
        return type("Module", (), {"main": target})

    monkeypatch.setattr(dispatcher.importlib, "import_module", import_module)

    dispatcher.main(["watchdog"])

    assert imported == ["services.watchdog"]
    target.assert_called_once_with()
