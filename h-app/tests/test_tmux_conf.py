from pathlib import Path

import os
import pty
import shutil
import subprocess
import time

import pytest

from services.tmux_conf import LEGACY_TMUX_CONF, REPO_ROOT, SHIPPED_TMUX_CONF, install_tmux_conf

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


def test_the_shipped_conf_lives_in_assets_and_the_repo_root_has_none():
    assert SHIPPED_TMUX_CONF == REPO_ROOT / "assets" / "tmux.conf"
    assert LEGACY_TMUX_CONF == REPO_ROOT / "tmux.conf"
    assert not LEGACY_TMUX_CONF.exists()


# --- Installs made before the move hold a symlink to the old root path. ---


def _repo(tmp_path):
    """A fake repo with the NEW layout only: the old root tmux.conf is gone."""
    repo = tmp_path / "repo"
    (repo / "assets").mkdir(parents=True)
    source = repo / "assets" / "tmux.conf"
    source.write_text("set -g mouse on\n")
    home = tmp_path / "home"
    home.mkdir()
    return repo, source, repo / "tmux.conf", home / ".tmux.conf"


def test_a_legacy_link_to_the_old_root_path_is_relinked(tmp_path):
    repo, source, legacy, target = _repo(tmp_path)
    target.symlink_to(legacy)
    assert not target.exists()  # dangling, as on a real upgraded host

    lines = []
    install_tmux_conf(source=source, target=target, legacy=legacy, log=lines.append)

    assert target.is_symlink()
    assert target.resolve() == source.resolve()
    assert target.read_text() == "set -g mouse on\n"
    assert any("re-linked" in line for line in lines)


def test_a_relative_legacy_link_is_relinked(tmp_path):
    repo, source, legacy, target = _repo(tmp_path)
    target.symlink_to(os.path.relpath(legacy, target.parent))

    install_tmux_conf(source=source, target=target, legacy=legacy, log=lambda _: None)

    assert target.resolve() == source.resolve()


def test_a_legacy_link_through_a_symlinked_repo_directory_is_relinked(tmp_path):
    repo, source, legacy, target = _repo(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(repo)
    target.symlink_to(alias / "tmux.conf")

    install_tmux_conf(source=source, target=target, legacy=legacy, log=lambda _: None)

    assert target.resolve() == source.resolve()


def test_a_legacy_link_whose_old_file_still_exists_is_still_relinked(tmp_path):
    repo, source, legacy, target = _repo(tmp_path)
    legacy.write_text("# stale copy of the old root file\n")
    target.symlink_to(legacy)

    install_tmux_conf(source=source, target=target, legacy=legacy, log=lambda _: None)

    assert target.resolve() == source.resolve()
    assert legacy.read_text() == "# stale copy of the old root file\n"


def test_relinking_leaves_no_temporary_link_behind(tmp_path):
    repo, source, legacy, target = _repo(tmp_path)
    target.symlink_to(legacy)

    install_tmux_conf(source=source, target=target, legacy=legacy, log=lambda _: None)

    assert sorted(p.name for p in target.parent.iterdir()) == [".tmux.conf"]


def test_the_real_old_shipped_path_is_relinked_to_the_new_one(tmp_path):
    """The two real constants, as setup.sh and `h-mesh upgrade` call it."""
    home = tmp_path / "home"
    home.mkdir()
    target = home / ".tmux.conf"
    target.symlink_to(LEGACY_TMUX_CONF)  # what every pre-move install holds
    assert not target.exists()

    install_tmux_conf(target=target, log=lambda _: None)

    assert os.path.realpath(target) == os.path.realpath(SHIPPED_TMUX_CONF)


def test_a_dangling_link_elsewhere_is_not_ours_and_is_left_alone(tmp_path):
    repo, source, legacy, target = _repo(tmp_path)
    gone = tmp_path / "other-checkout" / "tmux.conf"
    target.symlink_to(gone)

    install_tmux_conf(source=source, target=target, legacy=legacy, log=lambda _: None)

    assert os.readlink(target) == str(gone)


def test_a_link_to_another_file_in_the_repo_root_is_left_alone(tmp_path):
    repo, source, legacy, target = _repo(tmp_path)
    mine = repo / "my-tmux.conf"
    mine.write_text("# mine\n")
    target.symlink_to(mine)

    install_tmux_conf(source=source, target=target, legacy=legacy, log=lambda _: None)

    assert os.readlink(target) == str(mine)


def test_a_users_real_file_at_the_target_is_untouched_even_with_a_legacy_path(tmp_path):
    repo, source, legacy, target = _repo(tmp_path)
    target.write_text("# my own config\n")

    install_tmux_conf(source=source, target=target, legacy=legacy, log=lambda _: None)

    assert not target.is_symlink()
    assert target.read_text() == "# my own config\n"


def test_an_already_linked_new_install_is_not_rewritten(tmp_path):
    repo, source, legacy, target = _repo(tmp_path)
    target.symlink_to(source)
    before = os.lstat(target).st_ino
    lines = []

    install_tmux_conf(source=source, target=target, legacy=legacy, log=lines.append)

    assert os.lstat(target).st_ino == before
    assert any("already installed" in line for line in lines)


def test_a_fresh_install_links_the_new_location(tmp_path):
    repo, source, legacy, target = _repo(tmp_path)

    install_tmux_conf(source=source, target=target, legacy=legacy, log=lambda _: None)

    assert os.readlink(target) == str(source)


# --- The shipped file itself. ---

CONF = SHIPPED_TMUX_CONF.read_text(encoding="utf-8")


def test_mouse_stays_on_by_default():
    assert "\nset -g mouse on\n" in CONF


def test_ctrl_b_m_toggles_mouse_and_shows_the_state():
    assert 'bind m set -g mouse \\; display "mouse #{?mouse,on,off}"' in CONF


def test_the_conf_documents_the_mouse_toggle_and_what_it_overrides():
    assert "Ctrl+b m" in CONF
    assert "select-pane -m" in CONF  # the default `m` (mark pane) it replaces
    assert "Shift-drag" in CONF and "Option" in CONF
    assert "set-clipboard" not in CONF and "OSC 52" not in CONF  # out of scope


def test_the_dockerfile_no_longer_copies_a_root_tmux_conf_and_still_ships_assets():
    dockerfile = (REPO_ROOT / "container" / "Dockerfile").read_text(encoding="utf-8")
    assert "setup.sh tmux.conf" not in dockerfile
    assert "COPY --chown=ubuntu:ubuntu assets/ ./assets/" in dockerfile


@pytest.mark.skipif(shutil.which("tmux") is None, reason="needs tmux")
def test_the_real_binding_flips_mouse_on_a_private_socket(tmp_path):
    """Press Ctrl+b m on a real attached client; mouse goes on -> off -> on."""
    sock = str(tmp_path / "s")
    env = dict(os.environ, HOME=str(tmp_path), TERM="xterm-256color")
    env.pop("TMUX", None)

    def tmux(*args):
        return subprocess.run(["tmux", "-S", sock, *args], env=env, capture_output=True, text=True, timeout=10)

    def mouse():
        return tmux("show", "-gv", "mouse").stdout.strip()

    def wait_for(want):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if mouse() == want:
                return
            time.sleep(0.05)
        assert mouse() == want

    assert tmux("-f", str(SHIPPED_TMUX_CONF), "new-session", "-d", "-s", "t", "/bin/sleep", "60").returncode == 0
    master, slave = pty.openpty()
    client = subprocess.Popen(["tmux", "-S", sock, "attach", "-t", "t"], stdin=slave, stdout=slave,
                              stderr=slave, env=env)
    try:
        deadline = time.monotonic() + 5
        while not tmux("list-clients").stdout.strip() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert tmux("list-clients").stdout.strip(), "client did not attach"
        assert mouse() == "on"
        os.write(master, b"\x02m")
        wait_for("off")
        os.write(master, b"\x02m")
        wait_for("on")
    finally:
        client.kill()
        client.wait(timeout=5)
        tmux("kill-server")
        os.close(master)
        os.close(slave)
