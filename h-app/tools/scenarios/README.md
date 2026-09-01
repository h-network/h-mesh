# Stress/conservation scenarios (ported from h-flock)

Host-native ports of h-flock's `container/scenarios/` stress harness
(`/workdir/architect/repo/container/scenarios/` on the architect's checkout —
read-only reference, never modified). h-flock ran these inside a Docker
container via a `dx() { docker exec ...; }` wrapper and read custody from
`docker logs`. h-mesh has no container: everything here runs directly on the
host (run it *on* the target VM, or over `ssh host ...`), and custody comes
from the switch daemon's own stdout, which `setup.sh` already durably
redirects to `$H_MESH_RUN_DIR/switch.log`.

The message-bus vocabulary carried over almost unchanged — h-flock's
`flock.bus`/`flock.switch`/`flock.port` map onto h-mesh's `core.envelope` +
`core.keys` + `core.registry` + `core.service.Switch` + a per-kick
`modules.<port_type>.port <agent>` subprocess — same Redis key shape
(`pod:tenant:agent:{egress,ingress,dead,delivering}`), same envelope fields.
`reconcile-unicast.py` and `analyse-run.py` are copied here **unmodified**:
both are pure custody-log readers with no `flock`-specific imports, so they
needed no porting at all.

## conservation.sh

Message conservation under injected switch and port death. Negative controls
(terminal-strand classification, an intentional duplicate, an intentional
silent loss) prove the harness itself can go red before trusting its
positive result, then a "clean stressed run" fires real switch-kill and
port-kill injections into live traffic and reconciles what actually arrived
against what was sent.

```
TENANT=my-throwaway-tenant STATIONS=20 ROUNDS=50 \
  ./conservation.sh
```

Requires a tenant whose daemons (`core.service`, `services.tmux_reconciler`)
are already running — e.g. via `setup.sh` — and a Python with h-mesh
editable-installed (`PYTHON=/path/to/venv/python`, defaults to `python3`).

Synthetic "cons-N" stations are registered as port_type **`tmux`**, not
h-flock's mailbox-only `api` — a deliberate choice, not a fallback: it
piggybacks on the tenant's real `tmux_reconciler` to create plain `bash -il`
windows (no `launch` key, so no CLI starts) and so exercises the actual
production window-creation and delivery path. Switch the registry's
`port_type` to `"api"` in `seed_stations()` (and drop `wait_for_windows`) for
a lighter, mailbox-only run instead.

Scale defaults to `STATIONS=20 ROUNDS=50` (1,000 messages), down from
h-flock's `STATIONS=100 ROUNDS=100` (10,000) — a first-pass size for a single
VM; the injection schedule (3 switch-kills interleaved with 5 port-kills)
scales with `STATIONS*ROUNDS` rather than hardcoded line counts, so raising
either env var raises the traffic proportionally.

**Not ported in this pass:** h-flock's `BUILD67` (memory-ceiling stress under
a paused destination) and `BROADCAST69` (fan-out conservation) modes — both
are opt-in in the original (`BUILD67=1` / `BROADCAST69=1`), not part of its
default run, left for a follow-up once the default flow's value is confirmed.

**Live finding from the first real run (2026-09-01):** `core/dispatch.py`'s
`delivery_lock()` was a bare Redis `HSETNX` with no lease/TTL. A port process
killed while holding it (exactly what a `port-kill` injection does) never
released it — every subsequent kicked delivery to that agent spun forever in
the lock's retry loop, and the message(s) queued for it never drained. In a
60-message/6-station run this stranded 28 messages (47%) permanently, queue
depth flat for the rest of the run. **Fixed and reverified same day** —
`switch-agent/delivery-lock-lease` (leased `delivering` entries) reduces the
same 60-message run to `stranded=0 lost_unexplained=0`, negative controls
still pass, total run time ~10min → ~30s.

## payload-ack.sh

Send-and-verify-receipt: a bespoke consumer (`payload-ack-port.py`) checksum-
verifies each message and Acks it back to the sender; `payload-ack-judge.py`
reconciles five stages (sent → opened → verified → ack_sent → ack_opened)
from custody alone, gated by a harness-known expected count so a dropped
record can't make the books look clean by accident.

```
TENANT=my-throwaway-tenant COUNT=4 ROUNDS=20 \
  ./payload-ack.sh
```

Same daemon/Python requirements as `conservation.sh`. `payload-ack-judge.py`
is copied unmodified (pure custody-log reasoning, no `flock` imports).

**A real finding surfaced and fixed here too:** the first run showed
`verified=19` against `expected=20` — not a dropped write. Byte-level
inspection (`cat -A`) after full process exit showed the record was present
but glued onto the tail of an *unrelated* line with no separating newline —
stderr from the switch's own kick attempt (this scenario originally routed
its synthetic stations through a port_type with no real module, letting the
kick fail on import) landed on the same shared log fd, at the same instant,
as this scenario's own JSON write, tearing one legitimate line into two
processes' output glued together with no boundary — unparseable by both
`payload-ack-judge.py` and `reconcile-unicast.py`'s naive line-by-line
`json.loads`. Confirmed via `strace` that both writers use a single atomic
`write()` syscall each; the collision is a framing race between two
*independent* processes sharing one fd (the switch's kick children inherit
its stdout/stderr — `core/service.py`'s `transmission()` — with nothing
coordinating them against unstructured output). Not h-mesh-specific to this
port: h-flock has the identical exposure (all processes share one container
stdout via `docker logs`); this harness just made it deterministic by giving
the switch a guaranteed-failing import on every single kick. Fixed *in this
script* by installing a real, silent no-op at the module path instead of
relying on the import failure (see the header comment) — reruns since are
clean (`5/5` stages matching, `rc=0`, verified at `COUNT=2..4`,
`ROUNDS=10..20`). The underlying fd-sharing exposure is real and general;
reported to architect and switch-agent, who isolated kicked-port
stdout/stderr onto its own file (`ports.log`) with a validated pipe for real
custody records — verified live: 30 forced import failures plus 1 valid
delivery, `switch.log` malformed count 0, all 5 custody stages preserved for
the valid stream, all 30 failures present in `ports.log` and absent from
`switch.log`.

`payload-ack-judge.py` was also hardened here: it now counts nonblank lines
that fail `json.loads` and fails (`rc=7`) on that count alone, even when
every stage total happens to balance — a torn/dropped line can lower one
stage by exactly one without necessarily moving the harness's known-expected
comparison off balance, so "stages match" was never actually proof nothing
was lost. Verified: a synthetic torn line hand-appended to an otherwise
clean, fully-balanced log flips the result from "clean" to `rc=7` under the
patched version. `reconcile-unicast.py` did not need the same fix — it
already gates its exit code on its own parse-failure counters.

## tmux-window-loss.sh

Proves observable at-most-once loss plus terminal recovery, not delivery: a
message sent while its window is absent is dead-lettered `window_missing`,
never opened, and reconciliation restores exactly one fresh pane (new pane
PID, old one gone). `services.tmux_reconciler` maps onto h-flock's
`flock.tmuxhost` (same SIGSTOP/SIGCONT mechanic to open a recreate-gap).
Starts the real `services.api` REST server since `setup.sh` doesn't run it
by default.

```
TENANT=my-throwaway-tenant ./tmux-window-loss.sh
```

One real difference from h-flock's version: it required a non-empty
`launch` key as a precondition (its fixture agents always had a concrete
CLI). A bare `bash -il` window with no `launch` key at all is a normal,
valid h-mesh state (see `conservation.sh`'s stations), so this port only
requires `port_type=tmux`. Clean pass on first real run — a positive result,
h-mesh handles this correctly.

## tmux-boundary.sh

Checks that credentials (`API_TOKEN`, `REDIS_PASSWORD`, `REDISCLI_AUTH`,
`REDIS_URL`) are invisible both to tmux's global environment and to real
pane processes' `/proc/<pid>/environ`. No docker exec boundary to cross here
— pane processes and this script run as the same host user, so `/proc`
reads are direct.

```
TENANT=my-throwaway-tenant ./tmux-boundary.sh
```

Clean pass on first real run — another positive result.

## tmux-concurrent-hire.sh

Two `StartAgent` requests for the same never-before-seen agent name, fired
concurrently: exactly one window should exist afterward, no duplicate or
split registry state, and an identical unchanged rehire stays idempotent.

```
TENANT=my-throwaway-tenant ./tmux-concurrent-hire.sh
```

Uses **real** `claude` hires against the local vLLM endpoint (see
`reference-vllm-endpoint` in project memory) via a provider name, per the
ticket's own instruction to hire real agents rather than test a toy. Races
claude against *itself* rather than h-flock's claude-vs-codex: `h-agent`'s
own policy is that codex (and agy) refuse to start under a local provider
at all, so racing them here would just be "claude always wins because codex
always refuses" — not a real race. The property under test (does concurrent
StartAgent for one new name produce exactly one window) is fully exercised
by two same-cli concurrent requests.

**Needs a workaround, not a clean environment:** a freshly `setup.sh`'d host
cannot hire any real CLI at all. `modules/tmux/ops.py`'s `window_env()`
constructs the hired pane's `PATH` explicitly and never includes wherever
`h-agent` itself was installed — on `setup.sh`'s own documented default
(`${PREFIX:-$HOME/.local}/bin`), every hire fails: window created, pane's
`execvp("h-agent")` fails with ENOENT, window destroyed (`remain-on-exit` is
off) within milliseconds, reconciler retries forever with capped backoff.
Confirmed directly (pulled the real constructed `PATH` string, `env -i
PATH="<that>" which h-agent` → not found) and three times live via real
`StartAgent` envelopes. Reported to architect, routed to tmux-agent as
`d137fc18` (unfixed as of this writing). This script fails fast with a
clear reason if the workaround isn't applied — **prepend `h-agent`'s bin
directory to the RECONCILER daemon's own `PATH` before starting it**, since
`window_env()` reads `os.environ` at construction time. Once that's in
place, real hires work end to end (verified: a real `claude` pane connected
to `nemotron-lightning`, correct workdir, permissions bypassed, fully
interactive). **Every result from this script reflects that workaround, not
a clean install — don't read a pass here as proof the PATH bug is fixed.**
