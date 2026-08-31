# h-mesh

An agentic CLI framework where agents address each other directly, by name, and a single switch forwards every message to its destination. Work is tracked on a shared board per agent: pull a ticket, work it, mark it done.

Each agent is a real, stateful terminal session -- not a one-shot API call. It runs its own CLI (Claude, Codex, or Gemini/agy) in a live TUI, with conversation history that persists and resumes across restarts.

The design borrows its layering from networking, not just its name -- a bus layer that only knows envelopes and queues, a switch layer that forwards by address without reading payloads, and an edge layer where each agent's actual delivery -- a terminal, a mailbox, or another destination-specific target -- lives. Same separation of concerns Ethernet uses between physical transport, switching, and the endpoint.

Every endpoint is a module, and every module owns its own port -- the piece that assembles an outgoing envelope and disassembles an incoming one into that endpoint's specific action.

A Telegram interface is included, talking to the framework over the same REST API any external client would use -- a separate module, not a special case.

Every hop a message takes is logged, the same way a real packet's path can be traced -- nothing moves silently.

## Bootstrap script

`./setup.sh` wraps the manual install below into one step for a fresh host or
pod: it installs h-mesh into a venv, persists that venv's `bin` dir on `PATH`
(via `~/.bashrc` and `~/.profile`, so it's on `PATH` for every future
shell/pane, not just the one running setup.sh), seeds the registry's fixed
lifecycle participants (`host` -> office, `api` -> api) for the given
pod/tenant, and starts the `h-mesh-switch` and `h-mesh-tmux-reconciler`
daemons.

```bash
./setup.sh --pod mypod --tenant mytenant
```

Run `./setup.sh --help` for the full set of flags (Redis URL, tmux session
name and socket, an existing venv, `--skip-install`, `--no-daemons`, etc.).
Daemon logs and PID files are written under `$H_MESH_RUN_DIR`
(default `~/.h-mesh/run/<tenant>`). Once daemons are running, hire the first
agent as `host`:

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
for the full list). It also re-persists the venv bin dir on `PATH`, so
running it repairs an install that predates that fix.

⚠ **Known limit:** a hired agent's tmux pane inherits its environment once,
at creation. Upgrading restarts h-mesh's own daemons and reinstalls the code
they run, but it cannot reach into an already-hired agent's live pane and
refresh its env -- that agent keeps whatever it started with until it's
re-hired or its window is otherwise recreated. This is deliberate: forcing
that refresh would mean killing live agent sessions on every upgrade.

`h-mesh-start` (`services.daemons`) starts the same daemons without pulling
or reinstalling -- useful after a crash, or on a host where they aren't
running yet. It's also duplicate-safe: a daemon it finds already alive (via
its pidfile) is left running rather than started a second time.

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
