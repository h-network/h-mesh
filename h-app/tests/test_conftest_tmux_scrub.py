import os

import pytest

from modules.tmux.ops import AmbientTmuxError, require_isolated_tmux


def test_ambient_tmux_vars_are_scrubbed_by_default():
    assert "TMUX_TMPDIR" not in os.environ
    assert "TMUX_SESSION" not in os.environ
    assert "TMUX_SOCKET" not in os.environ


def test_a_forgotten_isolation_override_fails_closed_not_open():
    # The actual point of the autouse fixture: with nothing overridden, the
    # existing ambient-tmux guard has nothing to silently fall back to, so
    # it refuses -- rather than a forgotten test finding this office's real
    # tmux server still sitting there in os.environ.
    with pytest.raises(AmbientTmuxError):
        require_isolated_tmux()


def test_a_test_can_still_set_real_isolated_values_itself(monkeypatch, tmp_path):
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))
    require_isolated_tmux()  # does not raise
