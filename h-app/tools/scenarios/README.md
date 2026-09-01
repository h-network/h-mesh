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

**Known live finding from the first real run (2026-09-01, reported to
architect separately):** `core/dispatch.py`'s `delivery_lock()` is a bare
Redis `HSETNX` with no lease/TTL. A port process killed while holding it
(exactly what a `port-kill` injection does) never releases it — every
subsequent kicked delivery to that agent spins forever in the lock's retry
loop, and the message(s) queued for it never drain. In a 60-message/6-station
run this stranded 28 messages (47%) permanently, with the queue depth flat
for the rest of the run. Not fixed here, per this ticket's scope — reported
for architect to route.
