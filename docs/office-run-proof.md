# `office run` windows: status, `exited` state and `run-rm`, fresh-install proof

> Historical record: counts, commits and offices are as of 2026-11-05; see docs/inter-office.md for current behaviour.

Run on the isolated h-lab VM on 2026-11-05 (03:54Z-03:57Z) from a fresh clone of
pushed commit `c0834993d19ba3ee33c483272a25e90087e0b7bf`
(`switch-agent/office-run`), on a throwaway office created by the real
`./container/bootstrap.sh`: pod `runp`, tenant `proof`, API port 18120, session
port 18121, compose project `h-mesh-runp-proof`, image
`h-mesh-run-proof:c083499`.

**What was hand-configured:** nothing in `.env`. The bootstrap was given
`AGENTS=architect DEFAULT_CLI=codex` and the ports, nothing else. `docker exec`
was given `AGENT_NAME=architect` (the lead, as the existing inter-office proofs
do) and `TMUX_TMPDIR=/home/ubuntu/.h-mesh/tmux` (the container's tmux location;
`office run` refuses to run without an isolated tmux). The driver is
[`office-run-proof.sh`](office-run-proof.sh). No credential was printed or used;
the `architect` pane sits at the codex sign-in screen, which does not matter here.
The operator offices were not touched.

An interactive `tmux attach` needs a terminal, which this session does not have,
so "attach" below is `tmux capture-pane` on each window.

## Result

```text
office run scratch                      -> opened non-addressable window __run__scratch in proof
office run monitor -- top               -> opened non-addressable window __run__monitor in proof
office run quickie -- sh -c 'echo one-shot-output; exit 3'

$ office status          (after ~12 s, i.e. a few reconciler passes)
  architect    idle       —                                   no activity yet
  run:monitor  running    run window, not an agent: top
  run:quickie  exited 3   run window, not an agent: env
  run:scratch  running    run window, not an agent: bash

$ office peers -i
interfaces (api/office -- recognized, not tmux colleagues): api (api), host (office)
run windows (non-addressable -- never a delivery target): monitor (running), quickie (exited 3), scratch (running)
```

Sends to every run window (the window name, the `__run__` name, and the
`session:window` form) were all refused before anything was queued:

```text
send -a scratch               -> office: error: unknown destination agent 'scratch'            exit 1
send -a monitor               -> office: error: unknown destination agent 'monitor'            exit 1
send -a __run__scratch        -> office: error: unknown destination agent '__run__scratch'     exit 1
send -a proof:__run__monitor  -> office: error: unknown destination agent 'proof:__run__monitor' exit 1
send -a quickie               -> office: error: unknown destination agent 'quickie'            exit 1
broadcast "touch /tmp/PWNED-broadcast; q q q"   -> exit 0 (goes to the architect only)
```

Nothing reached either live pane: the `bash` pane text was identical before and
after (a single prompt, no `PWNED` or `q q q` text), the `top` pane differed only
in its live counters, no `/tmp/PWNED*` file existed, and after the attempts
`top` was still running (`pane_dead` 0) and still listed `running`. Sending `q`
to `top` would have quit it.

Removal went through the new verb only, with no raw `tmux kill-window`:

```text
AGENT_NAME=nobody office run-rm scratch -> office: error: office run-rm is lead-only (the lead is 'architect', you are 'nobody')   exit 1
office run-rm ghost                     -> office: error: unknown run window 'ghost' ...                                          exit 1
office run-rm scratch / monitor / quickie -> removed run window scratch / monitor / quickie
windows afterwards: architect only; office status lists no run window; run.windows has 0 entries; 0 pending removal requests
```

(The same three windows were also removed with `run-rm` once before this
transcript, after a first attempt of the driver stopped on a shell bug, to start the
final run from an empty set. Cleanup afterwards: compose project `h-mesh-runp-proof`
brought down with `down -v --remove-orphans`, checkout and image removed, no
container or volume left.)

## Limits

- The refusal shown live is the CLI's: a run window has no registry entry, so
  there is no destination to send to. The delivery layer's own refusal
  (`submit_text` rejecting the `__run__` namespace, and the openers never pasting)
  is covered by `tests/test_office_run.py` and by
  `test_nothing_can_deliver_to_a_run_window_even_with_a_forged_registry_entry`,
  which use real tmux; it was not injected live.
- The command column is what tmux reports for the pane: `env` for the one-shot
  that had already exited when first seen (the process was still the `env`
  wrapper), the real name for processes that were running.
- The listing refreshes once per reconciler pass (about 5 s).
