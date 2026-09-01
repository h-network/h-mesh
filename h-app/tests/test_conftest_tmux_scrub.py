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


def test_require_isolated_tmux_with_tmux_and_isolated_tmpdir(monkeypatch, tmp_path):
    monkeypatch.setenv("TMUX", "/tmp/ambient_office.sock,12345,0")
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))
    require_isolated_tmux()  # does not raise because tmp_path != ambient_office.sock


def test_require_isolated_tmux_raises_when_target_matches_ambient_socket(monkeypatch, tmp_path):
    ambient_sock = str(tmp_path / "ambient.sock")
    monkeypatch.setenv("TMUX", f"{ambient_sock},12345,0")
    monkeypatch.setenv("TMUX_SOCKET", ambient_sock)
    with pytest.raises(AmbientTmuxError) as excinfo:
        require_isolated_tmux()
    assert "matches the ambient session socket ($TMUX)" in str(excinfo.value)


def test_require_isolated_tmux_raises_when_explicit_socket_matches_ambient_socket(monkeypatch, tmp_path):
    ambient_sock = str(tmp_path / "ambient.sock")
    monkeypatch.setenv("TMUX", f"{ambient_sock},12345,0")
    with pytest.raises(AmbientTmuxError) as excinfo:
        require_isolated_tmux(socket=ambient_sock)
    assert "matches the ambient session socket ($TMUX)" in str(excinfo.value)
