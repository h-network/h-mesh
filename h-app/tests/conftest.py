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
"""

import pytest


@pytest.fixture(autouse=True)
def _scrub_ambient_tmux_env(monkeypatch):
    for var in ("TMUX_TMPDIR", "TMUX_SESSION", "TMUX_SOCKET"):
        monkeypatch.delenv(var, raising=False)
