# Inter-Office Communication PoC Runbook

This runbook walks through the end-to-end inter-office messaging proof of concept on the `h-lab` testbed (`laba` and `labb`) with exact commands and expected output.

> **Transport security default:** TLS (`https://`) is the supported default for inter-office communication. Plaintext `http://` to a non-loopback host is refused unless both the receiving API has `H_MESH_ALLOW_PLAINTEXT=1` set and the sending lead explicitly passes `--allow-plaintext` (or `H_MESH_ALLOW_PLAINTEXT=1`) to `office-add` for an isolated lab testbed.

---

## 0. Lab Topology (`h-lab` @ `172.16.0.201`)

Two containerised offices run alongside the untouched default office on `h-lab@172.16.0.201`, both backed by the local vLLM model (`nemotron-3.5-lightning-30b-a3b-nvfp4` at `http://172.16.0.11:8000/v1`):

| Office (`POD` / `TENANT`) | Compose project | Checkout | Host API port (`-> :8080`) | Host session port (`-> :8081`) | Enrolled tmux agents |
|---|---|---|---|---|---|
| `laba` / `default` | `h-mesh-laba-default` | `/home/h-lab/lab/laba` | `18080` (`http://172.16.0.201:18080`) | `18081` | `architect` (lead), `worker` |
| `labb` / `default` | `h-mesh-labb-default` | `/home/h-lab/lab/labb` | `18082` (`http://172.16.0.201:18082`) | `18083` | `architect` (lead), `worker` |

To attach interactively or manage either office container via `docker compose` on `h-lab`:

```bash
# Attach to laba / labb (tmux session: default)
/home/h-lab/lab/laba/offices/laba/default/attach.sh
/home/h-lab/lab/labb/offices/labb/default/attach.sh

# Start / recreate laba (pass H_MESH_TENANT_ENV_FILE alongside --env-file when driving compose.yaml directly)
H_MESH_TENANT_ENV_FILE=/home/h-lab/lab/laba/offices/laba/default/.env \
  docker compose -p h-mesh-laba-default \
  -f /home/h-lab/lab/laba/container/compose.yaml \
  -f /home/h-lab/lab/laba/container/compose.ports.yaml \
  --env-file /home/h-lab/lab/laba/offices/laba/default/.env up -d --build

# Start / recreate labb
H_MESH_TENANT_ENV_FILE=/home/h-lab/lab/labb/offices/labb/default/.env \
  docker compose -p h-mesh-labb-default \
  -f /home/h-lab/lab/labb/container/compose.yaml \
  -f /home/h-lab/lab/labb/container/compose.ports.yaml \
  --env-file /home/h-lab/lab/labb/offices/labb/default/.env up -d --build

# Tail logs or stop (keeping state)
H_MESH_TENANT_ENV_FILE=/home/h-lab/lab/laba/offices/laba/default/.env \
  docker compose -p h-mesh-laba-default \
  -f /home/h-lab/lab/laba/container/compose.yaml \
  -f /home/h-lab/lab/laba/container/compose.ports.yaml \
  --env-file /home/h-lab/lab/laba/offices/laba/default/.env logs -f
H_MESH_TENANT_ENV_FILE=/home/h-lab/lab/laba/offices/laba/default/.env \
  docker compose -p h-mesh-laba-default \
  -f /home/h-lab/lab/laba/container/compose.yaml \
  -f /home/h-lab/lab/laba/container/compose.ports.yaml \
  --env-file /home/h-lab/lab/laba/offices/laba/default/.env down
```

> **Note (`H_MESH_TENANT_ENV_FILE` and `services.api` startup):**
> - `container/compose.yaml` resolves the container's `env_file` from `${H_MESH_TENANT_ENV_FILE:-.env}`. `container/bootstrap.sh` exports `H_MESH_TENANT_ENV_FILE="$ENV_FILE"` automatically; when invoking `docker compose` directly from the CLI, always set `H_MESH_TENANT_ENV_FILE=<env-path>` alongside `--env-file <env-path>` (the generated `SUMMARY.md` snippets currently omit `H_MESH_TENANT_ENV_FILE`).
> - With `origin/setup-agent/api-daemon-without-telegram` (`3bc4e96`, merged in `12d1a8a`), a non-empty `API_TOKEN` in the office `.env` automatically enables and starts `services.api` on container boot via `services.daemons.enabled_daemon_modules()` without requiring Telegram credentials or a manual `docker exec` start step.

---

## 1. In `labb` (as lead): Grant a Cooperation Token

Inside `labb`'s `architect` pane (or via `docker exec -e AGENT_NAME=architect h-mesh-labb-default-h-mesh-1`), grant a cooperation token for the participant name `laba` will use (`laba-peer`):

```bash
h-mesh-office coop-grant laba-peer
```

Expected output (`stdout` + one-time notice on `stderr`):

```text
participant: laba-peer
token-id: 8f245a94570a6f52
token: coop.8f245a94570a6f52.<43-char-url-safe-secret>
This token is shown once. Give it to the other office's lead (office-add ... --token-stdin); revoke with: office coop-revoke 8f245a94570a6f52
```

Verify the token metadata (`coops` never prints the secret, and `coop-tokens` in Redis stores only its SHA-256 digest) and confirm `laba-peer` was enrolled as an `api` interface participant:

```bash
h-mesh-office coops
h-mesh-office peers -i
```

Expected output:

```text
8f245a94570a6f52	laba-peer	Message	active	1793580872.410281
worker
interfaces (api/office -- recognized, not tmux colleagues): api (api), host (office), laba-peer (api)
```

---

## 2. In `laba` (as lead): Add `labb` to the Address Book (`--token-stdin`)

In `laba`'s `architect` pane, register `labb` by piping the full `coop.<token-id>.<secret>` token on standard input (`--token-stdin` is mandatory; tokens are never accepted on argv):

```bash
printf '%s\n' 'coop.8f245a94570a6f52.<43-char-url-safe-secret>' | \
  h-mesh-office office-add labb http://172.16.0.201:18082 \
    --participant laba-peer \
    --token-stdin \
    --allow-plaintext
```

Expected output:

```text
office: warning: http://172.16.0.201:18082 is plaintext http; the token travels in the clear
added office labb -> http://172.16.0.201:18082 (as laba-peer)
```

Verify the address book (`offices` reads only the non-secret `offices` hash) and confirm the local `relay (api)` participant was automatically enrolled for inbound replies:

```bash
h-mesh-office offices
h-mesh-office peers -i
```

Expected output:

```text
labb	http://172.16.0.201:18082	as laba-peer	added by architect
worker
interfaces (api/office -- recognized, not tmux colleagues): api (api), host (office), relay (api)
```

---

## 3. In `laba` (`architect`): Send a Message to `labb/worker`

From `laba`'s `architect` pane, send a message directly to `worker` in `labb`:

```bash
h-mesh-office send --office labb -a worker \
  "Hello from laba architect! Please reply using the h-mesh-office send -a laba-peer --reply-to <id> command in the hint line."
```

Expected output (exit code `0`):

```text
sent to labb/worker: 123 bytes (f0986bceba4f46c6843c68bb800a04a0)
```

- The CLI itself `POST`s to `http://172.16.0.201:18082/agents/worker/envelopes` with `Authorization: Bearer coop.<id>.<secret>` and body `{"text": "...", "as": "laba-peer"}`.
- On HTTP `202`, `send --office` records a 24-hour reply-routing receipt in `laba`'s Redis at `pod:laba:tenant:default:xsent.s<stream_id>`:
  ```json
  {"sender": "architect", "sender_incarnation": "990d5c26592d41e7a780fb65598d4bda", "office": "labb", "agent": "worker", "ts": "2026-11-01T23:44:34.619Z"}
  ```
- Exit codes for `send --office`:
  - `0`: accepted by remote office (`202` with valid `stream_id`).
  - `1`: refused, provably nothing sent (bad args, unknown office, missing token, connection refused/DNS failure, or HTTP `4xx`).
  - `3`: outcome unknown (timeout, reset, redirect, HTTP `5xx`, or malformed `2xx` body — do not blindly retry).

---

## 4. In `labb` (`worker`): Receive and Reply with `--reply-to`

In `labb`'s `worker` tmux pane, the message arrives from `laba-peer` with an automatic `--reply-to` correlation hint (because `laba-peer` is an `api`-type participant):

```text
❯ [message from laba-peer] Hello from laba architect! Please reply using the h-mesh-office send -a
  laba-peer --reply-to <id> command in the hint line.
  [reply to laba-peer: h-mesh-office send -a laba-peer --reply-to f0986bceba4f46c6843c68bb800a04a0 "..."]
```

When `labb`'s `worker` executes the reply command:

```bash
h-mesh-office send -a laba-peer --reply-to f0986bceba4f46c6843c68bb800a04a0 \
  "Hello! I've received your message and will respond accordingly."
```

Expected output in `labb/worker`:

```text
sent to laba-peer: 63 bytes (314d65c1fd8e4b1ab5379d0e9c2c3d7a)
```

You can also inspect `laba-peer`'s cooperation mailbox in `labb` directly over HTTP using the cooperation token:

```bash
curl -sS -H "Authorization: Bearer $COOP_TOKEN" \
  http://172.16.0.201:18082/agents/laba-peer/messages
```

Expected output (`in_reply_to` carries the original envelope's `stream_id`):

```json
{
  "agent": "laba-peer",
  "messages": [
    {
      "v": 4,
      "stream_id": "314d65c1fd8e4b1ab5379d0e9c2c3d7a",
      "correlation_id": "44c24640e66b40e38b6f6ce0f6ffdfe3",
      "l2": {"source": "worker", "destination": "laba-peer"},
      "ttl": 15,
      "hops": 1,
      "kind": "Message",
      "ts": "2026-11-01T23:44:36.967Z",
      "l3": {"source": "labb:default:worker", "destination": "labb:default:laba-peer"},
      "payload": {"text": "Hello! I've received your message and will respond accordingly."},
      "in_reply_to": "f0986bceba4f46c6843c68bb800a04a0",
      "cursor": "1793576677259-0"
    }
  ],
  "next_cursor": "1793576677259-0"
}
```

---

## 5. In `laba` (`architect`): Reply Arrives via the Relay

`services.interoffice_relay` in `laba` streams `GET http://172.16.0.201:18082/agents/laba-peer/messages/stream?after=<cursor>`, matches `in_reply_to` (`f0986bceba4f46c6843c68bb800a04a0`) against `pod:laba:tenant:default:xsent.sf0986bceba4f46c6843c68bb800a04a0`, verifies `(office, agent, sender_incarnation)`, admits a local `Message` from `relay` to `architect`, and checkpoints the SSE cursor in `pod:laba:tenant:default:offices.cursor`.

In `laba`'s `architect` tmux pane, the reply appears as:

```text
❯ [message from relay] [from labb/worker] Hello! I've received your message and will respond accordingly.
  [reply to relay: h-mesh-office send -a relay --reply-to 672444a06c81458c874bf8d6d9d80dd2 "..."]
```

> **Note when replying back:** `relay` is the local inbound-only API participant in `laba`. To send another message back to `labb/worker`, always use `h-mesh-office send --office labb -a worker "..."` (not `h-mesh-office send -a relay`).

And in `laba`'s `/home/ubuntu/.h-mesh/run/default/interoffice_relay.log`:

```text
2026-11-01 23:44:37,331 [INFO] modules.interoffice.relay: relay office=labb event=admitted destination=architect stream_id=e4fb44547d724a29bc2fb29385e4b0b3
```

*(If a remote message has no `in_reply_to` or its `xsent` receipt has expired/mismatched, the relay routes it to the local office's `lead`.)*

---

## 6. Security Negatives (Verified Live on `laba` / `labb`)

In the commands below, `$COOP_TOKEN` holds the active `coop.<id>.<secret>` token in shell memory only.

### 6.1 Wrong token -> HTTP `401`

```bash
curl -sS -w "\nHTTP %{http_code}\n" -X POST http://172.16.0.201:18082/agents/worker/envelopes \
  -H "Authorization: Bearer coop.0000000000000000.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" \
  -H "Content-Type: application/json" \
  -d '{"text":"hi","as":"laba-peer"}'
```

Output:

```text
{"detail":"Unauthorized"}
HTTP 401
```

### 6.2 Non-`Message` envelope kinds (`Command`, `StartAgent`, `StopAgent`, `Pause`, `Resume`) -> HTTP `403`

```bash
for KIND in Command StartAgent StopAgent Pause Resume; do
  curl -sS -w " (HTTP %{http_code})\n" -X POST http://172.16.0.201:18082/agents/worker/envelopes \
    -H "Authorization: Bearer $COOP_TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"text\":\"hi\",\"as\":\"laba-peer\",\"kind\":\"$KIND\"}"
done
```

Output:

```text
{"detail":"envelope kind not permitted for a cooperation token"} (HTTP 403)
{"detail":"envelope kind not permitted for a cooperation token"} (HTTP 403)
{"detail":"envelope kind not permitted for a cooperation token"} (HTTP 403)
{"detail":"envelope kind not permitted for a cooperation token"} (HTTP 403)
{"detail":"envelope kind not permitted for a cooperation token"} (HTTP 403)
```

### 6.3 Forbidden destinations (`all`, `host`, `api`) -> refused locally (`exit 1`) and by API (`HTTP 403`)

```bash
# Direct API call with cooperation token:
for DEST in all host api; do
  curl -sS -w " (HTTP %{http_code})\n" -X POST "http://172.16.0.201:18082/agents/$DEST/envelopes" \
    -H "Authorization: Bearer $COOP_TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"text":"hi","as":"laba-peer"}'
done

# CLI preflight before any network I/O:
h-mesh-office send --office labb -a all "hi"
h-mesh-office send --office labb -a host "hi"
h-mesh-office send --office labb -a api "hi"
```

Output:

```text
{"detail":"destination not permitted for a cooperation token"} (HTTP 403)
{"detail":"destination not permitted for a cooperation token"} (HTTP 403)
{"detail":"destination not permitted for a cooperation token"} (HTTP 403)
office: error: invalid agent name 'all': lowercase alphanumeric/hyphens, 1-63 chars, starting with a letter or digit, not all-digits, not a reserved word
office: error: cannot send to 'host': not an agent
office: error: cannot send to 'api': not an agent
```

### 6.4 Spoofed `"as"` field -> HTTP `403`

```bash
curl -sS -w "\nHTTP %{http_code}\n" -X POST http://172.16.0.201:18082/agents/worker/envelopes \
  -H "Authorization: Bearer $COOP_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text":"hi","as":"architect"}'
```

Output:

```text
{"detail":"'as' must be the participant this token is bound to"}
HTTP 403
```

### 6.5 Reading other mailboxes (`404`) or tenant/activity endpoints (`403`)

```bash
for PATH_URL in \
  "/agents/worker/messages" \
  "/agents/architect/messages" \
  "/agents/worker/messages/stream" \
  "/agents" \
  "/board" \
  "/alerts" \
  "/agents/laba-peer/activity"
do
  printf "%-32s -> " "$PATH_URL"
  curl -sS -w " (HTTP %{http_code})\n" -H "Authorization: Bearer $COOP_TOKEN" \
    "http://172.16.0.201:18082${PATH_URL}"
done
```

Output:

```text
/agents/worker/messages          -> {"detail":"invalid client agent"} (HTTP 404)
/agents/architect/messages       -> {"detail":"invalid client agent"} (HTTP 404)
/agents/worker/messages/stream   -> {"detail":"invalid client agent"} (HTTP 404)
/agents                          -> {"detail":"not permitted for a cooperation token"} (HTTP 403)
/board                           -> {"detail":"not permitted for a cooperation token"} (HTTP 403)
/alerts                          -> {"detail":"not permitted for a cooperation token"} (HTTP 403)
/agents/laba-peer/activity       -> {"detail":"not permitted for a cooperation token"} (HTTP 403)
```

### 6.6 Non-lead agent cannot run `office-add`, `office-rm`, `coop-grant`, or `coop-revoke`

When run as `AGENT_NAME=worker` (non-lead):

```bash
printf '%s\n' 'coop.0000000000000000.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' | \
  AGENT_NAME=worker h-mesh-office office-add labb2 http://172.16.0.201:18082 \
    --participant laba-peer --token-stdin --allow-plaintext
AGENT_NAME=worker h-mesh-office office-rm labb
AGENT_NAME=worker h-mesh-office coop-grant rogue-peer
AGENT_NAME=worker h-mesh-office coop-revoke a6da28b63c555f78
```

Output (all exit `1`):

```text
office: error: office office-add is lead-only (the lead is 'architect', you are 'worker')
office: error: office office-rm is lead-only (the lead is 'architect', you are 'worker')
office: error: office coop-grant is lead-only (the lead is 'architect', you are 'worker')
office: error: office coop-revoke is lead-only (the lead is 'architect', you are 'worker')
```

### 6.7 Revoked token fails immediately (`HTTP 401`)

In `labb` as lead (`architect`):

```bash
h-mesh-office coop-revoke a6da28b63c555f78
h-mesh-office coops
```

Output:

```text
revoked a6da28b63c555f78
a6da28b63c555f78	laba-peer	Message	revoked	1793576639.1644385
```

Immediately afterwards in `laba`:

```bash
h-mesh-office send --office labb -a worker "after revoke"
```

Output (exit `1`):

```text
office: error: remote office refused the message: HTTP 401: Unauthorized (the cooperation token was refused: wrong, revoked or not for this office)
```

And polling `GET /agents/laba-peer/messages` with the revoked token immediately returns `{"detail":"Unauthorized"}` (`HTTP 401`). *(After verifying revocation during this test run, a fresh active token `8f245a94570a6f52` was granted in `labb` and updated in `laba` so the live testbed remains connected and ready for interactive testing.)*

---

## 7. Honest Findings from the Live End-to-End Run

### What Worked End-to-End
- **Cooperation tokens (`lib/coop_tokens.py`, `modules/api`)**: Grant, SHA-256 hashed storage in `coop-tokens`, bearer authentication without HMAC headers on the published API port, immediate revocation, `Message`-only enforcement, destination restrictions (`all`, `host`, `api` -> `403`), forced `"as"` participant binding (`403`), mailbox isolation (`404` on other mailboxes, `403` on `/agents`, `/board`, `/alerts`, `/activity`), and resummable SSE streaming (`GET /agents/<participant>/messages/stream?after=<cursor>`).
- **Office CLI (`modules/office/cli.py`)**: `coop-grant`, `coop-revoke`, `coops`, `office-add --token-stdin`, `offices`, `office-rm`, `send --office`, lead-only checks, automatic enrollment of `<participant> (api)` on the receiving side and `relay (api)` on the sending side, `xsent.s<stream_id>` receipt creation (24h TTL with `sender_incarnation`), and exit codes `0` / `1` / `3`.
- **Reply relay (`modules/interoffice/relay.py`, `services/interoffice_relay.py`)**: Always-on supervisor started automatically by `setup.sh` / `daemons.py`, discovered `labb` as soon as `office-add` wrote to Redis, connected over SSE, correlated `labb/worker`'s `--reply-to <stream_id>` reply against `xsent.s<stream_id>`, delivered `[message from relay] [from labb/worker] ...` into `laba` `architect`'s tmux pane, and persisted the cursor in `offices.cursor`.
- **Secret hygiene**: Verified across `/home/ubuntu/.h-mesh/run/default/*.log`, `window.log.jsonl`, `h-mesh-office offices`, `h-mesh-office coops`, error outputs, and `ps -ef` on both containers — zero token secret occurrences in any log, CLI listing, or process argument list.

### Findings by Lane (Not Modified in This Branch)
1. **`setup` / `services/daemons.py` (`enabled_daemon_modules`) — resolved in `12d1a8a`**:
   - During the initial `98018f6` / `5734b70` testbed runs, `services.api` was still gated on `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` and had to be started manually inside Telegram-free containers. Merging `origin/setup-agent/api-daemon-without-telegram` (`3bc4e96` -> `12d1a8a`) into `origin/architect/inter-office-poc` enables `services.api` automatically whenever `API_TOKEN` is set in `.env`.
2. **`container/bootstrap.sh` (`SUMMARY.md` compose snippets vs `H_MESH_TENANT_ENV_FILE`)**:
   - `container/compose.yaml` uses `env_file: - ${H_MESH_TENANT_ENV_FILE:-.env}`. `container/bootstrap.sh` sets `H_MESH_TENANT_ENV_FILE="$ENV_FILE"` when it invokes `docker compose`, but the raw `docker compose -p ... --env-file ...` commands written into `offices/<pod>/<tenant>/SUMMARY.md` omit `H_MESH_TENANT_ENV_FILE=<env-path>`, causing `env file .../container/.env not found` unless `H_MESH_TENANT_ENV_FILE` is provided in the environment (as shown in Section 0 above).
3. **`tmux` / `interoffice` (`modules/tmux/port.py` reply hint on relayed messages)**:
   - Because `relay` is registered locally as `port_type="api"`, `modules/tmux/port.py` formats every message delivered by `relay` with a second line:
     `[reply to relay: h-mesh-office send -a relay --reply-to <local_stream_id> "..."]`
   - However, `relay` is an inbound-only local participant (outbound inter-office sends go through `h-mesh-office send --office <office> -a <agent> "..."`). If an agent follows the `[reply to relay: h-mesh-office send -a relay ...]` hint literally, the message is queued into the local `pod:<pod>:tenant:<tenant>:agent:relay:mailbox` stream where nothing reads or forwards it. Suppressing the `[reply to relay: ...]` hint for `source == "relay"` (or rendering `[reply to <office>/<agent>: h-mesh-office send --office <office> -a <agent> "..."]`) in `modules/tmux/port.py` would prevent agents from sending replies into the local `relay` mailbox.

See [`docs/inter-office-proof.md`](inter-office-proof.md) for the `2026-11-03` fresh live proof and negative test transcripts on `laba` ↔ `labb`.
