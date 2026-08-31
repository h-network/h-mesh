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
exec ./setup.sh "$@"
