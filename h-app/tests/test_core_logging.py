"""The stdlib logging threshold in core/logging.py (H_MESH_LOG_LEVEL).

The JSON custody side of that module is exercised through the daemons that
emit it; this covers only the diagnostic side -- what level a port process
starts at, and what happens when the environment asks for nonsense.
"""

import logging
import sys
from pathlib import Path

H_APP = Path(__file__).resolve().parents[1]
if str(H_APP) not in sys.path:
    sys.path.insert(0, str(H_APP))

from core.logging import LOG_LEVEL_ENV_VAR, configure_logging, resolve_log_level


def test_resolve_log_level_accepts_the_standard_names_however_they_are_written():
    assert resolve_log_level("DEBUG") == logging.DEBUG
    assert resolve_log_level("debug") == logging.DEBUG
    assert resolve_log_level("  Warning\n") == logging.WARNING
    assert resolve_log_level("ERROR") == logging.ERROR
    assert resolve_log_level("CRITICAL") == logging.CRITICAL
    # stdlib's own aliases, so an obvious spelling is not a silent demotion
    assert resolve_log_level("WARN") == logging.WARNING
    assert resolve_log_level("FATAL") == logging.CRITICAL


def test_resolve_log_level_falls_back_to_info_rather_than_raising():
    """A port is a short-lived delivery process started by the switch. A typo
    in the tenant env must cost log detail, not the delivery."""
    assert resolve_log_level(None) == logging.INFO
    assert resolve_log_level("") == logging.INFO
    assert resolve_log_level("   ") == logging.INFO
    assert resolve_log_level("DEGUB") == logging.INFO
    # a number is not a name; nothing here maps 10 onto DEBUG
    assert resolve_log_level("10") == logging.INFO


def test_configure_logging_reads_the_environment_and_reports_what_it_applied(monkeypatch):
    root_level = logging.getLogger().level
    try:
        monkeypatch.setenv(LOG_LEVEL_ENV_VAR, "DEBUG")
        assert configure_logging() == logging.DEBUG
        monkeypatch.delenv(LOG_LEVEL_ENV_VAR)
        assert configure_logging() == logging.INFO
    finally:
        logging.getLogger().setLevel(root_level)


def test_configure_logging_says_so_when_it_falls_back(monkeypatch, caplog):
    """A silently-demoted DEGUB rebuilds the exact blind spot the knob
    removes: someone believing they run at DEBUG while debug is dropped."""
    root_level = logging.getLogger().level
    try:
        monkeypatch.setenv(LOG_LEVEL_ENV_VAR, "DEGUB")
        with caplog.at_level(logging.DEBUG, logger="core.logging"):
            assert configure_logging() == logging.INFO
        warnings = [r for r in caplog.records if LOG_LEVEL_ENV_VAR in r.getMessage()]
        assert [r.levelno for r in warnings] == [logging.WARNING]
        assert "DEGUB" in warnings[0].getMessage()

        caplog.clear()
        monkeypatch.setenv(LOG_LEVEL_ENV_VAR, "warning")
        with caplog.at_level(logging.DEBUG, logger="core.logging"):
            assert configure_logging() == logging.WARNING
        assert [r for r in caplog.records if LOG_LEVEL_ENV_VAR in r.getMessage()] == []
    finally:
        logging.getLogger().setLevel(root_level)


def test_every_port_entry_point_configures_logging():
    """The ports are the processes that host core.dispatch, so each one's
    main() is where the threshold gets set -- never at import of a library
    module, which would decide verbosity for whoever imported it."""
    import inspect

    from modules.api import port as api_port
    from modules.office import port as office_port
    from modules.openshell import port as openshell_port
    from modules.tmux import port as tmux_port

    for module in (api_port, office_port, openshell_port, tmux_port):
        source = inspect.getsource(module.main)
        assert "configure_logging()" in source, f"{module.__name__}.main does not configure logging"


def test_every_long_running_daemon_entry_point_configures_logging():
    """The daemons `h-mesh start` leaves running, plus the two started by
    hand. Same rule as the ports: the process that starts is the process that
    picks the threshold, so a logger line added inside any of them later is
    raisable instead of invisible below WARNING."""
    import inspect

    from core import service as switch
    from services import api, session, tmux_reconciler, web_console

    for module in (switch, api, session, tmux_reconciler, web_console):
        source = inspect.getsource(module.main)
        assert "configure_logging()" in source, f"{module.__name__}.main does not configure logging"


def test_the_telegram_bot_launcher_does_not_configure_logging_a_second_time():
    """services/telegram_bot.py imports clients.telegram.bot, which already
    configures at import from the same variable. A second basicConfig here
    would be a no-op that reads like the real thing."""
    import inspect

    from services import telegram_bot

    assert "configure_logging()" not in inspect.getsource(telegram_bot.main)
