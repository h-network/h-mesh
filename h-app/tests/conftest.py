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
"""

import pytest


@pytest.fixture(autouse=True)
def _scrub_ambient_tmux_env(monkeypatch):
    for var in ("TMUX_TMPDIR", "TMUX_SESSION", "TMUX_SOCKET"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _scrub_ambient_telegram_and_api_env(monkeypatch):
    for var in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_VOICE", "API_TOKEN"):
        monkeypatch.delenv(var, raising=False)
