# Release Gate A: Fresh-Install End-to-End Verification (`bc00197`)

- **Date:** 2026-11-04
- **Target commit:** `bc001977508fb00caab17f2eb9540657be63bdc2` (`origin/architect/release-candidate`, VM-certified `1429/1429`)
- **Host:** `h-lab@172.16.0.201`
- **Throwaway checkout:** `/tmp/release-gate-a-bc00197/h-mesh` (`umask 022`)
- **Throwaway image:** `h-mesh:release-gate-a-bc00197`
- **Throwaway offices:**
  - **Office X:** pod `gatex`, tenant `lab`, compose project `h-mesh-gatex-lab`, container `h-mesh-gatex-lab-h-mesh-1`, `API_PORT=29080`, `SESSION_PORT=29081`
  - **Office Y:** pod `gatey`, tenant `lab`, compose project `h-mesh-gatey-lab`, container `h-mesh-gatey-lab-h-mesh-1`, `API_PORT=29082`, `SESSION_PORT=29083`
- **Local LLM endpoint:** `http://172.16.0.11:8000` (`vllm`, model `nemotron-3.5-lightning-30b-a3b-nvfp4`)

---

## Summary Table

| Step | Check | Status | Notes |
| :--- | :--- | :---: | :--- |
| **Step 1** | Fresh `./container/bootstrap.sh` on X (`gatex/lab`) and Y (`gatey/lab`): `API_TOKEN` generated (`0600`), `services.api` + `services.interoffice_relay` up, published API port answers `401`, `attach.sh` exists (`0755`) and works (`attach` + `Ctrl-b d` detach) | **PASS** | Both offices came up cleanly via real interactive `./container/bootstrap.sh` (answering `""` then `y` to the TLS/plaintext prompt). All 5 daemons (`switch`, `tmux_reconciler`, `watchdog`, `interoffice_relay`, `api`) started automatically. |
| **Step 2** | Pair X and Y with `coop-grant` / `office-add` using remote grant participant names; test wrong-name case deliberately and record operator error | **PASS** | Wrong `--participant` name accepted by `office-add` locally, then rejected on `send --office` with `office: error: remote office refused the message: HTTP 403: 'as' must be the participant this token is bound to (refused by the remote office's cooperation policy)` (`interoffice_relay.log` logs `event=http-error status=404`). Re-running `office-add` with the remote grant's participant name updated the pairing cleanly. |
| **Step 3** | Send X -> Y and Y -> X to local-model `worker`; confirm reply returns to sender's lead via relay, reply-hint line is visible in lead's pane, and relayed reply preserves original `correlation_id` | **PASS** | Both `gatey/worker` and `gatex/worker` (running on `nemotron-3.5-lightning-30b-a3b-nvfp4`) replied via `h-mesh-office send -a <peer> --reply-to <msg_id>`. Relayed replies arrived in `gatex/architect` and `gatey/architect` panes with `[reply to <office>/<agent>: h-mesh-office send --office <office> -a <agent> "..."]` and preserved the exact original `correlation_id` (`e1fb9046f2a148a4a3756c67652a83ee` and `a557ec229be6494aa5a2870c7219467b`). |
| **Step 4** | Leave both offices idle 10+ minutes after the exchange: zero unanswered-message / watchdog alerts in any lead pane; plus verify local tmux-to-tmux ticket flow (`add` / `take` / `done`) | **PASS** | Left both offices idle for **14m 27s** (`17:14:24.439Z` -> `17:28:51.406Z`). `unreplied` and `unreplied.alerted` hashes stayed `{}` for `architect` and `worker` in both offices; zero watchdog alert envelopes or pane notices appeared. Local ticket flow (`add` -> `take` -> `done --outcome completed`) also verified in `gatex`. |
| **Step 5** | Run `h-mesh-office status`, `peers`, `list`, `usage` in both offices and note anything that errors or looks wrong | **PASS** | All four commands (`status`, `peers`, `list`, `usage`) plus `offices` and `coops` exited `0` with clean output in both offices, including `usage` token accounting for `nemotron-3.5-lightning-30b-a3b-nvfp4`. |

---

## Hand-Configured Items (Operator Friction Inventory)

Per the release-gate rules, nothing was hand-edited except the documented local-model `PROVIDER_LOCAL_*` settings:

1. **Pre-seeded `offices/<pod>/lab/.env` before running `./container/bootstrap.sh`:**
   ```dotenv
   PROVIDER_NAME=local
   PROVIDER_LOCAL_KIND=vllm
   PROVIDER_LOCAL_URL=http://172.16.0.11:8000
   PROVIDER_LOCAL_MODEL=nemotron-3.5-lightning-30b-a3b-nvfp4
   PROVIDER_LOCAL_TOKEN=local
   AGENT_PROVIDERS=architect=local,worker=local
   ```
   - **Why:** `container/bootstrap.sh` does not interactively prompt for `PROVIDER_LOCAL_*` or `AGENT_PROVIDERS`, and immediately starts the container at the end of the wizard (`docker compose ... up -d`). Pre-seeding those 6 lines before invoking `./container/bootstrap.sh` allowed `bootstrap.sh` to append `COMPOSE_PROJECT_NAME`, `POD_NAME`, `TENANT`, `ROLES`, `API_PORT`, `SESSION_PORT`, `TLS_DOMAIN`, `ALLOW_PLAINTEXT_API=1`, and auto-generate `API_TOKEN` (`0600`) without requiring a post-bootstrap `.env` edit and container restart.
2. **Interactive `./container/bootstrap.sh` invocation:**
   - Office X (`gatex/lab`):
     ```bash
     IMAGE_TAG=h-mesh:release-gate-a-bc00197 API_PORT=29080 SESSION_PORT=29081 \
       ./container/bootstrap.sh --pod gatex --tenant lab --roles "architect worker"
     ```
   - Office Y (`gatey/lab`):
     ```bash
     IMAGE_TAG=h-mesh:release-gate-a-bc00197 API_PORT=29082 SESSION_PORT=29083 \
       ./container/bootstrap.sh --pod gatey --tenant lab --roles "architect worker" --skip-build
     ```
   - At the interactive TLS prompt (`Domain name for automatic HTTPS (leave blank for plaintext HTTP):`), pressed `Enter` (`""`), then answered `y` to `Continue without TLS? [y/N]:`.

Zero other files, environment variables, Redis keys, or daemons were hand-configured or manually started.

---

## Step 1: Fresh Bootstrap Verification

### Evidence (`gatex/lab` and `gatey/lab`)

- **Generated artifacts and `.env` permissions (no secret values printed):**
  ```text
  gatex: .env mode=600 token_len=64 attach.sh mode=755 SUMMARY.md exists=yes
    keys=['AGENT_PROVIDERS', 'ALLOW_PLAINTEXT_API', 'API_PORT', 'API_TOKEN', 'COMPOSE_PROJECT_NAME', 'POD_NAME', 'PROVIDER_LOCAL_KIND', 'PROVIDER_LOCAL_MODEL', 'PROVIDER_LOCAL_TOKEN', 'PROVIDER_LOCAL_URL', 'PROVIDER_NAME', 'ROLES', 'SESSION_PORT', 'TENANT', 'TLS_DOMAIN']
  gatey: .env mode=600 token_len=64 attach.sh mode=755 SUMMARY.md exists=yes
    keys=['AGENT_PROVIDERS', 'ALLOW_PLAINTEXT_API', 'API_PORT', 'API_TOKEN', 'COMPOSE_PROJECT_NAME', 'POD_NAME', 'PROVIDER_LOCAL_KIND', 'PROVIDER_LOCAL_MODEL', 'PROVIDER_LOCAL_TOKEN', 'PROVIDER_LOCAL_URL', 'PROVIDER_NAME', 'ROLES', 'SESSION_PORT', 'TENANT', 'TLS_DOMAIN']
  ```

- **Daemons up automatically + published API port answers `401` unauthenticated (`200` authenticated inside container):**
  ```text
  gatex: published_unauth_http=401 internal_auth_http=200 daemons={'api': 172, 'interoffice_relay': 171, 'switch': 168, 'tmux_reconciler': 169, 'watchdog': 170}
  gatey: published_unauth_http=401 internal_auth_http=200 daemons={'api': 170, 'interoffice_relay': 169, 'switch': 166, 'tmux_reconciler': 167, 'watchdog': 168}
  ```

- **`attach.sh` interactive attach + `Ctrl-b d` detach check:**
  ```text
  gatex attach.sh: detached=True exit_code=0 captured_bytes=2176
  gatey attach.sh: detached=True exit_code=0 captured_bytes=2176
  ```

---

## Step 2: Inter-Office Pairing (`coop-grant` / `office-add`) & Wrong-Name Case

### 2a. Issue Cooperation Grants

- On **Office Y (`gatey`)**, issued grant for Office X (`gatex-peer`):
  ```text
  $ h-mesh-office coop-grant gatex-peer
  token:    <REDACTED_LEN_64>
  token-id: 907c541cd0bd40c9
  ```
- On **Office X (`gatex`)**, issued grant for Office Y (`gatey-peer`):
  ```text
  $ h-mesh-office coop-grant gatey-peer
  token:    <REDACTED_LEN_64>
  token-id: c6826466c148ff45
  ```

### 2b. Deliberate Wrong-Name Case (`--participant gatey-peer` on `gatex` instead of remote grant name `gatex-peer`)

1. **`office-add` command (using local grant name `gatey-peer` instead of remote grant name `gatex-peer`):**
   ```text
   $ h-mesh-office office-add gatey http://172.16.0.201:29082 --participant gatey-peer --token-stdin --allow-plaintext
   added office gatey -> http://172.16.0.201:29082 (as gatey-peer)
   (exit=0)
   ```
   *(Observation: `office-add` only writes local Redis state and does not make a preflight call to the remote office, so the participant mismatch is not caught at `office-add` time.)*

2. **Attempting `h-mesh-office send --office gatey` with the wrong participant name:**
   ```text
   $ h-mesh-office send --office gatey -a worker "test with wrong participant name"
   office: error: remote office refused the message: HTTP 403: 'as' must be the participant this token is bound to (refused by the remote office's cooperation policy)
   (exit=1)
   ```

3. **Background relay log (`interoffice_relay.log` on `gatex`) when polling with the mismatched participant name:**
   ```text
   2026-11-04 17:13:15,146 [WARNING] modules.interoffice.relay: relay office=gatey event=http-error status=404
   ```
   *(Because `GET /agents/gatey-peer/messages` on `gatey` checks `token_sub != name` and raises `KeyError`, which returns HTTP 404.)*

### 2c. Correct Pairing Using Remote Grant Participant Names

- On **Office X (`gatex`)**, updated `gatey` with `--participant gatex-peer`:
  ```text
  $ h-mesh-office office-add gatey http://172.16.0.201:29082 --participant gatex-peer --token-stdin --allow-plaintext
  updated office gatey -> http://172.16.0.201:29082 (as gatex-peer)
  ```
- On **Office Y (`gatey`)**, added `gatex` with `--participant gatey-peer`:
  ```text
  $ h-mesh-office office-add gatex http://172.16.0.201:29080 --participant gatey-peer --token-stdin --allow-plaintext
  added office gatex -> http://172.16.0.201:29080 (as gatey-peer)
  ```

---

## Step 3: Both-Way Inter-Office Send (`X -> Y` and `Y -> X`), Reply-Hint Line, and `correlation_id` Preservation

### 3a. Outbound Sends from Lead (`architect`) to Remote `worker`

- **Office X -> Office Y (`gatex/architect` -> `gatey/worker`):**
  ```text
  $ h-mesh-office send --office gatey -a worker "Hello gatey/worker from gatex/architect! Please reply with a single short acknowledgment using h-mesh-office send -a gatex-peer --reply-to <message-id-from-header> 'got it, closing ack from gatey/worker (no reply needed)'"
  sent to gatey/worker: 215 bytes (72fe4401fbbc4f4fadedc0574479f5b2)
  ```
  Outbound `xsent` record stored in `gatex` Redis (`pod:gatex:tenant:lab:xsent.s72fe4401fbbc4f4fadedc0574479f5b2`):
  ```json
  {
    "sender": "architect",
    "sender_incarnation": "98e5651032524248922090eb15bbed77",
    "correlation_id": "e1fb9046f2a148a4a3756c67652a83ee",
    "office": "gatey",
    "agent": "worker",
    "ts": "2026-11-04T17:14:20.568Z"
  }
  ```

- **Office Y -> Office X (`gatey/architect` -> `gatex/worker`):**
  ```text
  $ h-mesh-office send --office gatex -a worker "Hello gatex/worker from gatey/architect! Please reply with a single short acknowledgment using h-mesh-office send -a gatey-peer --reply-to <message-id-from-header> 'got it, closing ack from gatex/worker (no reply needed)'"
  sent to gatex/worker: 215 bytes (e4afa65bbee24456a7d1921e0b002cf1)
  ```
  Outbound `xsent` record stored in `gatey` Redis (`pod:gatey:tenant:lab:xsent.se4afa65bbee24456a7d1921e0b002cf1`):
  ```json
  {
    "sender": "architect",
    "sender_incarnation": "20863397fe1140d487a58d26b2a564fb",
    "correlation_id": "a557ec229be6494aa5a2870c7219467b",
    "office": "gatex",
    "agent": "worker",
    "ts": "2026-11-04T17:14:20.904Z"
  }
  ```

### 3b. Local-Model `worker` Replies and Relay Delivery Back to Lead

Both `gatey/worker` and `gatex/worker` (running Claude Code against `vllm` `nemotron-3.5-lightning-30b-a3b-nvfp4`) received the message in their tmux panes and executed `h-mesh-office send -a <peer> --reply-to <id> ...`:

- **`gatey/worker` pane excerpt:**
  ```text
  ❯ [message from gatex-peer] (id: 72fe4401fbbc4f4fadedc0574479f5b2) [from gatex/architect] Hello gatey/worker from gatex/architect! ...
  ⏺ Bash(h-mesh-office send -a gatex-peer --reply-to 72fe4401fbbc4f4fadedc0574479f5b2 'got it, closing ack from gatey/worker (no reply needed)')
    ⎿  sent to gatex-peer: 55 bytes (72ef76f97b044f72821c56d9c76bf6ca)
  ```
- **`gatex/worker` pane excerpt:**
  ```text
  ❯ [message from gatey-peer] (id: e4afa65bbee24456a7d1921e0b002cf1) [from gatey/architect] Hello gatex/worker from gatey/architect! ...
  ⏺ Bash(h-mesh-office send -a gatey-peer --reply-to e4afa65bbee24456a7d1921e0b002cf1 'got it, closing ack from gatex/worker (no reply needed)')
    ⎿  sent to gatey-peer: 55 bytes (be0bb083bbac4debbefcb2131c6c6137)
  ```

### 3c. Reply-Hint Line in Lead Panes (`gatex/architect` and `gatey/architect`)

- **`gatex` lead pane (`lab:architect`):**
  ```text
  ❯ [message from relay] [from gatey/worker] got it, closing ack from gatey/worker (no reply needed)
    [reply to gatey/worker: h-mesh-office send --office gatey -a worker "..."]
  ```
- **`gatey` lead pane (`lab:architect`):**
  ```text
  ❯ [message from relay] [from gatex/worker] got it, closing ack from gatex/worker (no reply needed)
    [reply to gatex/worker: h-mesh-office send --office gatex -a worker "..."]
  ```

### 3d. `correlation_id` Preservation Across the Relay

- **`gatex` (`switch.log` + `pod:gatex:tenant:lab:agent:architect:opened`):**
  - Outbound `correlation_id` recorded in `xsent.s72fe4401fbbc4f4fadedc0574479f5b2`: **`e1fb9046f2a148a4a3756c67652a83ee`**
  - Relayed reply envelope (`stream_id="dbde454508bd4f7eae838aa114309e72"`) delivered by `relay` to `architect`:
    ```text
    2026-11-04 17:14:22,190 [INFO] services.switch: event=Enqueue stream_id=dbde454508bd4f7eae838aa114309e72 correlation_id=e1fb9046f2a148a4a3756c67652a83ee sender=relay recipient=architect kind=Message
    2026-11-04 17:14:24,089 [INFO] modules.tmux.output: event=opened stream_id=dbde454508bd4f7eae838aa114309e72 correlation_id=e1fb9046f2a148a4a3756c67652a83ee sender=relay recipient=architect kind=Message
    ```
- **`gatey` (`switch.log` + `pod:gatey:tenant:lab:agent:architect:opened`):**
  - Outbound `correlation_id` recorded in `xsent.se4afa65bbee24456a7d1921e0b002cf1`: **`a557ec229be6494aa5a2870c7219467b`**
  - Relayed reply envelope (`stream_id="683a1194e43e450fa6c11f3396abeec1"`) delivered by `relay` to `architect`:
    ```text
    2026-11-04 17:14:22,223 [INFO] services.switch: event=Enqueue stream_id=683a1194e43e450fa6c11f3396abeec1 correlation_id=a557ec229be6494aa5a2870c7219467b sender=relay recipient=architect kind=Message
    2026-11-04 17:14:24,439 [INFO] modules.tmux.output: event=opened stream_id=683a1194e43e450fa6c11f3396abeec1 correlation_id=a557ec229be6494aa5a2870c7219467b sender=relay recipient=architect kind=Message
    ```

---

## Step 4: 10+ Minute Idle Watchdog Quiet Check & Local Ticket Flow

### 4a. 10+ Minute Idle Watchdog Check (`17:14:24.439Z` -> `17:28:51.406Z`, 14m 27s Idle)

Both `gatex` and `gatey` were left completely idle for **14 minutes 27 seconds** after the relayed replies opened at `2026-11-04T17:14:24.439Z`:

```text
Current UTC time: 2026-11-04T17:28:51.406Z (idle since 2026-11-04T17:14:24.439Z = >14m)
=== gatex ===
{
  "interoffice_participants": {
    "gatey-peer": "coop",
    "relay": "relay"
  },
  "architect_unreplied": {},
  "architect_unreplied_alerted": {},
  "worker_unreplied": {},
  "worker_unreplied_alerted": {},
  "watchdog_envelopes": [],
  "architect_pane_has_watchdog_alert": false,
  "worker_pane_has_watchdog_alert": false
}
--- gatex architect pane tail ---
    - Agent: architect (lead)
    - Tenant: lab
    - Status: architect idle, worker working
    - Tickets: empty (todo/doing/hold/done/invalid)
    - Peers: worker (tmux colleague), plus external interfaces (api, gatey-peer, host, relay)
    - Working directory: /workdir/architect (contains only AGENTS.md)
    - Memory: empty memory directory
    No tickets are currently available. The worker is already active. What task should I focus on?
  ✻ Churned for 10s · done 17:14

=== gatey ===
{
  "interoffice_participants": {
    "gatex-peer": "coop",
    "relay": "relay"
  },
  "architect_unreplied": {},
  "architect_unreplied_alerted": {},
  "worker_unreplied": {},
  "worker_unreplied_alerted": {},
  "watchdog_envelopes": [],
  "architect_pane_has_watchdog_alert": false,
  "worker_pane_has_watchdog_alert": false
}
--- gatey architect pane tail ---
    - Agent: architect in tenant "lab"
    - Status: idle
    - Peers: worker (currently working)
    - Task board: empty (todo, doing, hold, done all empty)
    - Office tools: h-mesh-office only
    - Directory: /workdir/architect contains only AGENTS.md
    No tickets are currently assigned. What work would you like me to handle?
  ✻ Brewed for 7s · done 17:14
```

- **Why no false unanswered-message alerts fired:**
  1. `pod:<pod>:tenant:lab:interoffice-participants` registers both the `coop` participant (`gatey-peer` / `gatex-peer`) and `relay` (`relay=relay`), so `services.watchdog.check_unanswered_messages` skips messages from `relay` (lines 715–717 of `services/watchdog.py`).
  2. When `worker` replied to the `coop` participant via `h-mesh-office send -a <peer> --reply-to <id>`, `core.service` cleared `agent:worker:unreplied` for `<peer>` because `recipient in interoffice_participants`.
  3. `watchdog.log` in both offices recorded only `event="usage"` entries and zero unanswered-message or idle alerts.

### 4b. Plain Local `tmux`-to-`tmux` Ticket Flow (`add` / `take` / `done`)

Executed inside `gatex`:
1. **`architect` adds ticket for `worker`:**
   ```text
   $ h-mesh-office add -a worker --title "Verify local ticket flow" --description "Plain tmux-to-tmux ticket test"
   db4494f703426e3e27604dbbbbfae0b2
   ```
2. **`worker` takes ticket:**
   ```text
   $ h-mesh-office take
   Title: Verify local ticket flow
   Priority: medium
   Status: doing
   Ticket: db4494f703426e3e27604dbbbbfae0b2
   Description:
   Plain tmux-to-tmux ticket test
   ```
3. **`worker` completes ticket:**
   ```text
   $ h-mesh-office done --outcome completed
   done db4494f703426e3e27604dbbbbfae0b2: "Verify local ticket flow" (completed)
   ```
4. **Board persistence (`switch.log`, `/home/ubuntu/.h-mesh/tasks.jsonl`, and `h-mesh-office list -a worker`):**
   - `switch.log`:
     ```text
     2026-11-04 17:14:12,356 [INFO] lib.board_interaction: event=board_write_confirmed stream_id=c725a54d2b4d4912a177203be813b311 correlation_id=1b80085d72db4f55812df50a7c73d1da sender=architect recipient=worker kind=AddTicket
     ```
   - `/home/ubuntu/.h-mesh/tasks.jsonl`:
     ```json
     {"timestamp": "2026-11-04T17:14:12.356137+00:00", "event": "add", "agent": "worker", "id": "db4494f703426e3e27604dbbbbfae0b2", "title": "Verify local ticket flow", "priority": "medium", "status": "todo", "created": "2026-11-04T17:14:12.349Z"}
     {"timestamp": "2026-11-04T17:14:13.849281+00:00", "event": "take", "agent": "worker", "id": "db4494f703426e3e27604dbbbbfae0b2", "title": "Verify local ticket flow", "status": "doing", "started": "2026-11-04T17:14:13.849Z"}
     {"timestamp": "2026-11-04T17:14:16.186919+00:00", "event": "done", "agent": "worker", "id": "db4494f703426e3e27604dbbbbfae0b2", "title": "Verify local ticket flow", "status": "done", "outcome": "completed", "completed": "2026-11-04T17:14:16.186Z"}
     ```
   - `h-mesh-office list -a worker`:
     ```text
     Agent: worker

     DOING
       (none)

     TODO
       (none)

     DONE (recent 5)
       db4494f7  Verify local ticket flow  age:1m  outcome:completed
     ```
   - **Note on board notices:** `AddTicket` (`modules/tmux/port.py` -> `lib.board_interaction.add_ticket`) writes to Redis (`tasks.todo`), `switch.log` (`board_write_confirmed`), and `/home/ubuntu/.h-mesh/tasks.jsonl`, and does not paste a terminal notification into the tmux pane (consistent with `AGENTS.md`: *"You have a task board. Nothing will notify you about it — check it yourself"*).

---

## Step 5: CLI Checks (`status`, `peers`, `list`, `usage`, `offices`, `coops`)

All commands ran as `architect` inside both `gatex` and `gatey` and exited `0`:

### Office X (`gatex/lab`)
```text
--- gatex: status --- (exit=0)
Tenant: lab  Pod: gatex  Lead: architect

AGENT      PROVIDER  INC#  INCARNATION       LAST ACTIVE  TODO  DOING
architect  local     1     98e5651032524248  3m ago          0  -
worker     local     1     a7fb5c8d3e4144de  1m ago          0  -

--- gatex: peers --- (exit=0)
Peer agents in lab:
  architect (lead)
  worker

--- gatex: peers -i --- (exit=0)
Peer agents in lab:
  architect (lead)
  worker
Interfaces in lab:
  gatey-peer (api)
  relay (api)

--- gatex: list --- (exit=0)
Agent: architect

DOING
  (none)

TODO
  (none)

DONE (recent 5)
  (none)

--- gatex: usage --- (exit=0)
agent      cli      model                     input   cache_r  cache_w   output       USD
architect  claude   nemotron-3.5-lightning-30b-a3b-nvfp4   96.0k         -        -     1.9k  unpriced
worker     claude   nemotron-3.5-lightning-30b-a3b-nvfp4   13.7k         -        -      386  unpriced
                                                                                   ------
                                                                                     0.00

--- gatex: offices --- (exit=0)
OFFICE  URL                        AS          PLAIN  ADDED
gatey   http://172.16.0.201:29082  gatex-peer  yes    2026-11-04T17:13:25Z

--- gatex: coops --- (exit=0)
PARTICIPANT  TOKEN ID          CREATED
gatey-peer   c6826466c148ff45  2026-11-04T17:12:51Z
```

### Office Y (`gatey/lab`)
```text
--- gatey: status --- (exit=0)
Tenant: lab  Pod: gatey  Lead: architect

AGENT      PROVIDER  INC#  INCARNATION       LAST ACTIVE  TODO  DOING
architect  local     1     20863397fe1140d4  3m ago          0  -
worker     local     1     34c8bf1f344d4160  1m ago          0  -

--- gatey: peers --- (exit=0)
Peer agents in lab:
  architect (lead)
  worker

--- gatey: peers -i --- (exit=0)
Peer agents in lab:
  architect (lead)
  worker
Interfaces in lab:
  gatex-peer (api)
  relay (api)

--- gatey: list --- (exit=0)
Agent: architect

DOING
  (none)

TODO
  (none)

DONE (recent 5)
  (none)

--- gatey: usage --- (exit=0)
agent      cli      model                     input   cache_r  cache_w   output       USD
architect  claude   nemotron-3.5-lightning-30b-a3b-nvfp4   65.6k         -        -     1.1k  unpriced
worker     claude   nemotron-3.5-lightning-30b-a3b-nvfp4   13.6k         -        -      365  unpriced
                                                                                   ------
                                                                                     0.00

--- gatey: offices --- (exit=0)
OFFICE  URL                        AS          PLAIN  ADDED
gatex   http://172.16.0.201:29080  gatey-peer  yes    2026-11-04T17:13:25Z

--- gatey: coops --- (exit=0)
PARTICIPANT  TOKEN ID          CREATED
gatex-peer   907c541cd0bd40c9  2026-11-04T17:12:51Z
```

---

## Operator / New-User UX Observations

1. **Local provider setup vs. interactive `bootstrap.sh`:**
   - `container/bootstrap.sh` prompts for pod, tenant, roles, provider (`claude`), and TLS domain, then immediately runs `docker compose ... up -d`. It does not prompt for `PROVIDER_LOCAL_*` or `AGENT_PROVIDERS`. A new operator using a local `vllm`/`ollama` backend either needs to pre-create `offices/<pod>/<tenant>/.env` with `PROVIDER_LOCAL_*` before running `bootstrap.sh`, or edit `.env` after initial startup and recreate the container.
2. **`office-add` does not validate `--participant` against the remote office at registration time:**
   - Running `h-mesh-office office-add <name> <url> --participant <wrong-name> --token-stdin` succeeds with `added office ...` even if `<wrong-name>` does not match the remote `coop-grant` token's bound subject. The mismatch is only discovered when `h-mesh-office send --office` fails with `HTTP 403: 'as' must be the participant this token is bound to` (while `interoffice_relay.log` logs `event=http-error status=404` on poll). A preflight check in `office-add` (or a reminder in `coop-grant` output showing the exact `office-add ... --participant <name>` command to run on the peer office) would prevent this operator mistake up front.

---

## Teardown Verification

- Removed compose projects `h-mesh-gatex-lab` and `h-mesh-gatey-lab` along with their volumes (`-v --remove-orphans`) and deleted `/tmp/release-gate-a-bc00197`.
- Per lead instruction, **kept image `h-mesh:release-gate-a-bc00197` (`054b7d5b8033`)** on `h-lab` for Gate B (`setup-agent`) to reuse.
- Operator containers (`h-mesh-default-home-h-mesh-1`, `h-mesh-default-ef-h-mesh-1`) and their volumes were untouched.

