# agentlifecycle

A library, not a port. Generic start/stop/pause/resume logic for an agent:
writes registry/policy/HMAC-key desired state, and delegates the actual
mechanism (a tmux window, an openshell sandbox, whatever) to callbacks the
caller supplies. This module never imports tmux, openshell, or any other
port-specific module -- a module that needs lifecycle behavior imports
`lifecycle.py` and passes its own real callbacks in.

| file | what it holds |
|---|---|
| `lifecycle.py` | `start_agent`/`stop_agent`/`pause_agent`/`resume_agent`, callback-driven, no port of its own |
