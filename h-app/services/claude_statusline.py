"""Install the context-usage statusLine into a claude config dir.

Same idempotent-safe pattern as services.venv_path/services.tmux_conf:
setup.sh installs it into the default account's config dir (~/.claude) at
bootstrap, h-mesh upgrade repairs existing installs, and
modules.tmux.ops.write_agent_guide installs it into a PROFILED account's
own config dir (~/.claude-<profile>) at hire time -- that directory doesn't
exist at all until an agent using that profile is actually hired, so
setup.sh/upgrade (which only ever touch the default account) can't cover
it up front.

claude only. codex and agy have their own separate statusline mechanisms
(or none) -- this is never installed into their config dirs, and callers
are expected to gate on the agent's own `cli` before calling it.
"""

import argparse
import json
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parents[2]
SHIPPED_STATUSLINE = REPO_ROOT / "assets" / "statusline.py"


def _copy_if_different(src: Path, dst: Path) -> bool:
    if dst.exists() and dst.read_bytes() == src.read_bytes():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    return True


def _merge_statusline_setting(settings_path: Path, script_path: Path) -> bool:
    """Add/overwrite only the `statusLine` key -- every other setting (the
    base image's own seeded defaults, or anything an operator added by
    hand) is preserved untouched, same as h-agent's own installer merges
    settings.json without clobbering it."""
    data: dict = {}
    if settings_path.exists():
        try:
            loaded = json.loads(settings_path.read_text())
            if isinstance(loaded, dict):
                data = loaded
        except (json.JSONDecodeError, OSError):
            pass

    desired = {"type": "command", "command": f"python3 {script_path}"}
    if data.get("statusLine") == desired:
        return False

    data["statusLine"] = desired
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(data, indent=2) + "\n")
    return True


def install_statusline(config_dir: Path, *, log: Callable[[str], None] = print) -> None:
    """Idempotently install the statusline script and settings.json entry
    into config_dir (a claude config dir, e.g. ~/.claude or ~/.claude-work)."""
    target_script = config_dir / "scripts" / "statusline.py"
    script_changed = _copy_if_different(SHIPPED_STATUSLINE, target_script)
    target_script.chmod(0o755)

    settings_changed = _merge_statusline_setting(config_dir / "settings.json", target_script)

    if script_changed or settings_changed:
        log(f"  • {config_dir}: statusline installed")
    else:
        log(f"  • {config_dir}: statusline already up to date")


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="h-mesh install-claude-statusline",
        description="Install the context-usage statusLine into a claude config dir.",
    )
    parser.add_argument("config_dir", help="Path to a claude config dir, e.g. ~/.claude")
    args = parser.parse_args(argv)
    install_statusline(Path(args.config_dir))


if __name__ == "__main__":
    main()
