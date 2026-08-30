# Template port

`TemplatePort` is a minimal copyable edge adapter. It shows the four operations
every port owns without adding endpoint-specific behavior to core:

1. `register(agent, port_type)` publishes the participant in the registry.
2. `send(destination, payload, kind)` writes through `core.channels.send`.
3. `receive(openers)` consumes through `core.channels.receive` and dispatches
   by envelope kind.
4. `cleanup()` removes the registry row and this port's queue state.

Always clean up in `finally`. Run the real-Redis example with:

```sh
REDIS_URL=redis://127.0.0.1:6379/0 python3 h-app/tools/templates/demo_template_port.py
```

Real modules should copy the mechanics and replace only the opener behavior at
the far edge.
