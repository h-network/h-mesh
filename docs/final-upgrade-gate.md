# Final upgrade gate: rc-next-3

> Historical record: counts, commits and offices are as of 2026-11-04; see docs/inter-office.md for current behaviour.

Runtime candidate: `86dcddffc0664efdc9612d8f9f28fe5099f66686`
(`origin/architect/rc-next-3`). This branch adds evidence only; no runtime,
test, manifest or deployment configuration changes.

Result: **PASS**, proof process exit `0`: both operator-shape offices, the
tokenless develop container, and the native old/second/third transition.

The operator baseline was corrected before verification: those offices were
created at `91afbe654bd3766cc5c45d5de488002be0a2b462`, with a token already
present. The older tokenless develop baseline is a separate migration case.
No operator office was inspected or changed by this proof.

## Container commands and checks

The fresh proof checkout was
`/home/h-lab/ci/setup-final-upgrade.c9k08h5a/repo` (`$R` below).
Only the pod names and published ports differ from the reported operator
shape. Tenants remain `home` and `ef`; both use `AGENTS=architect`,
`DEFAULT_CLI=claude`, `H_MESH_ALLOW_PLAINTEXT=1`, no Telegram and no TLS.
There were no supplied API/model credentials and no edits to generated `.env`.

The candidate image belongs to docker-agent and was reused with `--skip-build`:

```text
h-mesh:rc-next-3-86dcddf
sha256:f0f961b0d9a77d04a91179daf37bd05fed4b676909a7f9fcdf24f51fb4d70a3d
```

It was never retagged or removed. The owned `91afbe6` image had been built
from that exact checkout in the first attempt, then reused after disk recovery.

```bash
git clone --quiet --single-branch --branch architect/rc-next-3 \
  https://github.com/h-network/h-mesh.git "$R"
git -C "$R" checkout --quiet --detach 91afbe654bd3766cc5c45d5de488002be0a2b462

env -i HOME="$HOME" PATH=/usr/local/bin:/usr/bin:/bin \
  H_MESH_IMAGE=h-mesh-setup-final-gate:operator-91afbe6 \
  AGENTS=architect DEFAULT_CLI=claude H_MESH_ALLOW_PLAINTEXT=1 \
  "$R/container/bootstrap.sh" --pod setup-final-ops-c9k08h5a --tenant home \
  --api-port 18410 --session-port 18411 --non-interactive --skip-build
# Repeat for tenant ef with ports 18412/18413.
```

For each old office, an isolated helper invoked the old CLI and old
`core.channels.send` to create four tickets, one persisted API-client inbox
envelope, and unanswered messages from `teleclient` and a real old-code
`coop-grant legacycoop` participant. The token from that grant was captured
privately and discarded. The helper saved the exact registry, incarnation
IDs, ticket records, inbox bytes and cooperation records. It aged the two
unanswered entries and seeded a legacy cooperation alert crossing.

Each old container was removed while retaining its three volumes. Every
Compose command used its explicit project, for example:

```bash
H_MESH_IMAGE=h-mesh-setup-final-gate:operator-91afbe6 \
H_MESH_TENANT_ENV_FILE="$R/offices/setup-final-ops-c9k08h5a/home/.env" \
docker compose -p h-mesh-setup-final-ops-c9k08h5a-home \
  -f "$R/container/compose.yaml" -f "$R/container/compose.ports.yaml" \
  --env-file "$R/offices/setup-final-ops-c9k08h5a/home/.env" down --remove-orphans
# Same explicit ef project after its seeding; then remove only the owned image:
docker image rm h-mesh-setup-final-gate:operator-91afbe6

git -C "$R" checkout --quiet --detach 86dcddffc0664efdc9612d8f9f28fe5099f66686
env -i HOME="$HOME" PATH=/usr/local/bin:/usr/bin:/bin \
  H_MESH_IMAGE=h-mesh:rc-next-3-86dcddf \
  "$R/container/bootstrap.sh" --pod setup-final-ops-c9k08h5a --tenant home \
  --api-port 18410 --session-port 18411 --non-interactive --skip-build
# Repeat for ef, ports 18412/18413. No plaintext/token overrides were needed.
```

Both upgraded offices retained their original API tokens exactly once in a
mode-0600 `.env`, retained the plaintext setting and all saved state, and ran
exactly one switch, tmux reconciler, watchdog, API and relay. Each published
API returned HTTP 401 without credentials. The new watchdog silently removed
the old cooperation backlog and alert crossing, retained the client backlog,
and delivered a new post-upgrade unanswered-client alert.

After upgrading both offices, the proof ran these commands in each direction:

```bash
# Inside the explicitly named owned container, AGENT_NAME=architect, TENANT=home/ef:
h-mesh-office coop-grant visitor
h-mesh-office office-add remote http://172.16.0.201:18412 --token-stdin --allow-plaintext
h-mesh-office offices --check
h-mesh-office send --office remote -a architect "<owned proof request>"
# At the recipient, after normal delivery:
h-mesh-office send -a visitor --reply-to "<accepted stream_id>" "<owned proof reply>"
```

The grant was passed on stdin without being logged. `office-add` inferred
`visitor` through the upgraded remote `/whoami`; `offices --check` reported
`link: ok`. Actual accepted POSTs and correlated SSE replies opened in both
directions. Repeating each candidate bootstrap command then left `.env` and
tenant config byte-identical, with the same token, daemon PIDs and windows,
while preserving all original state.

## Tokenless develop case

The checkout was switched to `4d1f3889c37998f2de040d911147d3a89766a6d1`.
Architect explicitly authorized one initial legacy `.env` fixture before the
real old bootstrap, containing only POD, TENANT, DEFAULT_CLI, plaintext opt-in
and ports. This was necessary because that old bootstrap only persisted the
plaintext choice through its Telegram wizard. No later `.env` edits occurred.

The real old command used pod `setup-final-legacy-c9k08h5a`, tenant `legacy`,
ports `18414/18415`, and owned image
`h-mesh-setup-final-gate:legacy-4d1f388`. It started without an API token, API
or relay, and its published API returned HTTP 000. The same state fixture
used old API participants `teleclient` and `relay`; the latter had a declared
pre-marker address-book entry because develop had no cooperation API.

After removing only the old container and its owned image, the checkout was
switched to the candidate and its bootstrap was rerun with the borrowed image
and `--skip-build`, preserving the old named volumes. The exact commands are
in the command evidence below.

Result: one new token, exactly once and mode 0600 in both office and tenant
config; API and relay running with the original daemons; published HTTP 401;
all original tickets, inbox bytes, registry and incarnation IDs preserved.
The legacy relay backlog and old alert crossing were pruned silently while
a fresh client alert was delivered. A second candidate bootstrap retained
the same token, configuration, state, daemon PIDs and window.

## Native three-run handoff

The native proof uses a separate fresh develop clone, private venv, HOME,
state/run/work directories and tmux socket. Redis binds an ephemeral loopback
port; its identity is verified against the sandbox's unique Unix socket.
The API binds `127.0.0.1:0`. The roster is intentionally empty, with no model
or credentials needed. The local old `develop` branch tracks the pinned
`origin/architect/rc-next-3` for its ordinary upgrade pull.

```bash
git clone --quiet --branch develop https://github.com/h-network/h-mesh.git "$N/repo"
python3 -m venv "$N/venv"
"$N/venv/bin/python" -m pip install --no-cache-dir -e "$N/repo"
git -C "$N/repo" branch --set-upstream-to=origin/architect/rc-next-3 develop

"$N/repo/setup.sh" --host --pod setup-final-native --tenant hostproof \
  --redis-url "$PRIVATE_REDIS_URL" --session hostproof \
  --tmux-socket "$N/tmux.sock" --tmux-tmpdir "$N/tmux" --venv "$N/venv" \
  --skip-deps --skip-install --non-interactive

# Old loaded interpreter pulls/installs the candidate:
"$N/venv/bin/h-mesh-upgrade" --pod setup-final-native --tenant hostproof \
  --redis-url "$PRIVATE_REDIS_URL" --session hostproof \
  --tmux-socket "$N/tmux.sock" --tmux-tmpdir "$N/tmux" --venv "$N/venv"

# Documented fresh second step, then an identical third invocation:
"$N/venv/bin/h-mesh-upgrade" --pod setup-final-native --tenant hostproof \
  --redis-url "$PRIVATE_REDIS_URL" --session hostproof \
  --tmux-socket "$N/tmux.sock" --tmux-tmpdir "$N/tmux" --venv "$N/venv" \
  --skip-pull --skip-install
```

The actual sandbox was `/home/h-lab/ci/setup-final-native.cijc9a1u`.

| Invocation | Observed result |
|---|---|
| Old ordinary upgrade | Exit 0; Git reaches `86dcddf`, but token, API and relay remain absent, reproducing the documented old loaded-code gap. |
| Fresh second invocation | Exit 0; exactly one mode-0600 token; API and relay plus the original three daemons; API bound to loopback. |
| Identical third invocation | Exit 0; byte-identical config and token; old PIDs replaced by clean restarts; exactly one live process per daemon module. |

Native exact argv evidence:
`/home/h-lab/ci/logs/setup-final-native-commands-cijc9a1u.json`.

## Isolation and evidence

All execution was on the h-lab VM, under `~/ci/suite.lock` in transient
unit `setup-final-upgrade-3288661.service`, with `KillMode=control-group`.
No default/home, default/ef, laba or labb office was targeted. The borrowed
candidate image remained present with the identical image ID.

The initial attempt at 20:26 UTC exhausted disk while saving evidence after
the `91afbe6` build; it established no candidate defect. Architect pruned
regenerable build cache, and the only leftover owned project/volumes were
removed explicitly. The successful retry reused that already built image.
Every later build/up boundary checked at least 2.5 GiB free; neither images
nor volumes were pruned. Owned old images were removed after their last seed.

Proof log: `/home/h-lab/ci/logs/setup-final-upgrade-20261104T203016Z.log`.

Exact argv and per-command output:
`/home/h-lab/ci/logs/setup-final-upgrade-commands-c9k08h5a`.
