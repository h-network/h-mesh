from pathlib import Path

from services.tmux_conf import SHIPPED_TMUX_CONF, install_tmux_conf

# ⚠ Every test here uses an explicit source/target, never the real
# ~/.tmux.conf -- this module symlinks files in place.


def test_install_tmux_conf_symlinks_a_fresh_target(tmp_path):
    source = tmp_path / "tmux.conf"
    source.write_text("set -g mouse on\n")
    target = tmp_path / "home" / ".tmux.conf"
    target.parent.mkdir()

    install_tmux_conf(source=source, target=target)

    assert target.is_symlink()
    assert target.resolve() == source.resolve()
    assert target.read_text() == "set -g mouse on\n"


def test_install_tmux_conf_is_idempotent(tmp_path):
    source = tmp_path / "tmux.conf"
    source.write_text("set -g mouse on\n")
    target = tmp_path / "home" / ".tmux.conf"
    target.parent.mkdir()

    install_tmux_conf(source=source, target=target)
    install_tmux_conf(source=source, target=target)

    assert target.is_symlink()
    assert target.resolve() == source.resolve()


def test_install_tmux_conf_leaves_an_existing_real_file_alone(tmp_path):
    source = tmp_path / "tmux.conf"
    source.write_text("set -g mouse on\n")
    target = tmp_path / "home" / ".tmux.conf"
    target.parent.mkdir()
    target.write_text("# my own config\nset -g mouse off\n")

    install_tmux_conf(source=source, target=target)

    assert not target.is_symlink()
    assert target.read_text() == "# my own config\nset -g mouse off\n"


def test_install_tmux_conf_leaves_a_foreign_symlink_alone(tmp_path):
    source = tmp_path / "tmux.conf"
    source.write_text("set -g mouse on\n")
    elsewhere = tmp_path / "elsewhere.conf"
    elsewhere.write_text("# not h-mesh's\n")
    target = tmp_path / "home" / ".tmux.conf"
    target.parent.mkdir()
    target.symlink_to(elsewhere)

    install_tmux_conf(source=source, target=target)

    assert target.resolve() == elsewhere.resolve()


def test_shipped_tmux_conf_exists_and_is_reachable_from_the_module():
    assert SHIPPED_TMUX_CONF.name == "tmux.conf"
    assert SHIPPED_TMUX_CONF.exists(), f"expected {SHIPPED_TMUX_CONF} to exist in the repo"
