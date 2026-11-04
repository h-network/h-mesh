# Headless `claude_sdk` Live Test (`live_to` / `live_cc_source` / `kind="Progress"`)

- **Date:** 2026-11-04
- **Base commit tested:** `7524990ce7bc465c9cd2010705522387ca5852db` (`origin/architect/combined-next`)
- **Host:** `h-lab` (`172.16.0.201`)
- **Throwaway install:**
  - Checkout: `/tmp/claude-sdk-proof-7524990/h-mesh` (`umask 022`)
  - Pod / Tenant: `claudesdktest` / `lab`
  - Compose project: `h-mesh-claudesdktest-lab`
  - Image tag: `h-mesh:claude-sdk-test-7524990`
  - Published ports: `API_PORT=28080`, `SESSION_PORT=28081` (`H_MESH_ALLOW_PLAINTEXT=1`)
  - Local model endpoint: `http://172.16.0.11:8000` (`nemotron-3.5-lightning-30b-a3b-nvfp4`)

---

## Summary Table

| Step | Scenario | Verdict | Summary |
| :--- | :--- | :--- | :--- |
| **1a** | Fresh install registration & `PROVIDER_LOCAL_*` out-of-the-box | **FAIL** | No `office hire` / `bootstrap.sh` path for `claude_sdk`; `modules/claude_sdk/port.py` ignores `PROVIDER_LOCAL_*` and Redis `providers:local`, spawning bundled `claude` with no credentials (`Not logged in · Please run /login (exit code: 1)` -> `open_unresolved`). |
| **1b** | **WORKAROUND**: `ANTHROPIC_*` exported into `core.service` env | **PASS** | Exporting `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, and `ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU}_MODEL` into the throwaway office's daemon env allows `claude_sdk` to run against local vLLM (`nemotron-3.5-lightning-30b-a3b-nvfp4`). |
| **2** | `Message` to `sdk-bot` **without** `live_to` | **PASS** | `sdk-bot` logs 3 internal hop events (`claude_sdk_query_started`, `claude_sdk_turn`, `claude_sdk_query_finished`), emits **0** `Progress` envelopes, and sends **1** final `Message` reply (`2b5abd044c92444895a535ca2a09b36b`) opened in `architect`'s tmux pane. |
| **3a** | `Message` with `live_to="architect"` (`tmux` destination) | **FAIL** | `sdk-bot` emits 3 ordered `Progress` envelopes (`58377ccd861848e0bc15a7277f58845e`, `297854c5dd6d435a9d6615418d6e668a`, `fda8efc92ca34dfdb717158681ce60bc`) + 1 final `Message` (`9d9578ac8adc4b8e92b7994ae3dd4bc1`), all with `correlation_id=aed357a14563479ebf20736b333171cb`. The switch forwards all 4, but `modules/tmux/port.py` has no `"Progress"` opener and **dead-letters all 3 `Progress` envelopes** into `pod:claudesdktest:tenant:lab:agent:architect:dead` (`reason="unknown kind: Progress"`). Only the final `Message` lands in `architect`'s pane. |
| **3b** | `Message` with `live_to="api-watcher"` + `live_cc_source=true` (`api` destinations) | **PASS** *(with `in_reply_to` caveat)* | All 3 `Progress` envelopes arrive in order in `GET /agents/api-watcher/messages` (`cd2e124d...`, `8652ed6e...`, `b0d6d081...`) and in `GET /agents/sender-a/messages` (`e97613c3...`, `e645f270...`, `a8f585e5...`), followed by the final `Message` (`aad2294b...`) in `sender-a`'s inbox, all correlated via `correlation_id=84803eb602b14694be1355704f02f130`. (`in_reply_to` is stripped by `modules/api/port.py` with `reply_correlation_dropped`; see §1.4). |
| **4a** | Negative case: `live_to` unauthorized for `source` | **PASS** *(pre-query `DeadLetter`, no reply)* | `restricted-sender` (`export=["public"]`) can reach `sdk-bot` (`import=["public"], export=["public","secret"]`), which can reach `secret-target` (`import=["secret"]`), but `restricted-sender` cannot reach `secret-target`. `_deliver_message` raises `DeadLetter("live_to not authorized for 'restricted-sender': 'secret-target'")` **before** `_run_query()` runs: incoming `Message` (`b386a80e96154b968bb2cf5b94d02d24`) lands in `pod:claudesdktest:tenant:lab:agent:sdk-bot:dead`, with **0** `Progress` envelopes and **0** final replies sent. |
| **4b** | Negative case: `live_cc_source="yes"` (non-boolean string) | **PASS** | `payload.get("live_cc_source") is True` evaluates to `False`. `api-watcher` receives the 3 `Progress` envelopes (`2b3aead3...`, `0a98adae...`, `0142962c...`), while `sender-a` receives **0** `Progress` envelopes and only the final `Message` reply (`ccc31a2e7e394a31b27e2a16e39996c0`). |
| **5** | Switch vs port acceptance of `kind="Progress"` | **PARTIAL** | **Switch (`core/service.py`)**: accepts and forwards `kind="Progress"` (header-only `parse_for_switch`). **`api` port (`modules/api/port.py`)**: accepts `kind="Progress"` into `inbox`. **`tmux`, `openshell`, `office`, and `claude_sdk` ports**: reject and dead-letter `kind="Progress"` with `reason="unknown kind: Progress"`. |

---

## 1. Code Trace & Hand-Configuration Required

### 1.1 Registration & Configuration Surface
Neither `setup.sh`, `container/bootstrap.sh`, nor `h-mesh-office hire` (`modules/office/cli.py` -> `modules/tmux/ops.py::hire_agent`) supports registering a `claude_sdk` agent (`hire_agent` hardcodes `port_type="tmux"`). To register `sdk-bot`:
1. Direct Redis registry write:
   ```
   HSET pod:claudesdktest:tenant:lab:registry sdk-bot claude_sdk
   ```
2. Optional `ClaudeAgentOptions` JSON string key (`pod:<pod>:tenant:<tenant>:agent:<agent>:sdk-options`, read via `GET`, **not** `HGETALL`):
   ```
   SET pod:claudesdktest:tenant:lab:agent:sdk-bot:sdk-options '{"system_prompt":"You are sdk-bot. Reply briefly.","max_turns":2}'
   ```

### 1.2 SDK, CLI, and `PROVIDER_LOCAL_*` Gap (Why Step 1a Failed Out-of-the-Box)
- Inside the image, `/opt/h-mesh` has `claude-agent-sdk==0.2.178`, which bundles `/opt/h-mesh/lib/python3.12/site-packages/claude_agent_sdk/_bundled/claude` (`SubprocessCLITransport._find_bundled_cli()` selects this binary before `/home/ubuntu/.local/bin/claude`).
- For `tmux` agents, `modules/tmux/ops.py::window_env` sets `AGENT_PROVIDER_URL`, `AGENT_PROVIDER_TOKEN`, and `AGENT_PROVIDER_MODEL` from Redis `providers:local`, and `/home/ubuntu/.local/bin/h-agent` (lines 323–353) translates those into `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_DEFAULT_OPUS_MODEL`, `ANTHROPIC_DEFAULT_SONNET_MODEL`, and `ANTHROPIC_DEFAULT_HAIKU_MODEL` before `exec claude`.
- By contrast, `modules/claude_sdk/port.py::deliver_claude_sdk` (lines 530–532) invokes `claude_agent_sdk.query()` directly (never `h-agent`) and only calls `lib/profile_env.py::resolve_claude_profile_env(profile)`, which exclusively maps `CLAUDE_OAUTH_TOKEN_<PROFILE-OR-DEFAULT>` -> `CLAUDE_CODE_OAUTH_TOKEN`. It ignores `PROVIDER_LOCAL_*` and Redis `providers:local`, and `ALLOWED_SDK_OPTION_FIELDS` (lines 142–146) blocks `"env"` and `"cli_path"` in `sdk-options`.
- Consequently, on a fresh install with only `PROVIDER_LOCAL_*` in `.env`, `sdk-bot` fails on its first query with:
  ```json
  {"ts":"2026-11-04T16:50:29.258Z","module":"claude_sdk","event":"received","writer":"claude_sdk","stream_id":"84994c967f554c28a65cd7129ac4b67b","correlation_id":"8b7aa09b6047426aba9d683407f463ae","source":"architect","destination":"sdk-bot"}
  {"ts":"2026-11-04T16:50:29.779Z","module":"claude_sdk","event":"claude_sdk_query_started","writer":"claude_sdk","stream_id":"84994c967f554c28a65cd7129ac4b67b","correlation_id":"8b7aa09b6047426aba9d683407f463ae","source":"architect","destination":"sdk-bot","evidence":"init"}
  {"ts":"2026-11-04T16:50:29.796Z","module":"claude_sdk","event":"claude_sdk_turn","writer":"claude_sdk","stream_id":"84994c967f554c28a65cd7129ac4b67b","correlation_id":"8b7aa09b6047426aba9d683407f463ae","source":"architect","destination":"sdk-bot","reason":"stop_reason=stop_sequence"}
  {"ts":"2026-11-04T16:50:29.797Z","module":"claude_sdk","event":"claude_sdk_query_finished","writer":"claude_sdk","stream_id":"84994c967f554c28a65cd7129ac4b67b","correlation_id":"8b7aa09b6047426aba9d683407f463ae","source":"architect","destination":"sdk-bot","reason":"is_error=True num_turns=1","evidence":"success"}
  {"ts":"2026-11-04T16:50:29.821Z","module":"claude_sdk","event":"open_unresolved","writer":"claude_sdk","stream_id":"84994c967f554c28a65cd7129ac4b67b","correlation_id":"8b7aa09b6047426aba9d683407f463ae","source":"architect","destination":"sdk-bot","reason":"opener failed: Claude Code returned an error result: Not logged in \u00b7 Please run /login (exit code: 1)"}
  ```
- Note: Because `_run_query` raises `ClaudeSdkError` (not `DeadLetter`), `core.channels.receive()` logs `open_unresolved` and `LREM`s the message from `processing` without pushing it to `dead` (`ingress=0, processing=0, dead=0`).

### 1.3 Sender Surface Gap
`h-mesh-office send` (`modules/office/cli.py`) only sends `{"text": ...}` and has no flags for `live_to` or `live_cc_source`. Sending a `Message` with `live_to` / `live_cc_source` requires calling `core.channels.send()` (or `POST /agents/<agent>/envelopes` with explicit `{"kind": "Message", "payload": {"text": "...", "live_to": "...", "live_cc_source": ...}}`).

### 1.4 `reply_correlation_dropped` on `Progress` Envelopes to `api` Participants
When `Progress` envelopes are delivered to an `api` participant (`modules/api/port.py`), `deliver_api` logs `reply_correlation_dropped` (`reason="in_reply_to was never delivered to the claimed source"`) and strips `in_reply_to` before `XADD`ing to `inbox` (while preserving `correlation_id` in the v4 header) for three reasons:
1. `claude_sdk` agents have no lifecycle `StartAgent` / `hire_agent` path, so `pod:<pod>:tenant:<tenant>:agent:<agent>:incarnation` is never minted unless written manually.
2. In `modules/claude_sdk/port.py` (line 466), `record_delivered` is called *after* `dispatch(message)` returns, whereas `on_hop` sends `Progress` envelopes *during* `dispatch(message)`.
3. Even if `record_delivered` ran before `dispatch(message)`, `record_delivered` binds `(agent, stream_id) -> source`. For any third-party `live_to != source`, `was_delivered(..., source=live_to)` returns `False`.

---

## 2. Workaround Applied After Capturing Unconfigured Failure

To exercise `claude_sdk` live against local vLLM on the throwaway container without modifying code, the daemons inside `h-mesh-claudesdktest-lab-h-mesh-1` were restarted with `h-agent`'s translated `ANTHROPIC_*` environment variables:

```bash
docker exec h-mesh-claudesdktest-lab-h-mesh-1 bash -c "
ANTHROPIC_BASE_URL=http://172.16.0.11:8000 \
ANTHROPIC_AUTH_TOKEN=local \
ANTHROPIC_DEFAULT_OPUS_MODEL=nemotron-3.5-lightning-30b-a3b-nvfp4 \
ANTHROPIC_DEFAULT_SONNET_MODEL=nemotron-3.5-lightning-30b-a3b-nvfp4 \
ANTHROPIC_DEFAULT_HAIKU_MODEL=nemotron-3.5-lightning-30b-a3b-nvfp4 \
h-mesh-upgrade --pod claudesdktest --tenant lab --skip-pull
"
```

---

## 3. Live Test Evidence

### Step 2: `Message` Without `live_to` (`PASS`)
- **Incoming `Message`:** `stream_id="8ed0dc453f6d4e8ca8078346b4dec4d6"`, `correlation_id="924fcd0cf1454b95b7042bff94465ce0"`, `source="architect"`, `destination="sdk-bot"`
- **Final `Message` reply:** `stream_id="2b5abd044c92444895a535ca2a09b36b"`, `correlation_id="8ed0dc453f6d4e8ca8078346b4dec4d6"`, `source="sdk-bot"`, `destination="architect"`
- **`Progress` envelopes emitted:** `0`

#### Switch Log Lines (`switch.log`)
```json
{"ts":"2026-11-04T16:53:19.564Z","module":"test","event":"sent","writer":"test","stream_id":"8ed0dc453f6d4e8ca8078346b4dec4d6","correlation_id":"924fcd0cf1454b95b7042bff94465ce0","source":"architect","destination":"sdk-bot"}
{"ts":"2026-11-04T16:53:19.564Z","module":"switch","event":"popped","writer":"switch","stream_id":"8ed0dc453f6d4e8ca8078346b4dec4d6","correlation_id":"924fcd0cf1454b95b7042bff94465ce0","source":"architect","destination":"sdk-bot"}
{"ts":"2026-11-04T16:53:19.565Z","module":"switch","event":"forwarded","writer":"switch","stream_id":"8ed0dc453f6d4e8ca8078346b4dec4d6","correlation_id":"924fcd0cf1454b95b7042bff94465ce0","source":"architect","destination":"sdk-bot"}
{"ts":"2026-11-04T16:53:19.565Z","module":"switch","event":"kick_started","writer":"switch","stream_id":"8ed0dc453f6d4e8ca8078346b4dec4d6","correlation_id":"924fcd0cf1454b95b7042bff94465ce0","source":"architect","destination":"sdk-bot"}
{"ts":"2026-11-04T16:53:19.793Z","module":"claude_sdk","event":"received","writer":"claude_sdk","stream_id":"8ed0dc453f6d4e8ca8078346b4dec4d6","correlation_id":"924fcd0cf1454b95b7042bff94465ce0","source":"architect","destination":"sdk-bot"}
{"ts":"2026-11-04T16:53:20.288Z","module":"claude_sdk","event":"claude_sdk_query_started","writer":"claude_sdk","stream_id":"8ed0dc453f6d4e8ca8078346b4dec4d6","correlation_id":"924fcd0cf1454b95b7042bff94465ce0","source":"architect","destination":"sdk-bot","evidence":"init"}
{"ts":"2026-11-04T16:53:21.076Z","module":"claude_sdk","event":"claude_sdk_turn","writer":"claude_sdk","stream_id":"8ed0dc453f6d4e8ca8078346b4dec4d6","correlation_id":"924fcd0cf1454b95b7042bff94465ce0","source":"architect","destination":"sdk-bot","reason":"stop_reason=None"}
{"ts":"2026-11-04T16:53:21.079Z","module":"claude_sdk","event":"claude_sdk_query_finished","writer":"claude_sdk","stream_id":"8ed0dc453f6d4e8ca8078346b4dec4d6","correlation_id":"924fcd0cf1454b95b7042bff94465ce0","source":"architect","destination":"sdk-bot","reason":"is_error=False num_turns=1","evidence":"success"}
{"ts":"2026-11-04T16:53:21.090Z","module":"claude_sdk","event":"sent","writer":"claude_sdk","stream_id":"2b5abd044c92444895a535ca2a09b36b","correlation_id":"8ed0dc453f6d4e8ca8078346b4dec4d6","source":"sdk-bot","destination":"architect"}
{"ts":"2026-11-04T16:53:21.090Z","module":"switch","event":"popped","writer":"switch","stream_id":"2b5abd044c92444895a535ca2a09b36b","correlation_id":"8ed0dc453f6d4e8ca8078346b4dec4d6","source":"sdk-bot","destination":"architect"}
{"ts":"2026-11-04T16:53:21.091Z","module":"claude_sdk","event":"opened","writer":"claude_sdk","stream_id":"8ed0dc453f6d4e8ca8078346b4dec4d6","correlation_id":"924fcd0cf1454b95b7042bff94465ce0","source":"architect","destination":"sdk-bot"}
{"ts":"2026-11-04T16:53:21.091Z","module":"switch","event":"forwarded","writer":"switch","stream_id":"2b5abd044c92444895a535ca2a09b36b","correlation_id":"8ed0dc453f6d4e8ca8078346b4dec4d6","source":"sdk-bot","destination":"architect"}
{"ts":"2026-11-04T16:53:21.091Z","module":"switch","event":"kick_started","writer":"switch","stream_id":"2b5abd044c92444895a535ca2a09b36b","correlation_id":"8ed0dc453f6d4e8ca8078346b4dec4d6","source":"sdk-bot","destination":"architect"}
{"ts":"2026-11-04T16:53:21.338Z","module":"tmux","event":"received","writer":"tmux","stream_id":"2b5abd044c92444895a535ca2a09b36b","correlation_id":"8ed0dc453f6d4e8ca8078346b4dec4d6","source":"sdk-bot","destination":"architect"}
{"ts":"2026-11-04T16:53:21.846Z","module":"tmux","event":"opened","writer":"tmux","stream_id":"2b5abd044c92444895a535ca2a09b36b","correlation_id":"8ed0dc453f6d4e8ca8078346b4dec4d6","source":"sdk-bot","destination":"architect"}
```

---

### Step 3a: `Message` With `live_to="architect"` (`FAIL` — Dead-Lettered by `tmux` Port)
- **Incoming `Message`:** `stream_id="aed357a14563479ebf20736b333171cb"`, `correlation_id="2f7350baa6c44df0ba2683332da42c5f"`, `source="architect"`, `destination="sdk-bot"`, `payload={"text": "Reply with the single word STEP3A_OK.", "live_to": "architect"}`
- **Emitted Envelopes from `sdk-bot` to `architect` (in order, all with `correlation_id="aed357a14563479ebf20736b333171cb"`):**
  1. `stream_id="58377ccd861848e0bc15a7277f58845e"` — `kind="Progress"`, `payload={"event":"claude_sdk_query_started","evidence":"init"}` -> **dead-lettered** by `tmux` port (`reason="unknown kind: Progress"`)
  2. `stream_id="297854c5dd6d435a9d6615418d6e668a"` — `kind="Progress"`, `payload={"event":"claude_sdk_turn","reason":"stop_reason=None"}` -> **dead-lettered** by `tmux` port (`reason="unknown kind: Progress"`)
  3. `stream_id="fda8efc92ca34dfdb717158681ce60bc"` — `kind="Progress"`, `payload={"event":"claude_sdk_query_finished","evidence":"success","reason":"is_error=False num_turns=1"}` -> **dead-lettered** by `tmux` port (`reason="unknown kind: Progress"`)
  4. `stream_id="9d9578ac8adc4b8e92b7994ae3dd4bc1"` — `kind="Message"`, `payload={"text":"...STEP3A_OK"}` -> **opened** by `tmux` port into `architect`'s pane

#### Switch Log Lines (`switch.log`)
```json
{"ts":"2026-11-04T16:53:24.567Z","module":"test","event":"sent","writer":"test","stream_id":"aed357a14563479ebf20736b333171cb","correlation_id":"2f7350baa6c44df0ba2683332da42c5f","source":"architect","destination":"sdk-bot"}
{"ts":"2026-11-04T16:53:24.567Z","module":"switch","event":"popped","writer":"switch","stream_id":"aed357a14563479ebf20736b333171cb","correlation_id":"2f7350baa6c44df0ba2683332da42c5f","source":"architect","destination":"sdk-bot"}
{"ts":"2026-11-04T16:53:24.567Z","module":"switch","event":"forwarded","writer":"switch","stream_id":"aed357a14563479ebf20736b333171cb","correlation_id":"2f7350baa6c44df0ba2683332da42c5f","source":"architect","destination":"sdk-bot"}
{"ts":"2026-11-04T16:53:24.568Z","module":"switch","event":"kick_started","writer":"switch","stream_id":"aed357a14563479ebf20736b333171cb","correlation_id":"2f7350baa6c44df0ba2683332da42c5f","source":"architect","destination":"sdk-bot"}
{"ts":"2026-11-04T16:53:24.822Z","module":"claude_sdk","event":"received","writer":"claude_sdk","stream_id":"aed357a14563479ebf20736b333171cb","correlation_id":"2f7350baa6c44df0ba2683332da42c5f","source":"architect","destination":"sdk-bot"}
{"ts":"2026-11-04T16:53:25.330Z","module":"claude_sdk","event":"claude_sdk_query_started","writer":"claude_sdk","stream_id":"aed357a14563479ebf20736b333171cb","correlation_id":"2f7350baa6c44df0ba2683332da42c5f","source":"architect","destination":"sdk-bot","evidence":"init"}
{"ts":"2026-11-04T16:53:25.331Z","module":"claude_sdk","event":"sent","writer":"claude_sdk","stream_id":"58377ccd861848e0bc15a7277f58845e","correlation_id":"aed357a14563479ebf20736b333171cb","source":"sdk-bot","destination":"architect"}
{"ts":"2026-11-04T16:53:25.331Z","module":"switch","event":"popped","writer":"switch","stream_id":"58377ccd861848e0bc15a7277f58845e","correlation_id":"aed357a14563479ebf20736b333171cb","source":"sdk-bot","destination":"architect"}
{"ts":"2026-11-04T16:53:25.331Z","module":"switch","event":"forwarded","writer":"switch","stream_id":"58377ccd861848e0bc15a7277f58845e","correlation_id":"aed357a14563479ebf20736b333171cb","source":"sdk-bot","destination":"architect"}
{"ts":"2026-11-04T16:53:25.332Z","module":"switch","event":"kick_started","writer":"switch","stream_id":"58377ccd861848e0bc15a7277f58845e","correlation_id":"aed357a14563479ebf20736b333171cb","source":"sdk-bot","destination":"architect"}
{"ts":"2026-11-04T16:53:25.594Z","module":"tmux","event":"received","writer":"tmux","stream_id":"58377ccd861848e0bc15a7277f58845e","correlation_id":"aed357a14563479ebf20736b333171cb","source":"sdk-bot","destination":"architect"}
{"ts":"2026-11-04T16:53:25.594Z","module":"tmux","event":"dead_lettered","writer":"tmux","stream_id":"58377ccd861848e0bc15a7277f58845e","correlation_id":"aed357a14563479ebf20736b333171cb","source":"sdk-bot","destination":"architect","reason":"unknown kind: Progress"}
{"ts":"2026-11-04T16:53:26.121Z","module":"claude_sdk","event":"claude_sdk_turn","writer":"claude_sdk","stream_id":"aed357a14563479ebf20736b333171cb","correlation_id":"2f7350baa6c44df0ba2683332da42c5f","source":"architect","destination":"sdk-bot","reason":"stop_reason=None"}
{"ts":"2026-11-04T16:53:26.121Z","module":"claude_sdk","event":"sent","writer":"claude_sdk","stream_id":"297854c5dd6d435a9d6615418d6e668a","correlation_id":"aed357a14563479ebf20736b333171cb","source":"sdk-bot","destination":"architect"}
{"ts":"2026-11-04T16:53:26.121Z","module":"switch","event":"popped","writer":"switch","stream_id":"297854c5dd6d435a9d6615418d6e668a","correlation_id":"aed357a14563479ebf20736b333171cb","source":"sdk-bot","destination":"architect"}
{"ts":"2026-11-04T16:53:26.122Z","module":"switch","event":"forwarded","writer":"switch","stream_id":"297854c5dd6d435a9d6615418d6e668a","correlation_id":"aed357a14563479ebf20736b333171cb","source":"sdk-bot","destination":"architect"}
{"ts":"2026-11-04T16:53:26.122Z","module":"switch","event":"kick_started","writer":"switch","stream_id":"297854c5dd6d435a9d6615418d6e668a","correlation_id":"aed357a14563479ebf20736b333171cb","source":"sdk-bot","destination":"architect"}
{"ts":"2026-11-04T16:53:26.127Z","module":"claude_sdk","event":"claude_sdk_query_finished","writer":"claude_sdk","stream_id":"aed357a14563479ebf20736b333171cb","correlation_id":"2f7350baa6c44df0ba2683332da42c5f","source":"architect","destination":"sdk-bot","reason":"is_error=False num_turns=1","evidence":"success"}
{"ts":"2026-11-04T16:53:26.128Z","module":"claude_sdk","event":"sent","writer":"claude_sdk","stream_id":"fda8efc92ca34dfdb717158681ce60bc","correlation_id":"aed357a14563479ebf20736b333171cb","source":"sdk-bot","destination":"architect"}
{"ts":"2026-11-04T16:53:26.128Z","module":"switch","event":"popped","writer":"switch","stream_id":"fda8efc92ca34dfdb717158681ce60bc","correlation_id":"aed357a14563479ebf20736b333171cb","source":"sdk-bot","destination":"architect"}
{"ts":"2026-11-04T16:53:26.128Z","module":"switch","event":"forwarded","writer":"switch","stream_id":"fda8efc92ca34dfdb717158681ce60bc","correlation_id":"aed357a14563479ebf20736b333171cb","source":"sdk-bot","destination":"architect"}
{"ts":"2026-11-04T16:53:26.129Z","module":"switch","event":"kick_started","writer":"switch","stream_id":"fda8efc92ca34dfdb717158681ce60bc","correlation_id":"aed357a14563479ebf20736b333171cb","source":"sdk-bot","destination":"architect"}
{"ts":"2026-11-04T16:53:26.139Z","module":"claude_sdk","event":"sent","writer":"claude_sdk","stream_id":"9d9578ac8adc4b8e92b7994ae3dd4bc1","correlation_id":"aed357a14563479ebf20736b333171cb","source":"sdk-bot","destination":"architect"}
{"ts":"2026-11-04T16:53:26.139Z","module":"switch","event":"popped","writer":"switch","stream_id":"9d9578ac8adc4b8e92b7994ae3dd4bc1","correlation_id":"aed357a14563479ebf20736b333171cb","source":"sdk-bot","destination":"architect"}
{"ts":"2026-11-04T16:53:26.140Z","module":"claude_sdk","event":"opened","writer":"claude_sdk","stream_id":"aed357a14563479ebf20736b333171cb","correlation_id":"2f7350baa6c44df0ba2683332da42c5f","source":"architect","destination":"sdk-bot"}
{"ts":"2026-11-04T16:53:26.140Z","module":"switch","event":"forwarded","writer":"switch","stream_id":"9d9578ac8adc4b8e92b7994ae3dd4bc1","correlation_id":"aed357a14563479ebf20736b333171cb","source":"sdk-bot","destination":"architect"}
{"ts":"2026-11-04T16:53:26.140Z","module":"switch","event":"kick_started","writer":"switch","stream_id":"9d9578ac8adc4b8e92b7994ae3dd4bc1","correlation_id":"aed357a14563479ebf20736b333171cb","source":"sdk-bot","destination":"architect"}
{"ts":"2026-11-04T16:53:26.384Z","module":"tmux","event":"received","writer":"tmux","stream_id":"297854c5dd6d435a9d6615418d6e668a","correlation_id":"aed357a14563479ebf20736b333171cb","source":"sdk-bot","destination":"architect"}
{"ts":"2026-11-04T16:53:26.384Z","module":"tmux","event":"dead_lettered","writer":"tmux","stream_id":"297854c5dd6d435a9d6615418d6e668a","correlation_id":"aed357a14563479ebf20736b333171cb","source":"sdk-bot","destination":"architect","reason":"unknown kind: Progress"}
{"ts":"2026-11-04T16:53:26.384Z","module":"tmux","event":"received","writer":"tmux","stream_id":"fda8efc92ca34dfdb717158681ce60bc","correlation_id":"aed357a14563479ebf20736b333171cb","source":"sdk-bot","destination":"architect"}
{"ts":"2026-11-04T16:53:26.384Z","module":"tmux","event":"dead_lettered","writer":"tmux","stream_id":"fda8efc92ca34dfdb717158681ce60bc","correlation_id":"aed357a14563479ebf20736b333171cb","source":"sdk-bot","destination":"architect","reason":"unknown kind: Progress"}
{"ts":"2026-11-04T16:53:26.384Z","module":"tmux","event":"received","writer":"tmux","stream_id":"9d9578ac8adc4b8e92b7994ae3dd4bc1","correlation_id":"aed357a14563479ebf20736b333171cb","source":"sdk-bot","destination":"architect"}
{"ts":"2026-11-04T16:53:26.896Z","module":"tmux","event":"opened","writer":"tmux","stream_id":"9d9578ac8adc4b8e92b7994ae3dd4bc1","correlation_id":"aed357a14563479ebf20736b333171cb","source":"sdk-bot","destination":"architect"}
```

#### Dead-Letter Queue (`pod:claudesdktest:tenant:lab:agent:architect:dead`)
```json
{"v": 4, "stream_id": "58377ccd861848e0bc15a7277f58845e", "correlation_id": "aed357a14563479ebf20736b333171cb", "l2": {"source": "sdk-bot", "destination": "architect"}, "ttl": 15, "hops": 1, "kind": "Progress", "ts": "2026-11-04T16:53:25.331Z", "l3": {"source": "claudesdktest:lab:sdk-bot", "destination": "claudesdktest:lab:architect"}, "payload": {"event": "claude_sdk_query_started", "evidence": "init"}, "in_reply_to": "aed357a14563479ebf20736b333171cb"}
{"v": 4, "stream_id": "297854c5dd6d435a9d6615418d6e668a", "correlation_id": "aed357a14563479ebf20736b333171cb", "l2": {"source": "sdk-bot", "destination": "architect"}, "ttl": 15, "hops": 1, "kind": "Progress", "ts": "2026-11-04T16:53:26.121Z", "l3": {"source": "claudesdktest:lab:sdk-bot", "destination": "claudesdktest:lab:architect"}, "payload": {"event": "claude_sdk_turn", "reason": "stop_reason=None"}, "in_reply_to": "aed357a14563479ebf20736b333171cb"}
{"v": 4, "stream_id": "fda8efc92ca34dfdb717158681ce60bc", "correlation_id": "aed357a14563479ebf20736b333171cb", "l2": {"source": "sdk-bot", "destination": "architect"}, "ttl": 15, "hops": 1, "kind": "Progress", "ts": "2026-11-04T16:53:26.128Z", "l3": {"source": "claudesdktest:lab:sdk-bot", "destination": "claudesdktest:lab:architect"}, "payload": {"event": "claude_sdk_query_finished", "evidence": "success", "reason": "is_error=False num_turns=1"}, "in_reply_to": "aed357a14563479ebf20736b333171cb"}
```

#### `architect` Tmux Pane Capture (`tmux capture-pane -p -t lab:architect`)
```text
 ▐▛███▛█   Claude Code v2.1.302
▝▜██████▀  nemotron-3.5-lightning-30b-a3b-nvfp4[1m] with high effort · API Usage Billing
 ▝▝   ▝▝   /workdir/architect
❯ [message from sdk-bot] The user wants me to reply with the single word "STEP2_OK". This is a simple instruction that
  doesn't require any tools or complex processing. I should just output exactly that word.</think>STEP2_OK
● The user wants me to reply with the single word "STEP2_OK". This is a simple instruction that doesn't require any
  tools or complex processing. I should just output exactly that word.
  The user also mentioned that this is a simple instruction that doesn't require any tools or complex processing.
  I should output just "STEP2_OK" as requested.</think>STEP2_OK
✻ Brewed for 0s · done 16:53
❯ [message from sdk-bot] The user wants me to reply with the single word "STEP3A_OK". This is a very simple request - I
  just need to output that exact word. No tools needed, no analysis required.</think>STEP3A_OK
● The user is asking me to reply with the single word "STEP3A_OK". This is another simple instruction that doesn't
  require any tools or complex processing. I should just output exactly that word.</think>STEP3A_OK
✻ Crunched for 0s · done 16:53
```

---

### Step 3b: `Message` With `live_to="api-watcher"` and `live_cc_source=True` (`PASS`)
- **Incoming `Message`:** `stream_id="84803eb602b14694be1355704f02f130"`, `correlation_id="3f07baadca444128b50c0ef4273cb667"`, `source="sender-a"`, `destination="sdk-bot"`, `payload={"text": "Reply with the single word STEP3B_OK.", "live_to": "api-watcher", "live_cc_source": true}`
- **`GET /agents/api-watcher/messages` (3 ordered `Progress` envelopes):**
  ```json
  {
    "agent": "api-watcher",
    "messages": [
      {
        "v": 4,
        "stream_id": "cd2e124db4c840ffa15bf045cfb0eaa2",
        "correlation_id": "84803eb602b14694be1355704f02f130",
        "l2": {"source": "sdk-bot", "destination": "api-watcher"},
        "ttl": 15,
        "hops": 1,
        "kind": "Progress",
        "ts": "2026-11-04T16:54:18.291Z",
        "l3": {"source": "claudesdktest:lab:sdk-bot", "destination": "claudesdktest:lab:api-watcher"},
        "payload": {"event": "claude_sdk_query_started", "evidence": "init"},
        "cursor": "1793811258549-0"
      },
      {
        "v": 4,
        "stream_id": "8652ed6ebfa34373b0905e5e9b7ff7b4",
        "correlation_id": "84803eb602b14694be1355704f02f130",
        "l2": {"source": "sdk-bot", "destination": "api-watcher"},
        "ttl": 15,
        "hops": 1,
        "kind": "Progress",
        "ts": "2026-11-04T16:54:18.766Z",
        "l3": {"source": "claudesdktest:lab:sdk-bot", "destination": "claudesdktest:lab:api-watcher"},
        "payload": {"event": "claude_sdk_turn", "reason": "stop_reason=None"},
        "cursor": "1793811259034-0"
      },
      {
        "v": 4,
        "stream_id": "b0d6d08190de4c93bb4c9117fda90681",
        "correlation_id": "84803eb602b14694be1355704f02f130",
        "l2": {"source": "sdk-bot", "destination": "api-watcher"},
        "ttl": 15,
        "hops": 1,
        "kind": "Progress",
        "ts": "2026-11-04T16:54:18.771Z",
        "l3": {"source": "claudesdktest:lab:sdk-bot", "destination": "claudesdktest:lab:api-watcher"},
        "payload": {"event": "claude_sdk_query_finished", "evidence": "success", "reason": "is_error=False num_turns=1"},
        "cursor": "1793811259034-1"
      }
    ],
    "next_cursor": "1793811259034-1"
  }
  ```
- **`GET /agents/sender-a/messages` (3 ordered `Progress` envelopes + 1 final `Message` reply):**
  ```json
  {
    "agent": "sender-a",
    "messages": [
      {
        "v": 4,
        "stream_id": "e97613c3a7a0491d935869913ab0f0de",
        "correlation_id": "84803eb602b14694be1355704f02f130",
        "l2": {"source": "sdk-bot", "destination": "sender-a"},
        "ttl": 15,
        "hops": 1,
        "kind": "Progress",
        "ts": "2026-11-04T16:54:18.292Z",
        "l3": {"source": "claudesdktest:lab:sdk-bot", "destination": "claudesdktest:lab:sender-a"},
        "payload": {"event": "claude_sdk_query_started", "evidence": "init"},
        "cursor": "1793811258548-0"
      },
      {
        "v": 4,
        "stream_id": "e645f270ae8c4ea890cc0c20344fab6d",
        "correlation_id": "84803eb602b14694be1355704f02f130",
        "l2": {"source": "sdk-bot", "destination": "sender-a"},
        "ttl": 15,
        "hops": 1,
        "kind": "Progress",
        "ts": "2026-11-04T16:54:18.766Z",
        "l3": {"source": "claudesdktest:lab:sdk-bot", "destination": "claudesdktest:lab:sender-a"},
        "payload": {"event": "claude_sdk_turn", "reason": "stop_reason=None"},
        "cursor": "1793811259117-0"
      },
      {
        "v": 4,
        "stream_id": "a8f585e578584e77b3493e15aabf876a",
        "correlation_id": "84803eb602b14694be1355704f02f130",
        "l2": {"source": "sdk-bot", "destination": "sender-a"},
        "ttl": 15,
        "hops": 1,
        "kind": "Progress",
        "ts": "2026-11-04T16:54:18.773Z",
        "l3": {"source": "claudesdktest:lab:sdk-bot", "destination": "claudesdktest:lab:sender-a"},
        "payload": {"event": "claude_sdk_query_finished", "evidence": "success", "reason": "is_error=False num_turns=1"},
        "cursor": "1793811259118-0"
      },
      {
        "v": 4,
        "stream_id": "aad2294b0f50444bac9cd77ae3228a99",
        "correlation_id": "84803eb602b14694be1355704f02f130",
        "l2": {"source": "sdk-bot", "destination": "sender-a"},
        "ttl": 15,
        "hops": 1,
        "kind": "Message",
        "ts": "2026-11-04T16:54:18.787Z",
        "l3": {"source": "claudesdktest:lab:sdk-bot", "destination": "claudesdktest:lab:sender-a"},
        "payload": {"text": "The user wants me to reply with the single word \"STEP3B_OK\". This is a very simple, direct request. I should just output that word as requested.\n\nThe user has specified a concise output style, so I'll just output the word as requested.</think>STEP3B_OK"},
        "cursor": "1793811259118-1"
      }
    ],
    "next_cursor": "1793811259118-1"
  }
  ```

---

### Step 4a: Negative Case — `live_to` Unauthorized for `source` (`PASS`)
- **Tag policy setup:**
  - `restricted-sender`: `export=["public"]`, `import=["public"]`
  - `sdk-bot`: `import=["public"]`, `export=["public", "secret"]`
  - `secret-target`: `import=["secret"]`
  - `restricted-sender -> sdk-bot` is allowed (`"public"`), and `sdk-bot -> secret-target` is allowed (`"secret"`), but `restricted-sender -> secret-target` is **denied** (`{"public"} & {"secret"} == set()`).
- **Incoming `Message`:** `stream_id="b386a80e96154b968bb2cf5b94d02d24"`, `correlation_id="1c228c8da0da4f63a9d608c4b17bd4c0"`, `source="restricted-sender"`, `destination="sdk-bot"`, `payload={"text": "Reply with the single word STEP4A_OK.", "live_to": "secret-target"}`
- **What happens:** `modules/claude_sdk/port.py::_deliver_message` (lines 386–387) checks `policy_allows(r, pod=pod, tenant=tenant, source=source, destination=live_to)` **before** `_run_query()` is called and raises `DeadLetter("live_to not authorized for 'restricted-sender': 'secret-target'")`.
  - The incoming `Message` (`b386a80e96154b968bb2cf5b94d02d24`) is moved to `pod:claudesdktest:tenant:lab:agent:sdk-bot:dead`.
  - `_run_query()` never runs; `GET /agents/secret-target/messages` is `[]` and `GET /agents/restricted-sender/messages` is `[]` (no `Progress` envelopes and **no final reply**).
- **Switch Log Lines (`switch.log`):**
  ```json
  {"ts":"2026-11-04T16:54:52.116Z","module":"test","event":"sent","writer":"test","stream_id":"b386a80e96154b968bb2cf5b94d02d24","correlation_id":"1c228c8da0da4f63a9d608c4b17bd4c0","source":"restricted-sender","destination":"sdk-bot"}
  {"ts":"2026-11-04T16:54:52.116Z","module":"switch","event":"popped","writer":"switch","stream_id":"b386a80e96154b968bb2cf5b94d02d24","correlation_id":"1c228c8da0da4f63a9d608c4b17bd4c0","source":"restricted-sender","destination":"sdk-bot"}
  {"ts":"2026-11-04T16:54:52.116Z","module":"switch","event":"forwarded","writer":"switch","stream_id":"b386a80e96154b968bb2cf5b94d02d24","correlation_id":"1c228c8da0da4f63a9d608c4b17bd4c0","source":"restricted-sender","destination":"sdk-bot"}
  {"ts":"2026-11-04T16:54:52.117Z","module":"switch","event":"kick_started","writer":"switch","stream_id":"b386a80e96154b968bb2cf5b94d02d24","correlation_id":"1c228c8da0da4f63a9d608c4b17bd4c0","source":"restricted-sender","destination":"sdk-bot"}
  {"ts":"2026-11-04T16:54:52.346Z","module":"claude_sdk","event":"received","writer":"claude_sdk","stream_id":"b386a80e96154b968bb2cf5b94d02d24","correlation_id":"1c228c8da0da4f63a9d608c4b17bd4c0","source":"restricted-sender","destination":"sdk-bot"}
  {"ts":"2026-11-04T16:54:52.346Z","module":"claude_sdk","event":"dead_lettered","writer":"claude_sdk","stream_id":"b386a80e96154b968bb2cf5b94d02d24","correlation_id":"1c228c8da0da4f63a9d608c4b17bd4c0","source":"restricted-sender","destination":"sdk-bot","reason":"live_to not authorized for 'restricted-sender': 'secret-target'"}
  ```
- **Dead-letter entry in `pod:claudesdktest:tenant:lab:agent:sdk-bot:dead`:**
  ```json
  {"v": 4, "stream_id": "b386a80e96154b968bb2cf5b94d02d24", "correlation_id": "1c228c8da0da4f63a9d608c4b17bd4c0", "l2": {"source": "restricted-sender", "destination": "sdk-bot"}, "ttl": 15, "hops": 1, "kind": "Message", "ts": "2026-11-04T16:54:52.116Z", "l3": {"source": "claudesdktest:lab:restricted-sender", "destination": "claudesdktest:lab:sdk-bot"}, "payload": {"text": "Reply with the single word STEP4A_OK.", "live_to": "secret-target"}}
  ```

---

### Step 4b: Negative Case — `live_cc_source="yes"` (Non-Boolean) (`PASS`)
- **Incoming `Message`:** `stream_id="ccf7c1a198c34ef497b1a860ba631007"`, `correlation_id="e72ba35358a74cb69d7b203f3b598e45"`, `source="sender-a"`, `destination="sdk-bot"`, `payload={"text": "Reply with the single word STEP4B_OK.", "live_to": "api-watcher", "live_cc_source": "yes"}`
- **`GET /agents/api-watcher/messages`:** 3 `Progress` envelopes (`2b3aead3f8f540f5aa56c8dc15c50391`, `0a98adae49ef4f68a55ce715c9ced8ab`, `0142962c015248d7abf87da828a06097`), all with `correlation_id="ccf7c1a198c34ef497b1a860ba631007"`.
- **`GET /agents/sender-a/messages`:** **0** `Progress` envelopes; only the 1 final `Message` reply (`stream_id="ccc31a2e7e394a31b27e2a16e39996c0"`, `correlation_id="ccf7c1a198c34ef497b1a860ba631007"`).

---

## 4. Sender & Verification Script Used Inside Container

```python
import json, subprocess, time, urllib.request, redis
from core.channels import send
from core.envelope import parse
from core.keys import prefix
from core.policy import tags_key
from services.tenant_config import read_tenant_env

r = redis.Redis.from_url("redis://127.0.0.1:6379/0", decode_responses=True)
pod, tenant = "claudesdktest", "lab"
api_token = read_tenant_env(tenant)["API_TOKEN"]

def api_get(path: str) -> dict:
    req = urllib.request.Request(
        f"http://127.0.0.1:8080{path}",
        headers={"Authorization": f"Bearer {api_token}"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())

# Register sdk-bot (claude_sdk) and configure sdk-options JSON string
r.hset(f"pod:{pod}:tenant:{tenant}:registry", "sdk-bot", "claude_sdk")
r.set(
    prefix(pod, tenant, "sdk-bot", "sdk-options"),
    json.dumps({"system_prompt": "You are sdk-bot. Reply briefly.", "max_turns": 2}),
)

# Step 2: Message without live_to
sid_step2 = send(
    r, pod=pod, tenant=tenant,
    source="architect", destination="sdk-bot",
    kind="Message",
    payload={"text": "Reply with the single word STEP2_OK."},
    module="test",
)

# Step 3a: Message with live_to="architect" (tmux)
sid_step3a = send(
    r, pod=pod, tenant=tenant,
    source="architect", destination="sdk-bot",
    kind="Message",
    payload={"text": "Reply with the single word STEP3A_OK.", "live_to": "architect"},
    module="test",
)

# Step 3b: Message with live_to="api-watcher" (api) and live_cc_source=True
r.hset(f"pod:{pod}:tenant:{tenant}:registry", mapping={"sender-a": "api", "api-watcher": "api"})
sid_step3b = send(
    r, pod=pod, tenant=tenant,
    source="sender-a", destination="sdk-bot",
    kind="Message",
    payload={"text": "Reply with the single word STEP3B_OK.", "live_to": "api-watcher", "live_cc_source": True},
    module="test",
)

# Step 4b: Negative case live_cc_source="yes" (non-bool)
sid_step4b = send(
    r, pod=pod, tenant=tenant,
    source="sender-a", destination="sdk-bot",
    kind="Message",
    payload={"text": "Reply with the single word STEP4B_OK.", "live_to": "api-watcher", "live_cc_source": "yes"},
    module="test",
)

# Step 4a: Negative case live_to unauthorized for source
r.hset(f"pod:{pod}:tenant:{tenant}:registry", mapping={"restricted-sender": "api", "secret-target": "api"})
r.hset(tags_key(pod, tenant, "restricted-sender"), mapping={"export": json.dumps(["public"]), "import": json.dumps(["public"])})
r.hset(tags_key(pod, tenant, "sdk-bot"), mapping={"import": json.dumps(["public"]), "export": json.dumps(["public", "secret"])})
r.hset(tags_key(pod, tenant, "secret-target"), mapping={"import": json.dumps(["secret"])})
sid_step4a = send(
    r, pod=pod, tenant=tenant,
    source="restricted-sender", destination="sdk-bot",
    kind="Message",
    payload={"text": "Reply with the single word STEP4A_OK.", "live_to": "secret-target"},
    module="test",
)
```
