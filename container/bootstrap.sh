#!/usr/bin/env bash
# container/bootstrap.sh — host-vs-container picker's container path.
#
# setup.sh hands off here entirely (see its own "Host-vs-container picker"
# comment) once --container/H_MESH_INSTALL_MODE=container is chosen. This is
# a separate, smaller wizard than setup.sh's own: it collects what a
# tenant's env file needs (POD, TENANT, AGENTS, DEFAULT_CLI, the default
# account's OAuth token, optional Telegram bot config, and -- if Telegram
# is enabled -- a TLS-or-plaintext decision), writes/updates that file, and
# hands off to `docker compose up --build` -- setup.sh's own non-interactive
# path, roster seeding, hire, and daemon-start logic still run, just inside
# the container (see container/entrypoint.sh), not reimplemented here.
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
# only, same as setup.sh. Per-agent exceptions (AGENT_CLIS/AGENT_PROFILES/
# AGENT_PROVIDERS), a second-or-later account, and local model provider
# config (PROVIDER_LOCAL_*) aren't prompted for here -- this script has no
# per-agent or multi-account UI at all, uniform-single-account is its whole
# model; add those directly to the office's env file. `API_TOKEN` is never
# prompted anywhere, even on a bare host -- always generated. Every one of
# these reaches the container exactly as documented in README.md's
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
DEFAULT_CLI, the default account's OAuth token, optional Telegram bot
config, and -- if Telegram is enabled -- a TLS-or-plaintext decision),
writes it to offices/<pod>/<tenant>/.env, then runs
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

Per-agent exceptions (AGENT_CLIS/AGENT_PROFILES/AGENT_PROVIDERS), a second
or later account, and local model provider config (PROVIDER_LOCAL_*) are
not prompted for here -- this script has no per-agent or multi-account UI
at all, uniform-single-account is its whole model; add those directly to
the office's env file. See README.md's "Bootstrap script" section for the
complete non-interactive variable set, all of which apply unchanged inside
the container.
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

if [ "$INTERACTIVE" -eq 1 ]; then
    # Same literal ASCII art as setup.sh's own host wizard (figlet -f
    # ansishadow.flf "H-MESH") -- a human at a real terminal picking
    # --container gets the same identity setup.sh's own wizard gives one
    # picking --host, not a visibly stripped-down variant.
    YELLOW="\033[0;33m"
    CYAN="\033[0;36m"
    GREY="\033[0;37m"
    NC="\033[0m"
    echo -e "${YELLOW}"
    cat <<'BANNER'
██╗  ██╗      ███╗   ███╗███████╗███████╗██╗  ██╗
██║  ██║      ████╗ ████║██╔════╝██╔════╝██║  ██║
███████║█████╗██╔████╔██║█████╗  ███████╗███████║
██╔══██║╚════╝██║╚██╔╝██║██╔══╝  ╚════██║██╔══██║
██║  ██║      ██║ ╚═╝ ██║███████╗███████║██║  ██║
╚═╝  ╚═╝      ╚═╝     ╚═╝╚══════╝╚══════╝╚═╝  ╚═╝
BANNER
    echo -e "${NC}"
    echo -e "  ${CYAN}H-MESH${NC} ${GREY}//${NC} ${CYAN}agentic office framework${NC} ${GREY}//${NC} ${CYAN}h-network${NC} ${GREY}//${NC} ${CYAN}container${NC}"
    echo ""
fi

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

check_bool() {
    local val="$1" def="$2"
    val="${val:-$def}"
    case "$val" in
        [Yy]|[Yy][Ee][Ss]) return 0 ;;
        [Nn]|[Nn][Oo])    return 1 ;;
        *) echo "error: expected yes or no, got '$val'" >&2; exit 1 ;;
    esac
}

# ⚠ These two are NOT the same kind of "advanced" as AGENT_CLIS/
# AGENT_PROFILES/AGENT_PROVIDERS/PROVIDER_LOCAL_* (still file-only, still
# correctly deferred -- this script has no per-agent CLI/profile UI at
# all, uniform-office is its whole model, so those never apply to it) or
# API_TOKEN (never a prompt even in the host wizard -- always generated).
# CLAUDE_OAUTH_TOKEN_DEFAULT is required for a single-account office to do
# anything; Telegram is genuinely optional but the *choice* to enable it
# is something setup.sh's own host wizard offers and this one didn't.
# Mirrors that wizard's ask_token()/Telegram-enable block for the single
# ("default") account this script's own model supports -- see ticket
# b87f9f0a.
CLAUDE_OAUTH_TOKEN_DEFAULT=""
TELEGRAM_BOT_TOKEN=""
TELEGRAM_CHAT_ID=""
TELEGRAM_VOICE=""
if [ "$INTERACTIVE" -eq 1 ]; then
    EXISTING_TOKEN="$(env_file_get CLAUDE_OAUTH_TOKEN_DEFAULT)"
    if [ -n "$EXISTING_TOKEN" ]; then
        read -rsp "OAuth token for the default account [keep existing]: " IN_TOKEN; echo
    else
        read -rsp "OAuth token for the default account (blank to log in interactively later): " IN_TOKEN; echo
    fi
    CLAUDE_OAUTH_TOKEN_DEFAULT="${IN_TOKEN:-$EXISTING_TOKEN}"
    [ -n "$CLAUDE_OAUTH_TOKEN_DEFAULT" ] && upsert_env_line CLAUDE_OAUTH_TOKEN_DEFAULT "$CLAUDE_OAUTH_TOKEN_DEFAULT"

    EXISTING_TG_TOKEN="$(env_file_get TELEGRAM_BOT_TOKEN)"
    EXISTING_TG_CHAT="$(env_file_get TELEGRAM_CHAT_ID)"
    read -rp "Run the Telegram bot? [y/N]: " WANT_TELEGRAM
    if check_bool "$WANT_TELEGRAM" "n"; then
        if [ -n "$EXISTING_TG_TOKEN" ]; then
            read -rsp "  Telegram Bot Token [keep existing]: " IN_TG_TOKEN; echo
        else
            read -rsp "  Telegram Bot Token (required, blank to skip): " IN_TG_TOKEN; echo
        fi
        TELEGRAM_BOT_TOKEN="${IN_TG_TOKEN:-$EXISTING_TG_TOKEN}"
        read -rp "  Telegram Chat ID (required)${EXISTING_TG_CHAT:+ [$EXISTING_TG_CHAT]}: " IN_TG_CHAT
        TELEGRAM_CHAT_ID="${IN_TG_CHAT:-$EXISTING_TG_CHAT}"
        if [ -n "$TELEGRAM_BOT_TOKEN" ] && [ -n "$TELEGRAM_CHAT_ID" ]; then
            EXISTING_TG_VOICE="$(env_file_get TELEGRAM_VOICE)"
            if [ "$EXISTING_TG_VOICE" = "1" ]; then
                read -rp "  Enable spoken voice replies? [Y/n]: " WANT_VOICE
                check_bool "$WANT_VOICE" "y" && TELEGRAM_VOICE=1 || TELEGRAM_VOICE=0
            else
                read -rp "  Enable spoken voice replies? [y/N]: " WANT_VOICE
                check_bool "$WANT_VOICE" "n" && TELEGRAM_VOICE=1 || TELEGRAM_VOICE=0
            fi
            upsert_env_line TELEGRAM_BOT_TOKEN "$TELEGRAM_BOT_TOKEN"
            upsert_env_line TELEGRAM_CHAT_ID "$TELEGRAM_CHAT_ID"
            upsert_env_line TELEGRAM_VOICE "$TELEGRAM_VOICE"

            # ⚠ Telegram unconditionally turns on the api/session doors,
            # both bound 0.0.0.0 *inside* this container (Dockerfile's
            # API_BIND/SESSION_BIND) -- container/entrypoint.sh refuses to
            # start at all without either real TLS certs or an explicit
            # H_MESH_ALLOW_PLAINTEXT=1. The predecessor project's own
            # wizard asks exactly this (real cert path / self-signed
            # generation / explicit plaintext) whenever a door is being
            # exposed; this container exposes these two the moment
            # Telegram is on, so the same decision is always required here
            # too -- forced now, not left to surface as a crash-loop after
            # the fact (a real outage, not a hypothetical -- see ticket
            # b87f9f0a's own follow-up).
            #
            # Self-signed generation isn't offered here the way the
            # predecessor project's is: that needs a compose.yaml bind
            # mount to actually deliver generated cert files into the
            # container, which doesn't exist yet -- real, separate scope,
            # not something to improvise under this fix. A real cert path
            # is still accepted for an operator who has already arranged
            # to get one into the container themselves (e.g. a manual
            # compose.yaml volume
            # edit); this only records the in-container paths.
            EXISTING_TLS_CERT="$(env_file_get API_TLS_CERT)"
            EXISTING_PLAINTEXT="$(env_file_get H_MESH_ALLOW_PLAINTEXT)"
            if [ -z "$EXISTING_TLS_CERT" ] && [ "$EXISTING_PLAINTEXT" != "1" ]; then
                echo
                echo "  Telegram turns on the API and session doors, both bound 0.0.0.0"
                echo "  inside this container -- it will not start without deciding how"
                echo "  they're reached."
                read -rp "  Path to a TLS certificate already reachable inside the container (blank to accept plaintext instead): " IN_TLS_CERT
                if [ -n "$IN_TLS_CERT" ]; then
                    read -rp "  Path to its key: " IN_TLS_KEY
                    if [ -z "$IN_TLS_KEY" ]; then
                        echo "  error: a TLS certificate requires its key" >&2
                        exit 1
                    fi
                    upsert_env_line API_TLS_CERT "$IN_TLS_CERT"
                    upsert_env_line API_TLS_KEY "$IN_TLS_KEY"
                else
                    echo "  ⚠ Plain HTTP inside the container: the api token and everything"
                    echo "    typed crosses the network unencrypted between the host and this"
                    echo "    container. Recorded as H_MESH_ALLOW_PLAINTEXT=1 in $ENV_FILE."
                    upsert_env_line H_MESH_ALLOW_PLAINTEXT 1
                fi
            fi
        else
            echo "  ⚠ Both a Telegram Bot Token and Chat ID are required -- Telegram bot not enabled." >&2
            TELEGRAM_BOT_TOKEN=""
            TELEGRAM_CHAT_ID=""
        fi
    fi
fi

echo
echo "  Pod:          $POD"
echo "  Tenant:       $TENANT"
echo "  Agents:       $AGENTS"
echo "  Default CLI:  $DEFAULT_CLI"
echo "  OAuth token:  $([ -n "$CLAUDE_OAUTH_TOKEN_DEFAULT" ] && echo "configured" || echo "not set -- log in interactively later, or add CLAUDE_OAUTH_TOKEN_DEFAULT to $ENV_FILE")"
echo "  Telegram:     $([ -n "$TELEGRAM_BOT_TOKEN" ] && [ -n "$TELEGRAM_CHAT_ID" ] && echo "enabled, chat id $TELEGRAM_CHAT_ID" || echo "not enabled")"
if [ -n "$TELEGRAM_BOT_TOKEN" ] && [ -n "$TELEGRAM_CHAT_ID" ]; then
    if [ -n "$(env_file_get API_TLS_CERT)" ]; then
        echo "  API/session:  TLS ($(env_file_get API_TLS_CERT))"
    else
        echo "  API/session:  plaintext (H_MESH_ALLOW_PLAINTEXT=1)"
    fi
fi
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
