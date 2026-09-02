"""Test-suite-wide safety nets.

⚠ Autouse, not opt-in. A test that spawns `services.tmux_reconciler`
(directly, or indirectly via `setup.sh`/`install.sh`/`services.daemons`)
without its own explicit `TMUX_TMPDIR`/`TMUX_SESSION`/`TMUX_SOCKET`
override inherits this office's real ambient values, points a genuine
reconciler at the *live* office, and reaps real agents' tmux windows. This
has happened three times now, each time a different test that forgot to
isolate. Scrubbing these three vars from `os.environ` before every test
means a forgotten override now fails *closed* --
`modules.tmux.ops.require_isolated_tmux()` refuses to run with nothing set
-- instead of failing *open* onto the real office. A test that genuinely
needs real (isolated) values still sets them itself, exactly as before;
this only removes the ambient fallback that let a missing override go
unnoticed.

⚠ Same class of leak, same fix, for `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`/
`TELEGRAM_VOICE`/`API_TOKEN`: this office runs a real Telegram bot, so
those are genuinely set in the ambient shell. A test building env via a
bare `dict(os.environ)` (the same pattern that leaked tmux vars) inherits
them, and `services.daemons.enabled_daemon_modules()` -- which decides
whether to start the real `api`/`telegram_bot` daemons by checking for
exactly these vars -- would then try to start them, with this office's
live bot token, in a test that never asked for any of that. Caught before
it reached an actual daemon (test_upgrade.py's api daemon failed to bind,
not "successfully impersonated the live bot"), but scrub these
unconditionally too rather than trust every future test to remember.

⚠ `pytest_sessionstart` below reaps orphaned tmpdirs/tmux servers/daemons
left by a PREVIOUSLY KILLED test process (see `_leak_manifest.py` for why
in-process `finally` cleanup cannot close this gap by itself). It runs once,
automatically, at the start of every session on this shared sandbox --
nothing a test needs to opt into.
"""

import pytest

from _leak_manifest import reap_all_orphans


@pytest.fixture(autouse=True)
def _scrub_ambient_tmux_env(monkeypatch):
    for var in ("TMUX_TMPDIR", "TMUX_SESSION", "TMUX_SOCKET"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _scrub_ambient_telegram_and_api_env(monkeypatch):
    for var in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_VOICE", "API_TOKEN"):
        monkeypatch.delenv(var, raising=False)


def pytest_sessionstart(session: pytest.Session) -> None:
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    log = reporter.write_line if reporter is not None else print
    reaped = reap_all_orphans(log=log)
    if reaped:
        log(f"conftest: reaped {reaped} orphaned test tmpdir(s) from a previously killed run")


@pytest.fixture
def managed_tmpdir(tmp_path_factory):
    """Factory fixture: `tmpdir = managed_tmpdir("h_mesh_test_foo_")` creates
    a `tempfile.mkdtemp`-equivalent directory, registers it against a crash
    (see `_leak_manifest.py`), and guarantees the same cleanup
    `tempfile.mkdtemp` callers were doing by hand -- on ANY normal test exit,
    including an assertion failure, not only the success path. It does not
    protect against an external SIGKILL of this process; `pytest_sessionstart`
    above is what closes that gap, on the next session."""
    import tempfile

    from _leak_manifest import clear, reap_orphan, register

    created: list[tuple[str, object]] = []

    def make(prefix: str) -> str:
        tmpdir = tempfile.mkdtemp(prefix=prefix)
        entry = register(tmpdir)
        created.append((tmpdir, entry))
        return tmpdir

    yield make

    for tmpdir, entry in created:
        reap_orphan(tmpdir)
        clear(entry)
