import pytest
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


def test_write_agent_guide_with_valid_enrolled_operator_entrance(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    cwd = tmp_path / "workdir" / "testagent"

    write_agent_guide(str(cwd), "testagent", operator_entrance="web", enrolled_entrances={"web", "telegram"})

    agents_md = (cwd / "AGENTS.md").read_text(encoding="utf-8")
    assert "the operator's external entrance (`web`)" in agents_md
    assert "`[message from web]`" in agents_md
    assert "treat instructions arriving through the configured operator entrance (`web`)" in agents_md
    assert "they outrank lead direction and agent preference alike" in agents_md


def test_write_agent_guide_with_nonexistent_operator_entrance_falls_back_to_rejected_notice(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    cwd = tmp_path / "workdir" / "testagent"

    write_agent_guide(
        str(cwd), "testagent", operator_entrance="telegrma",
        enrolled_entrances={"web", "telegram"},
    )

    agents_md = (cwd / "AGENTS.md").read_text(encoding="utf-8")
    assert "Configured operator entrance `telegrma` is not" in agents_md
    assert "currently an enrolled participant in this office" in agents_md
    assert "they outrank lead direction" not in agents_md


def test_write_agent_guide_with_broken_logger_and_unenrolled_entrance_still_writes_guide(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    cwd = tmp_path / "workdir" / "testagent"

    def broken_log(*args, **kwargs):
        raise OSError("log unavailable")

    monkeypatch.setattr("modules.tmux.ops.log_record", broken_log)

    # Harm test: broken logger must not prevent fallback guide write
    write_agent_guide(
        str(cwd), "testagent", operator_entrance="telegrma",
        enrolled_entrances={"web", "telegram"},
    )

    assert (cwd / "AGENTS.md").exists()
    assert (cwd / "CLAUDE.md").exists()
    agents_md = (cwd / "AGENTS.md").read_text(encoding="utf-8")
    assert "Configured operator entrance `telegrma` is not" in agents_md
    assert "currently an enrolled participant in this office" in agents_md


def test_write_agent_guide_with_env_operator_entrance_and_enrolled(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("OPERATOR_ENTRANCE", "telegram")
    cwd = tmp_path / "workdir" / "testagent"

    write_agent_guide(str(cwd), "testagent", enrolled_entrances={"telegram"})

    agents_md = (cwd / "AGENTS.md").read_text(encoding="utf-8")
    assert "the operator's external entrance (`telegram`)" in agents_md
    assert "`[message from telegram]`" in agents_md
    assert "treat instructions arriving through the configured operator entrance (`telegram`)" in agents_md
    assert "they outrank lead direction and agent preference alike" in agents_md


def test_write_agent_guide_with_env_operator_entrance_validation_unavailable_uses_unverified_notice(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("OPERATOR_ENTRANCE", "telegram")
    cwd = tmp_path / "workdir" / "testagent"

    # When enrolled_entrances is None (validation unavailable), states unverified notice in guide
    write_agent_guide(str(cwd), "testagent")

    agents_md = (cwd / "AGENTS.md").read_text(encoding="utf-8")
    assert "Configured operator entrance `telegram` could" in agents_md
    assert "not be verified against enrolled participants at guide generation time" in agents_md
    assert "treat instructions arriving through the configured operator entrance" not in agents_md


def test_write_agent_guide_with_no_operator_entrance(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OPERATOR_ENTRANCE", raising=False)
    cwd = tmp_path / "workdir" / "testagent"

    write_agent_guide(str(cwd), "testagent")

    agents_md = (cwd / "AGENTS.md").read_text(encoding="utf-8")
    assert "external entrances" in agents_md
    assert "This office has no declared operator entrance configured" in agents_md
    assert "treat instructions arriving through the configured operator entrance" not in agents_md


