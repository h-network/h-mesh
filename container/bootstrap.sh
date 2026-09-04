#!/usr/bin/env bash
# container/bootstrap.sh — host-vs-container picker's container path.
#
# setup.sh hands off here entirely (see its own "Host-vs-container picker"
# comment) once --container/H_MESH_INSTALL_MODE=container is chosen. This is
# a separate, much smaller wizard than setup.sh's own: it only collects what
# a tenant's env file needs (POD, TENANT, AGENTS, DEFAULT_CLI), writes/
# updates that file, and hands off to `docker compose up --build` -- setup.sh's
# own non-interactive path, roster seeding, hire, and daemon-start logic
# still run, just inside the container (see container/entrypoint.sh), not
# reimplemented here.
#
# ⚠ ONE OFFICE, ONE DIRECTORY, ONE EXPLICIT PROJECT NAME. Docker Compose's
# own project-name default is the *containing folder's* name -- identical
# ("container") for every checkout of this repo regardless of pod/tenant,
# so two checkouts (or two offices deployed from the same one) collide on
# the same container/volume names and one `up` silently recreates the
# other's live tenant. Measured live, not theorized. This script always
# resolves an office to $REPO_ROOT/offices/<pod>/<tenant>/.env and always
# passes `docker compose -p h-mesh-<pod>-<tenant>` explicitly -- never the
# implicit default -- so multiple offices on one host, or one checkout,
# are isolated by construction rather than by an operator remembering to
# pass `-p` themselves. (The predecessor project solves the identical
# problem the same way, with a per-office directory of its own.)
#
# At a terminal, with no flags, this prompts the same way setup.sh's own
# wizard does; piped/scripted or with --non-interactive, it reads flags/env
# only, same as setup.sh. Advanced options (AGENT_CLIS/AGENT_PROFILES/
# AGENT_PROVIDERS, CLAUDE_OAUTH_TOKEN_<PROFILE>, PROVIDER_LOCAL_*,
# TELEGRAM_*, API_TOKEN, H_MESH_ALLOW_PLAINTEXT, API_TLS_CERT/KEY) aren't
# prompted for here -- add them to the office's env file directly; every one
# of them reaches the container exactly as documented in README.md's
# "Bootstrap script" section.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

ENV_FILE_OVERRIDE="${H_MESH_TENANT_ENV_FILE:-}"
NON_INTERACTIVE=0
SKIP_BUILD=0
ATTACH=0
POD="${POD:-}"
TENANT="${TENANT:-}"
AGENTS="${AGENTS:-}"
DEFAULT_CLI="${DEFAULT_CLI:-}"

usage() {
    cat <<EOF
Usage: ./setup.sh --container [options]
       ./container/bootstrap.sh [options]

Collects what an office's env file needs (POD, TENANT, AGENTS,
DEFAULT_CLI), writes it to offices/<pod>/<tenant>/.env, then runs
'docker compose -p h-mesh-<pod>-<tenant> up --build' -- setup.sh's own
non-interactive path runs inside the container from there (see
container/entrypoint.sh).

Options:
  --pod <name>            Pod name (default: \$POD, or existing office's value, or "default")
  --tenant <name>         Tenant name (default: \$TENANT, or existing office's value, or "default")
  --agents <a,b,c>        Comma-separated agent names (default: \$AGENTS, or existing value, or "architect")
  --cli <claude|codex|agy> Default CLI (default: \$DEFAULT_CLI, or existing value, or "claude")
  --env-file <path>       Use this exact env file instead of offices/<pod>/<tenant>/.env
                          (advanced/testing use -- also disables the project-name isolation
                          this script otherwise guarantees; pass --project-name too if you use it)
  --project-name <name>   Override the computed h-mesh-<pod>-<tenant> Compose project name
  --skip-build            Run 'docker compose up' without '--build' (image must already exist)
  --non-interactive       Never prompt; use flags/env/existing office values only
  --attach                Skip build/up entirely; attach to the running office's tmux
                          session in one step (same --pod/--tenant target it otherwise
                          would). Requires the office to already be up -- see --skip-build
                          if you only meant to skip a rebuild, not the up itself.
  -h, --help              Show this help message

Advanced options (per-agent exceptions, credentials, Telegram, TLS) are not
prompted for here -- add them directly to the office's env file; see
README.md's "Bootstrap script" section for the complete non-interactive
variable set, all of which apply unchanged inside the container.
EOF
    exit 0
}

PROJECT_NAME_OVERRIDE=""
while [ $# -gt 0 ]; do
    case "$1" in
        --pod) POD="$2"; shift 2 ;;
        --tenant) TENANT="$2"; shift 2 ;;
        --agents) AGENTS="$2"; shift 2 ;;
        --cli) DEFAULT_CLI="$2"; shift 2 ;;
        --env-file) ENV_FILE_OVERRIDE="$2"; shift 2 ;;
        --project-name) PROJECT_NAME_OVERRIDE="$2"; shift 2 ;;
        --skip-build) SKIP_BUILD=1; shift ;;
        --non-interactive) NON_INTERACTIVE=1; shift ;;
        --attach) ATTACH=1; shift ;;
        -h|--help) usage ;;
        *) echo "Unknown option: $1" >&2; usage ;;
    esac
done

INTERACTIVE=1
[ "$NON_INTERACTIVE" -eq 1 ] && INTERACTIVE=0
[ -t 0 ] || INTERACTIVE=0

echo "=== h-mesh :: container bootstrap ==="

if ! command -v docker >/dev/null 2>&1; then
    echo "error: docker is required for --container -- install it and re-run (https://docs.docker.com/engine/install/)" >&2
    exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
    echo "error: 'docker compose' is required (docker compose v2 plugin, not standalone docker-compose)" >&2
    exit 1
fi

# Same contract as core/keys.py's validate_segment (the Redis-key identity
# rule every daemon and CLI in this project already enforces) -- kept as a
# plain bash regex, not a call into the Python package, because this script
# runs before any venv/image exists to import it from.
validate_identity_segment() {
    local value="$1" label="$2"
    if [[ ! "$value" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]]; then
        echo "error: $label must be lowercase alphanumeric/hyphens, 1-63 chars, starting with a letter or digit (got: '$value')" >&2
        return 1
    fi
    if [[ "$value" =~ ^[0-9]+$ ]]; then
        echo "error: $label cannot be all digits (got: '$value')" >&2
        return 1
    fi
    case "$value" in
        pod|tenant|agent|all)
            echo "error: $label cannot be the reserved word '$value'" >&2
            return 1 ;;
    esac
    return 0
}

# Pod/tenant are resolved BEFORE the env file path -- the path is derived
# from them (offices/<pod>/<tenant>/.env), so there is no file to read
# defaults from until they're known. A re-run against an already-configured
# office still gets "blank keeps existing" for AGENTS/DEFAULT_CLI below,
# once that file is located.
if [ "$INTERACTIVE" -eq 1 ] && [ -z "$POD" ]; then
    read -rp "Pod name [default]: " POD
fi
POD="${POD:-default}"
if [ "$INTERACTIVE" -eq 1 ] && [ -z "$TENANT" ]; then
    read -rp "Tenant name [default]: " TENANT
fi
TENANT="${TENANT:-default}"

validate_identity_segment "$POD" "POD" || exit 1
validate_identity_segment "$TENANT" "TENANT" || exit 1

if [ -n "$ENV_FILE_OVERRIDE" ]; then
    ENV_FILE="$ENV_FILE_OVERRIDE"
    case "$ENV_FILE" in
        /*) ;;
        *) ENV_FILE="$(cd "$(dirname "$ENV_FILE")" 2>/dev/null && pwd)/$(basename "$ENV_FILE")" || {
            echo "error: directory for --env-file $ENV_FILE does not exist" >&2
            exit 1
        } ;;
    esac
else
    ENV_FILE="$REPO_ROOT/offices/$POD/$TENANT/.env"
fi

PROJECT_NAME="${PROJECT_NAME_OVERRIDE:-h-mesh-$POD-$TENANT}"

# --attach only ever needs POD/TENANT/ENV_FILE/PROJECT_NAME (resolved above,
# identically to the up path) -- it deliberately never reaches the AGENTS/
# DEFAULT_CLI prompts or `docker compose up` below, since a running office's
# agent roster isn't this flag's business to touch. `exec`, not a plain
# call: replaces this script's own process with tmux's, so signals (Ctrl-C
# to detach, etc.) reach tmux directly instead of an intermediate shell.
if [ "$ATTACH" -eq 1 ]; then
    exec docker compose -p "$PROJECT_NAME" -f "$SCRIPT_DIR/compose.yaml" --env-file "$ENV_FILE" \
        exec h-mesh sh -c 'exec env TMUX_TMPDIR="$HOME/.h-mesh/tmux" tmux attach -t "$1"' -- "$TENANT"
fi

# ⚠ Read existing values from the env file BEFORE prompting, same "blank
# keeps existing" contract setup.sh's own wizard has -- re-running this
# against an already-configured office must not silently reset it. A plain
# `KEY=value` grep, not sourcing the file: the env file may carry secrets
# (tokens), and sourcing an operator-edited file would execute it as shell.
env_file_get() {
    local key="$1"
    [ -f "$ENV_FILE" ] || return 0
    sed -n "s/^${key}=//p" "$ENV_FILE" | tail -n1
}

[ -z "$AGENTS" ] && AGENTS="$(env_file_get AGENTS)"
[ -z "$DEFAULT_CLI" ] && DEFAULT_CLI="$(env_file_get DEFAULT_CLI)"
AGENTS="${AGENTS:-architect}"
DEFAULT_CLI="${DEFAULT_CLI:-claude}"

if [ "$INTERACTIVE" -eq 1 ]; then
    read -rp "Agent names, comma-separated [$AGENTS]: " _in; AGENTS="${_in:-$AGENTS}"
    read -rp "Default CLI (claude/codex/agy) [$DEFAULT_CLI]: " _in; DEFAULT_CLI="${_in:-$DEFAULT_CLI}"
fi

case "$DEFAULT_CLI" in
    claude|codex|agy) ;;
    *) echo "error: --cli must be claude, codex, or agy (got: $DEFAULT_CLI)" >&2; exit 1 ;;
esac

# ⚠ Upsert, not overwrite -- TELEGRAM_*/CLAUDE_OAUTH_TOKEN_*/API_TOKEN/etc.
# an operator already added directly to the env file must survive a re-run
# of this script exactly the way setup.sh's own persisted tenant config
# survives a re-run of the wizard.
upsert_env_line() {
    local key="$1" value="$2" tmp
    mkdir -p "$(dirname "$ENV_FILE")"
    touch "$ENV_FILE"
    tmp="$(mktemp "${ENV_FILE}.XXXXXX")"
    if grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
        sed "s|^${key}=.*|${key}=${value}|" "$ENV_FILE" > "$tmp"
    else
        cp "$ENV_FILE" "$tmp"
        printf '%s=%s\n' "$key" "$value" >> "$tmp"
    fi
    mv "$tmp" "$ENV_FILE"
}

upsert_env_line POD "$POD"
upsert_env_line TENANT "$TENANT"
upsert_env_line AGENTS "$AGENTS"
upsert_env_line DEFAULT_CLI "$DEFAULT_CLI"

echo
echo "  Pod:          $POD"
echo "  Tenant:       $TENANT"
echo "  Agents:       $AGENTS"
echo "  Default CLI:  $DEFAULT_CLI"
echo "  Env file:     $ENV_FILE"
echo "  Project name: $PROJECT_NAME"
echo

COMPOSE_ARGS=(-p "$PROJECT_NAME" -f "$SCRIPT_DIR/compose.yaml" --env-file "$ENV_FILE" up -d)
[ "$SKIP_BUILD" -eq 0 ] && COMPOSE_ARGS+=(--build)

echo "Running: docker compose ${COMPOSE_ARGS[*]}"
( cd "$REPO_ROOT" && H_MESH_TENANT_ENV_FILE="$ENV_FILE" docker compose "${COMPOSE_ARGS[@]}" ) || exit 1

echo
echo "✓ Container started (project: $PROJECT_NAME)."
echo "  Attach:  $SCRIPT_DIR/bootstrap.sh --pod $POD --tenant $TENANT --attach"
echo "           (or directly: docker compose -p $PROJECT_NAME -f $SCRIPT_DIR/compose.yaml --env-file $ENV_FILE exec h-mesh env TMUX_TMPDIR=\"\$HOME/.h-mesh/tmux\" tmux attach -t $TENANT)"
echo "  Logs:    docker compose -p $PROJECT_NAME -f $SCRIPT_DIR/compose.yaml --env-file $ENV_FILE logs -f"
echo "  Status:  docker ps --filter name=$PROJECT_NAME"
echo "  Stop:    docker compose -p $PROJECT_NAME -f $SCRIPT_DIR/compose.yaml --env-file $ENV_FILE down       # keeps state"
echo "           docker compose -p $PROJECT_NAME -f $SCRIPT_DIR/compose.yaml --env-file $ENV_FILE down -v    # also drops it"
