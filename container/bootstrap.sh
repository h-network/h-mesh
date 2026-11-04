#!/usr/bin/env bash
# container/bootstrap.sh — host-vs-container picker's container path.
#
# setup.sh hands off here entirely (see its own "Host-vs-container picker"
# comment) once --container/H_MESH_INSTALL_MODE=container is chosen. This is
# a separate, smaller wizard than setup.sh's own: it collects what a
# tenant's env file needs (POD, TENANT, AGENTS, DEFAULT_CLI, Claude account
# names and OAuth tokens, host port-publishing choice, optional Telegram bot
# config, and a TLS-or-plaintext decision),
# writes/updates that file, and
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
# AGENT_PROVIDERS) and local model provider config (PROVIDER_LOCAL_*) aren't
# prompted for here -- add those directly to the office's env file.
# `API_TOKEN` is never
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
H_MESH_BIND_PORTS="${H_MESH_BIND_PORTS:-}"
API_PORT="${API_PORT:-}"
SESSION_PORT="${SESSION_PORT:-}"
ACCOUNTS="${ACCOUNTS:-}"
DEFAULT_ACCOUNT="${DEFAULT_ACCOUNT:-}"

usage() {
    cat <<EOF
Usage: ./setup.sh --container [options]
       ./container/bootstrap.sh [options]

Collects what an office's env file needs (POD, TENANT, AGENTS,
DEFAULT_CLI, Claude account names and OAuth tokens, host port publishing,
optional Telegram bot config, and a
TLS-or-plaintext decision),
writes it to offices/<pod>/<tenant>/.env (alongside a regenerated
SUMMARY.md and executable attach.sh containing no secrets), then runs
'docker compose -p h-mesh-<pod>-<tenant> up --build' -- setup.sh's own
non-interactive path runs inside the container from there (see
container/entrypoint.sh).

Options:
  --pod <name>            Pod name (default: \$POD, or existing office's value, or "default")
  --tenant <name>         Tenant name (default: \$TENANT, or existing office's value, or "default")
  --agents <a,b,c>        Comma-separated agent names (default: \$AGENTS, or existing value, or "architect")
  --cli <claude|codex|agy> Default CLI (default: \$DEFAULT_CLI, or existing value, or "claude")
  --bind-ports           Publish API/session ports to the host (the default)
  --no-bind-ports        Do not publish API/session ports to the host
  --api-port <port>      API host port (default: \$API_PORT, existing value, or 8080)
  --session-port <port>  Session host port (default: \$SESSION_PORT, existing value, or 8081)
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

Per-agent exceptions (AGENT_CLIS/AGENT_PROFILES/AGENT_PROVIDERS) and local
model provider config (PROVIDER_LOCAL_*) are not prompted for here; add
those directly to the office's env file. See README.md's "Bootstrap script" section for the
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
        --bind-ports) H_MESH_BIND_PORTS=1; shift ;;
        --no-bind-ports) H_MESH_BIND_PORTS=0; shift ;;
        --api-port) API_PORT="$2"; shift 2 ;;
        --session-port) SESSION_PORT="$2"; shift 2 ;;
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
[ -z "$H_MESH_BIND_PORTS" ] && H_MESH_BIND_PORTS="$(env_file_get H_MESH_BIND_PORTS)"
[ -z "$API_PORT" ] && API_PORT="$(env_file_get API_PORT)"
[ -z "$SESSION_PORT" ] && SESSION_PORT="$(env_file_get SESSION_PORT)"
H_MESH_BIND_PORTS="${H_MESH_BIND_PORTS:-1}"
API_PORT="${API_PORT:-8080}"
SESSION_PORT="${SESSION_PORT:-8081}"
[ -z "$ACCOUNTS" ] && ACCOUNTS="$(env_file_get ACCOUNTS)"
[ -z "$DEFAULT_ACCOUNT" ] && DEFAULT_ACCOUNT="$(env_file_get DEFAULT_ACCOUNT)"
AGENTS="${AGENTS:-architect}"
DEFAULT_CLI="${DEFAULT_CLI:-claude}"
ACCOUNTS="${ACCOUNTS:-default}"

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
    (umask 077; touch "$ENV_FILE") || return 1
    chmod 0600 "$ENV_FILE" || return 1
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

# Bootstrap runs before the container has a tenant config. Persist the API
# identity here, so Compose and every later recreate inherit the same one.
# Existing office values win over ambient credentials from another office.
RESOLVED_API_TOKEN="$(env_file_get API_TOKEN)"
if [ -z "$RESOLVED_API_TOKEN" ]; then
    RESOLVED_API_TOKEN="${API_TOKEN:-}"
    if [ -z "$RESOLVED_API_TOKEN" ]; then
        RESOLVED_API_TOKEN="$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')" || exit 1
        [[ "$RESOLVED_API_TOKEN" =~ ^[0-9a-f]{64}$ ]] || {
            echo "error: could not generate API_TOKEN" >&2; exit 1;
        }
    fi
    upsert_env_line API_TOKEN "$RESOLVED_API_TOKEN" || exit 1
fi

check_bool() {
    local val="$1" def="$2"
    val="${val:-$def}"
    case "$val" in
        [Yy]|[Yy][Ee][Ss]) return 0 ;;
        [Nn]|[Nn][Oo])    return 1 ;;
        *) echo "error: expected yes or no, got '$val'" >&2; exit 1 ;;
    esac
}

normalize_bind_ports() {
    case "$1" in
        1|[Yy]|[Yy][Ee][Ss]) echo 1 ;;
        0|[Nn]|[Nn][Oo]) echo 0 ;;
        *) echo "error: H_MESH_BIND_PORTS must be 1 or 0 (got: $1)" >&2; return 1 ;;
    esac
}

validate_port() {
    local value="$1" label="$2"
    if ! [[ "$value" =~ ^[0-9]+$ ]] || [ "$value" -lt 1 ] || [ "$value" -gt 65535 ]; then
        echo "error: $label must be an integer from 1 to 65535 (got: '$value')" >&2
        return 1
    fi
}

warn_port_collision() {
    local port="$1" label="$2" candidate claimed_key claimed_port
    while IFS= read -r candidate; do
        [ "$candidate" = "$ENV_FILE" ] && continue
        for claimed_key in API_PORT SESSION_PORT; do
            claimed_port="$(sed -n "s/^${claimed_key}=//p" "$candidate" | tail -n1)"
            if [ "$claimed_port" = "$port" ]; then
                echo "warning: $label port $port is already claimed as $claimed_key in $candidate" >&2
            fi
        done
    done < <(find "$REPO_ROOT/offices" -mindepth 3 -maxdepth 3 -name .env -type f 2>/dev/null)

    if command -v ss >/dev/null 2>&1 && ss -H -ltn 2>/dev/null | awk '{print $4}' | grep -Eq "(^|:)${port}$"; then
        echo "warning: $label port $port is already listening on this host" >&2
    elif command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
        echo "warning: $label port $port is already listening on this host" >&2
    fi
}

H_MESH_BIND_PORTS="$(normalize_bind_ports "$H_MESH_BIND_PORTS")" || exit 1
if [ "$INTERACTIVE" -eq 1 ]; then
    read -rp "Publish the API/session ports to the host? [Y/n]: " _in
    if check_bool "$_in" "y"; then
        H_MESH_BIND_PORTS=1
        read -rp "API host port [$API_PORT]: " _in; API_PORT="${_in:-$API_PORT}"
        read -rp "Session host port [$SESSION_PORT]: " _in; SESSION_PORT="${_in:-$SESSION_PORT}"
    else
        H_MESH_BIND_PORTS=0
    fi
fi
validate_port "$API_PORT" "API port" || exit 1
validate_port "$SESSION_PORT" "session port" || exit 1
if [ "$H_MESH_BIND_PORTS" -eq 1 ]; then
    warn_port_collision "$API_PORT" "API"
    warn_port_collision "$SESSION_PORT" "session"
fi
upsert_env_line H_MESH_BIND_PORTS "$H_MESH_BIND_PORTS"
upsert_env_line API_PORT "$API_PORT"
upsert_env_line SESSION_PORT "$SESSION_PORT"

# Match setup.sh's account-name/token flow: account names live in ACCOUNTS,
# and each token is stored under CLAUDE_OAUTH_TOKEN_<SLUG>. Read env values
# indirectly rather than sourcing the office file, which may contain
# operator-edited content and secrets.
slug() { echo "$1" | tr '[:upper:]' '[:lower:]' | tr ' ' '-'; }
token_var() { printf 'CLAUDE_OAUTH_TOKEN_%s' "$(printf '%s' "$1" | tr 'a-z-' 'A-Z_')"; }
IFS=',' read -r -a PROFILES <<< "$ACCOUNTS"
TOKEN_VARS=()

resolve_token() {
    local profile="$1" var existing live entered
    var="$(token_var "$profile")"
    existing="$(env_file_get "$var")"
    live="${!var:-}"
    [ -n "$live" ] && existing="$live"
    if [ "$INTERACTIVE" -eq 1 ]; then
        if [ -n "$existing" ]; then
            read -rsp "  OAuth token for '$profile' [keep existing]: " entered; echo
        else
            read -rsp "  OAuth token for '$profile' (blank to log in interactively later): " entered; echo
        fi
        [ -n "$entered" ] || entered="$existing"
    else
        entered="$existing"
    fi
    TOKEN_VARS+=("$var")
    [ -n "$entered" ] && upsert_env_line "$var" "$entered"
}

TELEGRAM_BOT_TOKEN=""
TELEGRAM_CHAT_ID=""
TELEGRAM_VOICE=""
if [ "$INTERACTIVE" -eq 1 ]; then
    read -rp "Use more than one account? [y/N]: " USE_PROFILES
    if check_bool "$USE_PROFILES" "n"; then
        read -rp "  How many accounts? [${#PROFILES[@]}]: " NP
        NP="${NP:-${#PROFILES[@]}}"
        [[ "$NP" =~ ^[1-9][0-9]*$ ]] || { echo "  error: expected a positive number" >&2; exit 2; }
        NEW_PROFILES=()
        for i in $(seq 1 "$NP"); do
            if [ "$i" -eq 1 ]; then pdef="default"; else pdef="${PROFILES[$((i-1))]:-account-$i}"; fi
            read -rp "  Account #$i name [$pdef]: " P
            P="$(slug "${P:-$pdef}")"
            NEW_PROFILES+=("$P")
            resolve_token "$P"
        done
        PROFILES=("${NEW_PROFILES[@]}")
    else
        PROFILES=(default)
        DEFAULT_ACCOUNT=default
        resolve_token default
    fi
else
    for P in "${PROFILES[@]}"; do resolve_token "$P"; done
fi
ACCOUNTS="$(IFS=,; echo "${PROFILES[*]}")"
if [ "$INTERACTIVE" -eq 1 ] && { [ "${#PROFILES[@]}" -gt 1 ] || [ "${PROFILES[0]}" != "default" ]; }; then
    DEFAULT_ACCOUNT="${DEFAULT_ACCOUNT:-${PROFILES[0]}}"
    read -rp "  Default account for every agent [$DEFAULT_ACCOUNT]: " IN_DEFAULT_ACCOUNT
    DEFAULT_ACCOUNT="$(slug "${IN_DEFAULT_ACCOUNT:-$DEFAULT_ACCOUNT}")"
fi
DEFAULT_ACCOUNT="${DEFAULT_ACCOUNT:-${PROFILES[0]}}"
if ! printf '%s\n' "${PROFILES[@]}" | grep -qx "$DEFAULT_ACCOUNT"; then
    echo "error: default account '$DEFAULT_ACCOUNT' is not listed in ACCOUNTS ($ACCOUNTS)" >&2
    exit 1
fi
upsert_env_line ACCOUNTS "$ACCOUNTS"
upsert_env_line DEFAULT_ACCOUNT "$DEFAULT_ACCOUNT"

if [ "$INTERACTIVE" -eq 1 ]; then
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

        else
            echo "  ⚠ Both a Telegram Bot Token and Chat ID are required -- Telegram bot not enabled." >&2
            TELEGRAM_BOT_TOKEN=""
            TELEGRAM_CHAT_ID=""
        fi
    fi
fi

# Compose's env_file is the durable input; persist explicitly supplied
# transport settings too, so later recreates work from a clean shell.
for transport_key in API_TLS_CERT API_TLS_KEY SESSION_TLS_CERT SESSION_TLS_KEY H_MESH_ALLOW_PLAINTEXT; do
    [ -n "${!transport_key:-}" ] && upsert_env_line "$transport_key" "${!transport_key}"
done
TLS_CERT="$(env_file_get API_TLS_CERT)"
TLS_KEY="$(env_file_get API_TLS_KEY)"
PLAINTEXT="$(env_file_get H_MESH_ALLOW_PLAINTEXT)"
if [ "$INTERACTIVE" -eq 1 ] && { [ -z "$TLS_CERT" ] || [ -z "$TLS_KEY" ]; } && [ "$PLAINTEXT" != "1" ]; then
    echo
    echo "  Every office starts the API. Choose how to reach its published port."
    echo "  TLS paths must already be reachable inside the container."
    read -rp "  TLS certificate path (blank to explicitly accept plaintext): " TLS_CERT
    if [ -n "$TLS_CERT" ]; then
        read -rp "  TLS key path: " TLS_KEY
        [ -n "$TLS_KEY" ] || { echo "error: a TLS certificate requires its key" >&2; exit 1; }
        upsert_env_line API_TLS_CERT "$TLS_CERT"
        upsert_env_line API_TLS_KEY "$TLS_KEY"
    else
        echo "  Plaintext sends bearer tokens and messages unencrypted."
        read -rp "  Accept plaintext for this container? [y/N]: " ACCEPT_PLAINTEXT
        if ! check_bool "$ACCEPT_PLAINTEXT" n; then
            echo "error: choose TLS or explicitly accept plaintext to finish the guided install" >&2
            exit 1
        fi
        PLAINTEXT=1
        upsert_env_line H_MESH_ALLOW_PLAINTEXT 1
    fi
fi
API_ENABLEMENT=""
if { [ -z "$TLS_CERT" ] || [ -z "$TLS_KEY" ]; } && [ "$PLAINTEXT" != "1" ]; then
    API_ENABLEMENT="API is loopback-only inside the container; published API port $API_PORT is closed. To open it, rerun bootstrap with API_TLS_CERT and API_TLS_KEY (in-container paths), or H_MESH_ALLOW_PLAINTEXT=1 to explicitly accept plaintext."
fi

echo
echo "  Pod:          $POD"
echo "  Tenant:       $TENANT"
echo "  Agents:       $AGENTS"
echo "  Default CLI:  $DEFAULT_CLI"
if [ "$H_MESH_BIND_PORTS" -eq 1 ]; then
    echo "  Host ports:   API $API_PORT, session $SESSION_PORT"
else
    echo "  Host ports:   not published"
fi
echo "  Accounts:     $ACCOUNTS"
echo "  Default acct: $DEFAULT_ACCOUNT"
CONFIGURED_TOKENS=0
for token_name in "${TOKEN_VARS[@]}"; do [ -n "$(env_file_get "$token_name")" ] && CONFIGURED_TOKENS=$((CONFIGURED_TOKENS + 1)); done
echo "  OAuth tokens: $CONFIGURED_TOKENS/${#PROFILES[@]} configured"
SUMMARY_TG_TOKEN="$(env_file_get TELEGRAM_BOT_TOKEN)"
SUMMARY_TG_CHAT="$(env_file_get TELEGRAM_CHAT_ID)"
SUMMARY_API_TOKEN="$(env_file_get API_TOKEN)"
echo "  Telegram:     $([ -n "$SUMMARY_TG_TOKEN" ] && [ -n "$SUMMARY_TG_CHAT" ] && echo "enabled, chat id $SUMMARY_TG_CHAT" || echo "not enabled")"
if { [ -n "$SUMMARY_TG_TOKEN" ] && [ -n "$SUMMARY_TG_CHAT" ]; } || [ -n "$SUMMARY_API_TOKEN" ]; then
    if [ -n "$TLS_CERT" ] && [ -n "$TLS_KEY" ]; then
        echo "  API/session:  TLS ($(env_file_get API_TLS_CERT))"
    elif [ "$PLAINTEXT" = "1" ]; then
        echo "  API/session:  plaintext (H_MESH_ALLOW_PLAINTEXT=1)"
    else
        echo "  API/session:  loopback-only inside the container"
    fi
fi
echo "  Env file:     $ENV_FILE"
echo "  Project name: $PROJECT_NAME"
echo

COMPOSE_FILES="-f $SCRIPT_DIR/compose.yaml"
[ "$H_MESH_BIND_PORTS" -eq 1 ] && COMPOSE_FILES="$COMPOSE_FILES -f $SCRIPT_DIR/compose.ports.yaml"

OFFICE_DIR="$(dirname "$ENV_FILE")"
SUMMARY_FILE="$OFFICE_DIR/SUMMARY.md"
ATTACH_SCRIPT="$OFFICE_DIR/attach.sh"
mkdir -p "$OFFICE_DIR"

if [ "$H_MESH_BIND_PORTS" -eq 1 ]; then
    SUMMARY_PORTS="API $API_PORT, session $SESSION_PORT"
else
    SUMMARY_PORTS="not published"
fi

if [ -n "$SUMMARY_TG_TOKEN" ] && [ -n "$SUMMARY_TG_CHAT" ]; then
    SUMMARY_TELEGRAM="enabled"
else
    SUMMARY_TELEGRAM="not enabled (disabled)"
fi
if { [ -n "$SUMMARY_TG_TOKEN" ] && [ -n "$SUMMARY_TG_CHAT" ]; } || [ -n "$SUMMARY_API_TOKEN" ]; then
    if [ -n "$TLS_CERT" ] && [ -n "$TLS_KEY" ]; then
        SUMMARY_TLS="TLS ($(env_file_get API_TLS_CERT))"
    elif [ "$PLAINTEXT" = "1" ]; then
        SUMMARY_TLS="plaintext (H_MESH_ALLOW_PLAINTEXT=1)"
    else
        SUMMARY_TLS="loopback-only inside the container"
    fi
else
    SUMMARY_TLS="not enabled"
fi

cat > "$ATTACH_SCRIPT" <<EOF
#!/usr/bin/env bash
# Attach to the h-mesh office container for pod=$POD tenant=$TENANT.
# Equivalent to: $SCRIPT_DIR/bootstrap.sh --pod $POD --tenant $TENANT --attach
set -euo pipefail
exec docker compose -p "$PROJECT_NAME" $COMPOSE_FILES --env-file "$ENV_FILE" \\
    exec h-mesh sh -c 'exec env TMUX_TMPDIR="\$HOME/.h-mesh/tmux" tmux attach -t "\$1"' -- "$TENANT"
EOF
chmod 0755 "$ATTACH_SCRIPT"

cat > "$SUMMARY_FILE" <<EOF
# h-mesh Office Summary ($POD / $TENANT)

- **Pod:** $POD
- **Tenant:** $TENANT
- **Agents:** $AGENTS
- **Default CLI:** $DEFAULT_CLI
- **Accounts:** $ACCOUNTS (default: $DEFAULT_ACCOUNT; OAuth tokens: $CONFIGURED_TOKENS/${#PROFILES[@]} configured)
- **Published ports:** $SUMMARY_PORTS
- **Telegram:** $SUMMARY_TELEGRAM
- **API/session transport:** $SUMMARY_TLS
- **Compose project name:** $PROJECT_NAME

$API_ENABLEMENT

## Commands

### Attach
\`\`\`bash
$ATTACH_SCRIPT
# or via bootstrap.sh:
$SCRIPT_DIR/bootstrap.sh --pod $POD --tenant $TENANT --attach
# or directly:
docker compose -p $PROJECT_NAME $COMPOSE_FILES --env-file $ENV_FILE exec h-mesh env TMUX_TMPDIR="\$HOME/.h-mesh/tmux" tmux attach -t $TENANT
\`\`\`

### Tail logs
\`\`\`bash
docker compose -p $PROJECT_NAME $COMPOSE_FILES --env-file $ENV_FILE logs -f
\`\`\`

### Stop
\`\`\`bash
docker compose -p $PROJECT_NAME $COMPOSE_FILES --env-file $ENV_FILE down       # keeps state
docker compose -p $PROJECT_NAME $COMPOSE_FILES --env-file $ENV_FILE down -v    # also drops it
\`\`\`

### Start
\`\`\`bash
docker compose -p $PROJECT_NAME $COMPOSE_FILES --env-file $ENV_FILE up -d
\`\`\`

### Upgrade
\`\`\`bash
docker compose -p $PROJECT_NAME $COMPOSE_FILES --env-file $ENV_FILE up -d --build
# or in-place inside the running container:
docker compose -p $PROJECT_NAME $COMPOSE_FILES --env-file $ENV_FILE exec h-mesh h-mesh-upgrade --pod $POD --tenant $TENANT
\`\`\`
EOF
chmod 0644 "$SUMMARY_FILE"

COMPOSE_ARGS=(-p "$PROJECT_NAME" -f "$SCRIPT_DIR/compose.yaml" --env-file "$ENV_FILE" up -d)
[ "$H_MESH_BIND_PORTS" -eq 1 ] && COMPOSE_ARGS=(-p "$PROJECT_NAME" -f "$SCRIPT_DIR/compose.yaml" -f "$SCRIPT_DIR/compose.ports.yaml" --env-file "$ENV_FILE" up -d)
[ "$SKIP_BUILD" -eq 0 ] && COMPOSE_ARGS+=(--build)

echo "Running: docker compose ${COMPOSE_ARGS[*]}"
( cd "$REPO_ROOT" && H_MESH_TENANT_ENV_FILE="$ENV_FILE" docker compose "${COMPOSE_ARGS[@]}" ) || exit 1

echo
echo "✓ Container started (project: $PROJECT_NAME)."
echo "  Summary: $SUMMARY_FILE"
echo "  Attach:  $ATTACH_SCRIPT"
echo "           (or: $SCRIPT_DIR/bootstrap.sh --pod $POD --tenant $TENANT --attach)"
echo "           (or directly: docker compose -p $PROJECT_NAME $COMPOSE_FILES --env-file $ENV_FILE exec h-mesh env TMUX_TMPDIR=\"\$HOME/.h-mesh/tmux\" tmux attach -t $TENANT)"
echo "  Logs:    docker compose -p $PROJECT_NAME $COMPOSE_FILES --env-file $ENV_FILE logs -f"
echo "  Status:  docker ps --filter name=$PROJECT_NAME"
echo "  Stop:    docker compose -p $PROJECT_NAME $COMPOSE_FILES --env-file $ENV_FILE down       # keeps state"
echo "           docker compose -p $PROJECT_NAME $COMPOSE_FILES --env-file $ENV_FILE down -v    # also drops it"

# Keep the action for a closed published port last, after Compose output.
[ -z "$API_ENABLEMENT" ] || echo "$API_ENABLEMENT"
exit 0
