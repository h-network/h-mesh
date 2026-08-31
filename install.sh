#!/bin/sh
# install.sh -- one-line installer for h-mesh.
#
#   curl -fsSL https://raw.githubusercontent.com/h-network/h-mesh/main/install.sh | sh
#
# Clones (or updates) h-network/h-mesh, then hands off entirely to its own
# setup.sh for the actual install/wizard -- this script's only job is
# getting a checkout onto disk. POSIX sh, not bash: piped to `sh` ignores
# any shebang, so this can't rely on bash-only syntax the way setup.sh
# itself does; it doesn't need to.
#
# Extra arguments pass straight through to setup.sh, e.g.:
#   curl -fsSL .../install.sh | sh -s -- --pod mypod --tenant mytenant
set -eu

repo=${H_MESH_REPOSITORY:-h-network/h-mesh}
ref=${H_MESH_VERSION:-main}
url=${H_MESH_CLONE_URL:-https://github.com/$repo.git}
dest=${H_MESH_INSTALL_DIR:-$HOME/h-mesh}

if ! command -v git >/dev/null 2>&1; then
    echo "error: installing h-mesh requires git" >&2
    exit 1
fi

if [ -d "$dest/.git" ]; then
    echo "Updating existing h-mesh checkout at $dest..."
    git -C "$dest" fetch origin "$ref"
    git -C "$dest" checkout "$ref"
    git -C "$dest" pull --ff-only origin "$ref"
elif [ -e "$dest" ]; then
    echo "error: $dest already exists and is not a git checkout" >&2
    echo "  set H_MESH_INSTALL_DIR to choose a different location" >&2
    exit 1
else
    echo "Cloning h-mesh into $dest..."
    git clone --branch "$ref" "$url" "$dest"
fi

cd "$dest"

# ⚠ `curl ... | sh` consumes stdin as sh's own script source -- by the time
# we get here, fd 0 is the exhausted pipe, not a terminal, even when this
# was run interactively. Without this, setup.sh's own `[ -t 0 ]` check
# (correctly) sees a non-tty and silently skips the wizard, with no prompts
# and no error -- a real user hit exactly that live. Re-point stdin at the
# controlling terminal before handing off, when there is one; if there
# isn't (genuinely non-interactive -- CI, cron, a script piped from a
# file), opening /dev/tty fails and setup.sh keeps today's correct
# non-interactive behavior.
# ⚠ Probed in a subshell with a no-op, not a bare `exec 3</dev/tty` as the
# if-condition directly -- POSIX requires a shell to exit outright when a
# bare `exec`'s own redirection fails, even one used only as an if's
# condition. That's fine for the real handoff below (failing there really
# should end the script), but as a *probe* it would have killed this
# script instead of just answering "no tty" -- measured: `sh install.sh`
# with no controlling terminal exited before ever reaching the fallback
# branch.
if ( : < /dev/tty ) 2>/dev/null; then
    exec ./setup.sh "$@" < /dev/tty
else
    exec ./setup.sh "$@"
fi
