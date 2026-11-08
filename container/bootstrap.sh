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
H_MESH_OFFICE_NETWORK="${H_MESH_OFFICE_NETWORK:-${OFFICE_NETWORK:-}}"
OFFICE_NETWORK_EXPLICIT=0
[ -n "$H_MESH_OFFICE_NETWORK" ] && OFFICE_NETWORK_EXPLICIT=1
H_MESH_BIND_PORTS="${H_MESH_BIND_PORTS:-}"
BIND_PORTS_EXPLICIT=0
[ -n "$H_MESH_BIND_PORTS" ] && BIND_PORTS_EXPLICIT=1
API_PORT="${API_PORT:-}"
SESSION_PORT="${SESSION_PORT:-}"
ACCOUNTS="${ACCOUNTS:-}"
DEFAULT_ACCOUNT="${DEFAULT_ACCOUNT:-}"
PROVIDER_LOCAL_URL="${PROVIDER_LOCAL_URL:-}"
PROVIDER_LOCAL_MODEL="${PROVIDER_LOCAL_MODEL:-}"
PROVIDER_LOCAL_TOKEN="${PROVIDER_LOCAL_TOKEN:-}"
AGENT_PROVIDERS="${AGENT_PROVIDERS:-}"
PROVIDER_AGENTS_ARG=""
PROVIDER_EXPLICIT=0
[ -n "$PROVIDER_LOCAL_URL" ] || [ -n "$PROVIDER_LOCAL_MODEL" ] || [ -n "$AGENT_PROVIDERS" ] && PROVIDER_EXPLICIT=1
NO_PROVIDER=0

usage() {
    cat <<EOF
Usage: ./setup.sh --container [options]
       ./container/bootstrap.sh [options]

Collects what an office's env file needs (POD, TENANT, AGENTS,
DEFAULT_CLI, optional shared h-mesh office network, host port publishing,
Claude account names and OAuth tokens, optional local model provider config,
optional Telegram bot config, and a TLS-or-plaintext decision when the API
is reachable via an office network or published host port),
writes it to offices/<pod>/<tenant>/.env (alongside an optional
compose.network.yaml, a regenerated SUMMARY.md, and an executable attach.sh
containing no secrets), then runs
'docker compose -p h-mesh-<pod>-<tenant> up --build' -- setup.sh's own
non-interactive path runs inside the container from there (see
container/entrypoint.sh).

Options:
  --pod <name>            Pod name (default: \$POD, or existing office's value, or "default")
  --tenant <name>         Tenant name (default: \$TENANT, or existing office's value, or "default")
  --agents <a,b,c>        Comma-separated agent names (default: \$AGENTS, or existing value, or "architect")
  --cli <claude|codex|agy> Default CLI (default: \$DEFAULT_CLI, or existing value, or "claude")
  --office-network <name> Create or join a labelled h-mesh office network (io.h-mesh.office-network=1)
                          with container DNS alias <pod>-<tenant> (default: \$H_MESH_OFFICE_NETWORK or existing value)
  --no-office-network     Detach from / do not join an office network
  --bind-ports            Publish API/session ports to the host (default when no office network is selected)
  --no-bind-ports         Do not publish API/session ports to the host (default when an office network is selected)
  --api-port <port>       API host port (default: \$API_PORT, existing value, or 8080)
  --session-port <port>   Session host port (default: \$SESSION_PORT, existing value, or 8081)
  --provider-url <url>    Local OpenAI-compatible provider base URL (sets PROVIDER_LOCAL_URL)
  --provider-model <id>   Local model name (sets PROVIDER_LOCAL_MODEL)
  --provider-agents <a,b> Agents using the local provider, or 'all' (sets AGENT_PROVIDERS; default: all agents)
  --no-provider           Remove local model provider configuration from the office env file
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

Per-agent CLI/account exceptions (AGENT_CLIS/AGENT_PROFILES) are not
prompted for here; add those directly to the office's env file. Optional
local provider token can be supplied interactively or via PROVIDER_LOCAL_TOKEN.
See README.md's "Bootstrap script" section for the
TLS inputs API_TLS_CERT/API_TLS_KEY (and optional SESSION_TLS_CERT/KEY and
H_MESH_CA_CERT) are readable HOST files, validated and staged read-only for
the container. Requires openssl and host UID 1000 or root. See README for the
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
        --office-network) H_MESH_OFFICE_NETWORK="${2:-none}"; OFFICE_NETWORK_EXPLICIT=1; shift 2 ;;
        --no-office-network) H_MESH_OFFICE_NETWORK="none"; OFFICE_NETWORK_EXPLICIT=1; shift ;;
        --bind-ports) H_MESH_BIND_PORTS=1; BIND_PORTS_EXPLICIT=1; shift ;;
        --no-bind-ports) H_MESH_BIND_PORTS=0; BIND_PORTS_EXPLICIT=1; shift ;;
        --api-port) API_PORT="$2"; shift 2 ;;
        --session-port) SESSION_PORT="$2"; shift 2 ;;
        --provider-url) PROVIDER_LOCAL_URL="$2"; PROVIDER_EXPLICIT=1; shift 2 ;;
        --provider-model) PROVIDER_LOCAL_MODEL="$2"; PROVIDER_EXPLICIT=1; shift 2 ;;
        --provider-agents) PROVIDER_AGENTS_ARG="$2"; PROVIDER_EXPLICIT=1; shift 2 ;;
        --no-provider) NO_PROVIDER=1; PROVIDER_EXPLICIT=1; shift ;;
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
    local key="$1" line found=""
    [ -f "$ENV_FILE" ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            "${key}="*) found="${line#*=}" ;;
        esac
    done < "$ENV_FILE"
    [ -z "$found" ] || printf '%s\n' "$found"
}

EXISTING_OFFICE_NETWORK="$(env_file_get H_MESH_OFFICE_NETWORK)"
EXISTING_BIND_PORTS="$(env_file_get H_MESH_BIND_PORTS)"
EXISTING_PROVIDER_URL="$(env_file_get PROVIDER_LOCAL_URL)"
EXISTING_PROVIDER_MODEL="$(env_file_get PROVIDER_LOCAL_MODEL)"
EXISTING_PROVIDER_TOKEN="$(env_file_get PROVIDER_LOCAL_TOKEN)"
EXISTING_AGENT_PROVIDERS="$(env_file_get AGENT_PROVIDERS)"
EXISTING_TG_TOKEN="$(env_file_get TELEGRAM_BOT_TOKEN)"
EXISTING_TG_CHAT="$(env_file_get TELEGRAM_CHAT_ID)"
EXISTING_TG_VOICE="$(env_file_get TELEGRAM_VOICE)"
TLS_CERT="${API_TLS_CERT:-$(env_file_get API_TLS_CERT)}"
TLS_KEY="${API_TLS_KEY:-$(env_file_get API_TLS_KEY)}"
SESSION_CERT="${SESSION_TLS_CERT:-$(env_file_get SESSION_TLS_CERT)}"
SESSION_KEY="${SESSION_TLS_KEY:-$(env_file_get SESSION_TLS_KEY)}"
CA_CERT="${H_MESH_CA_CERT:-$(env_file_get H_MESH_CA_CERT)}"
PLAINTEXT="${H_MESH_ALLOW_PLAINTEXT:-$(env_file_get H_MESH_ALLOW_PLAINTEXT)}"
[ -z "$AGENTS" ] && AGENTS="$(env_file_get AGENTS)"
[ -z "$DEFAULT_CLI" ] && DEFAULT_CLI="$(env_file_get DEFAULT_CLI)"
[ -z "$API_PORT" ] && API_PORT="$(env_file_get API_PORT)"
[ -z "$SESSION_PORT" ] && SESSION_PORT="$(env_file_get SESSION_PORT)"
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
    local key="$1" value="$2" escaped tmp
    mkdir -p "$(dirname "$ENV_FILE")"
    (umask 077; touch "$ENV_FILE") || return 1
    chmod 0600 "$ENV_FILE" || return 1
    tmp="$(mktemp "${ENV_FILE}.XXXXXX")"
    if grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
        escaped="$(printf '%s' "$value" | sed 's/[\\&|]/\\&/g')"
        sed "s|^${key}=.*|${key}=${escaped}|" "$ENV_FILE" > "$tmp"
    else
        cp "$ENV_FILE" "$tmp"
        printf '%s=%s\n' "$key" "$value" >> "$tmp"
    fi
    mv "$tmp" "$ENV_FILE"
}

remove_env_line() {
    local key="$1" tmp
    [ -f "$ENV_FILE" ] || return 0
    grep -q "^${key}=" "$ENV_FILE" 2>/dev/null || return 0
    tmp="$(mktemp "${ENV_FILE}.XXXXXX")" || return 1
    chmod 0600 "$tmp" || return 1
    grep -v "^${key}=" "$ENV_FILE" > "$tmp" || true
    mv "$tmp" "$ENV_FILE"
}

upsert_env_line POD "$POD" || exit 1
upsert_env_line TENANT "$TENANT" || exit 1
upsert_env_line AGENTS "$AGENTS" || exit 1
upsert_env_line DEFAULT_CLI "$DEFAULT_CLI" || exit 1

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

OFFICE_NETWORK_LABEL="io.h-mesh.office-network=1"

validate_dns_label() {
    local value="$1" label="$2"
    if [ "${#value}" -lt 1 ] || [ "${#value}" -gt 63 ] || [[ ! "$value" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]] || [[ "$value" =~ ^[0-9]+$ ]]; then
        echo "error: $label '$value' is not a legal DNS label (must be 1-63 lowercase alphanumeric/hyphen chars, start and end with a letter or digit, and not be all digits)" >&2
        return 1
    fi
    return 0
}

validate_office_network_name() {
    local value="$1"
    if [ "${#value}" -lt 1 ] || [ "${#value}" -gt 63 ] || [[ ! "$value" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]] || [[ "$value" =~ ^[0-9]+$ ]]; then
        echo "error: office network name '$value' must be 1-63 lowercase alphanumeric/hyphen chars, starting and ending with a letter or digit" >&2
        return 1
    fi
    return 0
}

list_labelled_office_networks() {
    docker network ls --filter "label=${OFFICE_NETWORK_LABEL}" --format '{{.Name}}' 2>/dev/null
}

ensure_office_network() {
    local net="$1" mode="${2:-create_or_join}" inspect_out rc
    validate_office_network_name "$net" || return 1
    inspect_out="$(docker network inspect --format '{{index .Labels "io.h-mesh.office-network"}}' "$net" 2>/dev/null)"
    rc=$?
    if [ "$rc" -eq 0 ]; then
        if [ "$inspect_out" != "1" ]; then
            echo "error: docker network '$net' exists without label ${OFFICE_NETWORK_LABEL}; refusing to join a network bootstrap did not create" >&2
            return 1
        fi
        return 0
    fi
    if [ "$mode" = "join_only" ]; then
        echo "error: office network '$net' does not exist with label ${OFFICE_NETWORK_LABEL}" >&2
        return 1
    fi
    case "$net" in
        bridge|host|default|null)
            echo "error: docker network '$net' is a built-in network without label ${OFFICE_NETWORK_LABEL}; refusing to join a network bootstrap did not create" >&2
            return 1 ;;
    esac
    docker network create --label "$OFFICE_NETWORK_LABEL" "$net" >/dev/null || {
        echo "error: failed to create office network '$net'" >&2
        return 1
    }
    return 0
}

OFFICE_NETWORK_MODE="create_or_join"
if [ "$INTERACTIVE" -eq 1 ] && [ "$OFFICE_NETWORK_EXPLICIT" -eq 0 ]; then
    echo "Office network options (1) none, (2) create a new office network, (3) join an existing office network."
    if [ -n "$EXISTING_OFFICE_NETWORK" ]; then
        read -rp "Office network choice (1/2/3, blank keeps '$EXISTING_OFFICE_NETWORK'): " _net_choice
    else
        read -rp "Office network choice (1/2/3) [1]: " _net_choice
    fi
    case "$_net_choice" in
        "")
            H_MESH_OFFICE_NETWORK="$EXISTING_OFFICE_NETWORK"
            OFFICE_NETWORK_MODE="create_or_join"
            ;;
        1|none|NONE)
            H_MESH_OFFICE_NETWORK=""
            ;;
        2|create|CREATE)
            _def_net="${EXISTING_OFFICE_NETWORK:-h-mesh-net}"
            read -rp "  New office network name [$_def_net]: " _net_name
            H_MESH_OFFICE_NETWORK="${_net_name:-$_def_net}"
            OFFICE_NETWORK_MODE="create_or_join"
            ;;
        3|join|JOIN)
            echo "  Existing h-mesh office networks (label ${OFFICE_NETWORK_LABEL})"
            LABELLED_NETS=()
            while IFS= read -r _ln; do
                [ -n "$_ln" ] && LABELLED_NETS+=("$_ln")
            done < <(list_labelled_office_networks)
            if [ "${#LABELLED_NETS[@]}" -eq 0 ]; then
                echo "error: no existing h-mesh office networks found (label ${OFFICE_NETWORK_LABEL}); choose 2 to create one or pass --office-network <name>" >&2
                exit 1
            fi
            for _idx in "${!LABELLED_NETS[@]}"; do
                echo "    $((_idx + 1))) ${LABELLED_NETS[$_idx]}"
            done
            if [ "${#LABELLED_NETS[@]}" -eq 1 ]; then
                read -rp "  Join office network (number or name) [${LABELLED_NETS[0]}]: " _join_in
                _join_in="${_join_in:-${LABELLED_NETS[0]}}"
            else
                read -rp "  Join office network (number or name): " _join_in
                if [ -z "$_join_in" ]; then
                    echo "error: specify which office network to join (number or name)" >&2
                    exit 1
                fi
            fi
            if [[ "$_join_in" =~ ^[0-9]+$ ]] && [ "$_join_in" -ge 1 ] && [ "$_join_in" -le "${#LABELLED_NETS[@]}" ]; then
                H_MESH_OFFICE_NETWORK="${LABELLED_NETS[$((_join_in - 1))]}"
            else
                H_MESH_OFFICE_NETWORK="$_join_in"
            fi
            OFFICE_NETWORK_MODE="join_only"
            ;;
        *)
            echo "error: invalid office network choice '$_net_choice' (expected 1, 2, or 3)" >&2
            exit 1
            ;;
    esac
else
    [ "$OFFICE_NETWORK_EXPLICIT" -eq 1 ] || H_MESH_OFFICE_NETWORK="$EXISTING_OFFICE_NETWORK"
    case "$H_MESH_OFFICE_NETWORK" in
        none|NONE) H_MESH_OFFICE_NETWORK="" ;;
    esac
fi

H_MESH_OFFICE_ALIAS=""
if [ -n "$H_MESH_OFFICE_NETWORK" ]; then
    H_MESH_OFFICE_ALIAS="${POD}-${TENANT}"
    validate_dns_label "$H_MESH_OFFICE_ALIAS" "office network alias" || exit 1
    validate_office_network_name "$H_MESH_OFFICE_NETWORK" || exit 1
fi

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

if [ "$BIND_PORTS_EXPLICIT" -eq 0 ]; then
    if [ -n "$H_MESH_OFFICE_NETWORK" ]; then
        if [ -n "$EXISTING_OFFICE_NETWORK" ] && [ -n "$EXISTING_BIND_PORTS" ]; then
            H_MESH_BIND_PORTS="$EXISTING_BIND_PORTS"
        else
            H_MESH_BIND_PORTS=0
        fi
    else
        H_MESH_BIND_PORTS="${EXISTING_BIND_PORTS:-1}"
    fi
fi
case "$H_MESH_BIND_PORTS" in
    1|[Yy]|[Yy][Ee][Ss]) H_MESH_BIND_PORTS=1 ;;
    0|[Nn]|[Nn][Oo]) H_MESH_BIND_PORTS=0 ;;
    *) echo "error: H_MESH_BIND_PORTS must be 1 or 0 (got: $H_MESH_BIND_PORTS)" >&2; exit 1 ;;
esac
if [ "$INTERACTIVE" -eq 1 ]; then
    if [ "$H_MESH_BIND_PORTS" -eq 0 ]; then
        read -rp "Publish the API/session ports to the host? [y/N]: " _in
        if check_bool "$_in" "n"; then
            H_MESH_BIND_PORTS=1
            read -rp "API host port [$API_PORT]: " _in; API_PORT="${_in:-$API_PORT}"
            read -rp "Session host port [$SESSION_PORT]: " _in; SESSION_PORT="${_in:-$SESSION_PORT}"
        else
            H_MESH_BIND_PORTS=0
        fi
    else
        read -rp "Publish the API/session ports to the host? [Y/n]: " _in
        if check_bool "$_in" "y"; then
            H_MESH_BIND_PORTS=1
            read -rp "API host port [$API_PORT]: " _in; API_PORT="${_in:-$API_PORT}"
            read -rp "Session host port [$SESSION_PORT]: " _in; SESSION_PORT="${_in:-$SESSION_PORT}"
        else
            H_MESH_BIND_PORTS=0
        fi
    fi
fi
validate_port "$API_PORT" "API port" || exit 1
validate_port "$SESSION_PORT" "session port" || exit 1

# Match setup.sh's account-name/token flow: account names live in ACCOUNTS,
# and each token is stored under CLAUDE_OAUTH_TOKEN_<SLUG>. Read env values
# indirectly rather than sourcing the office file, which may contain
# operator-edited content and secrets.
slug() {
    local s="${1,,}"
    printf '%s' "${s// /-}"
}
token_var() {
    local s="${1^^}"
    printf 'CLAUDE_OAUTH_TOKEN_%s' "${s//-/_}"
}
IFS=',' read -r -a PROFILES <<< "$ACCOUNTS"
TOKEN_VARS=()
TOKEN_VALS=()

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
    TOKEN_VALS+=("$entered")
}

TELEGRAM_BOT_TOKEN=""
TELEGRAM_CHAT_ID=""
TELEGRAM_VOICE=""
WANT_TELEGRAM_PERSIST=0
if [ "$INTERACTIVE" -eq 1 ]; then
    read -rp "Use more than one account? [y/N]: " USE_PROFILES
    if check_bool "$USE_PROFILES" "n"; then
        read -rp "  How many accounts? [${#PROFILES[@]}]: " NP
        NP="${NP:-${#PROFILES[@]}}"
        [[ "$NP" =~ ^[1-9][0-9]*$ ]] || { echo "  error: expected a positive number" >&2; exit 2; }
        NEW_PROFILES=()
        for ((i = 1; i <= NP; i++)); do
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

provider_agents_from_map() {
    local map="$1" fallback="$2" pair agent out=()
    if [ -n "$map" ]; then
        for pair in ${map//,/ }; do
            agent="${pair%%=*}"
            [ -n "$agent" ] && out+=("$agent")
        done
    fi
    if [ "${#out[@]}" -gt 0 ]; then
        (IFS=,; echo "${out[*]}")
    else
        echo "$fallback"
    fi
}

build_agent_providers_map() {
    local raw="$1" roster_csv="$2" item agent prov
    local -a roster=() pairs=() seen=()
    IFS=',' read -r -a roster <<< "$roster_csv"
    if [ "$raw" = "all" ] || [ "$raw" = "ALL" ]; then
        raw="$roster_csv"
    fi
    for item in ${raw//,/ }; do
        [ -n "$item" ] || continue
        if [[ "$item" == *=* ]]; then
            agent="$(slug "${item%%=*}")"
            prov="$(slug "${item#*=}")"
        else
            agent="$(slug "$item")"
            prov="local"
        fi
        if [ -z "$agent" ] || [ -z "$prov" ]; then
            echo "error: invalid AGENT_PROVIDERS entry '$item'" >&2
            return 1
        fi
        if ! printf '%s\n' "${roster[@]}" | grep -qx "$agent"; then
            echo "error: local provider agent '$agent' is not listed in AGENTS ($roster_csv)" >&2
            return 1
        fi
        if ! printf '%s\n' "${seen[@]}" | grep -qx "$agent"; then
            seen+=("$agent")
            pairs+=("${agent}=${prov}")
        fi
    done
    if [ "${#pairs[@]}" -eq 0 ]; then
        echo "error: at least one agent must be selected for the local model provider" >&2
        return 1
    fi
    (IFS=,; echo "${pairs[*]}")
}

WANT_PROVIDER_PERSIST=0
if [ "$NO_PROVIDER" -eq 1 ]; then
    PROVIDER_LOCAL_URL=""
    PROVIDER_LOCAL_MODEL=""
    PROVIDER_LOCAL_TOKEN=""
    AGENT_PROVIDERS=""
elif [ "$INTERACTIVE" -eq 1 ] && [ "$PROVIDER_EXPLICIT" -eq 0 ]; then
    _existing_url="${PROVIDER_LOCAL_URL:-$EXISTING_PROVIDER_URL}"
    _existing_model="${PROVIDER_LOCAL_MODEL:-$EXISTING_PROVIDER_MODEL}"
    if [ -n "$_existing_url" ] && [ -n "$_existing_model" ]; then
        read -rp "A local model provider is already configured ($_existing_url, $_existing_model). Keep it? [Y/n]: " KEEP_PROVIDER
        if check_bool "$KEEP_PROVIDER" "y"; then
            PROVIDER_LOCAL_URL="${_existing_url%/}"
            PROVIDER_LOCAL_URL="${PROVIDER_LOCAL_URL%/v1}"
            PROVIDER_LOCAL_MODEL="$_existing_model"
            PROVIDER_LOCAL_TOKEN="${PROVIDER_LOCAL_TOKEN:-$EXISTING_PROVIDER_TOKEN}"
            if [ -n "${AGENT_PROVIDERS:-$EXISTING_AGENT_PROVIDERS}" ]; then
                AGENT_PROVIDERS="${AGENT_PROVIDERS:-$EXISTING_AGENT_PROVIDERS}"
            else
                AGENT_PROVIDERS="$(build_agent_providers_map "$AGENTS" "$AGENTS")" || exit 1
            fi
            WANT_PROVIDER_PERSIST=1
        else
            NO_PROVIDER=1
            PROVIDER_LOCAL_URL=""
            PROVIDER_LOCAL_MODEL=""
            PROVIDER_LOCAL_TOKEN=""
            AGENT_PROVIDERS=""
        fi
    else
        read -rp "Point any agent at a local model provider? [y/N]: " USE_PROVIDER
        if check_bool "$USE_PROVIDER" "n"; then
            _def_url="${PROVIDER_LOCAL_URL:-$EXISTING_PROVIDER_URL}"
            read -rp "  Local provider base URL, e.g. http://10.0.0.5:8000 (no trailing /v1)${_def_url:+ [$_def_url]}: " _in_url
            PROVIDER_LOCAL_URL="${_in_url:-$_def_url}"
            PROVIDER_LOCAL_URL="${PROVIDER_LOCAL_URL%/}"
            PROVIDER_LOCAL_URL="${PROVIDER_LOCAL_URL%/v1}"
            _def_model="${PROVIDER_LOCAL_MODEL:-$EXISTING_PROVIDER_MODEL}"
            read -rp "  Local model name${_def_model:+ [$_def_model]}: " _in_model
            PROVIDER_LOCAL_MODEL="${_in_model:-$_def_model}"
            _def_prov_agents="$(provider_agents_from_map "${AGENT_PROVIDERS:-$EXISTING_AGENT_PROVIDERS}" "$AGENTS")"
            read -rp "  Which agents use it? (comma- or space-separated, or 'all') [$_def_prov_agents]: " _in_agents
            _in_agents="${_in_agents:-$_def_prov_agents}"
            if [ -n "${PROVIDER_LOCAL_TOKEN:-$EXISTING_PROVIDER_TOKEN}" ]; then
                read -rsp "  Optional API token for local provider [keep existing]: " _in_tok; echo
                PROVIDER_LOCAL_TOKEN="${_in_tok:-${PROVIDER_LOCAL_TOKEN:-$EXISTING_PROVIDER_TOKEN}}"
            else
                read -rsp "  Optional API token for local provider (blank for none): " _in_tok; echo
                PROVIDER_LOCAL_TOKEN="$_in_tok"
            fi
            if [ -n "$PROVIDER_LOCAL_URL" ] && [ -n "$PROVIDER_LOCAL_MODEL" ]; then
                AGENT_PROVIDERS="$(build_agent_providers_map "$_in_agents" "$AGENTS")" || exit 1
                WANT_PROVIDER_PERSIST=1
            else
                echo "  ⚠ Both a local provider URL and model name are required -- local model provider not enabled." >&2
                PROVIDER_LOCAL_URL=""
                PROVIDER_LOCAL_MODEL=""
                PROVIDER_LOCAL_TOKEN=""
                AGENT_PROVIDERS=""
            fi
        else
            NO_PROVIDER=1
            PROVIDER_LOCAL_URL=""
            PROVIDER_LOCAL_MODEL=""
            PROVIDER_LOCAL_TOKEN=""
            AGENT_PROVIDERS=""
        fi
    fi
else
    PROVIDER_LOCAL_URL="${PROVIDER_LOCAL_URL:-$EXISTING_PROVIDER_URL}"
    PROVIDER_LOCAL_URL="${PROVIDER_LOCAL_URL%/}"
    PROVIDER_LOCAL_URL="${PROVIDER_LOCAL_URL%/v1}"
    PROVIDER_LOCAL_MODEL="${PROVIDER_LOCAL_MODEL:-$EXISTING_PROVIDER_MODEL}"
    PROVIDER_LOCAL_TOKEN="${PROVIDER_LOCAL_TOKEN:-$EXISTING_PROVIDER_TOKEN}"
    if [ -n "$PROVIDER_AGENTS_ARG" ]; then
        AGENT_PROVIDERS="$(build_agent_providers_map "$PROVIDER_AGENTS_ARG" "$AGENTS")" || exit 1
    elif [ -n "$AGENT_PROVIDERS" ]; then
        AGENT_PROVIDERS="$(build_agent_providers_map "$AGENT_PROVIDERS" "$AGENTS")" || exit 1
    elif [ -n "$EXISTING_AGENT_PROVIDERS" ]; then
        AGENT_PROVIDERS="$EXISTING_AGENT_PROVIDERS"
    elif [ -n "$PROVIDER_LOCAL_URL" ] && [ -n "$PROVIDER_LOCAL_MODEL" ]; then
        AGENT_PROVIDERS="$(build_agent_providers_map "$AGENTS" "$AGENTS")" || exit 1
    fi
    if [ "$PROVIDER_EXPLICIT" -eq 1 ]; then
        if [ -z "$PROVIDER_LOCAL_URL" ] || [ -z "$PROVIDER_LOCAL_MODEL" ]; then
            echo "error: local model provider requires both --provider-url (PROVIDER_LOCAL_URL) and --provider-model (PROVIDER_LOCAL_MODEL)" >&2
            exit 1
        fi
        WANT_PROVIDER_PERSIST=1
    elif [ -n "$PROVIDER_LOCAL_URL" ] && [ -n "$PROVIDER_LOCAL_MODEL" ] && [ -n "$AGENT_PROVIDERS" ]; then
        WANT_PROVIDER_PERSIST=1
    fi
fi

if [ "$INTERACTIVE" -eq 1 ]; then
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
            if [ "$EXISTING_TG_VOICE" = "1" ]; then
                read -rp "  Enable spoken voice replies? [Y/n]: " WANT_VOICE
                check_bool "$WANT_VOICE" "y" && TELEGRAM_VOICE=1 || TELEGRAM_VOICE=0
            else
                read -rp "  Enable spoken voice replies? [y/N]: " WANT_VOICE
                check_bool "$WANT_VOICE" "n" && TELEGRAM_VOICE=1 || TELEGRAM_VOICE=0
            fi
            WANT_TELEGRAM_PERSIST=1
        else
            echo "  ⚠ Both a Telegram Bot Token and Chat ID are required -- Telegram bot not enabled." >&2
            TELEGRAM_BOT_TOKEN=""
            TELEGRAM_CHAT_ID=""
        fi
    fi
fi

# Resolve host files without persisting unvalidated paths. A rerun maps the
# managed container paths back to its own staged copies, preserving material.
# When neither an office network nor a published host port is active, nothing
# outside the container can reach the API, so skip the TLS/plaintext question
# and keep loopback-only inside the container.
if [ "$INTERACTIVE" -eq 1 ] && { [ "$H_MESH_BIND_PORTS" -eq 1 ] || [ -n "$H_MESH_OFFICE_NETWORK" ]; } && { [ -z "$TLS_CERT" ] || [ -z "$TLS_KEY" ]; } && [ "$PLAINTEXT" != "1" ]; then
    echo
    if [ "$H_MESH_BIND_PORTS" -eq 1 ]; then
        echo "Every office starts the API. Choose how to reach its published port."
    else
        echo "Every office starts the API. Peers on office network '$H_MESH_OFFICE_NETWORK' reach its API on port 8080 (http://$H_MESH_OFFICE_ALIAS:8080 or https://$H_MESH_OFFICE_ALIAS:8080)."
    fi
    echo "Supply readable HOST files; bootstrap validates and mounts private copies read-only."
    read -rp "TLS certificate HOST path (blank for plaintext; you will be asked to confirm): " TLS_CERT
    if [ -n "$TLS_CERT" ]; then
        read -rp "  TLS key HOST path: " TLS_KEY
        [ -n "$TLS_KEY" ] || { echo "error: a TLS certificate requires its key" >&2; exit 1; }
        read -rp "  Optional peer CA bundle HOST path (blank for system trust): " CA_CERT
    else
        echo "Plaintext sends bearer tokens and messages unencrypted."
        read -rp "Accept plaintext for this container? [y/N]: " ACCEPT_PLAINTEXT
        if ! check_bool "$ACCEPT_PLAINTEXT" n; then
            echo "error: choose TLS or explicitly accept plaintext to finish the guided install" >&2
            exit 1
        fi
        PLAINTEXT=1
    fi
fi

if [ -n "$H_MESH_OFFICE_NETWORK" ]; then
    ensure_office_network "$H_MESH_OFFICE_NETWORK" "$OFFICE_NETWORK_MODE" || exit 1
    upsert_env_line H_MESH_OFFICE_NETWORK "$H_MESH_OFFICE_NETWORK" || exit 1
    upsert_env_line H_MESH_OFFICE_ALIAS "$H_MESH_OFFICE_ALIAS" || exit 1
else
    remove_env_line H_MESH_OFFICE_NETWORK
    remove_env_line H_MESH_OFFICE_ALIAS
fi
if [ "$H_MESH_BIND_PORTS" -eq 1 ]; then
    warn_port_collision "$API_PORT" "API"
    warn_port_collision "$SESSION_PORT" "session"
fi
upsert_env_line H_MESH_BIND_PORTS "$H_MESH_BIND_PORTS" || exit 1
upsert_env_line API_PORT "$API_PORT" || exit 1
upsert_env_line SESSION_PORT "$SESSION_PORT" || exit 1
for _t_idx in "${!TOKEN_VARS[@]}"; do
    [ -z "${TOKEN_VALS[$_t_idx]}" ] || upsert_env_line "${TOKEN_VARS[$_t_idx]}" "${TOKEN_VALS[$_t_idx]}" || exit 1
done
upsert_env_line ACCOUNTS "$ACCOUNTS" || exit 1
upsert_env_line DEFAULT_ACCOUNT "$DEFAULT_ACCOUNT" || exit 1
if [ "$NO_PROVIDER" -eq 1 ]; then
    remove_env_line PROVIDER_LOCAL_URL
    remove_env_line PROVIDER_LOCAL_MODEL
    remove_env_line PROVIDER_LOCAL_TOKEN
    remove_env_line AGENT_PROVIDERS
elif [ "$WANT_PROVIDER_PERSIST" -eq 1 ]; then
    upsert_env_line PROVIDER_LOCAL_URL "$PROVIDER_LOCAL_URL" || exit 1
    upsert_env_line PROVIDER_LOCAL_MODEL "$PROVIDER_LOCAL_MODEL" || exit 1
    upsert_env_line AGENT_PROVIDERS "$AGENT_PROVIDERS" || exit 1
    [ -z "$PROVIDER_LOCAL_TOKEN" ] || upsert_env_line PROVIDER_LOCAL_TOKEN "$PROVIDER_LOCAL_TOKEN" || exit 1
fi
if [ "$WANT_TELEGRAM_PERSIST" -eq 1 ]; then
    upsert_env_line TELEGRAM_BOT_TOKEN "$TELEGRAM_BOT_TOKEN" || exit 1
    upsert_env_line TELEGRAM_CHAT_ID "$TELEGRAM_CHAT_ID" || exit 1
    upsert_env_line TELEGRAM_VOICE "$TELEGRAM_VOICE" || exit 1
fi
source "$SCRIPT_DIR/tls.sh"
stage_tls || exit 1
for transport_key in API_TLS_CERT API_TLS_KEY SESSION_TLS_CERT SESSION_TLS_KEY H_MESH_CA_CERT H_MESH_ALLOW_PLAINTEXT; do
    case "$transport_key" in
        API_TLS_CERT) transport_value="$TLS_CERT" ;; API_TLS_KEY) transport_value="$TLS_KEY" ;;
        SESSION_TLS_CERT) transport_value="$SESSION_CERT" ;; SESSION_TLS_KEY) transport_value="$SESSION_KEY" ;;
        H_MESH_CA_CERT) transport_value="$CA_CERT" ;; H_MESH_ALLOW_PLAINTEXT) transport_value="$PLAINTEXT" ;;
    esac
    [ -z "$transport_value" ] || upsert_env_line "$transport_key" "$transport_value" || exit 1
done
API_ENABLEMENT=""
if { [ -z "$TLS_CERT" ] || [ -z "$TLS_KEY" ]; } && [ "$PLAINTEXT" != "1" ]; then
    if [ "$H_MESH_BIND_PORTS" -eq 1 ]; then
        API_ENABLEMENT="API is loopback-only inside the container; published API port $API_PORT is closed. To open it, rerun bootstrap with API_TLS_CERT and API_TLS_KEY (readable HOST paths), or H_MESH_ALLOW_PLAINTEXT=1 to explicitly accept plaintext."
    elif [ -n "$H_MESH_OFFICE_NETWORK" ]; then
        API_ENABLEMENT="API is loopback-only inside the container; peer URL http://$H_MESH_OFFICE_ALIAS:8080 on office network '$H_MESH_OFFICE_NETWORK' is closed. To open it, rerun bootstrap with API_TLS_CERT and API_TLS_KEY (readable HOST paths), or H_MESH_ALLOW_PLAINTEXT=1 to explicitly accept plaintext."
    else
        API_ENABLEMENT="API is loopback-only inside the container (127.0.0.1:8080); no office network is joined and no host ports are published, so nothing outside the container can reach the API."
    fi
fi

echo
echo "  Pod:          $POD"
echo "  Tenant:       $TENANT"
echo "  Agents:       $AGENTS"
echo "  Default CLI:  $DEFAULT_CLI"
if [ -n "$H_MESH_OFFICE_NETWORK" ]; then
    echo "  Office net:   $H_MESH_OFFICE_NETWORK (alias: $H_MESH_OFFICE_ALIAS)"
else
    echo "  Office net:   none"
fi
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
SUMMARY_PROV_URL="$(env_file_get PROVIDER_LOCAL_URL)"
SUMMARY_PROV_MODEL="$(env_file_get PROVIDER_LOCAL_MODEL)"
SUMMARY_PROV_TOKEN="$(env_file_get PROVIDER_LOCAL_TOKEN)"
SUMMARY_AGENT_PROVIDERS="$(env_file_get AGENT_PROVIDERS)"
if [ -n "$SUMMARY_PROV_URL" ] && [ -n "$SUMMARY_PROV_MODEL" ]; then
    SUMMARY_PROV_TOK_STATE="$([ -n "$SUMMARY_PROV_TOKEN" ] && echo "configured" || echo "not set")"
    SUMMARY_LOCAL_PROVIDER="$SUMMARY_PROV_MODEL @ $SUMMARY_PROV_URL (agents: ${SUMMARY_AGENT_PROVIDERS:-none}; token: $SUMMARY_PROV_TOK_STATE)"
else
    SUMMARY_LOCAL_PROVIDER="not configured"
fi
echo "  Local model:  $SUMMARY_LOCAL_PROVIDER"
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

OFFICE_DIR="$(dirname "$ENV_FILE")"
SUMMARY_FILE="$OFFICE_DIR/SUMMARY.md"
ATTACH_SCRIPT="$OFFICE_DIR/attach.sh"
NETWORK_COMPOSE_FILE="$OFFICE_DIR/compose.network.yaml"
mkdir -p "$OFFICE_DIR"

if [ -n "$H_MESH_OFFICE_NETWORK" ]; then
    cat > "$NETWORK_COMPOSE_FILE" <<EOF
# Generated by container/bootstrap.sh for pod=$POD tenant=$TENANT.
# Attaches the office container to the shared h-mesh office network
# ($H_MESH_OFFICE_NETWORK) with the stable DNS alias $H_MESH_OFFICE_ALIAS.
services:
  h-mesh:
    networks:
      office:
        aliases:
          - $H_MESH_OFFICE_ALIAS

networks:
  office:
    external: true
    name: $H_MESH_OFFICE_NETWORK
EOF
    chmod 0644 "$NETWORK_COMPOSE_FILE"
else
    rm -f "$NETWORK_COMPOSE_FILE"
fi

COMPOSE_FILES="-f $SCRIPT_DIR/compose.yaml"
[ "$H_MESH_BIND_PORTS" -eq 1 ] && COMPOSE_FILES="$COMPOSE_FILES -f $SCRIPT_DIR/compose.ports.yaml"
[ "$TLS_MOUNT" -eq 0 ] || COMPOSE_FILES="$COMPOSE_FILES -f $SCRIPT_DIR/compose.tls.yaml"
[ -z "$H_MESH_OFFICE_NETWORK" ] || COMPOSE_FILES="$COMPOSE_FILES -f $NETWORK_COMPOSE_FILE"

if [ -n "$H_MESH_OFFICE_NETWORK" ]; then
    if [ -n "$TLS_CERT" ] && [ -n "$TLS_KEY" ]; then
        SUMMARY_NET_SCHEME="https"
    else
        SUMMARY_NET_SCHEME="http"
    fi
    SUMMARY_OFFICE_NETWORK="$H_MESH_OFFICE_NETWORK (alias: $H_MESH_OFFICE_ALIAS; peer URL: ${SUMMARY_NET_SCHEME}://${H_MESH_OFFICE_ALIAS}:8080)"
else
    SUMMARY_OFFICE_NETWORK="none"
fi

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
    elif [ "$H_MESH_BIND_PORTS" -eq 0 ] && [ -z "$H_MESH_OFFICE_NETWORK" ]; then
        SUMMARY_TLS="loopback-only inside the container (no office network or published host port)"
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
- **Local model provider:** $SUMMARY_LOCAL_PROVIDER
- **Office network:** $SUMMARY_OFFICE_NETWORK
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

COMPOSE_ARGS=(-p "$PROJECT_NAME" -f "$SCRIPT_DIR/compose.yaml")
[ "$H_MESH_BIND_PORTS" -eq 0 ] || COMPOSE_ARGS+=(-f "$SCRIPT_DIR/compose.ports.yaml")
[ "$TLS_MOUNT" -eq 0 ] || COMPOSE_ARGS+=(-f "$SCRIPT_DIR/compose.tls.yaml")
[ -z "$H_MESH_OFFICE_NETWORK" ] || COMPOSE_ARGS+=(-f "$NETWORK_COMPOSE_FILE")
COMPOSE_ARGS+=(--env-file "$ENV_FILE" up -d)
[ "$SKIP_BUILD" -eq 0 ] && COMPOSE_ARGS+=(--build)

echo "Running: docker compose ${COMPOSE_ARGS[*]}"
( cd "$REPO_ROOT" && H_MESH_TENANT_ENV_FILE="$ENV_FILE" H_MESH_TLS_DIR="$TLS_DIR" docker compose "${COMPOSE_ARGS[@]}" ) || exit 1

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
