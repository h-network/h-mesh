#!/usr/bin/env bash
# setup.sh — Host bootstrap script for h-mesh.
# Verifies/installs system dependencies, installs h-mesh in an isolated venv,
# persists venv PATH and a default tmux.conf, walks an interactive wizard
# (agent roster, CLI/account choices, local model provider) when run at a
# terminal, seeds the fixed lifecycle participants (host->office, api->api)
# in the Redis registry, starts the required daemons (h-mesh-switch,
# h-mesh-tmux-reconciler, h-mesh-watchdog, and -- only once Telegram is
# configured -- h-mesh-api, the Telegram bot, and h-mesh-session), and
# hires any agent from the wizard's roster that isn't already running.
#
# Without a terminal (or with --non-interactive), every wizard setting is
# instead read from a live env var of the same name if set (AGENTS,
# DEFAULT_CLI, ACCOUNTS, AGENT_CLIS/AGENT_PROFILES/AGENT_PROVIDERS,
# CLAUDE_OAUTH_TOKEN_<PROFILE>, PROVIDER_LOCAL_*, TELEGRAM_*, API_TOKEN),
# falling back to whatever's already persisted -- then persisted right back,
# same as an interactive answer would be. See ENV_TENANT_GET below.
#
# AGENT_CLIS/AGENT_PROFILES/AGENT_PROVIDERS are each a comma-separated list
# of agent=value pairs, e.g. AGENT_CLIS=worker1=codex,worker2=agy -- ONE '='
# per pair, agent name on the left. A malformed entry (wrong separator, a
# missing value) is refused with a hard error rather than silently treated
# as absent -- see validate_pair_map below. This matters most for
# AGENT_PROVIDERS: an agent with no provider override runs against the real
# vendor API, so a typo'd separator there means real spend, not just a
# wrong setting.
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

Non-interactive env vars for per-agent exceptions (AGENT_CLIS,
AGENT_PROFILES, AGENT_PROVIDERS) are each a comma-separated list of
agent=value pairs -- e.g. AGENT_CLIS=worker1=codex,worker2=agy. A
malformed entry (wrong separator, missing value) is refused with an
error, not silently ignored -- this matters most for AGENT_PROVIDERS,
where an agent with no recognized override runs against the real vendor
API instead of the local provider you configured.
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
        H_AGENT_URL="${H_AGENT_INSTALL_URL:-https://raw.githubusercontent.com/h-network/h-agent/main/install.sh}"
        if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then
            echo "error: h-agent is missing, and installing it needs curl or wget" >&2
            exit 1
        fi
        # ⚠ Retried once: confirmed live that h-agent's own installer (which
        # runs under set -euo pipefail) can abort after its CLI-install step
        # and before placing h-agent itself, on a transient failure --
        # re-running the identical installer by hand right after succeeded
        # with no other change. One retry covers that cheaply; a second
        # failure is treated as real.
        h_agent_installed=0
        h_agent_install_statuses=()
        for attempt in 1 2; do
            echo "  h-agent not found -- installing via its own installer (attempt $attempt/2)..."
            if command -v curl >/dev/null 2>&1; then
                curl -fsSL "$H_AGENT_URL" | bash
            else
                wget -qO- "$H_AGENT_URL" | bash
            fi
            h_agent_install_status=$?
            h_agent_install_statuses+=("$h_agent_install_status")
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
            if command -v h-agent >/dev/null 2>&1; then
                h_agent_installed=1
                break
            fi
            echo "  h-agent installer exited $h_agent_install_status and h-agent is still not on PATH." >&2
        done
        # ⚠ The bug this replaced: curl|bash's exit status was never
        # checked at all, and this script runs under pipefail but no -e --
        # so even a checked nonzero status wouldn't have stopped it on its
        # own. When h-agent's installer failed partway, setup.sh printed
        # "successfully" and moved on with h-agent nowhere on the host;
        # every window the reconciler later created died instantly with
        # "command not found", and nothing here ever said why. Verify the
        # actual binary, not just the installer's exit code -- an installer
        # can exit 0 and still not place the binary where this expects it.
        if [ "$h_agent_installed" -ne 1 ]; then
            echo "error: h-agent installer failed twice (last exit $h_agent_install_status) -- h-agent is still not on PATH or at ${PREFIX:-$HOME/.local}/bin/h-agent." >&2
            # ⚠ Exit code only, never upstream's message text -- matching a
            # phrase from an installer we don't own is a dependency on a
            # string nobody promised us, the same "trust a moving external
            # thing exactly" shape as the agy version pin that produced this
            # exact incident. Both retry attempts exiting identically is
            # itself evidence worth surfacing (a real host once needed the
            # retry for a genuinely transient ordering bug -- see above --
            # so this isn't every failure, just repeated-identical ones): a
            # human reading "attempt 2/2" on its own can read as possibly
            # flaky and be tempted to just run it again; a repeated,
            # identical exit code is not that.
            if [ "${#h_agent_install_statuses[@]}" -eq 2 ] \
                && [ "${h_agent_install_statuses[0]}" = "${h_agent_install_statuses[1]}" ]; then
                echo "  Both attempts failed identically (exit ${h_agent_install_statuses[0]} both times) -- this is a repeated, deterministic failure, not a transient one; re-running setup.sh will not help without a change upstream or to this host." >&2
            fi
            echo "  Install it manually (see $H_AGENT_URL) and re-run setup.sh." >&2
            exit 1
        fi
        echo "  ✓ h-agent installed"
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

# ⚠ Same precedence as services.daemons.merged_daemon_env()/resolve_config()
# elsewhere in this project: an explicit, non-empty live env var always wins
# over whatever is already persisted; a blank/unset one keeps the persisted
# value (or the hardcoded default) rather than clearing it. Used below for
# every wizard setting's *initial* value -- interactive mode still lets a
# human override it at the prompt; non-interactive mode has no prompt, so
# this env-or-persisted value is the final one.
ENV_TENANT_GET() {
    local key="$1" def="${2:-}" val
    val="${!key:-}"
    if [ -n "$val" ]; then printf '%s' "$val"; else TENANT_ENV_GET "$key" "$def"; fi
}

echo "Pod:          $POD"
echo "Tenant:       $TENANT"
echo "Redis URL:    $REDIS_URL"
echo

# Everything below the pod/tenant prompt reads existing tenant config (or a
# live env var of the same name, which wins -- see ENV_TENANT_GET) as its
# defaults -- re-running never silently wipes a prior answer. Blank keeps it.
AGENTS_CSV_EXISTING="$(ENV_TENANT_GET AGENTS "")"
IFS=',' read -r -a AGENTS <<< "$AGENTS_CSV_EXISTING"
[ "${#AGENTS[@]}" -eq 1 ] && [ -z "${AGENTS[0]}" ] && AGENTS=()

DEF_CLI="$(ENV_TENANT_GET DEFAULT_CLI claude)"
ACCOUNTS_CSV_EXISTING="$(ENV_TENANT_GET ACCOUNTS default)"
IFS=',' read -r -a PROFILES <<< "$ACCOUNTS_CSV_EXISTING"
DEF_PROFILE="$(ENV_TENANT_GET DEFAULT_ACCOUNT "${PROFILES[0]:-default}")"

# ⚠ No associative arrays -- matches the reference project's own reasoning
# (see its setup.sh): a bash without `declare -A` (macOS ships 3.2) would
# die on the first prompt. Agent names are slugged to [a-z0-9-], so one
# shell variable per key is a safe encoding.
_mk() { printf 'M_%s_%s' "$1" "$(printf '%s' "$2" | tr -c 'A-Za-z0-9' '_')"; }
mset() { eval "$(_mk "$1" "$2")=\$3"; }
mget() { eval "printf '%s' \"\${$(_mk "$1" "$2"):-}\""; }

# ⚠ A malformed entry (wrong separator, e.g. "worker1:local" instead of
# "worker1=local") is NOT the same as an absent one, and must not be
# silently treated as one. "${pair%%=*}"/"${pair#*=}" against a pair with
# no "=" at all both evaluate to the whole malformed string -- so a typo'd
# separator quietly created a bogus map key that never matched any real
# agent, leaving the REAL agent name with no override at all. For
# AGENT_PROVIDERS specifically, that meant hiring silently proceeded
# against the real vendor API instead of the local provider the user
# explicitly configured, with no on-screen indication anything was wrong --
# caught live, by inspecting a pane, not by this script. Refuse rather than
# ignore: falling back to a default IS the failure mode here, for all three
# maps -- AGENT_CLIS/AGENT_PROFILES silently reverting to the wrong
# CLI/account is exactly as much "configured one thing, silently got
# another" as AGENT_PROVIDERS silently billing the wrong API, just with a
# less expensive symptom.
validate_pair_map() {
    local csv="$1" var_name="$2" pair k v
    [ -z "$csv" ] && return 0
    for pair in ${csv//,/ }; do
        case "$pair" in
            *=*) : ;;
            *)
                echo "error: $var_name entry '$pair' is malformed (expected agent=value, e.g. worker1=local) -- refusing to continue rather than silently treat it as absent." >&2
                return 1
                ;;
        esac
        k="${pair%%=*}"; v="${pair#*=}"
        if [ -z "$k" ] || [ -z "$v" ]; then
            echo "error: $var_name entry '$pair' is malformed (expected agent=value, both non-empty) -- refusing to continue rather than silently treat it as absent." >&2
            return 1
        fi
    done
    return 0
}

# Seed the per-agent maps from persisted exceptions before any prompting, so
# a re-run's defaults reflect prior answers even for agents not touched this
# time.
for a in "${AGENTS[@]}"; do mset CLI "$a" "$DEF_CLI"; mset PROF "$a" "$DEF_PROFILE"; done
CLI_MAP_EXISTING="$(ENV_TENANT_GET AGENT_CLIS "")"
validate_pair_map "$CLI_MAP_EXISTING" AGENT_CLIS || exit 1
for pair in ${CLI_MAP_EXISTING//,/ }; do mset CLI "${pair%%=*}" "${pair#*=}"; done
PROFILE_MAP_EXISTING="$(ENV_TENANT_GET AGENT_PROFILES "")"
validate_pair_map "$PROFILE_MAP_EXISTING" AGENT_PROFILES || exit 1
for pair in ${PROFILE_MAP_EXISTING//,/ }; do mset PROF "${pair%%=*}" "${pair#*=}"; done
PROVIDER_MAP_EXISTING="$(ENV_TENANT_GET AGENT_PROVIDERS "")"
validate_pair_map "$PROVIDER_MAP_EXISTING" AGENT_PROVIDERS || exit 1
for pair in ${PROVIDER_MAP_EXISTING//,/ }; do mset EP "${pair%%=*}" "${pair#*=}"; done

TOKEN_VAR() { printf 'CLAUDE_OAUTH_TOKEN_%s' "$(printf '%s' "$1" | tr 'a-z-' 'A-Z_')"; }
TOKEN_VARS=""
ask_token() {
    local profile="$1" var existing prompt entered
    var="$(TOKEN_VAR "$profile")"
    existing="$(ENV_TENANT_GET "$var" "")"
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

# Non-interactive equivalent of ask_token(): no prompt, just an env-or-
# persisted read (same CLAUDE_OAUTH_TOKEN_<PROFILE> var merged_daemon_env()
# already layers onto a *running* daemon's env -- this is what makes that
# also land in the tenant config file, so a later clean-shell run sees it too).
resolve_token_noninteractive() {
    local profile="$1" var val
    var="$(TOKEN_VAR "$profile")"
    val="$(ENV_TENANT_GET "$var" "")"
    eval "$var=\$val"
    TOKEN_VARS="$TOKEN_VARS $var"
}

# ⚠ EP_NAME itself must round-trip through persisted config, not just its
# URL/model/kind -- modules.tmux.reconciler.get_agent_provider() already
# looks up PROVIDER_<NAME>_* using whatever name is registered per agent
# (it slugs the exact same way, see provider_key_prefix below), so a custom
# name has always worked for a *hired* agent. What was broken is purely
# this script's own re-read of its own prompt defaults: a hardcoded "local"
# here meant a re-run (e.g. to add another agent) showed the provider
# section as blank/unconfigured even though PROVIDER_OFFICE_GPU_URL (or
# whatever name was chosen) was sitting right there in the tenant config --
# and answering "y" again without noticing would persist a second,
# redundant "local"-named entry alongside the real one.
provider_key_prefix() { printf 'PROVIDER_%s' "$(printf '%s' "$1" | tr '[:lower:]-' '[:upper:]_')"; }
EP_NAME="$(ENV_TENANT_GET PROVIDER_NAME local)"
PROVIDER_KEY_PREFIX="$(provider_key_prefix "$EP_NAME")"
LOCAL_URL="$(ENV_TENANT_GET "${PROVIDER_KEY_PREFIX}_URL" "")"
LOCAL_MODEL="$(ENV_TENANT_GET "${PROVIDER_KEY_PREFIX}_MODEL" "")"
LOCAL_KIND="$(ENV_TENANT_GET "${PROVIDER_KEY_PREFIX}_KIND" vllm)"

TELEGRAM_BOT_TOKEN="$(ENV_TENANT_GET TELEGRAM_BOT_TOKEN "")"
TELEGRAM_CHAT_ID="$(ENV_TENANT_GET TELEGRAM_CHAT_ID "")"
TELEGRAM_VOICE="$(ENV_TENANT_GET TELEGRAM_VOICE "")"
API_TOKEN="$(ENV_TENANT_GET API_TOKEN "")"

# Shared by both modes -- interactive only reaches this after a human opts
# in via "Run the Telegram bot?"; non-interactive has no such prompt, so it
# runs this unconditionally on whatever env/persisted state resolved above.
# elif (not else): a genuinely unconfigured pair (both blank) is not an
# error, just "not using Telegram" -- only a *partial* pair warns.
finalize_telegram() {
    if [ -n "$TELEGRAM_BOT_TOKEN" ] && [ -n "$TELEGRAM_CHAT_ID" ]; then
        # ⚠ The bot talks to h-mesh's own REST API (see
        # clients/telegram/README.md) -- there's no separate "enable the
        # API" setting, a configured bot enables both. A bare-token
        # API_TOKEN, generated once and persisted like every other secret
        # here, not typed/exported by anyone.
        if [ -z "$API_TOKEN" ]; then
            API_TOKEN="$("$PYTHON" -c 'import secrets; print(secrets.token_hex(16))')"
        fi
        echo "  (h-mesh's REST API will be started too -- the Telegram bot talks to it)"
    elif [ -n "$TELEGRAM_BOT_TOKEN" ] || [ -n "$TELEGRAM_CHAT_ID" ]; then
        echo "  ⚠ Both a Telegram Bot Token and Chat ID are required -- Telegram bot not enabled." >&2
        TELEGRAM_BOT_TOKEN=""
        TELEGRAM_CHAT_ID=""
        TELEGRAM_VOICE=""
    fi
}

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

    # ⚠ Update agents that were FOLLOWING the old default to the new one --
    # not every agent unconditionally. An unconditional reset here silently
    # reverted a per-agent CLI override on every re-run (e.g. observer=codex
    # went back to claude just by accepting this prompt's own default,
    # without ever touching the "any agents differing" exceptions below) --
    # measured live. Every agent already has a non-empty CLI by this point
    # (seeded to the *old* DEF_CLI above if newly added this run), so
    # comparing against OLD_DEF_CLI correctly catches both a genuinely
    # default-following agent and a brand-new one, while leaving an agent
    # whose CLI already differs from the old default alone.
    OLD_DEF_CLI="$DEF_CLI"
    read -rp "  Default CLI (claude/codex/agy) [$DEF_CLI]: " IN_CLI
    DEF_CLI="$(slug "${IN_CLI:-$DEF_CLI}")"
    for a in "${AGENTS[@]}"; do
        [ "$(mget CLI "$a")" = "$OLD_DEF_CLI" ] && mset CLI "$a" "$DEF_CLI"
    done

    if [ "${#PROFILES[@]}" -gt 1 ] || [ "${PROFILES[0]}" != "default" ]; then
        echo "  Accounts: ${PROFILES[*]}"
        read -rp "  Default account for every agent [${PROFILES[0]}]: " IN_DEF_PROFILE
        # Same fix, same reasoning, as DEF_CLI just above.
        OLD_DEF_PROFILE="$DEF_PROFILE"
        DEF_PROFILE="$(slug "${IN_DEF_PROFILE:-${PROFILES[0]}}")"
        for a in "${AGENTS[@]}"; do
            [ "$(mget PROF "$a")" = "$OLD_DEF_PROFILE" ] && mset PROF "$a" "$DEF_PROFILE"
        done
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

    read -rp "Run the Telegram bot? [y/N]: " WANT_TELEGRAM
    if check_bool "$WANT_TELEGRAM" "n"; then
        if [ -n "$TELEGRAM_BOT_TOKEN" ]; then
            read -rsp "  Telegram Bot Token [keep existing]: " IN_TG_TOKEN; echo
        else
            read -rsp "  Telegram Bot Token (required, blank to skip): " IN_TG_TOKEN; echo
        fi
        TELEGRAM_BOT_TOKEN="${IN_TG_TOKEN:-$TELEGRAM_BOT_TOKEN}"

        read -rp "  Telegram Chat ID (required)${TELEGRAM_CHAT_ID:+ [$TELEGRAM_CHAT_ID]}: " IN_TG_CHAT
        TELEGRAM_CHAT_ID="${IN_TG_CHAT:-$TELEGRAM_CHAT_ID}"

        if [ -n "$TELEGRAM_BOT_TOKEN" ] && [ -n "$TELEGRAM_CHAT_ID" ]; then
            if [ "$TELEGRAM_VOICE" = "1" ]; then
                read -rp "  Enable spoken voice replies? [Y/n]: " WANT_VOICE
                if check_bool "$WANT_VOICE" "y"; then TELEGRAM_VOICE=1; else TELEGRAM_VOICE=0; fi
            else
                read -rp "  Enable spoken voice replies? [y/N]: " WANT_VOICE
                if check_bool "$WANT_VOICE" "n"; then TELEGRAM_VOICE=1; else TELEGRAM_VOICE=0; fi
            fi
        fi
        # Validation (both required), and API_TOKEN generation if so, happen
        # below unconditionally, in finalize_telegram -- same call non-
        # interactive mode uses, so a human declining here can't diverge
        # from what a non-interactive run with the same env would resolve to.
    fi
fi

# ── Settings that apply regardless of how AGENTS/CLI/PROF/TELEGRAM_* above
# got their values -- an interactive human's answers, or (INTERACTIVE=0)
# straight env-or-persisted reads with no prompt to override them. Runs
# once, after the wizard block, so it sees the final state either way.

# ⚠ agy keeps its state in ~/.gemini/antigravity-cli with no equivalent of
# CLAUDE_CONFIG_DIR/CODEX_HOME -- it cannot be pointed at a second account.
# Real invariant, not just interactive-prompt UX -- applies whether the
# agy+non-default-account combination came from a human's answers or from
# AGENT_CLIS/AGENT_PROFILES env vars in a non-interactive run.
for a in "${AGENTS[@]}"; do
    if [ "$(mget CLI "$a")" = "agy" ] && [ "$(mget PROF "$a")" != "default" ]; then
        echo "  warning: $a runs agy, which supports only one account -- ignoring account '$(mget PROF "$a")'" >&2
        mset PROF "$a" default
    fi
done

# Non-interactive has no ask_token() prompt -- resolve each account's OAuth
# token from CLAUDE_OAUTH_TOKEN_<PROFILE> env (or whatever's already
# persisted) instead, so it lands in the tenant config file the same way an
# interactive answer would, not just in a running daemon's live env (see
# services.daemons.merged_daemon_env, which already layers env on top of the
# persisted file for a *running* daemon -- this is what makes a later
# clean-shell run see it too).
if [ "$INTERACTIVE" -eq 0 ]; then
    for p in "${PROFILES[@]}"; do
        resolve_token_noninteractive "$p"
    done
fi

finalize_telegram

# Persist everything resolved above -- only exceptions travel for
# CLI/account/provider, so the file stays small and readable. Runs every
# time (not just after the interactive wizard) so a non-interactive run's
# env-derived settings converge on the same stored state an interactive
# answer would have produced, instead of only taking effect for daemons
# started in this one process's environment (see services.daemons.
# merged_daemon_env -- without this, a later "h-mesh start"/upgrade run
# from a clean shell would silently come up without whatever only ever
# lived in this run's exported env).
CLI_MAP=(); PROFILE_MAP=(); PROVIDER_MAP=()
for a in "${AGENTS[@]}"; do
    [ "$(mget CLI "$a")" != "$DEF_CLI" ] && CLI_MAP+=("${a}=$(mget CLI "$a")")
    [ "$(mget PROF "$a")" != "$DEF_PROFILE" ] && PROFILE_MAP+=("${a}=$(mget PROF "$a")")
    [ -n "$(mget EP "$a")" ] && PROVIDER_MAP+=("${a}=$(mget EP "$a")")
done

# ⚠ Never a refuse/block guard -- this script cannot tell "ambient leftover
# in a shared shell" from "a deliberate CI/deployment pipeline exporting a
# real token before a non-interactive run"; those look identical from
# inside the script, and the second one is exactly what ENV_TENANT_GET's
# env-wins precedence is *for*. Purely informational instead: names only,
# never values, so a human running this by hand can see what just got
# pulled from their live shell instead of finding out later. Built as
# plain function calls (not inside the `{...} | pipe` below) on purpose --
# a pipeline's non-last stage runs in a subshell, so a variable this
# accumulates inside `{...} | tenant_config set` would vanish the moment
# the pipe finished, back to empty in the parent shell.
PERSIST_LINES=()
ENV_SOURCED_KEYS=""
persist_line() {
    local key="$1" value="$2"
    PERSIST_LINES+=("${key}=${value}")
    if [ -n "${!key:-}" ] && [ "${!key}" = "$value" ]; then
        ENV_SOURCED_KEYS="$ENV_SOURCED_KEYS $key"
    fi
}

persist_line AGENTS "$(IFS=,; echo "${AGENTS[*]}")"
persist_line DEFAULT_CLI "$DEF_CLI"
persist_line ACCOUNTS "$(IFS=,; echo "${PROFILES[*]}")"
persist_line DEFAULT_ACCOUNT "$DEF_PROFILE"
for tv in $TOKEN_VARS; do
    eval "tval=\${$tv:-}"
    [ -n "$tval" ] && persist_line "$tv" "$tval"
done
[ "${#CLI_MAP[@]}"      -gt 0 ] && persist_line AGENT_CLIS "$(IFS=,; echo "${CLI_MAP[*]}")"
[ "${#PROFILE_MAP[@]}"  -gt 0 ] && persist_line AGENT_PROFILES "$(IFS=,; echo "${PROFILE_MAP[*]}")"
if [ "${#PROVIDER_MAP[@]}" -gt 0 ] && [ -n "$LOCAL_URL" ]; then
    persist_line AGENT_PROVIDERS "$(IFS=,; echo "${PROVIDER_MAP[*]}")"
    persist_line PROVIDER_NAME "$EP_NAME"
    PROVIDER_KEY_PREFIX="$(provider_key_prefix "$EP_NAME")"
    persist_line "${PROVIDER_KEY_PREFIX}_URL" "$LOCAL_URL"
    persist_line "${PROVIDER_KEY_PREFIX}_MODEL" "$LOCAL_MODEL"
    persist_line "${PROVIDER_KEY_PREFIX}_TOKEN" "local"
    persist_line "${PROVIDER_KEY_PREFIX}_KIND" "$LOCAL_KIND"
fi
[ -n "$TELEGRAM_BOT_TOKEN" ] && persist_line TELEGRAM_BOT_TOKEN "$TELEGRAM_BOT_TOKEN"
[ -n "$TELEGRAM_CHAT_ID" ] && persist_line TELEGRAM_CHAT_ID "$TELEGRAM_CHAT_ID"
[ "$TELEGRAM_VOICE" = "1" ] && persist_line TELEGRAM_VOICE "1"
[ -n "$API_TOKEN" ] && persist_line API_TOKEN "$API_TOKEN"

if [ -n "$ENV_SOURCED_KEYS" ]; then
    echo "Persisting from live environment:${ENV_SOURCED_KEYS}"
fi
printf '%s\n' "${PERSIST_LINES[@]}" | "$PYTHON" -m services.tenant_config set "$TENANT"

# Re-read what was just persisted -- the hire step below uses the
# *_EXISTING variables regardless of whether this run went through the
# wizard or not, so they must reflect whatever was just written, not
# whatever pre-wizard values were read at the top of this script.
CLI_MAP_EXISTING="$(TENANT_ENV_GET AGENT_CLIS "")"
PROFILE_MAP_EXISTING="$(TENANT_ENV_GET AGENT_PROFILES "")"
PROVIDER_MAP_EXISTING="$(TENANT_ENV_GET AGENT_PROVIDERS "")"

echo
printf '  %-16s %-8s %-10s\n' AGENT CLI ACCOUNT
for a in "${AGENTS[@]}"; do
    printf '  %-16s %-8s %-10s\n' "$a" "$(mget CLI "$a")" "$(mget PROF "$a")"
done
echo
if [ -n "$TELEGRAM_BOT_TOKEN" ] && [ -n "$TELEGRAM_CHAT_ID" ]; then
    voice_note=""
    [ "$TELEGRAM_VOICE" = "1" ] && voice_note=" (voice replies enabled)"
    echo "  Telegram bot: enabled, chat id ${TELEGRAM_CHAT_ID}${voice_note}"
else
    echo "  Telegram bot: not enabled"
fi
echo

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

# 2.7. Install the claude context-usage statusline for the default account.
# Profiled accounts get it at hire time instead (their config dir doesn't
# exist yet, until an agent using that profile is actually hired).
echo "Installing claude statusline (context-usage progress bar)..."
"$PYTHON" -m services.claude_statusline "$HOME/.claude"
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

# 6. Start required daemons (h-mesh-switch, h-mesh-tmux-reconciler,
#    h-mesh-watchdog, plus api/telegram_bot/session once Telegram is
#    configured -- see services.daemons.enabled_daemon_modules)
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
from services.daemons import DaemonError, enabled_daemon_modules, merged_daemon_env, start_daemons

env = merged_daemon_env(os.environ["TENANT"])
try:
    start_daemons(
        python=Path(sys.executable), run_dir=Path(os.environ["H_MESH_SETUP_RUN_DIR"]), env=env,
        daemon_modules=enabled_daemon_modules(env),
    )
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
    #
    # ⚠ HIRE_HAD_ERROR/HIRE_ERROR_SUMMARY, checked after this whole section
    # (including the no-roster branch below and the "attach to tmux" lines)
    # -- an honest per-agent dispatch is worthless if the script's own exit
    # code still says success. Reviewer FAILED an earlier version of this
    # branch for exactly that: every "•" line below could say "hire setup
    # error" or "hire failed" and the script still exited 0, so an
    # unattended caller (CI, an installer) saw a clean setup while a
    # requested roster hire was never admitted. A proven rejection (exit 1)
    # counts as an error here too, not just an unenumerated exit -- the
    # operator asked for a roster and did not get it, and that must be
    # detectable without parsing stderr (architect's explicit call: prefer
    # a human occasionally re-running a setup that mostly worked over CI
    # recording a partial roster as success).
    HIRE_HAD_ERROR=0
    HIRE_ERROR_SUMMARY=()
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

            # ⚠ --wait, not a bare exit-code check -- hire's own exit 0
            # without it only proves the StartAgent envelope was durably
            # enqueued (ADMITTED), not that the agent actually registered
            # (CREATED). Printing "hired" off that alone told an operator
            # their office came up when it knew only that a request was
            # accepted -- a real incident, in this exact summary.
            #
            # ⚠ --wait can only prove a real rejection (exit 1) or time out
            # (exit 2) -- it cannot currently prove success, for a brand-new
            # agent or a re-hire of an existing one alike (see
            # modules/office/cli.py's own --wait help text for why: no
            # signal anywhere ties a *successful* StartAgent back to the
            # specific request that caused it, so confirming one would risk
            # the exact same lie this fixed -- ticket ff53e7e9 tracks the
            # real fix).
            #
            # ⚠ Every status handled EXPLICITLY, none folded into a soft
            # "probably fine" default -- reviewer FAILED an earlier version
            # of this block for exactly that: `if status==1 then failed
            # else requested` treated any OTHER exit (a parse failure, a
            # launcher error, a signal death, an exit code this script has
            # never seen) as if the request had at least been sent, which
            # is the same false-positive shape this whole branch exists to
            # remove, rebuilt one layer out in the wrapper that reads the
            # CLI's own honest exit code. An unenumerated status means "I
            # do not know what happened", never "probably fine".
            HIRE_ARGS=("$a" --cli "$CLI_FOR_A" --wait)
            [ "$PROF_FOR_A" != "default" ] && HIRE_ARGS+=(--profile "$PROF_FOR_A")
            [ -n "$EP_FOR_A" ] && HIRE_ARGS+=(--provider "$EP_FOR_A")
            HIRE_OUTPUT="$(AGENT_NAME=host POD="$POD" TENANT="$TENANT" REDIS_URL="$REDIS_URL" \
                "$PYTHON" -m modules.office.cli hire "${HIRE_ARGS[@]}" 2>&1)"
            HIRE_STATUS=$?
            if [ "$HIRE_STATUS" -eq 1 ]; then
                echo "  • $a: hire failed -- $HIRE_OUTPUT" >&2
                HIRE_HAD_ERROR=1
                HIRE_ERROR_SUMMARY+=("$a: hire failed (rejected -- see the line above for detail)")
            elif [ "$HIRE_STATUS" -eq 2 ]; then
                echo "  • $a: requested ($CLI_FOR_A${PROF_FOR_A:+, account=$PROF_FOR_A}${EP_FOR_A:+, provider=$EP_FOR_A}) -- no rejection seen; run 'office status' if you want to confirm it came up"
            elif [ "$HIRE_STATUS" -eq 0 ]; then
                # Not currently reachable via --wait (see modules/office/
                # cli.py's own contract) -- kept as its own explicit case,
                # not folded into the error branch below, so a future
                # attributable-success signal can land here without this
                # dispatch silently mis-routing it.
                echo "  • $a: hired ($CLI_FOR_A${PROF_FOR_A:+, account=$PROF_FOR_A}${EP_FOR_A:+, provider=$EP_FOR_A})"
            else
                echo "  • $a: hire setup error (unexpected exit $HIRE_STATUS, not admitted or rejected -- treat as not sent) -- $HIRE_OUTPUT" >&2
                HIRE_HAD_ERROR=1
                HIRE_ERROR_SUMMARY+=("$a: hire setup error (unexpected exit $HIRE_STATUS -- see the line above for detail)")
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

    # ⚠ Checked last, after every summary and instruction above has already
    # printed -- the roster loop never aborts early on an individual
    # failure (a later agent's hire is still attempted), but the script's
    # OWN exit code must not say success when the roster it was asked to
    # produce is incomplete. See the HIRE_HAD_ERROR comment above the
    # roster loop for why both a proven rejection and an unenumerated exit
    # count here.
    if [ "$HIRE_HAD_ERROR" -eq 1 ]; then
        echo >&2
        echo "One or more roster hires did not succeed:" >&2
        for line in "${HIRE_ERROR_SUMMARY[@]}"; do
            echo "  • $line" >&2
        done
        echo "Re-run setup.sh, or hire the missing agent(s) manually, once you've resolved the cause above." >&2
        exit 1
    fi
fi
