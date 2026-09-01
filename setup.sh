#!/usr/bin/env bash
# setup.sh — Host bootstrap script for h-mesh.
# Verifies/installs system dependencies, installs h-mesh in an isolated venv,
# persists venv PATH and a default tmux.conf, walks an interactive wizard
# (agent roster, CLI/account choices, local model provider) when run at a
# terminal, seeds the fixed lifecycle participants (host->office, api->api)
# in the Redis registry, starts the required daemons (h-mesh-switch and
# h-mesh-tmux-reconciler), and hires any agent from the wizard's roster that
# isn't already running.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

POD="${POD:-default}"
TENANT="${TENANT:-default}"
REDIS_URL="${REDIS_URL:-redis://127.0.0.1:6379/0}"
TMUX_SESSION="${TMUX_SESSION:-}"
TMUX_TMPDIR="${TMUX_TMPDIR:-$HOME/.h-mesh/tmux}"
TMUX_SOCKET="${TMUX_SOCKET:-}"
VENV_PATH="${VENV_PATH:-}"
USE_VENV=1
SKIP_INSTALL=0
SKIP_DEPS=0
NO_DAEMONS=0
NON_INTERACTIVE=0

usage() {
    cat <<EOF
Usage: ./setup.sh [options]

Options:
  --pod <name>            Pod name (default: \$POD or "default")
  --tenant <name>         Tenant name (default: \$TENANT or "default")
  --redis-url <url>       Redis connection URL (default: \$REDIS_URL or "redis://127.0.0.1:6379/0")
  --session <name>        tmux session name (default: \$TMUX_SESSION or tenant name)
  --tmux-tmpdir <path>    tmux temporary/socket directory (default: \$TMUX_TMPDIR or ~/.h-mesh/tmux)
  --tmux-socket <path>    Explicit tmux socket path (default: \$TMUX_SOCKET or unset)
  --venv <path>           Virtual environment directory (default: \$VIRTUAL_ENV or .venv)
  --no-venv               Do not create/use a virtual environment; use ambient python3
  --skip-install          Skip pip install step
  --skip-deps             Skip system dependency checks/auto-install (redis-server, python3-venv, h-agent)
  --no-daemons            Seed registry only, do not start background daemons
  --non-interactive       Never prompt, even at a terminal; use flags/env/persisted config only
  -h, --help              Show this help message

With no flags, at a terminal, this runs an interactive wizard (agent
roster, CLI/account choices, local model provider) the same way the
reference project's setup.sh does. Piped or scripted (not a terminal), or
with --non-interactive, it never prompts -- flags, environment, and any
already-persisted tenant config are all it uses.
EOF
    exit 0
}

while [ $# -gt 0 ]; do
    case "$1" in
        --pod)
            POD="$2"; shift 2 ;;
        --tenant)
            TENANT="$2"; shift 2 ;;
        --redis-url)
            REDIS_URL="$2"; shift 2 ;;
        --session)
            TMUX_SESSION="$2"; shift 2 ;;
        --tmux-tmpdir)
            TMUX_TMPDIR="$2"; shift 2 ;;
        --tmux-socket)
            TMUX_SOCKET="$2"; shift 2 ;;
        --venv)
            VENV_PATH="$2"; USE_VENV=1; shift 2 ;;
        --no-venv)
            USE_VENV=0; shift ;;
        --skip-install)
            SKIP_INSTALL=1; shift ;;
        --skip-deps)
            SKIP_DEPS=1; shift ;;
        --no-daemons)
            NO_DAEMONS=1; shift ;;
        --non-interactive)
            NON_INTERACTIVE=1; shift ;;
        -h|--help)
            usage ;;
        *)
            echo "Unknown option: $1" >&2
            usage ;;
    esac
done

# ⚠ Auto-detected, not just a flag: a script piped input or run from cron has
# no terminal to read from, and a `read` there either gets EOF instantly (an
# empty answer to every question) or -- worse -- blocks forever on stdin that
# will never receive a keystroke. --non-interactive is there for a human at a
# real terminal who still wants flags-only behavior.
INTERACTIVE=1
[ "$NON_INTERACTIVE" -eq 1 ] && INTERACTIVE=0
[ -t 0 ] || INTERACTIVE=0

echo "=== h-mesh :: bootstrap ==="

# ── 0. System dependencies (redis-server, python3-venv, h-agent) ──────────────
if [ "$SKIP_DEPS" -eq 0 ]; then
    echo "Checking system dependencies..."

    install_apt_package() {
        local pkg="$1"
        if [ "$(id -u)" -eq 0 ]; then
            apt-get update -qq && apt-get install -y "$pkg"
        elif command -v sudo >/dev/null 2>&1; then
            sudo apt-get update -qq && sudo apt-get install -y "$pkg"
        else
            echo "error: '$pkg' is missing, and this script has neither root nor sudo to install it" >&2
            echo "  install it manually, e.g.: apt-get install -y $pkg" >&2
            exit 1
        fi
    }

    if command -v redis-server >/dev/null 2>&1; then
        echo "  ✓ redis-server present"
    else
        echo "  redis-server not found -- installing..."
        install_apt_package redis-server
    fi
    # Installed is not the same as running -- an apt install doesn't start
    # the service, and a bare host with no systemd needs it started directly.
    if ! redis-cli ping >/dev/null 2>&1; then
        if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files redis-server.service >/dev/null 2>&1; then
            echo "  starting redis-server via systemctl..."
            systemctl start redis-server 2>/dev/null || true
        fi
        if ! redis-cli ping >/dev/null 2>&1 && command -v redis-server >/dev/null 2>&1; then
            echo "  starting redis-server directly..."
            redis-server --daemonize yes >/dev/null 2>&1 || true
        fi
    fi

    # ⚠ `python3 -m venv --help` succeeds even when venv creation itself
    # would fail -- Debian/Ubuntu ship the module but not ensurepip's
    # dependencies until python3-venv is installed, and that only shows up
    # when actually creating one. Probe for real, cheaply (--without-pip
    # skips ensurepip's own network fetch).
    VENV_PROBE_DIR="$(mktemp -d)"
    if python3 -m venv "$VENV_PROBE_DIR/probe" --without-pip >/dev/null 2>&1; then
        echo "  ✓ python3-venv usable"
    else
        echo "  python3-venv not usable -- installing..."
        install_apt_package python3-venv
    fi
    rm -rf "$VENV_PROBE_DIR"

    if command -v h-agent >/dev/null 2>&1; then
        echo "  ✓ h-agent present"
    else
        echo "  h-agent not found -- installing via its own installer..."
        H_AGENT_URL="${H_AGENT_INSTALL_URL:-https://raw.githubusercontent.com/h-network/h-agent/main/install.sh}"
        if command -v curl >/dev/null 2>&1; then
            curl -fsSL "$H_AGENT_URL" | bash
        elif command -v wget >/dev/null 2>&1; then
            wget -qO- "$H_AGENT_URL" | bash
        else
            echo "error: h-agent is missing, and installing it needs curl or wget" >&2
            exit 1
        fi
        # ⚠ h-agent's installer only updates ~/.bashrc/~/.profile, for
        # FUTURE shells -- it never touches this process's own PATH. Left
        # alone, the daemons this same run starts later (which is what
        # actually execs tmux window commands) would inherit the
        # pre-install PATH and fail to find h-agent with "command not
        # found" (tmux reports that as the pane exiting status 127) --
        # measured live, on a host where h-agent had never been installed
        # before. ${PREFIX:-$HOME/.local}/bin matches the installer's own
        # default (see H_AGENT_URL's install.sh), and its override.
        export PATH="${PREFIX:-$HOME/.local}/bin:$PATH"
    fi
    echo
fi

slug() { echo "$1" | tr '[:upper:]' '[:lower:]' | tr ' ' '-'; }
check_bool() {
    local val="$1" def="$2"
    val="${val:-$def}"
    case "$val" in
        [Yy]|[Yy][Ee][Ss]) return 0 ;;
        [Nn]|[Nn][Oo])    return 1 ;;
        *) echo "error: expected yes or no, got '$val'" >&2; exit 2 ;;
    esac
}

# ── 1. Interactive wizard (pod/tenant, roster, CLI/accounts, provider) ────────
# Skipped entirely outside a real terminal (or with --non-interactive):
# flags/env and whatever's already persisted are all that mode uses.
if [ "$INTERACTIVE" -eq 1 ]; then
    # Literal ASCII art (figlet -f ansishadow.flf "H-MESH"), not generated
    # at runtime -- a fresh host has neither figlet nor that font
    # installed, and this is decoration for a human at a terminal, not
    # worth adding a dependency for.
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
    echo -e "  ${CYAN}H-MESH${NC} ${GREY}//${NC} ${CYAN}agentic office framework${NC} ${GREY}//${NC} ${CYAN}h-network${NC}"
    echo ""

    read -rp "Pod name [$POD]: " IN_POD; POD="$(slug "${IN_POD:-$POD}")"
    read -rp "Tenant name [$TENANT]: " IN_TENANT; TENANT="$(slug "${IN_TENANT:-$TENANT}")"
fi

if [ "$USE_VENV" -eq 1 ]; then
    if [ -n "$VENV_PATH" ]; then
        TARGET_VENV="$VENV_PATH"
    elif [ -n "${VIRTUAL_ENV:-}" ]; then
        TARGET_VENV="$VIRTUAL_ENV"
    else
        TARGET_VENV="$SCRIPT_DIR/.venv"
    fi
    if [ ! -d "$TARGET_VENV" ]; then
        echo "Creating virtual environment at $TARGET_VENV..."
        python3 -m venv "$TARGET_VENV"
    fi
    PYTHON="$TARGET_VENV/bin/python"
    echo "Virtualenv:   $TARGET_VENV"
else
    PYTHON="${PYTHON:-python3}"
    echo "Python:       $PYTHON (ambient)"
fi
export PYTHONPATH="${PYTHONPATH:+$PYTHONPATH:}$SCRIPT_DIR/h-app"

TENANT_ENV_GET() { "$PYTHON" -m services.tenant_config get "$TENANT" "$1" "${2:-}"; }

echo "Pod:          $POD"
echo "Tenant:       $TENANT"
echo "Redis URL:    $REDIS_URL"
echo

# Everything below the pod/tenant prompt reads existing tenant config as its
# defaults -- re-running never silently wipes a prior answer. Blank keeps it.
AGENTS_CSV_EXISTING="$(TENANT_ENV_GET AGENTS "")"
IFS=',' read -r -a AGENTS <<< "$AGENTS_CSV_EXISTING"
[ "${#AGENTS[@]}" -eq 1 ] && [ -z "${AGENTS[0]}" ] && AGENTS=()

DEF_CLI="$(TENANT_ENV_GET DEFAULT_CLI claude)"
ACCOUNTS_CSV_EXISTING="$(TENANT_ENV_GET ACCOUNTS default)"
IFS=',' read -r -a PROFILES <<< "$ACCOUNTS_CSV_EXISTING"
DEF_PROFILE="$(TENANT_ENV_GET DEFAULT_ACCOUNT "${PROFILES[0]:-default}")"

# ⚠ No associative arrays -- matches the reference project's own reasoning
# (see its setup.sh): a bash without `declare -A` (macOS ships 3.2) would
# die on the first prompt. Agent names are slugged to [a-z0-9-], so one
# shell variable per key is a safe encoding.
_mk() { printf 'M_%s_%s' "$1" "$(printf '%s' "$2" | tr -c 'A-Za-z0-9' '_')"; }
mset() { eval "$(_mk "$1" "$2")=\$3"; }
mget() { eval "printf '%s' \"\${$(_mk "$1" "$2"):-}\""; }

# Seed the per-agent maps from persisted exceptions before any prompting, so
# a re-run's defaults reflect prior answers even for agents not touched this
# time.
for a in "${AGENTS[@]}"; do mset CLI "$a" "$DEF_CLI"; mset PROF "$a" "$DEF_PROFILE"; done
CLI_MAP_EXISTING="$(TENANT_ENV_GET AGENT_CLIS "")"
for pair in ${CLI_MAP_EXISTING//,/ }; do mset CLI "${pair%%=*}" "${pair#*=}"; done
PROFILE_MAP_EXISTING="$(TENANT_ENV_GET AGENT_PROFILES "")"
for pair in ${PROFILE_MAP_EXISTING//,/ }; do mset PROF "${pair%%=*}" "${pair#*=}"; done
PROVIDER_MAP_EXISTING="$(TENANT_ENV_GET AGENT_PROVIDERS "")"
for pair in ${PROVIDER_MAP_EXISTING//,/ }; do mset EP "${pair%%=*}" "${pair#*=}"; done

TOKEN_VAR() { printf 'CLAUDE_OAUTH_TOKEN_%s' "$(printf '%s' "$1" | tr 'a-z-' 'A-Z_')"; }
TOKEN_VARS=""
ask_token() {
    local profile="$1" var existing prompt entered
    var="$(TOKEN_VAR "$profile")"
    existing="$(TENANT_ENV_GET "$var" "")"
    if [ -n "$existing" ]; then
        prompt="  OAuth token for '$profile' [keep existing]: "
    else
        prompt="  OAuth token for '$profile' (blank to log in interactively later): "
    fi
    read -rsp "$prompt" entered; echo
    [ -n "$entered" ] || entered="$existing"
    eval "$var=\$entered"
    TOKEN_VARS="$TOKEN_VARS $var"
}

LOCAL_URL="$(TENANT_ENV_GET PROVIDER_LOCAL_URL "")"
LOCAL_MODEL="$(TENANT_ENV_GET PROVIDER_LOCAL_MODEL "")"
LOCAL_KIND="$(TENANT_ENV_GET PROVIDER_LOCAL_KIND vllm)"
EP_NAME="local"

if [ "$INTERACTIVE" -eq 1 ]; then
    read -rp "How many agents? [${#AGENTS[@]}${AGENTS[0]:+ (${AGENTS[*]})}]: " N
    if [ -n "$N" ]; then
        [[ "$N" =~ ^[1-9][0-9]*$ ]] || { echo "error: expected a positive number, got '$N'" >&2; exit 2; }
        NEW_AGENTS=()
        for i in $(seq 1 "$N"); do
            if [ "$i" -eq 1 ]; then def="architect"; else def="${AGENTS[$((i-1))]:-sme-$i}"; fi
            read -rp "  Agent #$i name [$def]: " A
            A="$(slug "${A:-$def}")"
            [ -n "$A" ] || { echo "  error: an agent needs a name" >&2; exit 2; }
            NEW_AGENTS+=("$A")
        done
        AGENTS=("${NEW_AGENTS[@]}")
        for a in "${AGENTS[@]}"; do
            [ -n "$(mget CLI "$a")" ] || mset CLI "$a" "$DEF_CLI"
            [ -n "$(mget PROF "$a")" ] || mset PROF "$a" "$DEF_PROFILE"
        done
    elif [ "${#AGENTS[@]}" -eq 0 ]; then
        AGENTS=(architect)
        mset CLI architect "$DEF_CLI"
        mset PROF architect "$DEF_PROFILE"
    fi

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
            ask_token "$P"
        done
        PROFILES=("${NEW_PROFILES[@]}")
    else
        PROFILES=(default)
        ask_token default
    fi

    read -rp "  Default CLI (claude/codex/agy) [$DEF_CLI]: " IN_CLI
    DEF_CLI="$(slug "${IN_CLI:-$DEF_CLI}")"
    for a in "${AGENTS[@]}"; do mset CLI "$a" "$DEF_CLI"; done

    if [ "${#PROFILES[@]}" -gt 1 ] || [ "${PROFILES[0]}" != "default" ]; then
        echo "  Accounts: ${PROFILES[*]}"
        read -rp "  Default account for every agent [${PROFILES[0]}]: " IN_DEF_PROFILE
        DEF_PROFILE="$(slug "${IN_DEF_PROFILE:-${PROFILES[0]}}")"
        for a in "${AGENTS[@]}"; do mset PROF "$a" "$DEF_PROFILE"; done
    fi

    echo "  Agents: ${AGENTS[*]}"
    read -rp "  Any agents differing from that? (space-separated, blank for none): " EXC
    for want in ${EXC//,/ }; do
        want="$(slug "$want")"
        printf '%s\n' "${AGENTS[@]}" | grep -qx "$want" || { echo "  (skipping '$want' -- not an agent)"; continue; }
        read -rp "    $want -- CLI [$DEF_CLI]: " C; mset CLI "$want" "$(slug "${C:-$DEF_CLI}")"
        if [ "${#PROFILES[@]}" -gt 1 ]; then
            read -rp "    $want -- account [$DEF_PROFILE]: " P; P="$(slug "${P:-$DEF_PROFILE}")"
            if printf '%s\n' "${PROFILES[@]}" | grep -qx "$P"; then mset PROF "$want" "$P"
            else echo "    (no account '$P' -- keeping $(mget PROF "$want"))"; fi
        fi
    done

    # ⚠ agy keeps its state in ~/.gemini/antigravity-cli with no equivalent
    # of CLAUDE_CONFIG_DIR/CODEX_HOME -- it cannot be pointed at a second
    # account.
    for a in "${AGENTS[@]}"; do
        if [ "$(mget CLI "$a")" = "agy" ] && [ "$(mget PROF "$a")" != "default" ]; then
            echo "  warning: $a runs agy, which supports only one account -- ignoring account '$(mget PROF "$a")'" >&2
            mset PROF "$a" default
        fi
    done

    read -rp "Point any agent at a local model provider? [y/N]: " USE_PROVIDER
    if check_bool "$USE_PROVIDER" "n"; then
        read -rp "  Endpoint type -- vllm or ollama [$LOCAL_KIND]: " IN_KIND
        LOCAL_KIND="$(slug "${IN_KIND:-$LOCAL_KIND}")"
        case "$LOCAL_KIND" in
            vllm)   EP_HINT="http://10.0.0.5:8000" ;;
            ollama) EP_HINT="http://10.0.0.5:11434" ;;
            *)      echo "  unknown type '$LOCAL_KIND' -- treating it as vllm"; LOCAL_KIND=vllm; EP_HINT="http://10.0.0.5:8000" ;;
        esac
        read -rp "  Endpoint base URL, e.g. $EP_HINT (NO trailing /v1)${LOCAL_URL:+ [$LOCAL_URL]}: " IN_URL
        LOCAL_URL="${IN_URL:-$LOCAL_URL}"
        LOCAL_URL="${LOCAL_URL%/}"
        LOCAL_URL="${LOCAL_URL%/v1}"

        if [ -n "$LOCAL_URL" ]; then
            SERVED="$("$PYTHON" -m services.provider_probe models "$LOCAL_URL" "$LOCAL_KIND" 2>/dev/null | tr '\n' ' ')"
            [ -n "$SERVED" ] && echo "  served by that provider: $SERVED"
            SERVED_FIRST="${SERVED%% *}"
            read -rp "  Model id [${LOCAL_MODEL:-$SERVED_FIRST}]: " IN_MODEL
            LOCAL_MODEL="${IN_MODEL:-${LOCAL_MODEL:-$SERVED_FIRST}}"

            if [ -n "$LOCAL_MODEL" ]; then
                if "$PYTHON" -m services.provider_probe probe "$LOCAL_URL" "$LOCAL_MODEL"; then
                    echo "  ✓ probe succeeded"
                else
                    echo "  ⚠ probe did not succeed (see message above) -- saved anyway, retry later with h-mesh upgrade"
                fi
            fi

            read -rp "  Endpoint name [$EP_NAME]: " IN_EP_NAME; EP_NAME="$(slug "${IN_EP_NAME:-$EP_NAME}")"
            read -rp "  Which agents use it? (space-separated, blank for none): " EPS
            for want in $EPS; do
                for a in "${AGENTS[@]}"; do
                    [ "$a" = "$(slug "$want")" ] && mset EP "$a" "$EP_NAME"
                done
            done
        fi
    fi

    # Persist everything the wizard just collected -- only exceptions travel
    # for CLI/account/provider, so the file stays small and readable.
    CLI_MAP=(); PROFILE_MAP=(); PROVIDER_MAP=()
    for a in "${AGENTS[@]}"; do
        [ "$(mget CLI "$a")" != "$DEF_CLI" ] && CLI_MAP+=("${a}=$(mget CLI "$a")")
        [ "$(mget PROF "$a")" != "$DEF_PROFILE" ] && PROFILE_MAP+=("${a}=$(mget PROF "$a")")
        [ -n "$(mget EP "$a")" ] && PROVIDER_MAP+=("${a}=$(mget EP "$a")")
    done

    {
        echo "AGENTS=$(IFS=,; echo "${AGENTS[*]}")"
        echo "DEFAULT_CLI=${DEF_CLI}"
        echo "ACCOUNTS=$(IFS=,; echo "${PROFILES[*]}")"
        echo "DEFAULT_ACCOUNT=${DEF_PROFILE}"
        for tv in $TOKEN_VARS; do
            eval "tval=\${$tv:-}"
            [ -n "$tval" ] && echo "${tv}=${tval}"
        done
        [ "${#CLI_MAP[@]}"      -gt 0 ] && echo "AGENT_CLIS=$(IFS=,; echo "${CLI_MAP[*]}")"
        [ "${#PROFILE_MAP[@]}"  -gt 0 ] && echo "AGENT_PROFILES=$(IFS=,; echo "${PROFILE_MAP[*]}")"
        if [ "${#PROVIDER_MAP[@]}" -gt 0 ] && [ -n "$LOCAL_URL" ]; then
            echo "AGENT_PROVIDERS=$(IFS=,; echo "${PROVIDER_MAP[*]}")"
            PR_UPPER="$(echo "$EP_NAME" | tr '[:lower:]-' '[:upper:]_')"
            echo "PROVIDER_${PR_UPPER}_URL=${LOCAL_URL}"
            echo "PROVIDER_${PR_UPPER}_MODEL=${LOCAL_MODEL}"
            echo "PROVIDER_${PR_UPPER}_TOKEN=local"
            echo "PROVIDER_${PR_UPPER}_KIND=${LOCAL_KIND}"
        fi
    } | "$PYTHON" -m services.tenant_config set "$TENANT"

    # Re-read what was just persisted -- the hire step below uses the
    # *_EXISTING variables regardless of whether this run went through the
    # wizard or not, so they must reflect whatever the wizard just wrote,
    # not the pre-wizard values read at the top of this script.
    CLI_MAP_EXISTING="$(TENANT_ENV_GET AGENT_CLIS "")"
    PROFILE_MAP_EXISTING="$(TENANT_ENV_GET AGENT_PROFILES "")"
    PROVIDER_MAP_EXISTING="$(TENANT_ENV_GET AGENT_PROVIDERS "")"

    echo
    printf '  %-16s %-8s %-10s\n' AGENT CLI ACCOUNT
    for a in "${AGENTS[@]}"; do
        printf '  %-16s %-8s %-10s\n' "$a" "$(mget CLI "$a")" "$(mget PROF "$a")"
    done
    echo
fi

# 2. Install h-mesh package
if [ "$SKIP_INSTALL" -eq 0 ]; then
    echo "Installing h-mesh in editable mode..."
    "$PYTHON" -m pip install -e . || exit 1
    echo
fi

# 2.5. Persist the venv's bin dir on PATH for every future shell/pane -- a
# hired agent's tmux pane and an attaching human both start a fresh login
# shell, which doesn't inherit PATH from whatever shell ran this script.
if [ "$USE_VENV" -eq 1 ]; then
    echo "Persisting $TARGET_VENV/bin on PATH (~/.bashrc, ~/.profile)..."
    "$PYTHON" -m services.venv_path "$TARGET_VENV"
    echo
fi

# 2.6. Install h-mesh's default tmux.conf, unless the user already has one
echo "Installing default tmux.conf (unless one already exists)..."
"$PYTHON" -m services.tmux_conf
echo

# 3. Verify Redis connection
echo "Checking Redis connection at $REDIS_URL..."
if ! REDIS_URL="$REDIS_URL" "$PYTHON" -c '
import os, sys, redis
redis_url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
try:
    r = redis.Redis.from_url(redis_url)
    r.ping()
    print("✓ Redis is reachable")
except Exception as e:
    print(f"error: failed to connect to Redis at {redis_url}: {e}", file=sys.stderr)
    sys.exit(1)
'; then
    exit 1
fi
echo

# 4. Ensure isolated tmux directory exists
export TMUX_TMPDIR
mkdir -p "$TMUX_TMPDIR"
chmod 700 "$TMUX_TMPDIR"
TMUX_SESSION="${TMUX_SESSION:-$TENANT}"

# 5. Seed fixed lifecycle participants in the registry (host->office, api->api)
echo "Seeding registry for pod=$POD, tenant=$TENANT..."
POD="$POD" TENANT="$TENANT" REDIS_URL="$REDIS_URL" "$PYTHON" -c '
import os, sys, redis
from core.keys import prefix

pod = os.environ["POD"]
tenant = os.environ["TENANT"]
redis_url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
r = redis.Redis.from_url(redis_url)
registry_key = prefix(pod, tenant, resource="registry")

# Fixed participants needed for routing lifecycle and API envelopes
# "host" routes to modules.office.port; "api" routes to modules.api.port
r.hset(registry_key, mapping={"host": "office", "api": "api"})
print(f"✓ Registry seeded ({registry_key}): host -> office, api -> api")
' || exit 1
echo

# 6. Start required daemons (h-mesh-switch and h-mesh-tmux-reconciler)
if [ "$NO_DAEMONS" -eq 0 ]; then
    RUN_DIR="${H_MESH_RUN_DIR:-$HOME/.h-mesh/run/${TENANT}}"
    mkdir -p "$RUN_DIR"

    export POD TENANT REDIS_URL TMUX_SESSION TMUX_TMPDIR PYTHONUNBUFFERED=1
    [ -n "$TMUX_SOCKET" ] && export TMUX_SOCKET

    echo "Starting daemons (logs written to $RUN_DIR)..."

    # ⚠ Delegates to services.daemons.start_daemons(), not a hand-rolled
    # nohup here -- that function is duplicate-safe (skips any daemon
    # already alive). A bare `nohup ... &` on every run, with no check
    # first, is exactly what let a re-run of this script leave a second
    # tmux_reconciler running beside the first -- measured while adding the
    # wizard's re-run path.
    #
    # ⚠ merged_daemon_env(), not a bare dict(os.environ) -- this script's
    # own shell never exports the OAuth token/provider vars ask_token()
    # collects (they only ever get written to the persisted tenant config,
    # not to this process's environment), so without this the daemon
    # hiring the roster's first agent in THIS SAME run never sees a token
    # the wizard just asked for -- measured live, on a fresh install.
    # python=sys.executable (not services.daemons's own venv resolution,
    # via resolve_config()) so --no-venv's ambient-python mode -- which
    # that resolution doesn't support -- keeps working here.
    H_MESH_SETUP_RUN_DIR="$RUN_DIR" "$PYTHON" -c '
import os, sys
sys.path.insert(0, "h-app")
from pathlib import Path
from services.daemons import DaemonError, merged_daemon_env, start_daemons

env = merged_daemon_env(os.environ["TENANT"])
try:
    start_daemons(python=Path(sys.executable), run_dir=Path(os.environ["H_MESH_SETUP_RUN_DIR"]), env=env)
except DaemonError as exc:
    print(f"error: {exc}", file=sys.stderr)
    sys.exit(1)
' || exit 1

    echo
    echo "✓ Daemons are healthy."
    echo

    # 7. Hire any agent from the wizard's roster that isn't already running.
    # Only runs at all if a roster was ever persisted -- a flags-only host
    # that never used the wizard keeps today's behavior: hire manually.
    ROSTER_CSV="$("$PYTHON" -m services.tenant_config get "$TENANT" AGENTS "")"
    if [ -n "$ROSTER_CSV" ]; then
        echo "Hiring agents from the roster (skipping any already running)..."
        IFS=',' read -r -a ROSTER <<< "$ROSTER_CSV"
        for a in "${ROSTER[@]}"; do
            [ -n "$a" ] || continue
            ALREADY="$(AGENT_NAME=host POD="$POD" TENANT="$TENANT" REDIS_URL="$REDIS_URL" "$PYTHON" -c '
import sys
sys.path.insert(0, "h-app")
import os, redis
from core.registry import port_type
r = redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"))
pt = port_type(r, pod=os.environ["POD"], tenant=os.environ["TENANT"], agent=sys.argv[1])
print("1" if pt == "tmux" else "0")
' "$a")"
            if [ "$ALREADY" = "1" ]; then
                echo "  • $a: already running"
                continue
            fi
            CLI_FOR_A="$("$PYTHON" -m services.tenant_config get "$TENANT" DEFAULT_CLI claude)"
            for pair in ${CLI_MAP_EXISTING//,/ }; do [ "${pair%%=*}" = "$a" ] && CLI_FOR_A="${pair#*=}"; done
            PROF_FOR_A="$("$PYTHON" -m services.tenant_config get "$TENANT" DEFAULT_ACCOUNT default)"
            for pair in ${PROFILE_MAP_EXISTING//,/ }; do [ "${pair%%=*}" = "$a" ] && PROF_FOR_A="${pair#*=}"; done
            EP_FOR_A=""
            for pair in ${PROVIDER_MAP_EXISTING//,/ }; do [ "${pair%%=*}" = "$a" ] && EP_FOR_A="${pair#*=}"; done

            HIRE_ARGS=("$a" --cli "$CLI_FOR_A")
            [ "$PROF_FOR_A" != "default" ] && HIRE_ARGS+=(--profile "$PROF_FOR_A")
            [ -n "$EP_FOR_A" ] && HIRE_ARGS+=(--provider "$EP_FOR_A")
            if AGENT_NAME=host POD="$POD" TENANT="$TENANT" REDIS_URL="$REDIS_URL" \
                "$PYTHON" -m modules.office.cli hire "${HIRE_ARGS[@]}" >/dev/null; then
                echo "  • $a: hired ($CLI_FOR_A${PROF_FOR_A:+, account=$PROF_FOR_A}${EP_FOR_A:+, provider=$EP_FOR_A})"
            else
                echo "  • $a: hire failed -- check switch.log/tmux_reconciler.log" >&2
            fi
        done
        echo
    else
        echo "To hire an initial agent (as host):"
        echo "  export AGENT_NAME=host POD=$POD TENANT=$TENANT"
        echo "  $PYTHON -m modules.office.cli hire <agent-name>"
        echo
    fi

    echo "To attach to the tmux session:"
    echo "  TMUX_TMPDIR=$TMUX_TMPDIR tmux attach -t $TMUX_SESSION"
fi
