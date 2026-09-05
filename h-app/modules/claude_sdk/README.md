# claude_sdk module

The Claude Agent SDK one-off delivery port, for agents whose `port_type` is
`claude_sdk`. Each `Message` triggers exactly one `query()` call against the
vendor SDK -- no `ClaudeSDKClient`, no `continue`/`resume`/`fork_session`,
and the opener itself sends the reply, the same reply-inside-the-opener
shape `modules/openshell/port.py` uses for the same reason (no human or
long-lived process on the other end to notice a paste and answer later).

Run one delivery kick with `h-app` on `PYTHONPATH`:

```sh
python -m modules.claude_sdk.port AGENT
```

The runner reads `POD`, `TENANT`, and optionally `REDIS_URL`, then uses the
shared delivery lock before draining one agent's ingress. There is no hire
path yet -- this is registered directly in the tenant registry (`port_type`
`claude_sdk`), not through `office hire`.

## Credentials and options

Profile-scoped credentials resolve the same way a hired tmux agent's do (see
`lib/profile_env.py`): an agent's `profile` Redis resource selects
`CLAUDE_CONFIG_DIR`/`CODEX_HOME` and a `CLAUDE_OAUTH_TOKEN_<PROFILE>`-suffixed
token, merged into the one-shot `query()` call's own environment, never into
this port process's own `os.environ`.

An operator-configured, per-agent `sdk-options` Redis resource (a JSON
object) is merged onto `ClaudeAgentOptions`, restricted to an explicit
allowlist (`system_prompt`, `allowed_tools`, `disallowed_tools`,
`permission_mode`, `max_turns`, `max_budget_usd`, `model`, `fallback_model`,
`cwd`, `add_dirs`, `betas`, `setting_sources`) -- `env` and any
live-object/callable field are excluded, since `env` is owned exclusively by
the profile resolution above and nothing else here is JSON-expressible.
Nothing writes this resource yet (no `office hire`/CLI flag) -- set it
directly in Redis until that lands.

## Memory: context-addressed, optional by default

A `Message` payload's `context` field (any value, not derived from `source`
or any envelope field) names a hot-tier memory conversation
(`lib/chat_memory.py`'s `ChatMemory`, TTL-evicted, count-trimmed). The same
`context` on a later call recalls that conversation's recent turns, prepended
as prompt text before the `query()` call (`lib/chat_cycle.py`'s
`run_chat_cycle`). No `context` at all means a genuine one-off: no memory
read, no memory write -- not merely a context that happens not to repeat.

```json
{"text": "...", "context": "bgp-65001"}
```

A `ListContexts` envelope (any payload) replies with
`{"contexts": [...]}` -- every context this agent currently has live memory
for. The same data is also reachable externally via
`GET /agents/{agent}/contexts` (see `modules/api/README.md`), and from a
terminal via `office send -a AGENT --context CONTEXT TEXT` /
`office contexts -a AGENT` (see `modules/office/README.md`).

Long-term/semantic memory -- what, if anything, happens to a turn once its
TTL elapses -- is out of scope here by design; see `lib/chat_memory.py`'s own
module docstring.

## Live progress: `live_to`/`live_cc_source`

A `Message` payload's `live_to` (a destination name, typically a
`modules/webui`-registered agent) additionally fans every hop of the
underlying `query()` call out live as a `Progress` envelope -- one per
`SystemMessage`/`AssistantMessage`/`ResultMessage`/unrecognized hop, payload
`{"event": "...", "detail": "..."}` (the same vocabulary `_log_hop`'s own
`log_record` calls use, e.g. `claude_sdk_query_started`/`claude_sdk_turn`/
`claude_sdk_query_finished`). `correlation_id`/`in_reply_to` both anchor to
the incoming Message's own `stream_id`, same as the final reply, so a viewer
can group every hop and the eventual reply under one id. `live_cc_source`
(bool) also sends the same hops back to whoever sent the `Message`.

```json
{"text": "...", "live_to": "webui-1", "live_cc_source": true}
```

Neither field set is today's behavior exactly, byte-for-byte unchanged: this
is additive to, never a replacement for, the single final `Message` reply
every delivery already sends. A `live_to` that `send()` rejects (unregistered
name, policy denial) is logged and swallowed, not raised -- a live-viewing
convenience failing is not a reason to fail the delivery it was only ever
describing.

## Reply shape

Non-blank results are sent back as a `Message` to the envelope's source,
`correlation_id` and `in_reply_to` both set to the incoming `stream_id` --
exact by construction, the same shape `modules/openshell/port.py` uses and
for the same reason (no interactive agent here to cooperate with an opt-in
`office send --reply-to`). Delivery is recorded via
`lib/reply_correlation.py`'s `record_delivered()` only after the `query()`
call actually returns, so an `in_reply_to` claim can't validate for a call
that never completed.
