# webui module

The `webui` port is a small, push-only WebSocket participant for live mesh
streams. It accepts `Message` and `Progress` envelopes and broadcasts the full
JSON envelope to every connected browser tab. Keeping the complete envelope
preserves its source, destination, stream/correlation identifiers, and the
producer-owned payload shape.

`claude_sdk` emits one `Progress` envelope for each query hop, correlated to
and replying to the originating `Message`. Its payload is `{"event": "..."}`
with `evidence` and/or `reason` included only when that hop supplies them. The
known event names are `claude_sdk_query_started`, `claude_sdk_turn`,
`claude_sdk_query_finished`, and the forward-compatible `claude_sdk_hop`.
This relay does not validate that producer-owned vocabulary; it passes it
through unchanged.

Run it with `h-app` on `PYTHONPATH`:

```sh
POD=local TENANT=demo python -m modules.webui.port browser
```

It listens on `127.0.0.1:8765` by default. `WEBUI_HOST` and `WEBUI_PORT` may
override that address. Register `browser` directly in the tenant registry with
port type `webui`; there is no `office hire` integration yet.

The process maintains browser connections while its delivery thread drains
Redis under the shared per-agent delivery lock. A paused marker leaves ingress
queued. Browser-to-server frames are ignored: this module is a relay, not the
full operator console in `clients/web/`.

Start the module before sending live traffic and leave it running. The switch's
ordinary per-envelope kicks may start contenders, but they fail at the listening
socket before taking envelope custody; the established relay remains the sole
consumer.

Kinds other than `Message` and `Progress` are intentionally omitted from the
opener map. `core.channels.receive` therefore moves them cleanly to the agent's
dead-letter queue instead of crashing the process.
