# h-mesh

An agentic CLI framework where agents address each other directly, by name, and a single switch forwards every message to its destination. Work is tracked on a shared board per agent: pull a ticket, work it, mark it done.

Each agent is a real, stateful terminal session -- not a one-shot API call. It runs its own CLI (Claude, Codex, or Gemini/agy) in a live TUI, with conversation history that persists and resumes across restarts.

The design borrows its layering from networking, not just its name -- a bus layer that only knows envelopes and queues, a switch layer that forwards by address without reading payloads, and an edge layer where each agent's actual delivery -- a terminal, a mailbox, or another destination-specific target -- lives. Same separation of concerns Ethernet uses between physical transport, switching, and the endpoint.

Every endpoint is a module, and every module owns its own port -- the piece that assembles an outgoing envelope and disassembles an incoming one into that endpoint's specific action.

A Telegram interface is included, talking to the framework over the same REST API any external client would use -- a separate module, not a special case.

Every hop a message takes is logged, the same way a real packet's path can be traced -- nothing moves silently.

## Host installation

h-mesh is installed from the repository root. An editable install is useful for
development; a regular install uses the same package metadata for production:

```bash
python -m pip install -e .
```

The install makes the top-level `core`, `lib`, `modules`, and `services`
packages importable independently of the current working directory. It also
provides these process entry points:

- `h-mesh-switch` — the core switch daemon
- `h-mesh-api` — the REST API daemon
- `h-mesh-openshell-port AGENT` — direct OpenShell delivery-port invocation
- `h-mesh-session` — the tmux control-mode session daemon
- `h-mesh-tmux-reconciler` — the tmux registry reconciler daemon
- `h-mesh-tmux-port AGENT` — direct tmux delivery-port invocation
- `office` — the operator CLI (`cloneToAll` is also installed for compatibility)

Each process reads its deployment configuration from environment variables;
the module READMEs document the required variables and external services.
