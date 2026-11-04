#!/usr/bin/env bash
# Throwaway two-office proof for switch-agent/interoffice-no-unanswered.
# Stages: up | pair | traffic | watch | down. Run only while no suite run is in progress
# (the original run was serialized with suite runs by holding ~/ci/suite.lock).
# Only compose projects h-mesh-nua-a-proof / h-mesh-nua-b-proof and image
# h-mesh-nua-proof:a16a05e are ever touched. No token value is printed.
set -Eeuo pipefail
umask 077
ci="$HOME/ci"
SHA=a16a05e7813c013d486eadc28f6f95bb372e2808
BRANCH=switch-agent/interoffice-no-unanswered
root="$ci/nua-proof"
repo="$root/repo"
image=h-mesh-nua-proof:a16a05e
tenant=proof
HOSTIP=172.16.0.201
port_a=18110; sess_a=18111; port_b=18112; sess_b=18113
stage="${1:?stage}"
mkdir -p "$ci/logs"
exec > >(tee -a "$ci/logs/nua-proof-$stage-$(date -u +%Y%m%dT%H%M%SZ).log") 2>&1
ts() { date -u +%H:%M:%SZ; }

ctr() { echo "h-mesh-nua-$1-$tenant-h-mesh-1"; }
ex()  { local o=$1; shift; docker exec -u ubuntu -e AGENT_NAME=architect "$(ctr "$o")" "$@"; }
exi() { local o=$1; shift; docker exec -i -u ubuntu -e AGENT_NAME=architect "$(ctr "$o")" "$@"; }
compose() {
    local o=$1; shift
    H_MESH_TENANT_ENV_FILE="$repo/offices/nua-$o/$tenant/.env" H_MESH_IMAGE="$image" \
        docker compose -p "h-mesh-nua-$o-$tenant" -f "$repo/container/compose.yaml" \
        -f "$repo/container/compose.ports.yaml" --env-file "$repo/offices/nua-$o/$tenant/.env" "$@"
}
rcli() { local o=$1; shift; docker exec -u ubuntu "$(ctr "$o")" redis-cli --raw "$@"; }
ukey() { echo "pod:nua-$1:tenant:$tenant:agent:architect:unreplied"; }
snapshot() {
    for o in a b; do
        echo "[$(ts)] office $o unreplied: $(rcli "$o" HGETALL "$(ukey $o)" | tr '\n' ' ')| unreplied.alerted: $(rcli "$o" HGETALL "$(ukey $o).alerted" | tr '\n' ' ')"
    done
}
alerts() {
    # Alerts are envelopes with source "watchdog" admitted to the lead's ingress; pane text is
    # unusable here (the codex pane sits at a login screen), so read custody lists.
    for o in a b; do
        k="pod:nua-$o:tenant:$tenant:agent:architect"
        w=0
        for l in ingress processing opened dead; do
            n=$(rcli "$o" LRANGE "$k:$l" 0 -1 | grep -c "alert from watchdog" || true)
            w=$((w + n))
        done
        echo "[$(ts)] office $o: watchdog alert envelopes seen by lead: $w; lead 'opened' entries: $(rcli "$o" LLEN "$k:opened")"
    done
}
sources() {
    for o in a b; do
        echo "office $o lead 'opened' texts:"
        rcli "$o" LRANGE "pod:nua-$o:tenant:$tenant:agent:architect:opened" 0 -1 | grep -a -o '"text":"[^"]*"' || true
    done
}

case "$stage" in
up)
    test -z "$(ss -H -ltn "sport = :$port_a or sport = :$sess_a or sport = :$port_b or sport = :$sess_b")"
    test ! -e "$root"
    mkdir -p "$root"
    (umask 022; git clone --quiet --single-branch --branch "$BRANCH" \
        https://github.com/h-network/h-mesh.git "$repo")
    cd "$repo"
    test "$(git rev-parse HEAD)" = "$SHA"
    echo "checkout $repo @ $(git rev-parse HEAD)"
    for spec in "a $port_a $sess_a" "b $port_b $sess_b"; do
        set -- $spec
        echo "[$(ts)] bootstrap office nua-$1/$tenant api=$2 session=$3"
        env -i HOME="$HOME" PATH=/usr/local/bin:/usr/bin:/bin H_MESH_IMAGE="$image" \
            AGENTS=architect DEFAULT_CLI=codex H_MESH_ALLOW_PLAINTEXT=1 \
            ./container/bootstrap.sh --pod "nua-$1" --tenant "$tenant" \
            --api-port "$2" --session-port "$3" --non-interactive >"$root/bootstrap-$1.log" 2>&1
        tail -n 3 "$root/bootstrap-$1.log"
    done
    for o in a b; do
        for i in $(seq 1 90); do
            if ex "$o" python -c '
import os
from pathlib import Path
run = Path.home() / ".h-mesh" / "run" / os.environ["TENANT"]
for name in ("api", "interoffice_relay", "watchdog"):
    os.kill(int((run / f"{name}.pid").read_text()), 0)
' >/dev/null 2>&1; then echo "office $o: api, interoffice_relay, watchdog alive"; break; fi
            [ "$i" -eq 90 ] && { echo "office $o: daemons not ready"; ex "$o" ls /home/ubuntu/.h-mesh/run/$tenant || true; exit 1; }
            sleep 1
        done
    done
    ex a h-mesh-office peers -i
    ;;
pair)
    # B grants a-peer (the participant A will send as); A grants b-peer.
    outb=$(ex b h-mesh-office coop-grant a-peer 2>/dev/null)
    echo "B coop-grant:"; echo "$outb" | grep -v '^token:'
    outa=$(ex a h-mesh-office coop-grant b-peer 2>/dev/null)
    echo "A coop-grant:"; echo "$outa" | grep -v '^token:'
    printf '%s\n' "$(echo "$outb" | sed -n 's/^token: //p')" | exi a h-mesh-office office-add b "http://$HOSTIP:$port_b" --participant a-peer --token-stdin --allow-plaintext
    printf '%s\n' "$(echo "$outa" | sed -n 's/^token: //p')" | exi b h-mesh-office office-add a "http://$HOSTIP:$port_a" --participant b-peer --token-stdin --allow-plaintext
    unset outa outb
    for i in $(seq 1 60); do
        pa=$(ex a h-mesh-office peers -i 2>/dev/null || true); pb=$(ex b h-mesh-office peers -i 2>/dev/null || true)
        if echo "$pa" | grep -q 'b-peer (api)' && echo "$pa" | grep -q 'relay (api)' && echo "$pb" | grep -q 'a-peer (api)' && echo "$pb" | grep -q 'relay (api)'; then break; fi
        sleep 1
    done
    echo "A interfaces: $(echo "$pa" | tail -n1)"; echo "B interfaces: $(echo "$pb" | tail -n1)"
    echo "A marker hash:"; rcli a HGETALL "pod:nua-a:tenant:$tenant:interoffice-participants"
    echo "B marker hash:"; rcli b HGETALL "pod:nua-b:tenant:$tenant:interoffice-participants"
    ex a h-mesh-office offices
    ex b h-mesh-office offices
    ;;
traffic)
    echo "[$(ts)] A -> B/architect"
    ex a h-mesh-office send --office b -a architect "proof: message from office A to office B"
    echo "[$(ts)] B -> A/architect"
    ex b h-mesh-office send --office a -a architect "proof: message from office B to office A"
    sleep 5
    echo "[$(ts)] courtesy acks (reply to the remote participant; arrives at the other office via its relay)"
    ex b h-mesh-office send -a a-peer "acknowledged, please do not reply"
    ex a h-mesh-office send -a b-peer "acknowledged, please do not reply"
    sleep 10
    echo "architect pane tails (what actually arrived):"
    for o in a b; do
        echo "--- office $o"
        ex "$o" sh -c 'TMUX_TMPDIR="$HOME/.h-mesh/tmux" tmux capture-pane -p -S -200 -t "$TENANT":architect | grep -E "^\[message from|proof:|acknowledged" | head -12' || true
    done
    sources
    snapshot
    alerts
    ;;
telegram)
    # Telegram-style client: an enrolled api participant outside the inter-office marker.
    # core.channels.send is the same call modules/api makes for a client message.
    docker exec -i -u ubuntu -w /app/h-app -e AGENT_NAME=architect "$(ctr a)" /opt/h-mesh/bin/python - <<'PY'
import os, time, redis
from core.channels import send
from core.registry import port_type
r = redis.Redis.from_url("redis://127.0.0.1:6379"); pod, tenant = "nua-a", "proof"
send(r, pod=pod, tenant=tenant, source="architect", destination="host",
     payload={"agent": "tg-client", "port_type": "api"}, kind="StartAgent", module="proof")
for _ in range(60):
    if port_type(r, pod=pod, tenant=tenant, agent="tg-client") == "api":
        break
    time.sleep(1)
print("tg-client port_type:", port_type(r, pod=pod, tenant=tenant, agent="tg-client"))
send(r, pod=pod, tenant=tenant, source="tg-client", destination="architect",
     payload={"text": "proof: telegram-style client message"}, module="api")
print("sent as tg-client")
PY
    sleep 2
    snapshot
    ;;
watch)
    mins=${2:-6}
    end=$(( $(date +%s) + mins*60 ))
    while [ "$(date +%s)" -lt "$end" ]; do snapshot; alerts; sleep 30; done
    echo "== final =="; snapshot; alerts
    ;;
down)
    for o in a b; do
        if [ -f "$repo/offices/nua-$o/$tenant/.env" ]; then compose "$o" down -v --remove-orphans || true; fi
    done
    rm -rf -- "$root"
    docker image rm "$image" >/dev/null 2>&1 || true
    echo "cleanup done: projects h-mesh-nua-{a,b}-$tenant removed, checkout deleted, image $image removed"
    docker ps -a --format '{{.Names}}' | grep -c '^h-mesh-nua-' || true
    ;;
*) echo "unknown stage"; exit 2 ;;
esac
