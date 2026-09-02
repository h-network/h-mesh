<p align="center">
  <img src="docs/assets/banner.svg" alt="H-MESH // agentic office framework // h-network" width="600">

  [![LICENSE: POLYFORM NC 1.0](docs/assets/badges/license.svg)](LICENSE)
  ![AGENTS: CLAUDE CODEX AGY](docs/assets/badges/agents.svg)
  ![TMUX: CONFIGURED](docs/assets/badges/tmux.svg)
  ![PYTHON: 3.12+](docs/assets/badges/python.svg)
  ![REDIS: REQUIRED](docs/assets/badges/redis.svg)
</p>

# h-mesh

An agentic CLI framework where agents address each other directly, by name, and a single switch forwards every message to its destination. Work is tracked on a shared board per agent: pull a ticket, work it, mark it done.

Each agent is a real, stateful terminal session -- not a one-shot API call. It runs its own CLI (Claude, Codex, or Gemini/agy) in a live TUI, with conversation history that persists and resumes across restarts.

The design borrows its layering from networking, not just its name -- a bus layer that only knows envelopes and queues, a switch layer that forwards by address without reading payloads, and an edge layer where each agent's actual delivery -- a terminal, a mailbox, or another destination-specific target -- lives. Same separation of concerns Ethernet uses between physical transport, switching, and the endpoint.

Every endpoint is a module, and every module owns its own port -- the piece that assembles an outgoing envelope and disassembles an incoming one into that endpoint's specific action.

A Telegram interface is included, talking to the framework over the same REST API any external client would use -- a separate module, not a special case.

Every hop a message takes is logged, the same way a real packet's path can be traced -- nothing moves silently.

## Quick install

On a fresh host, one command clones h-mesh and hands off straight to
`setup.sh`:

```bash
curl -fsSL https://raw.githubusercontent.com/h-network/h-mesh/main/install.sh | sh
```

It clones into `~/.local/share/h-mesh` by default (`H_MESH_INSTALL_DIR` to
choose another location), or updates an existing checkout there if one
already exists. That's a hidden, machine-managed location on purpose --
`~/h-mesh` itself is reserved for agent working directories (see
`setup.sh`'s wizard), not the source checkout. Extra arguments pass straight through to `setup.sh` -- e.g. to skip
the wizard:

```bash
curl -fsSL https://raw.githubusercontent.com/h-network/h-mesh/main/install.sh \
  | sh -s -- --pod mypod --tenant mytenant --non-interactive
```

This is a companion to cloning it yourself, not a replacement -- see below
for the manual clone-then-`setup.sh` path, and [Host installation](#host-installation)
for installing just the Python package without any of the bootstrap steps.

## Bootstrap script

Cloning it yourself first works exactly the same way, if you'd rather skip
the one-liner above:

```bash
git clone https://github.com/h-network/h-mesh.git
cd h-mesh
./setup.sh
```

`./setup.sh` bootstraps a fresh host or pod (already have a checkout? this
is what `install.sh` above hands off to). Run at a real terminal with no
flags, it's an interactive wizard: pod/tenant name, how many agents and
their names (`architect` is agent #1 by default), which CLI each runs
(claude/codex/agy), account setup if more than one credential is needed,
optional local model provider config (endpoint URL and model id, verified
with a real probe against the endpoint before it's accepted), and optional
Telegram bot config (bot token, chat ID, spoken voice replies). Re-running
it never silently wipes a prior answer -- blank at a prompt keeps whatever's
already there.

Piped, scripted, or passed `--non-interactive`, it never prompts -- flags,
environment, and whatever's already been configured for that tenant are all
it uses. Per-agent exceptions (`AGENT_CLIS`, `AGENT_PROFILES`,
`AGENT_PROVIDERS`) are each set as a comma-separated list of `agent=value`
pairs, e.g. `AGENT_CLIS=worker1=codex,worker2=agy` -- exactly one `=` per
pair, agent name on the left. A malformed entry (wrong separator, a missing
value) is a hard error, not a silently-ignored one: this matters most for
`AGENT_PROVIDERS`, since an agent with no override that setup.sh recognizes
runs against the real vendor API, not the local provider you configured for
it -- a typo there is real spend, not just a wrong setting. Either way it
also:

- verifies/auto-installs system dependencies (`redis-server`, `python3-venv`,
  and `h-agent` itself via its own installer) -- skip with `--skip-deps`
- installs h-mesh into a venv and persists that venv's `bin` dir on `PATH`
  (via `~/.bashrc` and `~/.profile`, so it's on `PATH` for every future
  shell/pane, not just the one running setup.sh)
- installs h-mesh's default `~/.tmux.conf` (mouse mode, status bar, pane
  borders -- the same UX the container base image otherwise bakes in, for a
  bare host that has none; never overwrites an existing `~/.tmux.conf`)
- installs a context-usage statusline (a progress bar showing how full the
  model's context window is) for the default account's claude config;
  profiled accounts get it too, at hire time, since that account's config
  dir doesn't exist until then. codex/agy agents are untouched -- they have
  their own statusline mechanisms, or none
- seeds the registry's fixed lifecycle participants (`host` -> office,
  `api` -> api) for the given pod/tenant
- starts the `h-mesh-switch`, `h-mesh-tmux-reconciler` and `h-mesh-watchdog`
  daemons (always-on -- watchdog needs no credentials, and every
  multi-agent office wants presence sampling and stall/silence alerting
  from the first hire on), and -- only if a Telegram bot was configured --
  the REST API, Telegram bot, and session daemons too (the bot needs the
  API to talk to and session to back its `/watch` command; there's no
  separate "enable the API" question, a configured bot enables all three).
  All duplicate-safe: a re-run against an already-running install restarts
  nothing that's already up, and a daemon disabled since a previous run
  gets stopped, not orphaned
- hires every agent from the wizard's roster that isn't already running

```bash
./setup.sh                          # interactive wizard, at a terminal
./setup.sh --pod mypod --tenant mytenant --non-interactive   # scripted, no prompts
```

Run `./setup.sh --help` for the full set of flags (Redis URL, tmux session
name and socket, an existing venv, `--skip-install`, `--skip-deps`,
`--no-daemons`, `--non-interactive`, etc.). Daemon logs and PID files are
written under `$H_MESH_RUN_DIR` (default `~/.h-mesh/run/<tenant>`).

A host that never used the wizard (flags only, no roster ever configured)
keeps today's manual-hire behavior:

```bash
export AGENT_NAME=host POD=mypod TENANT=mytenant
h-mesh-office hire <agent-name>
```

Hiring starts a fresh CLI session by default, even when that agent name has
local session history. Pass `--resume` to opt into restoring prior history.

## Upgrading and restarting daemons

`h-mesh-upgrade` (`services.upgrade`) updates an existing install in place --
`git pull --ff-only`, reinstall, then cleanly restart the same daemons
setup.sh started (stop via SIGTERM/SIGKILL fallback, then start fresh with
the current environment). It won't double-start daemons against an
already-running install: it always stops first.

```bash
h-mesh-upgrade --pod mypod --tenant mytenant
```

Same pod/tenant/redis-url/session/tmux-tmpdir/tmux-socket/venv flags as
setup.sh, plus `--skip-install` and `--skip-pull` (`h-mesh-upgrade --help`
for the full list). Pod and tenant must be valid non-empty h-mesh names; an
invalid value is rejected before the upgrade stops or starts anything. It
also re-persists the venv bin dir on `PATH`,
re-installs the default `tmux.conf`, and re-installs the default account's
statusline, so running it repairs an install that predates any of those
fixes.

⚠ **Known limit:** a hired agent's tmux pane inherits its environment once,
at creation. Upgrading restarts h-mesh's own daemons and reinstalls the code
they run, but it cannot reach into an already-hired agent's live pane and
refresh its env -- that agent keeps whatever it started with until it's
re-hired or its window is otherwise recreated. This is deliberate: forcing
that refresh would mean killing live agent sessions on every upgrade.

`h-mesh-start` (`services.daemons`) starts the same daemons without pulling
or reinstalling -- useful after a crash, or on a host where they aren't
running yet. It's also duplicate-safe: a daemon it finds already alive (via
its authenticated pidfile) is left running rather than started a second time.
Pidfiles are paired with the Linux process start time, and daemon management
opens a Linux pidfd before authentication then signals through that same process
handle, so a reused numeric PID cannot redirect a signal to an unrelated
process. If ownership cannot be
proved, daemon management fails closed and reports the condition without
signalling; a stale PID that demonstrably belongs to another process is treated
as the normal “daemon not running” state. A start is
atomic with respect to processes that invocation creates: they are reported
as started only after surviving the startup health window, and if any one
fails, all newly created siblings are stopped. Processes found running before
the invocation are never part of that rollback.

```bash
h-mesh-start --pod mypod --tenant mytenant
```

## Host installation

h-mesh is installed from the repository root. An editable install is useful for
development; a regular install uses the same package metadata for production:

```bash
python -m venv .venv
.venv/bin/python -m pip install -e .
```

Contributors running the test suite should install the test extra with
`.venv/bin/python -m pip install -e '.[test]'`.

After activating the project virtual environment, run the complete suite with
the canonical runner. The runner resolves the repository root itself, so the
command works from any working directory:

```bash
python -m tools.run_tests
```

The configured test path covers the entire `h-app` source tree, including
tests colocated under `clients/web`. The runner asserts a minimum collection
count before executing pytest, so under-collection fails loudly while normal
test additions require no maintenance. If tests are intentionally removed,
update `EXPECTED_MINIMUM_TEST_COUNT` in the runner.
Passing an explicit path such as `pytest tests/` overrides configured discovery
and intentionally runs only the selected subtree; do not use a narrowed path
as evidence that the complete suite passes.

Use an isolated environment when developing or validating an install. In
particular, do not install h-mesh into an environment that provides another
application's live commands; keeping prefixes separate prevents either
application from replacing the other's executables.

The install makes the top-level `core`, `lib`, `modules`, and `services`
packages importable independently of the current working directory. It also
provides these process entry points:

- `h-mesh-switch` — the core switch daemon
- `h-mesh-api` — the REST API daemon
- `h-mesh-openshell-port AGENT` — direct OpenShell delivery-port invocation
- `h-mesh-session` — the tmux control-mode session daemon
- `h-mesh-tmux-reconciler` — the tmux registry reconciler daemon
- `h-mesh-watchdog` — the delivery and presence watchdog daemon
- `h-mesh-tmux-port AGENT` — direct tmux delivery-port invocation
- `h-mesh-office` — the operator CLI
- `h-mesh-clone-to-all` — the standalone clone-to-all compatibility command
- `h-mesh-upgrade` — pull, reinstall, and cleanly restart the daemons setup.sh started
- `h-mesh-start` — start those same daemons if not already running, without pulling or reinstalling

The `h-mesh-` prefix is intentional: h-mesh can share an installation prefix
with another application without replacing its live `office` executable.

Each process reads its deployment configuration from environment variables;
the module READMEs document the required variables and external services.

## License

PolyForm Noncommercial 1.0.0 -- see [`LICENSE`](LICENSE). Third-party
dependency licenses are credited in [`NOTICE`](NOTICE). "h-mesh" and
"h-network" naming/branding are covered by [`TRADEMARKS.md`](TRADEMARKS.md).
