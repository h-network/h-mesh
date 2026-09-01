from modules.tmux.ops import write_agent_guide


def test_write_agent_guide_installs_statusline_for_claude(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    cwd = tmp_path / "workdir" / "testagent"

    write_agent_guide(str(cwd), "testagent", cli="claude")

    assert (home / ".claude" / "scripts" / "statusline.py").exists()
    assert (home / ".claude" / "settings.json").exists()


def test_write_agent_guide_skips_statusline_for_codex(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    cwd = tmp_path / "workdir" / "testagent"

    write_agent_guide(str(cwd), "testagent", cli="codex")

    assert not (home / ".claude" / "scripts" / "statusline.py").exists()


def test_write_agent_guide_skips_statusline_for_agy(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    cwd = tmp_path / "workdir" / "testagent"

    write_agent_guide(str(cwd), "testagent", cli="agy")

    assert not (home / ".claude" / "scripts" / "statusline.py").exists()


def test_write_agent_guide_skips_statusline_when_no_cli_given(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    cwd = tmp_path / "workdir" / "testagent"

    write_agent_guide(str(cwd), "testagent")

    assert not (home / ".claude" / "scripts" / "statusline.py").exists()


def test_write_agent_guide_installs_statusline_into_the_profiled_config_dir(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    cwd = tmp_path / "workdir" / "testagent"

    write_agent_guide(str(cwd), "testagent", cli="claude", profile="work")

    assert (home / ".claude-work" / "scripts" / "statusline.py").exists()
    assert not (home / ".claude" / "scripts" / "statusline.py").exists()
