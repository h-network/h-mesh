# Inter-office senders do not open "unanswered message" entries: live proof

Verified on the isolated h-lab VM on 2026-11-04 (15:06Z-15:18Z), from a fresh
clone of pushed commit `a16a05e7813c013d486eadc28f6f95bb372e2808`
(`switch-agent/interoffice-no-unanswered`), using two throwaway offices created
by the real `./container/bootstrap.sh`. No `.env` was edited, no `API_TOKEN` was
supplied, and no token value was printed (the logs were checked: no
`coop.<id>.<secret>` string appears). The driver is
[`interoffice-unanswered-proof.sh`](interoffice-unanswered-proof.sh); the run was
serialized with suite runs by holding `~/ci/suite.lock` around each stage.

| | office A | office B |
|---|---|---|
| pod / tenant | `nua-a` / `proof` | `nua-b` / `proof` |
| compose project | `h-mesh-nua-a-proof` | `h-mesh-nua-b-proof` |
| published api / session port | 18110 / 18111 | 18112 / 18113 |
| agents | `architect` (lead, codex pane at its login screen) | same |

Image `h-mesh-nua-proof:a16a05e` (unique tag). The operator offices
`default/home`, `default/ef`, `laba` and `labb` were not touched.

## Commands and results

1. **Bootstrap** (per office, same for B with its own names and ports):

   ```bash
   env -i HOME="$HOME" PATH=/usr/local/bin:/usr/bin:/bin \
     H_MESH_IMAGE=h-mesh-nua-proof:a16a05e AGENTS=architect DEFAULT_CLI=codex \
     H_MESH_ALLOW_PLAINTEXT=1 ./container/bootstrap.sh --pod nua-a --tenant proof \
     --api-port 18110 --session-port 18111 --non-interactive
   ```

   Result: both offices up; `api`, `interoffice_relay` and `watchdog` alive in each.

2. **Pairing**, using the participant names each REMOTE grant created:

   ```bash
   # in B:  h-mesh-office coop-grant a-peer        -> participant a-peer, token-id afb536934f1eff58
   # in A:  h-mesh-office coop-grant b-peer        -> participant b-peer, token-id 85ab8b1dee79fd8d
   # token piped by the driver, never printed:
   # in A:  h-mesh-office office-add b http://172.16.0.201:18112 --participant a-peer --token-stdin --allow-plaintext
   # in B:  h-mesh-office office-add a http://172.16.0.201:18110 --participant b-peer --token-stdin --allow-plaintext
   ```

   Result: `added office b -> http://172.16.0.201:18112 (as a-peer)`,
   `added office a -> http://172.16.0.201:18110 (as b-peer)`. `peers -i` lists
   `b-peer (api)` and `relay (api)` in A, `a-peer (api)` and `relay (api)` in B.
   The new marker hash `interoffice-participants` holds `b-peer=coop`,
   `relay=relay` in A and `a-peer=coop`, `relay=relay` in B.

3. **Traffic** (15:07:28Z): `send --office b -a architect` from A and
   `send --office a -a architect` from B, then a courtesy
   `send -a a-peer "acknowledged, please do not reply"` in B and
   `send -a b-peer ...` in A (these reach the other office through its relay,
   source `relay`). Each lead's `opened` list then held exactly two entries:
   one from the peer participant (`b-peer` in A, `a-peer` in B) and one from
   `relay`. So the sources that used to open an entry (`relay`, the coop
   participant) were all exercised in both directions.

4. **Watch**, 15:07:28Z to 15:17:32Z (about 10 minutes, a sample every 30 s):
   in both offices `HGETALL ...:agent:architect:unreplied` was empty, so was
   `unreplied.alerted`, and zero `alert from watchdog` envelopes reached either
   lead (custody lists `ingress`/`processing`/`opened`/`dead` searched; the pane
   text is unusable because the codex pane never leaves its sign-in screen).

5. **Telegram-style client still opens one.** In A an `api` participant
   `tg-client` was enrolled (`StartAgent`) and sent `core.channels.send(source="tg-client",
   destination="architect", module="api")`, the call `modules/api` makes for a client
   message. Result:

   ```text
   15:14:29Z office a unreplied: tg-client {"count":1,"since":"2026-11-04T15:14:26.855Z"}
   15:16:00Z office a unreplied.alerted: tg-client 60    watchdog alerts seen by lead: 1
   15:17:01Z office a unreplied.alerted: tg-client 120   watchdog alerts seen by lead: 2
   ```

   Office B, with no client, stayed at zero. The 60 s then 120 s alerts are the
   existing exponential back-off, so the alert path was live while the
   inter-office senders produced none.

6. **Cleanup**: `compose -p h-mesh-nua-{a,b}-proof ... down -v --remove-orphans`
   (those two projects only), the checkout and image `h-mesh-nua-proof:a16a05e`
   removed; afterwards no `h-mesh-nua-*` container, volume or listener on
   18110-18113 remains.

## Not exercised live

- The watchdog's pruning of entries written before the fix (offices `home`/`ef`
  upgrading) is covered by `test_unreplied_duration_drops_an_inter_office_entry_without_alerting`
  only.
- No before/after control on the unfixed commit was run, so this document proves
  the fixed behaviour only; the old behaviour is as reported by the operator.

## Environment notes

The first attempt failed with `no space left on device` (VM `/` at 94%); the
architect approved `docker builder prune -f` (build cache only, 2.6 GB) and
the proof was then run. Image and volume pruning was not done by this proof.
