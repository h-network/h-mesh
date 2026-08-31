import tempfile
from pathlib import Path

from services.venv_path import persist_venv_on_path

# ⚠ Every test here must run against a throwaway HOME, never the real one --
# this module edits ~/.bashrc and ~/.profile in place. monkeypatch.setenv is
# used instead of patching os.environ directly so it's guaranteed to be
# restored even if a test fails partway through.


def test_persist_venv_on_path_adds_block_to_bashrc_and_profile(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    venv_dir = tmp_path / "myvenv"

    persist_venv_on_path(venv_dir)

    for filename in (".bashrc", ".profile"):
        content = (home / filename).read_text()
        assert f'export PATH="{venv_dir / "bin"}:$PATH"' in content
        assert "# >>> h-mesh venv PATH >>>" in content
        assert "# <<< h-mesh venv PATH <<<" in content


def test_persist_venv_on_path_preserves_existing_content(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    (home / ".bashrc").write_text("# my existing config\nalias ll='ls -la'\n")

    persist_venv_on_path(tmp_path / "myvenv")

    content = (home / ".bashrc").read_text()
    assert "# my existing config" in content
    assert "alias ll='ls -la'" in content
    assert "h-mesh venv PATH" in content


def test_persist_venv_on_path_is_idempotent(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    venv_dir = tmp_path / "myvenv"

    persist_venv_on_path(venv_dir)
    first = (home / ".bashrc").read_text()
    persist_venv_on_path(venv_dir)
    second = (home / ".bashrc").read_text()

    assert first == second
    assert second.count("# >>> h-mesh venv PATH >>>") == 1


def test_persist_venv_on_path_updates_in_place_when_venv_moves(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    (home / ".profile").write_text("echo before\n")

    persist_venv_on_path(tmp_path / "old-venv")
    persist_venv_on_path(tmp_path / "new-venv")

    content = (home / ".profile").read_text()
    assert "echo before" in content
    assert str(tmp_path / "old-venv" / "bin") not in content
    assert f'export PATH="{tmp_path / "new-venv" / "bin"}:$PATH"' in content
    assert content.count("# >>> h-mesh venv PATH >>>") == 1


def test_persist_venv_on_path_creates_missing_files(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    assert not (home / ".bashrc").exists()
    assert not (home / ".profile").exists()

    persist_venv_on_path(tmp_path / "myvenv")

    assert (home / ".bashrc").exists()
    assert (home / ".profile").exists()
