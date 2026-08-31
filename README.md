# h-mesh

An agentic CLI framework where agents address each other directly, by name, and a single switch forwards every message to its destination. Work is tracked on a shared board per agent: pull a ticket, work it, mark it done.

Each agent is a real, stateful terminal session -- not a one-shot API call. It runs its own CLI (Claude, Codex, or Gemini/agy) in a live TUI, with conversation history that persists and resumes across restarts.

The design borrows its layering from networking, not just its name -- a bus layer that only knows envelopes and queues, a switch layer that forwards by address without reading payloads, and an edge layer where each agent's actual delivery -- a terminal, a mailbox, or another destination-specific target -- lives. Same separation of concerns Ethernet uses between physical transport, switching, and the endpoint.

Every endpoint is a module, and every module owns its own port -- the piece that assembles an outgoing envelope and disassembles an incoming one into that endpoint's specific action.

A Telegram interface is included, talking to the framework over the same REST API any external client would use -- a separate module, not a special case.

Every hop a message takes is logged, the same way a real packet's path can be traced -- nothing moves silently.

## Bootstrap script

`./setup.sh` wraps the manual install below into one step for a fresh host or
pod: it installs h-mesh into a venv, seeds the registry's fixed lifecycle
participants (`host` -> office, `api` -> api) for the given pod/tenant, and
starts the `h-mesh-switch` and `h-mesh-tmux-reconciler` daemons.

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

The `h-mesh-` prefix is intentional: h-mesh can share an installation prefix
with another application without replacing its live `office` executable.

Each process reads its deployment configuration from environment variables;
the module READMEs document the required variables and external services.
