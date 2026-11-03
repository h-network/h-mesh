# Live Inter-Office Messaging Proof on `laba` ↔ `labb` (`2026-11-03`)

This document records the fresh, live end-to-end verification and negative-case proof of inter-office messaging between the two containerized offices `laba` (`172.16.0.201:18080`) and `labb` (`172.16.0.201:18082`) on the `h-lab` testbed VM (`172.16.0.201`), executed on `2026-11-03T22:19Z–22:30Z`.

No token or secret values appear in this document; only `token-id`, `participant`, `sha256` digests, and metadata are shown.

---

## PASS / FAIL Summary Table

| # | Proof Step | Result | Summary & Evidence |
|---|---|---|---|
| **1a** | Probe vLLM `/v1/models` on `172.16.0.11:8000` | **PASS** | `GET http://172.16.0.11:8000/v1/models` returned model ID `nemotron-3.5-lightning-30b-a3b-nvfp4` (`max_model_len: 262144`). Both `laba` and `labb` `.env` files configure `PROVIDER_LOCAL_MODEL=nemotron-3.5-lightning-30b-a3b-nvfp4`. |
| **1b** | Confirm `laba` / `labb` code (`branch + sha`) and redeploy target (`origin/architect/inter-office-poc` + runbook commit) | **PASS** *(with daemon startup caveat noted)* | Host checkouts `/home/h-lab/lab/laba` and `/home/h-lab/lab/labb` were on `inter-office-poc` @ `98018f6aa33351f80f9ea84de9bda905c13c4ba7`. Both were fast-forwarded to `origin/docker-agent/inter-office-runbook` @ `5734b7005dde5d3cf0e2d814e98e266945438867` (`98018f6` + `docs/inter-office-poc.md`), `h-mesh:latest` (`sha256:7a94c749d91acf4ffb1130f5348221a9f898cb5260a9562eeaccc63978fe60b7`) was built, and both containers (`cc59c44b9d3a`, `df399454c688`) were freshly recreated. **Caveat at test time (`98018f6`/`5734b70`):** `services.daemons.enabled_daemon_modules()` still gated `services.api` on `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`, so `services.api` had to be started explicitly via `docker exec` after container recreate (subsequently resolved on `origin/architect/inter-office-poc` at `12d1a8a` by merging `origin/setup-agent/api-daemon-without-telegram` @ `3bc4e96`). |
| **2a** | Fresh `coop-grant` in `labb` (`architect`) | **PASS** | Prior active tokens were revoked; `h-mesh-office coop-grant laba-peer` minted fresh `token-id: f84e46c4194998e3` (`participant: laba-peer`, `kinds: ["Message"]`, `status: active`, `created_ts: 1793744796.313405`) and registered `laba-peer (api)` in `labb`. |
| **2b** | Fresh `office-add` in `laba` (`architect`) via `--token-stdin` | **PASS** | `h-mesh-office office-add labb http://172.16.0.201:18082 --participant laba-peer --token-stdin --allow-plaintext` stored `labb -> http://172.16.0.201:18082 (as laba-peer)` in `laba`. |
| **2c** | Outbound `send --office labb -a worker` from `laba` (`architect`) | **PASS** | `h-mesh-office send --office labb -a worker "..."` returned `sent to labb/worker: 161 bytes (381409b3998e4a6e892832e353a989d5)` (`HTTP 202 Accepted` on `labb`, `correlation_id: d07fb792441748438899588213299d5c`) and recorded `pod:laba:tenant:default:xsent.s381409b3998e4a6e892832e353a989d5` in `laba` Redis. |
| **3a** | **Negative:** Plaintext `http://` to non-loopback refused without opt-in | **PASS** | Without `--allow-plaintext` and with `H_MESH_ALLOW_PLAINTEXT` unset, `h-mesh-office office-add labb http://172.16.0.201:18082 --participant laba-peer --token-stdin` exited `1` with `office: error: plaintext http to a non-loopback host sends the cooperation token in the clear; use https (the supported default), or pass --allow-plaintext for a lab test bed`, leaving `offices` empty (`no offices configured`). |
| **3b** | **Negative:** Wrong / unknown agent name gives clear error, not a hang | **PASS** | `h-mesh-office send --office labb -a no-such-agent "test to unknown agent"` exited `1` in `320 ms` with `office: error: remote office refused the message: HTTP 404: unknown agent`. |
| **3c** | **Negative:** Cooperation token cannot send `Command` or `StartAgent` | **PASS** | `POST /agents/worker/envelopes` with the cooperation token and `kind: "Command"` or `kind: "StartAgent"` was rejected with `HTTP 403 Forbidden` and `{"detail":"envelope kind not permitted for a cooperation token"}`. |
| **3d** | **Negative:** Revoked cooperation token is rejected | **PASS** | After `h-mesh-office coop-revoke f84e46c4194998e3` in `labb`, `h-mesh-office send --office labb -a worker "..."` in `laba` exited `1` with `HTTP 401: Unauthorized (the cooperation token was refused: wrong, revoked or not for this office)`, `GET /agents/laba-peer/messages` returned `HTTP 401` (`{"detail":"Unauthorized"}`), and `laba`'s `interoffice_relay.log` logged `relay office=labb event=http-error status=401`. |
| **4** | Reply path via local model (`nemotron-3.5-lightning-30b-a3b-nvfp4`) correlated back to `laba` via `services.interoffice_relay` | **PASS** *(with tmux reply-hint caveat noted)* | `labb/worker` (Claude Code on `nemotron-3.5-lightning-30b-a3b-nvfp4`) received `[message from laba-peer]` (`stream_id: 381409b3998e4a6e892832e353a989d5`) and executed `h-mesh-office send -a laba-peer --reply-to 381409b3998e4a6e892832e353a989d5 "Live proof ping received. Acknowledging architect's message."` (`stream_id: 3038a433ef7449818a85ccb19ac87a4c`, `in_reply_to: 381409b3998e4a6e892832e353a989d5`, `cursor: 1793744825891-0`). `laba`'s `services.interoffice_relay` resolved `reply_id -> architect` via `pod:laba:tenant:default:xsent.s381409b3998e4a6e892832e353a989d5`, injected `stream_id: bcecb49e214f4466a91a8f6ebe89a0c8` (`source: "relay"`, `destination: "architect"`), and delivered `[message from relay] [from labb/worker] Live proof ping received. Acknowledging architect's message.` into `laba/architect`'s tmux pane. |

---

## Step 1: Code, Containers, and vLLM Model Verification

### 1.1 Probe vLLM `/v1/models` on `172.16.0.11:8000`

Command executed on `h-lab@172.16.0.201`:
```bash
curl -fsS http://172.16.0.11:8000/v1/models
```

Raw output:
```json
{"object":"list","data":[{"id":"nemotron-3.5-lightning-30b-a3b-nvfp4","object":"model","created":1793744280,"owned_by":"vllm","root":"/model","parent":null,"max_model_len":262144,"permission":[{"id":"modelperm-9db8a65295645968","object":"model_permission","created":1793744280,"allow_create_engine":false,"allow_sampling":true,"allow_logprobs":true,"allow_search_indices":false,"allow_view":true,"allow_fine_tuning":false,"organization":"*","group":null,"is_blocking":false}]}]}
```

Both office `.env` files (`/home/h-lab/lab/laba/offices/laba/default/.env` and `/home/h-lab/lab/labb/offices/labb/default/.env`) configure this exact model ID (secrets redacted):
```ini
POD=laba
TENANT=default
AGENTS=architect,worker
DEFAULT_CLI=claude
ACCOUNTS=default
DEFAULT_ACCOUNT=default
H_MESH_BIND_PORTS=1
API_PORT=18080
SESSION_PORT=18081
PROVIDER_NAME=local
PROVIDER_LOCAL_URL=http://172.16.0.11:8000/v1
PROVIDER_LOCAL_MODEL=nemotron-3.5-lightning-30b-a3b-nvfp4
PROVIDER_LOCAL_TOKEN=<REDACTED>
PROVIDER_LOCAL_KIND=vllm
AGENT_PROVIDERS=architect=local,worker=local
H_MESH_ALLOW_PLAINTEXT=1
API_TOKEN=<REDACTED>
```
(`labb` is identical with `POD=labb`, `API_PORT=18082`, `SESSION_PORT=18083`.)

### 1.2 Confirm Checkout Branch/SHA and Redeploy `laba` and `labb`

Initial checkout state on `h-lab@172.16.0.201`:
```text
/home/h-lab/lab/laba: branch=inter-office-poc sha=98018f6aa33351f80f9ea84de9bda905c13c4ba7
/home/h-lab/lab/labb: branch=inter-office-poc sha=98018f6aa33351f80f9ea84de9bda905c13c4ba7
```

Both checkouts were fast-forwarded to `origin/docker-agent/inter-office-runbook` (`5734b7005dde5d3cf0e2d814e98e266945438867`, which is `origin/architect/inter-office-poc` @ `98018f6aa33351f80f9ea84de9bda905c13c4ba7` plus `docs/inter-office-poc.md`), `h-mesh:latest` (`sha256:7a94c749d91acf4ffb1130f5348221a9f898cb5260a9562eeaccc63978fe60b7`) was built, and both containers were recreated:

```bash
for d in /home/h-lab/lab/laba /home/h-lab/lab/labb; do
  git -C "$d" fetch origin --quiet
  git -C "$d" merge --ff-only origin/docker-agent/inter-office-runbook
  git -C "$d" log -2 --oneline
done
H_MESH_TENANT_ENV_FILE=/home/h-lab/lab/laba/offices/laba/default/.env \
  docker compose -p h-mesh-laba-default \
  -f /home/h-lab/lab/laba/container/compose.yaml \
  -f /home/h-lab/lab/laba/container/compose.ports.yaml \
  --env-file /home/h-lab/lab/laba/offices/laba/default/.env up -d --build --force-recreate
H_MESH_TENANT_ENV_FILE=/home/h-lab/lab/labb/offices/labb/default/.env \
  docker compose -p h-mesh-labb-default \
  -f /home/h-lab/lab/labb/container/compose.yaml \
  -f /home/h-lab/lab/labb/container/compose.ports.yaml \
  --env-file /home/h-lab/lab/labb/offices/labb/default/.env up -d --force-recreate
```

Raw output:
```text
Updating 98018f6..5734b70
Fast-forward
 docs/inter-office-poc.md | 398 +++++++++++++++++++++++++++++++++++++++++++++++
 1 file changed, 398 insertions(+)
 create mode 100644 docs/inter-office-poc.md
5734b70 docs: add inter-office PoC operator runbook and live laba/labb verification
98018f6 Merge remote-tracking branch 'origin/setup-agent/inter-office-relay' into architect/inter-office-poc
...
Container h-mesh-laba-default-h-mesh-1 Recreated
Container h-mesh-laba-default-h-mesh-1 Started
Container h-mesh-labb-default-h-mesh-1 Recreated
Container h-mesh-labb-default-h-mesh-1 Started
CONTAINER ID   IMAGE           COMMAND                  CREATED          STATUS                    PORTS                                                                                          NAMES
df399454c688   h-mesh:latest   "/usr/bin/tini -- /u…"   12 seconds ago   Up 12 seconds (healthy)   0.0.0.0:18082->8080/tcp, [::]:18082->8080/tcp, 0.0.0.0:18083->8081/tcp, [::]:18083->8081/tcp   h-mesh-labb-default-h-mesh-1
cc59c44b9d3a   h-mesh:latest   "/usr/bin/tini -- /u…"   14 seconds ago   Up 12 seconds (healthy)   0.0.0.0:18080->8080/tcp, [::]:18080->8080/tcp, 0.0.0.0:18081->8081/tcp, [::]:18081->8081/tcp   h-mesh-laba-default-h-mesh-1
```

Because `origin/setup-agent/api-daemon-without-telegram` (`3bc4e96`) is not merged into `origin/architect/inter-office-poc` (`98018f6`), `services.daemons.enabled_daemon_modules()` on `98018f6`/`5734b70` starts `core.service`, `services.tmux_reconciler`, `services.watchdog`, and `services.interoffice_relay` automatically, while `services.api` was started explicitly inside both containers:
```bash
for c in h-mesh-laba-default-h-mesh-1 h-mesh-labb-default-h-mesh-1; do
  docker exec -d -u ubuntu "$c" bash -c "cd /app/h-app && /opt/h-mesh/bin/python -u -m services.api >> /home/ubuntu/.h-mesh/run/default/api.log 2>&1"
done
```

---

## Step 2 & Step 4: Fresh Runbook Flow and Correlated Reply Proof

### 2.1 Clean Prior State in `laba` and `labb`

Commands:
```bash
docker exec -u ubuntu h-mesh-laba-default-h-mesh-1 env AGENT_NAME=architect TENANT=default h-mesh-office office-rm labb
docker exec -u ubuntu h-mesh-laba-default-h-mesh-1 env AGENT_NAME=architect TENANT=default h-mesh-office offices
docker exec -u ubuntu h-mesh-labb-default-h-mesh-1 env AGENT_NAME=architect TENANT=default h-mesh-office coop-revoke 8f245a94570a6f52
docker exec -u ubuntu h-mesh-labb-default-h-mesh-1 env AGENT_NAME=architect TENANT=default h-mesh-office coops
```

Raw output:
```text
removed office labb
no offices configured
revoked 8f245a94570a6f52
a6da28b63c555f78	laba-peer	Message	revoked	1793576639.1644385
8f245a94570a6f52	laba-peer	Message	revoked	1793580875.1381657
```

### 2.2 Mint Fresh Cooperation Token in `labb` (`coop-grant`)

Commands (storing the raw token only in a `0600` temporary file and omitting the `token:` line from stdout):
```bash
umask 077
OUT=$(docker exec -u ubuntu h-mesh-labb-default-h-mesh-1 env AGENT_NAME=architect TENANT=default h-mesh-office coop-grant laba-peer)
printf "%s\n" "$OUT" | sed -n "s/^token: //p" > /tmp/proof-coop.token
printf "%s\n" "$OUT" | grep -v "^token:"
docker exec -u ubuntu h-mesh-labb-default-h-mesh-1 env AGENT_NAME=architect TENANT=default h-mesh-office coops
docker exec -u ubuntu h-mesh-labb-default-h-mesh-1 env AGENT_NAME=architect TENANT=default h-mesh-office peers -i
```

Raw output:
```text
This token is shown once. Give it to the other office's lead (office-add ... --token-stdin); revoke with: office coop-revoke f84e46c4194998e3
participant: laba-peer
token-id: f84e46c4194998e3
a6da28b63c555f78	laba-peer	Message	revoked	1793576639.1644385
8f245a94570a6f52	laba-peer	Message	revoked	1793580875.1381657
e3ddd1203166d63c	laba-peer	Message	revoked	1793744773.0838592
f84e46c4194998e3	laba-peer	Message	active	1793744796.313405
worker
interfaces (api/office -- recognized, not tmux colleagues): api (api), host (office), laba-peer (api)
```

Corresponding non-secret Redis record in `labb` (`HGET pod:labb:tenant:default:coop-tokens f84e46c4194998e3` — stores only `sha256`):
```json
{"participant":"laba-peer","sha256":"cb429a43010e1a47a6d46d007ee95e5ab2614e93cd492322c24cd7e3d54f79d6","kinds":["Message"],"created_ts":1793744796.313405,"revoked":false}
```

### 2.3 Register `labb` in `laba` Address Book (`office-add --token-stdin --allow-plaintext`)

Commands:
```bash
docker exec -i -u ubuntu h-mesh-laba-default-h-mesh-1 \
  env -u H_MESH_ALLOW_PLAINTEXT AGENT_NAME=architect TENANT=default \
  h-mesh-office office-add labb http://172.16.0.201:18082 --participant laba-peer --token-stdin --allow-plaintext < /tmp/proof-coop.token
docker exec -u ubuntu h-mesh-laba-default-h-mesh-1 env AGENT_NAME=architect TENANT=default h-mesh-office offices
```

Raw output:
```text
added office labb -> http://172.16.0.201:18082 (as laba-peer)
office: warning: http://172.16.0.201:18082 is plaintext http; the token travels in the clear
labb	http://172.16.0.201:18082	as laba-peer	added by architect
```

### 2.4 Send Inter-Office Message from `laba` (`architect`) to `labb` (`worker`) and Trace Correlated Reply

Command:
```bash
docker exec -u ubuntu h-mesh-laba-default-h-mesh-1 env AGENT_NAME=architect TENANT=default \
  h-mesh-office send --office labb -a worker "Live proof ping from laba architect — please reply to laba-peer using the exact h-mesh-office send -a laba-peer --reply-to <id> command shown in the hint line."
```

Raw stdout:
```text
sent to labb/worker: 161 bytes (381409b3998e4a6e892832e353a989d5)
```

#### Outbound Correlation Record Written in `laba` Redis
Command:
```bash
docker exec -u ubuntu h-mesh-laba-default-h-mesh-1 redis-cli -u redis://127.0.0.1:6379/0 GET "pod:laba:tenant:default:xsent.s381409b3998e4a6e892832e353a989d5"
```
Raw output:
```json
{"sender": "architect", "sender_incarnation": "990d5c26592d41e7a780fb65598d4bda", "office": "labb", "agent": "worker", "ts": "2026-11-03T22:27:02.846Z"}
```

#### `labb` API and Switch Log Evidence (Inbound Delivery `laba-peer -> worker` and Reply `worker -> laba-peer`)

`labb` `/home/ubuntu/.h-mesh/run/default/api.log`:
```text
INFO:     172.20.0.1:60342 - "GET /agents/laba-peer/messages/stream HTTP/1.1" 200 OK
{"ts":"2026-11-03T22:27:02.844Z","module":"api","event":"sent","writer":"api","stream_id":"381409b3998e4a6e892832e353a989d5","correlation_id":"d07fb792441748438899588213299d5c","source":"laba-peer","destination":"worker"}
INFO:     172.20.0.1:37318 - "POST /agents/worker/envelopes HTTP/1.1" 202 Accepted
```

`labb` `/home/ubuntu/.h-mesh/run/default/switch.log`:
```json
{"ts":"2026-11-03T22:27:02.845Z","module":"switch","event":"popped","writer":"switch","stream_id":"381409b3998e4a6e892832e353a989d5","correlation_id":"d07fb792441748438899588213299d5c","source":"laba-peer","destination":"worker"}
{"ts":"2026-11-03T22:27:02.845Z","module":"switch","event":"forwarded","writer":"switch","stream_id":"381409b3998e4a6e892832e353a989d5","correlation_id":"d07fb792441748438899588213299d5c","source":"laba-peer","destination":"worker"}
{"ts":"2026-11-03T22:27:02.848Z","module":"switch","event":"kick_started","writer":"switch","stream_id":"381409b3998e4a6e892832e353a989d5","correlation_id":"d07fb792441748438899588213299d5c","source":"laba-peer","destination":"worker"}
{"ts":"2026-11-03T22:27:03.086Z","module":"tmux","event":"received","writer":"tmux","stream_id":"381409b3998e4a6e892832e353a989d5","correlation_id":"d07fb792441748438899588213299d5c","source":"laba-peer","destination":"worker"}
{"ts":"2026-11-03T22:27:03.595Z","module":"tmux","event":"opened","writer":"tmux","stream_id":"381409b3998e4a6e892832e353a989d5","correlation_id":"d07fb792441748438899588213299d5c","source":"laba-peer","destination":"worker"}
{"ts":"2026-11-03T22:27:05.634Z","module":"office","event":"sent","writer":"office","stream_id":"3038a433ef7449818a85ccb19ac87a4c","correlation_id":"c5e4a26a050c49e3995c1d2897c0457b","source":"worker","destination":"laba-peer"}
{"ts":"2026-11-03T22:27:05.634Z","module":"switch","event":"popped","writer":"switch","stream_id":"3038a433ef7449818a85ccb19ac87a4c","correlation_id":"c5e4a26a050c49e3995c1d2897c0457b","source":"worker","destination":"laba-peer"}
{"ts":"2026-11-03T22:27:05.637Z","module":"switch","event":"forwarded","writer":"switch","stream_id":"3038a433ef7449818a85ccb19ac87a4c","correlation_id":"c5e4a26a050c49e3995c1d2897c0457b","source":"worker","destination":"laba-peer"}
{"ts":"2026-11-03T22:27:05.638Z","module":"switch","event":"kick_started","writer":"switch","stream_id":"3038a433ef7449818a85ccb19ac87a4c","correlation_id":"c5e4a26a050c49e3995c1d2897c0457b","source":"worker","destination":"laba-peer"}
{"ts":"2026-11-03T22:27:05.891Z","module":"api","event":"received","writer":"api","stream_id":"3038a433ef7449818a85ccb19ac87a4c","correlation_id":"c5e4a26a050c49e3995c1d2897c0457b","source":"worker","destination":"laba-peer"}
{"ts":"2026-11-03T22:27:05.891Z","module":"api","event":"opened","writer":"api","stream_id":"3038a433ef7449818a85ccb19ac87a4c","correlation_id":"c5e4a26a050c49e3995c1d2897c0457b","source":"worker","destination":"laba-peer"}
```

#### `labb` `GET /agents/laba-peer/messages` Reply Envelope Evidence
Command:
```bash
curl -fsS -H "Authorization: Bearer $(cat /tmp/proof-coop.token)" "http://172.16.0.201:18082/agents/laba-peer/messages?count=5"
```
Relevant reply envelope from raw JSON output:
```json
{
  "v": 4,
  "stream_id": "3038a433ef7449818a85ccb19ac87a4c",
  "correlation_id": "c5e4a26a050c49e3995c1d2897c0457b",
  "l2": {"source": "worker", "destination": "laba-peer"},
  "ttl": 15,
  "hops": 1,
  "kind": "Message",
  "ts": "2026-11-03T22:27:05.634Z",
  "l3": {"source": "labb:default:worker", "destination": "labb:default:laba-peer"},
  "payload": {"text": "Live proof ping received. Acknowledging architect's message."},
  "in_reply_to": "381409b3998e4a6e892832e353a989d5",
  "cursor": "1793744825891-0"
}
```

#### `laba` Relay and Switch Log Evidence (`relay -> architect`)

`laba` `/home/ubuntu/.h-mesh/run/default/interoffice_relay.log`:
```text
{"ts":"2026-11-03T22:27:05.953Z","module":"interoffice","event":"sent","writer":"interoffice","stream_id":"bcecb49e214f4466a91a8f6ebe89a0c8","correlation_id":"6542b11dc2884dba8f825208300aa547","source":"relay","destination":"architect"}
2026-11-03 22:27:05,953 [INFO] modules.interoffice.relay: relay office=labb event=admitted destination=architect stream_id=bcecb49e214f4466a91a8f6ebe89a0c8
```

`laba` `/home/ubuntu/.h-mesh/run/default/switch.log`:
```json
{"ts":"2026-11-03T22:27:05.953Z","module":"switch","event":"popped","writer":"switch","stream_id":"bcecb49e214f4466a91a8f6ebe89a0c8","correlation_id":"6542b11dc2884dba8f825208300aa547","source":"relay","destination":"architect"}
{"ts":"2026-11-03T22:27:05.953Z","module":"switch","event":"forwarded","writer":"switch","stream_id":"bcecb49e214f4466a91a8f6ebe89a0c8","correlation_id":"6542b11dc2884dba8f825208300aa547","source":"relay","destination":"architect"}
{"ts":"2026-11-03T22:27:05.954Z","module":"switch","event":"kick_started","writer":"switch","stream_id":"bcecb49e214f4466a91a8f6ebe89a0c8","correlation_id":"6542b11dc2884dba8f825208300aa547","source":"relay","destination":"architect"}
{"ts":"2026-11-03T22:27:06.216Z","module":"tmux","event":"received","writer":"tmux","stream_id":"bcecb49e214f4466a91a8f6ebe89a0c8","correlation_id":"6542b11dc2884dba8f825208300aa547","source":"relay","destination":"architect"}
{"ts":"2026-11-03T22:27:06.727Z","module":"tmux","event":"opened","writer":"tmux","stream_id":"bcecb49e214f4466a91a8f6ebe89a0c8","correlation_id":"6542b11dc2884dba8f825208300aa547","source":"relay","destination":"architect"}
```

`laba` checkpointed SSE cursor in Redis (`HGET pod:laba:tenant:default:offices.cursor labb`):
```text
1793744826836-0
```

#### `laba` `architect` Tmux Pane Capture
Command:
```bash
docker exec -u ubuntu h-mesh-laba-default-h-mesh-1 bash -lc "TMUX_TMPDIR=/home/ubuntu/.h-mesh/tmux tmux capture-pane -pt default:architect -S -35"
```
Raw output:
```text
❯ [message from relay] [from labb/worker] Live proof ping received. Acknowledging architect's message.
  [reply to relay: h-mesh-office send -a relay --reply-to bcecb49e214f4466a91a8f6ebe89a0c8 "..."]
```

---

## Step 3: Live Negative Proofs

### 3.1 Plaintext `http://` to Non-Loopback Refused Without Opt-In

Command:
```bash
docker exec -i -u ubuntu h-mesh-laba-default-h-mesh-1 \
  env -u H_MESH_ALLOW_PLAINTEXT AGENT_NAME=architect TENANT=default \
  h-mesh-office office-add labb http://172.16.0.201:18082 --participant laba-peer --token-stdin < /tmp/proof-coop.token
```

Raw output and exit code:
```text
office: error: plaintext http to a non-loopback host sends the cooperation token in the clear; use https (the supported default), or pass --allow-plaintext for a lab test bed
exit_code=1
no offices configured
```

### 3.2 Wrong / Unknown Agent Name Gives Clear Error (No Hang)

Command:
```bash
T0=$(date +%s%3N)
docker exec -u ubuntu h-mesh-laba-default-h-mesh-1 env AGENT_NAME=architect TENANT=default \
  h-mesh-office send --office labb -a no-such-agent "test to unknown agent"
RC=$?
T1=$(date +%s%3N)
echo "exit_code=$RC elapsed_ms=$((T1 - T0))"
```

Raw output:
```text
office: error: remote office refused the message: HTTP 404: unknown agent
exit_code=1 elapsed_ms=320
```

### 3.3 Cooperation Token Cannot Send `Command` or `StartAgent`

Commands:
```bash
curl -sS -w "\nHTTP_STATUS=%{http_code}\n" \
  -X POST "http://172.16.0.201:18082/agents/worker/envelopes" \
  -H "Authorization: Bearer $(cat /tmp/proof-coop.token)" \
  -H "Content-Type: application/json" \
  -d '{"kind":"Command","payload":{"command":"pause"}}'

curl -sS -w "\nHTTP_STATUS=%{http_code}\n" \
  -X POST "http://172.16.0.201:18082/agents/worker/envelopes" \
  -H "Authorization: Bearer $(cat /tmp/proof-coop.token)" \
  -H "Content-Type: application/json" \
  -d '{"kind":"StartAgent","payload":{"agent":"rogue"}}'
```

Raw outputs:
```text
{"detail":"envelope kind not permitted for a cooperation token"}
HTTP_STATUS=403

{"detail":"envelope kind not permitted for a cooperation token"}
HTTP_STATUS=403
```

### 3.4 Revoked Cooperation Token Is Immediately Rejected

Commands:
```bash
docker exec -u ubuntu h-mesh-labb-default-h-mesh-1 env AGENT_NAME=architect TENANT=default \
  h-mesh-office coop-revoke f84e46c4194998e3
docker exec -u ubuntu h-mesh-labb-default-h-mesh-1 env AGENT_NAME=architect TENANT=default \
  h-mesh-office coops

docker exec -u ubuntu h-mesh-laba-default-h-mesh-1 env AGENT_NAME=architect TENANT=default \
  h-mesh-office send --office labb -a worker "message with revoked token"

curl -sS -w "\nHTTP_STATUS=%{http_code}\n" \
  -H "Authorization: Bearer $(cat /tmp/proof-coop.token)" \
  "http://172.16.0.201:18082/agents/laba-peer/messages"
```

Raw outputs:
```text
revoked f84e46c4194998e3
a6da28b63c555f78	laba-peer	Message	revoked	1793576639.1644385
8f245a94570a6f52	laba-peer	Message	revoked	1793580875.1381657
e3ddd1203166d63c	laba-peer	Message	revoked	1793744773.0838592
f84e46c4194998e3	laba-peer	Message	revoked	1793744796.313405
office: error: remote office refused the message: HTTP 401: Unauthorized (the cooperation token was refused: wrong, revoked or not for this office)
send_with_revoked_token_exit_code=1
{"detail":"Unauthorized"}
HTTP_STATUS=401
```

And `laba`'s `/home/ubuntu/.h-mesh/run/default/interoffice_relay.log` immediately recorded the `401` without leaking the token or URL:
```text
2026-11-03 22:28:40,817 [WARNING] modules.interoffice.relay: relay office=labb event=disconnected
2026-11-03 22:28:41,983 [WARNING] modules.interoffice.relay: relay office=labb event=http-error status=401
```

After completing the negative tests, a fresh active token (`token-id: 5aafbfa9dcbb1794`) was granted in `labb` and configured in `laba` so the `laba` ↔ `labb` link remains live, and `/tmp/proof-coop.token` was deleted.

---

## Step 5: Plain-Spoken Findings & Caveats Observed Live

1. **`services.api` was not auto-started on Telegram-free containers in `98018f6` / `5734b70` (subsequently merged at `12d1a8a`)**:
   At the time of this `2026-11-03T22:19Z–22:30Z` proof run on `5734b70` (parent `98018f6`), `services.daemons.enabled_daemon_modules()` still gated `services.api` on `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. When `laba` and `labb` were recreated from `5734b70`, `core.service`, `services.tmux_reconciler`, `services.watchdog`, and `services.interoffice_relay` started automatically, while `services.api` had to be started manually via `docker exec`. Immediately after this run, `origin/setup-agent/api-daemon-without-telegram` (`3bc4e96`) was merged into `origin/architect/inter-office-poc` at `12d1a8a`, enabling `services.api` automatically whenever `API_TOKEN` is set in `.env` (and the manual start step has been removed from [`docs/inter-office-poc.md`](inter-office-poc.md)).
2. **Relayed replies in `laba` display a local `[reply to relay: ...]` hint line that misleads the local model**:
   Because local `relay` is registered with `port_type="api"`, `modules/tmux/port.py` appends `[reply to relay: h-mesh-office send -a relay --reply-to <id> "..."]` when delivering `[message from relay] [from labb/worker] ...` into `laba/architect`'s tmux pane. In the live run, `laba/architect` (`nemotron-3.5-lightning-30b-a3b-nvfp4`) followed that hint and ran `h-mesh-office send -a relay --reply-to bcecb49e214f4466a91a8f6ebe89a0c8 "..."` (`stream_id: e30e8551b0bc4cccbcf4199fc76a640a`), which queues into local `relay`'s API inbox in `laba` rather than sending back across offices (`h-mesh-office send --office labb -a worker "..."`).
3. **`interoffice_relay` correlates via `pod:<pod>:tenant:<tenant>:xsent.s<remote_stream_id>`, then mints a fresh local `correlation_id` on `core.channels.send()`**:
   Reply correlation from `labb/worker` (`in_reply_to: 381409b3998e4a6e892832e353a989d5`) back to `laba/architect` works via `Store.sender()` reading `pod:laba:tenant:default:xsent.s381409b3998e4a6e892832e353a989d5`. When `OfficeWorker._admit()` calls `core.channels.send()` to inject the relayed message into `laba`, it does not pass `correlation_id`, so the local `relay -> architect` hop receives a new local `correlation_id` (`6542b11dc2884dba8f825208300aa547`).
4. **`SUMMARY.md` compose snippets on `98018f6` omit `H_MESH_TENANT_ENV_FILE`**:
   Running the raw `docker compose -p ... --env-file ... up -d` command from `offices/<pod>/<tenant>/SUMMARY.md` outside `container/bootstrap.sh` fails with `env file .../container/.env not found` unless `H_MESH_TENANT_ENV_FILE=<env-path>` is set in the environment (now documented in [`docs/inter-office-poc.md`](inter-office-poc.md) Section 0).
