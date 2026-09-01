import json
from pathlib import Path

from services.claude_statusline import SHIPPED_STATUSLINE, install_statusline


def test_install_statusline_installs_the_script(tmp_path):
    install_statusline(tmp_path)

    target = tmp_path / "scripts" / "statusline.py"
    assert target.exists()
    assert target.read_bytes() == SHIPPED_STATUSLINE.read_bytes()
    assert target.stat().st_mode & 0o755 == 0o755


def test_install_statusline_writes_the_settings_entry(tmp_path):
    install_statusline(tmp_path)

    settings = json.loads((tmp_path / "settings.json").read_text())
    assert settings["statusLine"] == {
        "type": "command",
        "command": f"python3 {tmp_path / 'scripts' / 'statusline.py'}",
    }


def test_install_statusline_preserves_other_settings_keys(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({
        "promptSuggestionEnabled": False,
        "env": {"DISABLE_TELEMETRY": "1"},
    }))

    install_statusline(tmp_path)

    settings = json.loads(settings_path.read_text())
    assert settings["promptSuggestionEnabled"] is False
    assert settings["env"] == {"DISABLE_TELEMETRY": "1"}
    assert "statusLine" in settings


def test_install_statusline_is_idempotent(tmp_path):
    install_statusline(tmp_path)
    first_script_mtime = (tmp_path / "scripts" / "statusline.py").stat().st_mtime_ns
    first_settings = (tmp_path / "settings.json").read_text()

    install_statusline(tmp_path)

    assert (tmp_path / "scripts" / "statusline.py").stat().st_mtime_ns == first_script_mtime
    assert (tmp_path / "settings.json").read_text() == first_settings


def test_install_statusline_updates_a_stale_script_and_settings_entry(tmp_path):
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir(parents=True)
    (scripts_dir / "statusline.py").write_text("# old version\n")
    (tmp_path / "settings.json").write_text(json.dumps({
        "statusLine": {"type": "command", "command": "python3 /somewhere/else/statusline.py"},
    }))

    install_statusline(tmp_path)

    target = tmp_path / "scripts" / "statusline.py"
    assert target.read_bytes() == SHIPPED_STATUSLINE.read_bytes()
    settings = json.loads((tmp_path / "settings.json").read_text())
    assert settings["statusLine"]["command"] == f"python3 {target}"


def test_install_statusline_handles_malformed_existing_settings(tmp_path):
    (tmp_path / "settings.json").write_text("not valid json{{{")

    install_statusline(tmp_path)  # must not raise

    settings = json.loads((tmp_path / "settings.json").read_text())
    assert "statusLine" in settings
