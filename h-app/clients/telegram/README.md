# Telegram Bot Client (`clients/telegram/`)

A Telegram bot client that talks to an **h-mesh** tenant over HTTP, allowing a user to communicate with the `architect` agent from Telegram.

---

## 1. Overview & Architecture

- **Participant Enrolment:** On startup, the bot enrols as a participant named `telegram` on the bus (`StartAgent` with `port_type: "api"`), retrying with backoff for up to 60s — `container/entrypoint.sh` starts the api door and this bundled client within the same instant, no readiness wait, so a single early attempt can lose that race (measured live).
- **Fire-and-forget prompts, delivery-side pushes replies:** A plain text message posts the envelope (`POST /agents/{agent}/envelopes`, always `202` immediately) and returns right away — no wait loop. `ReplyPusher`, a background thread, independently polls this bot's own mailbox (`GET /agents/telegram/messages`) and pushes each new reply into the chat as it arrives, on its own schedule. This matches how delivery actually works: nothing in the switch/port/api chain waits on anything, so nothing here should either.
  ⚠ **This replaced an earlier design that blocked inline** — `handle_user_prompt` used to poll-and-wait for a reply, unbounded, inside the same loop that read Telegram's `getUpdates`. One chat's unanswered prompt froze the *entire* bot, for every chat, until that one exchange resolved (measured live on the acceptance VM: the poller sat on one cursor for minutes while every message sent afterward went unread). Removed entirely rather than patched.
- **`blocked` Visibility:** If `architect` is `blocked`, the bot immediately reports `"architect is not accepting messages right now"` instead of posting.
- **Cursor Persistence:** `ReplyPusher` persists its mailbox cursor to disk (`~/.h-mesh/telegram.cursor.json` by default — see `--cursor-file` below) as it delivers each reply, and — like `AlertPusher` — seeds a fresh cursor store from the mailbox's current tail rather than replaying history on first run.
- **Discoverable commands:** `/menu`, `/status`, `/watch`, `/unwatch`, `/run`, and `/voice` are registered with Telegram itself via `setMyCommands` at enrol time, so they show up in the client's own `/` command picker instead of requiring the user to know and type them blind.
- **Text-to-Speech (TTS) Voice Replies:** Spoken voice replies via Microsoft Edge's neural TTS voices (`edge-tts` package, PyPI) using Telegram's `sendVoice` endpoint. Declared dependency in `pyproject.toml`. Spoken voice replies are opt-in per tenant (`TELEGRAM_VOICE=1`, prompted during `setup.sh`) and opt-in per chat via `/voice` or the sticky menu toggle (voice-enabled chats receive both the full text reply and the spoken voice audio).
- **Inbound messages are restricted to `--chat-id`/`TELEGRAM_CHAT_ID`.** Every real Telegram update funnels through `_dispatch_update`, which drops anything from a different chat *silently* — no reply, no answered callback query — so an unauthorized sender learns nothing, not even that a bot is listening. ⚠ **No configured chat_id refuses everything, not the reverse**: the menu now reaches hire/retire/pause/resume/broadcast, so "whoever messages first" stopped being an acceptable identity check the moment those landed. This only affects manual/ad-hoc runs without `--chat-id` — `setup.sh`'s normal flow requires both `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` before it enables the bot at all, so a real deployment always has one. CLI-driven one-shots (`--prompt`/`--status`/`--menu`, dry-run mode) call handlers directly and never go through this check — they're operator shell access, not untrusted network input.

---

## 2. Configuration & Running

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `H_MESH_API_URL` | `http://localhost:8080` | Base URL of the h-mesh REST API service |
| `H_MESH_API_TOKEN` | *required* | Bearer API token for authentication |
| `TELEGRAM_BOT_TOKEN` | *optional* | Telegram Bot API token (from @BotFather) |
| `H_MESH_CURSOR_FILE` (or `CURSOR_FILE`) | `~/.h-mesh/telegram.cursor.json` | Path to store `ReplyPusher`'s mailbox cursor |
| `TELEGRAM_CHAT_ID` | *optional* | Fixed chat for `--prompt`/`--status` one-shots, live alert push (§2b), **and the only chat the bot will respond to** — no reply, no push, no menu action for anyone else |
| `ALERTS_CURSOR_FILE` | derived from `CURSOR_FILE` | Path to store the alerts-stream cursor, kept separate from the mailbox cursor |
| `NO_ALERT_PUSH` | unset | Set to `1` to disable live alert push even when `TELEGRAM_CHAT_ID` is set |
| `NO_ACTIVITY_PUSH` | unset | Set to `1` to disable live-updating progress messages while agents execute tools |
| `H_MESH_SESSION_URL` | derived from `H_MESH_API_URL` (`:8081`) | Base WebSocket URL for the Session door, used by `/watch` (§2d) |
| `TELEGRAM_VOICE` | `0` | Set to `1` to enable the spoken TTS voice replies feature in this tenant |
| `TTS_VOICE` | `en-GB-RyanNeural` | Default Microsoft neural TTS voice for spoken replies (e.g. `en-GB-RyanNeural`) via `edge-tts` |
| `PANE_WATCH_CHROME_DEFAULT` | `4` | `/watch`: bottom pane rows cropped as UI chrome (input box, shortcut hint, separators) |
| `PANE_WATCH_CHROME_OVERRIDES` | unset | `/watch`: per-agent chrome-row exceptions, `"agent=n,agent2=n"` — see §2d, Codex needs `5` |
| `PANE_WATCH_TAIL_LINES` | `12` | `/watch`: how many rows back from the bottom of the pane to look before cropping chrome |
| `PANE_WATCH_REFRESH_SECONDS` | `2.0` | `/watch`: seconds between pane refreshes |
| `PANE_WATCH_MAX_DURATION_SECONDS` | `600` | `/watch`: auto-stop a forgotten watch after this many seconds |
| `MINI_APP_URL` | unset | Public HTTPS URL for `clients/web/mini.html` — adds a 📊 Dashboard `web_app` button to the sticky menu (§2a) when set, omitted entirely otherwise. See `clients/web/README.md`'s Telegram Mini App section — that server is not started here and needs its own `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` to accept the button's login |
| `RUN_ALLOWED_COMMANDS` | unset (unrestricted) | `/run` (§2h): comma-separated exact-match allowlist of native CLI slash commands, only enforced when set — global, not per-CLI/per-agent, see §2h |
| `H_MESH_LOG_LEVEL` | `INFO` | Logging threshold for this client — see "Log Verbosity" below |

### `allowed_updates` is asserted, not omitted

⚠ **`allowed_updates` PERSISTS SERVER-SIDE PER TOKEN.** Omitting it does not
mean "send everything" — it means "reuse whatever was last set for this token,
by anyone". An old webhook configuration, another process, or a previous
experiment that ever set a narrower list leaves this bot inheriting that filter
forever: `callback_query` stops arriving, every button dies, this client is
unchanged and the logs say nothing. It also explains intermittency, since the
inherited value changes whenever something else touches the token.

So `get_updates` sends the complete set on every call, including the polls that
carry an offset.

⚠ **This is not the narrowing the edited-message handler argues against.** That
reasoning — filtering here drops types silently and at a distance, so the next
handler added would fail by never being called — is sound, and it is about
removing types from the list. This adds none and removes none; it asserts the
full set so it cannot drift from outside.

⚠ **And a hardcoded list that drifts from the handlers is the same defect one
step along**, so `test_allowed_updates_matches_what_dispatch_routes` derives
the routed types from `_dispatch_update`'s own source and fails when they
diverge. Add a handler for a new update type without adding it to
`ALLOWED_UPDATE_TYPES` and the suite fails loudly rather than the type silently
never arriving.

### Log Verbosity

`H_MESH_LOG_LEVEL` sets the threshold `logging.basicConfig` is called with, at
import, once. Level **names** only — `DEBUG`, `INFO`, `WARNING`, `ERROR`,
`CRITICAL` (`WARN`/`FATAL` accepted as the stdlib aliases they are), case- and
whitespace-insensitive. Numbers are not names: `H_MESH_LOG_LEVEL=10` is an
unrecognised value, not DEBUG.

Anything unrecognised falls back to `INFO` and says so with a `WARNING` on the
way past, rather than crashing the daemon over a typo in a deploy's env. That
warning matters: a silently-demoted `DEGUB` would leave you believing you are
at DEBUG while every debug line is still dropped.

```bash
# One noisy run in the foreground
H_MESH_LOG_LEVEL=DEBUG python3 clients/telegram/bot.py --api-token "$H_MESH_API_TOKEN"
```

For the daemon started by `h-mesh start`, set it in the tenant env file
(`~/.h-mesh/<tenant>/env`, `H_MESH_LOG_LEVEL=DEBUG` on its own line) or export
it in the shell you start from — the daemon environment is the tenant file with
the live environment layered on top, so either reaches the bot, and the live
one wins.

What gets logged, and where the line is drawn:

| level | what |
|---|---|
| `INFO` | multi-step flow transitions (`flow hire: chat=… stage name -> profile, anchor 1 -> 4`), the hire/retire submission and its status code, an update ignored because it came from a chat that isn't `TELEGRAM_CHAT_ID`, and every handled Telegram failure (`editMessageText`, `setMessageReaction`) at `WARNING` |
| `DEBUG` | one line per update received (id, kind, chat, message id, **character count**, whether it carries a photo/document/reply), which branch of `handle_text_message` took it, and one line per h-mesh API call — `api -> POST /agents/host/envelopes (118 bytes)` and `api <- POST /agents/host/envelopes 202` |

⚠ **Shape, never content.** A message's text is logged as a character count
and the branch that consumed it; a flow's answers are logged as the stage
names they answered. Request logging is method, path, byte count and status
code — never a body (it carries chat text) and never a header (it carries the
bearer token). A callback's `data` *is* logged verbatim, because it is one of
this bot's own short codes (`hi`, `at:agent`), not something a person typed.
That is enough to answer "did the update arrive, which stage consumed it,
did the POST happen, what did the door say" — which is what a flow that
"just stops" needs, and all it needs.

The ports read the same variable, the same way (`core/README.md`, "Two logging
systems, one file"). This client keeps its own copy of the resolver because it
imports nothing from `core` — one knob, two implementations, kept identical on
purpose.

⚠ This is the verbosity knob and nothing else. A failure that is genuinely a
failure keeps its own severity, even when it is handled with a fallback — a
failed `editMessageText` (falls back to a fresh send) and a failed
`setMessageReaction` (falls back to the text confirmation, §2a) are both
`WARNING`, because both are a real Telegram call failing. Turning a level down
to make a log quieter is how the button bug stayed invisible; the fix was this
variable, not a demotion.

### Running in Dry-Run Mode (Without Telegram Token)

When `TELEGRAM_BOT_TOKEN` is not supplied (or `--dry-run` is passed), the bot operates in **dry-run mode**, sending real envelopes and state requests to h-mesh while printing all formatted Telegram message operations (`sendMessage`, `sendVoice`, `editMessageText`, `sendChatAction`) directly to stdout:

```bash
# Perform status check against real h-mesh data
python3 clients/telegram/bot.py --api-token "$H_MESH_API_TOKEN" --status

# Post a prompt to architect and return immediately (fire-and-forget — see §1;
# the reply, if any, is ReplyPusher's job, not this one-shot invocation's)
python3 clients/telegram/bot.py --api-token "$H_MESH_API_TOKEN" --prompt "can you check the auth change?"
```

### CLI Command Options

```bash
python3 clients/telegram/bot.py \
  --api-url http://localhost:8080 \
  --session-url ws://localhost:8081/session \
  --api-token "$H_MESH_API_TOKEN" \
  --bot-token "$TELEGRAM_BOT_TOKEN" \
  --cursor-file ~/.h-mesh/telegram.cursor.json \
  --agent architect \
  --voice \
  --tts-voice en-GB-RyanNeural \
  --pane-watch-chrome-overrides "backend=5,frontend=5" \
  --run-allowed-commands "/clear,/compact" \
  --dry-run \
  --prompt "can you check the auth change?"
```

---

## 2a. Menu: a pinned keyboard, not a one-off message

Sending `/menu` (registered with Telegram, so it's in the client's `/` picker
too) shows a **sticky keyboard** — `ReplyKeyboardMarkup`, pinned at the bottom
of the chat across messages, rather than an inline keyboard attached to one
message that scrolls away. Its buttons are the top-level office options
— built against `CONTRACTS.md`/`API.md`/`control/openers.py`, not `office`'s
own (narrower) argparse surface, per office-sme:

- **📋 Overview** — presence and open ticket (`doing[0]`) for every
  `port_type: "tmux"` agent in the roster. Excludes api clients (like this bot
  itself) and `host`, the same filter the web console uses for lifecycle
  controls.
- **🎫 Add ticket** — pick an agent from an inline sub-menu, then title,
  description (`-` to skip), then a priority (Low/Normal/High, tap only — see
  below). `/cancel` aborts at any text step. Posts `AddTicket` to `POST
  /agents/{agent}/envelopes`.
- **⏯ Lifecycle** — pick an agent from an inline sub-menu, then Pause, Resume,
  or Retire:
  - Pause/Resume post `PauseAgent`/`ResumeAgent` to `POST /agents/host/envelopes`.
    The tapped button is cleared immediately (`editMessageReplyMarkup`,
    keyboard-only — the result text isn't known yet) so a slow response can't
    be double-tapped; the eventual result carries **↩ Undo** (the other
    action, addressed at the same agent — Pause and Resume are both safe and
    idempotent-ish, so there's no window to expire or invalidate) and
    **📋 Copy name**, a `copy_text` button that puts the agent's name on the
    clipboard with no message sent.
  - **🗑 Retire** requires **typing the agent's name exactly** to confirm — the
    same pattern `clients/web/ui/lifecycle.js` uses, not a yes/no tap.
    `StopAgent` removes roster membership and identity state; queues and
    boards are kept for a later re-hire. A mismatched name re-prompts rather
    than cancelling, same as the web console's disabled-until-match button.
- **🔔 Alerts** — see §2b.
- **🎯 Message: `<agent>`** — pick which agent *this chat's* plain-text
  prompts (and `/status`) go to. Per-chat, not global: `--agent` is only the
  default until a chat picks one via this button. **This button's own label
  is dynamic** — it shows the chat's current target and updates the moment it
  changes, the one part of the keyboard that isn't a fixed constant (see
  `TelegramBot._sticky_keyboard`).
- **`@agent message text`** — a one-off override, not a persistent-target
  change: `@sme-2 can you check this?` reaches `sme-2` for that one message
  only, and the very next plain message still goes to whatever 🎯 Message
  agent has set. Deliberately anchored to the *start* of the text — an
  `@word` anywhere else in the message is ordinary content, not a second
  routing directive (Slack-style inline mentions notify; this one redirects
  the whole message, so unlike Slack it needs to be unambiguous about what
  it applies to). The name is validated against the same shape ➕ Hire
  enforces and the live roster (`_tmux_agents()`) before sending — an
  unknown name, a reserved word, or a real but non-tmux client (`telegram`
  itself, `host`) is refused with a plain-text error rather than silently
  dead-lettered. `@agent` with nothing after it prompts for a body instead
  of sending an empty message.
- **➕ Hire** — name (validated client-side against the same rule
  `clients/web/ui/lifecycle.js` uses: lowercase/digits/hyphens, not all
  digits, not a reserved word), then optional profile, then optional
  provider (`-` to skip either). Posts `StartAgent` with `port_type: "tmux"`,
  `cli: "claude"`. Unlike retire, hiring is not destructive — no identity or
  queues are ever removed — so it skips a type-the-name confirmation. Its
  opening prompt is **always a fresh send**, carrying `ForceReply` (compose
  box auto-opens, tagged "Reply to:" this message) — the one flow prompt
  that can, since `editMessageText`, which every later step in this flow
  uses, cannot attach a `ForceReply` at all. ⚠ `handle_hire_start` therefore
  *ignores* the `message_id` its caller passes: reached from the inline
  button, that id is the tapped menu message, and editing it in place
  silently dropped the `ForceReply` on the only path an operator actually
  uses. The menu message is left where it is rather than overwritten. A
  A `202` result is labelled **admitted**, not created: it proves the request
  reached the source egress queue, while downstream lifecycle custody is what
  proves the agent exists. The admitted result carries a **📋 Copy name**
  button for the requested agent name.
  ⚠ **No profile picker**: `office profiles` reads Redis directly and has no
  REST equivalent, so this client cannot list valid accounts ahead of time. A
  bad profile name still comes back as a clear `422` — and the api's error
  conveniently lists the valid ones (`control/openers.py`'s
  `available_profiles` check).
- **📢 Broadcast** — type a message, sent to every agent (`POST
  /agents/all/envelopes`).
- **🔊 Voice: ON / 🔇 Voice: OFF** — toggle spoken text-to-speech voice replies
  for this chat. Dynamic button label reflects current chat state (`/voice` command
  also toggles this).
- **📊 Dashboard** *(conditional — only when `MINI_APP_URL` is configured)* —
  a Telegram Mini App `web_app` button, distinct from every button above:
  tapping it opens `clients/web/mini.html` inside Telegram's own WebView
  client-side and never arrives at `handle_text_message` as a message the
  way every other sticky-keyboard tap does. Read-only — see
  `clients/web/README.md`'s Telegram Mini App section for the auth flow
  and what it can and can't do.
- **🙈 Hide menu** — actually dismisses the sticky keyboard. A persistent
  `ReplyKeyboardMarkup` (`is_persistent: true`, what every keyboard this bot
  sends already sets) can't be dismissed from the phone itself — Telegram's
  own "collapse" gesture is a temporary panel toggle, not a removal, and the
  keyboard reappears on the next refresh. This button is the one thing that
  actually does it, by replying with `reply_markup: {"remove_keyboard":
  true}` (`ReplyKeyboardRemove`) instead of the normal keyboard.
  `/menu` (unchanged) brings it straight back.

⚠ **Full `Command` passthrough is deliberately not exposed here**, same as
the web console (`clients/web/SPEC.md` §6): it pastes bare text into a pane
and *executes* it, with no live view of the pane to confirm what actually
happened — the wrong shape for unbounded remote execution from a phone.
`/run` (§2h) is a narrower, later exception to that: a fixed, pre-vetted
allowlist of native CLI slash commands, typed by hand rather than one tap
on a button, not a reopening of full passthrough. See §2h for the reasoning
behind where that line is actually drawn.

⚠ **Sticky-keyboard taps arrive as ordinary text messages** — Telegram sends
the button's label back as if the user typed it, with no `callback_query`.
`handle_text_message` matches the label against `TelegramBot.STICKY_LABELS`
(exact match for the six fixed buttons) or the `🎯 Message: ` prefix (for the
one dynamic button) before falling through to "this is a prompt for the
chat's target agent". Sub-flows one level down (agent pickers, Lifecycle's
Pause/Resume/Retire choice, Message-agent, Add Ticket's priority buttons) stay
**inline** — contextual, one-shot choices tied to a specific message, which is
what inline keyboards are for; the sticky keyboard is for top-level nav that
should always be one tap away without re-sending `/menu`.

⚠ **A step answered by a BUTTON edits in place; a step answered by TYPING
posts a new message.** That split is the whole rule, and the reason is that
**an edit never notifies**: the moment the operator types, their own message
is the newest thing in the chat, so a prompt edited into an older message
lands above it silently. From the chat that is indistinguishable from the flow
having died — which is exactly how the Hire flow presented before this was
fixed: name asked, one follow-up seen, then apparently nothing.

- **Button-answered screens edit** — Add Ticket's agent picker and its
  priority buttons, Lifecycle (including Pause/Resume/Retire and "◀ Back"),
  Message-agent's picker, Watch's picker: `_send_or_edit_message` keyed off
  `callback_query.message.message_id`, so the screen the operator is looking
  at is the screen that changes, and a chat doesn't fill up with a throwaway
  message per tap.
- **Typed-answer replies send fresh** — every reply produced by
  `_advance_pending_flow` (`_send_typed_answer_reply`): Hire's three prompts
  and its result, Add Ticket's description and priority prompts, Retire's
  confirmation retry and its result, `/cancel`'s acknowledgement. The flow's
  `message_id` still tracks its most recent message, so a later button tap
  edits *that* one.
- `/cancel` also clears the abandoned screen's inline keyboard
  (`editMessageReplyMarkup`), so a cancelled flow can't be resumed by tapping
  a button left behind on it.

Two more that stay fresh sends for their own reasons: Broadcast (its entry
point is a sticky-keyboard tap, never an inline button, so there's never a
message to edit into) and the last step of Message-agent (its confirmation
re-sends the sticky keyboard, and `editMessageText` can only carry an inline
keyboard, never a `ReplyKeyboardMarkup`).
The top-level sticky `ReplyKeyboardMarkup` menu itself is not part of any of
this — Telegram has no API to edit a custom reply keyboard in place, so
`/menu` and the sticky-keyboard taps that swap targets or toggle voice always
remain fresh sends.

⚠ **A screen can outlive the agent it names.** Edit-in-place means a picker
or an action screen can sit untouched in a chat for as long as the user
leaves it there; if the agent it references gets retired in the meantime, a
later tap on that same screen (`handle_callback_query`, via `_callback_agent`)
is caught before it ever reaches the mesh API — a real popup
(`answerCallbackQuery`'s `show_alert=True`), not a small toast that the very
edit a live tap would trigger could mask, and the stale screen is left
exactly as it was rather than edited into a misleading error.

Other Telegram-native touches, all elsewhere in this file: `record_voice`/
`upload_document` chat actions (§2c/2f — more accurate than the generic
`typing` for those specific waits), a 👀 reaction on a prompt's own message
the moment it's actually dispatched (a persistent, easy-to-spot marker,
unlike `send_chat_action`'s few-second indicator, for a reply that can
arrive much later via `ReplyPusher`), and `setChatMenuButton` pointing the
compose bar's own persistent button at the Mini App dashboard when
`MINI_APP_URL` and a chat id are both configured — set once at `enrol()`
time, alongside `setMyCommands`.

⚠ **The "✅ Sent to X"/"✅ Ran on X" text confirmation only disappears once
the 👀 reaction above actually lands** — verified by checking
`setMessageReaction`'s own response, not assumed from the call having been
made. Two things that fall back to the text instead: no `message_id` to
react to at all (every caller not tied to a live inbound Telegram
message — the CLI's own `--prompt` one-shot, which has no message to react
to, is the one that matters here and always gets the text), or the
reaction call itself failing (a chat can have reactions disabled entirely;
Telegram reports that as an ordinary API error, not a silent no-op, so it's
caught rather than assumed to have worked). A failed or blocked prompt was
never a candidate for this either way — the reaction only ever means
"dispatched", never "failed", so those replies are untouched.

While a chat has an open multi-step flow (Add Ticket, Hire, Retire,
Broadcast), its next plain text message is consumed as that flow's answer
rather than sent to `--agent` as a prompt (and takes priority over a
sticky-keyboard label, so typing a title that happens to match a button label
is still treated as the title).

⚠ **An edit is not a send.** Telegram delivers an `edited_message` update
whenever the operator edits *any* of their messages from the last 48 hours,
carrying the full edited text. This bot **declines those updates**: it logs one
line and dispatches nothing. Editing a message is not an act of sending one,
and treating it as one meant three things nobody had chosen — a typo fixed in
an old message could answer the question a flow was currently asking (with
text having nothing to do with it), could re-prompt the target agent with a
near-duplicate turn, and could **re-run a `/run` command that had already
executed once**. That behaviour arrived with the original port, with no
comment, no test and nothing in this README, so it was inherited rather than
decided; it is decided now.

When a flow is open, the chat is told (`✏️ Editing a message doesn't send it
… send it as a new message, or /cancel`), because that is the case where
saying nothing leaves the operator waiting on an answer the bot has already
discarded. With no flow open the edit is logged and otherwise ignored — there
is nothing waiting on them and a lecture per edit would be noise. An edit from
a chat that isn't `TELEGRAM_CHAT_ID` gets no note either; the chat check runs
first, so an unauthorized sender still learns nothing.

Two alternatives were considered and rejected. Treating an edit as a
*correction* of the answer it replaces requires the stage to be reversible,
and by the time an edit arrives a hire may already have been submitted.
Filtering at the source with `getUpdates`'s `allowed_updates` would be tidier
but drops update types silently and at a distance, so the next handler someone
adds would fail by never being called — declining in `_dispatch_update` costs
one visible log line per edit instead.

⚠ **Two mechanisms, and they do different jobs.** A per-chat worker gives
*ordering*; a per-chat transaction gives *mutual exclusion*. Neither implies
the other, and the branch that shipped only the second was wrong about it.

**Ordering — `ChatWorker`, one thread and one queue per chat.** `getUpdates`
returns updates in order; `run_polling` hands each to `submit_update`, which
routes it to that chat's worker. Updates for one chat are therefore handled in
arrival order, while different chats stay fully concurrent — the reason
updates were taken off the polling loop in the first place (a chat waiting on
a slow call must not stall the poller) still holds. A lock alone does *not*
give this: lock acquisition is not FIFO, so with only a lock the answers
`sme-9` then `-` could be applied `-` first, rejected against `stage=name`,
leaving the flow a step behind with nothing crashed and a log that reads
normally. A handler that raises is logged and the worker continues; before
this, each update owned a bare thread and an exception killed it in silence.

**Mutual exclusion — `TelegramBot.chat_txn(chat_id)`.** Still needed, because
the worker is not the only thread touching a chat's state: `ReplyPusher`,
`AlertPusher`, activity watchers and `/watch`'s own thread all do. Every
read-then-write holds it: a whole pending-flow step, `handle_addticket_priority`'s
claim, `/watch`'s replace (read incumbent, signal, install) and
`_stop_pane_watch`, `handle_voice_toggle`'s read-and-flip, the activity-render
swap and `finalize_activity`'s compare-and-pop, and every flow install. It is
a plain non-reentrant lock: nesting raises `ChatTransactionError` naming the
chat rather than deadlocking, which is why `handle_watch_pick` replaces inline
instead of calling `_stop_pane_watch`.

**What the guard does and does not catch.** Per-chat maps are `ChatDict`, a
`MutableMapping` over a private dict — so `update`, `clear`, `popitem`, `pop`,
`setdefault` and `|=` all route through one guarded choke point, where a
`dict` subclass had four holes. Stored values are handed out frozen
(`FrozenChatState`), so `state = pending.get(cid); state["stage"] = ...` is
refused at the write instead of quietly changing what another thread is
reading; a step advances by writing a successor back. ⚠ It does **not** catch a
STALE READ: reading outside a transaction and writing back inside one is
accepted, and is still wrong. Nothing in a container can see how old the value
in your hand is — that is what holding the transaction across the whole
read-then-write is for. Reads themselves stay unguarded: a lone read is always
safe.

**What a chat's own updates wait for**, with real numbers: a flow step is one
mesh call (10s timeout) plus typically one or two Telegram calls (30s each;
60s for a file download, 90s for TTS and the session stream), so tens of
seconds worst case for that one chat. The step holds throughout rather than
claiming and releasing, deliberately: the next answer must not be accepted
before the prompt it answers is visible. Claim-and-release would be safe only
with an explicit in-progress state that queues or rejects input arriving
mid-step. The transaction is released on exceptions.

⚠ **A limit with a fix in flight — a reply cannot yet say which turn it
answers.** `ReplyPusher` finalizes a chat+agent's activity render with no
render handle, because the api door mints a fresh `correlation_id` per
envelope and an agent's reply is its own envelope: nothing on the wire links
it to the prompt. With two overlapping prompts to one agent, the first reply
ends whichever render is installed, which may belong to the second,
still-running turn. The display stops early; no state is corrupted.

Raised with api-agent, who first declined — nothing populates an
`in_reply_to` without the agent cooperating, and the no-cooperation fallback
(FIFO oldest-delivered-first) assumes replies return in order, which is
exactly false here. **That decline has since been overtaken:** architect put
an opt-in exact-correlation option to them, they specified it, and it is being
built across the tmux port, `office send` and the openshell port — additive
and opt-in.

⚠ The fallback below is not going away when it lands. Correlation depends on
the replying agent passing the id back, so uncorrelated replies keep arriving,
and the by-key behaviour is what serves them;
`test_an_overlapping_reply_finalizes_the_wrong_turns_render` becomes the
fallback's test rather than something to delete.

It is also an ORDERING dependency, not only a missing correlation:
ReplyPusher's thread and the polling worker are excluded from each other by
the transaction, not ordered. Dropping the reply-triggered finalize would
remove the dependency, at the cost of every turn's display lingering until the
watcher notices — up to its 300s timeout — which was ruled against as a
certain cost traded for a rare one.

Try it without a bot token: `python3 clients/telegram/bot.py --api-token "$H_MESH_API_TOKEN" --menu`.

---

## 2b. Alerts

**🔔 Alerts** (one of the four sticky-keyboard buttons, §2a) shows the tenant's recent
watchdog alerts on demand via `GET /alerts`, and — the more valuable half —
`AlertPusher` proactively pushes each *new* alert to `--chat-id`/
`TELEGRAM_CHAT_ID` as it happens via `GET /alerts/stream`, running in a
background thread alongside the normal polling loop. Disable it with
`--no-alert-push` / `NO_ALERT_PUSH=1`; it is skipped automatically when no
chat id is configured (the bot cannot push into a chat it has never heard
from).

⚠ **Only three alert kinds ever reach either surface: `blocked`, `stalled`,
`credential`.** These are exactly what `GET /alerts`/`GET /alerts/stream`
document (API.md's Watchdog Alerts Feed). The two newer, lead-only alerts —
`doing_duration` and `todo_duration` (the "unpicked ticket" one) — are pasted
directly into the *lead's* tmux pane as an ordinary `Message` envelope
(`modules.watchdog.service`'s `_notify_lead`, confirmed against `_check_doing_duration`/
`_check_todo_duration`), and never touch the alerts stream at all. There is
currently no API surface — REST or SSE — that exposes them to anything but
the lead's own pane, so this bot cannot surface them regardless of which
agent it targets. Flagged to architect as a possible follow-up (e.g.
mirroring them onto the alerts stream too), not built here — it was out of
this ticket's scope and text-matching the lead's ordinary conversation
mailbox to guess which messages are secretly alerts would be fragile.

`GET /alerts` has no "give me the most recent N" query — without `after` it
reads from the *oldest* stored entry, same as every other stream endpoint
here. The on-demand view therefore fetches up to the retention cap (1000)
and takes the tail client-side; `AlertPusher` avoids the equivalent problem
on first run by seeding its cursor from `next_cursor` (the current tail)
instead of streaming from the beginning, so it does not replay the whole
retained history as if every alert were new.

---

## 2c. Live Activity Progress (Rolling `editMessageText`)

⚠ **The watcher's deadline holds when nothing is happening**, which is the
only case it exists for. `timeout_s` is evaluated once per iteration of the
activity stream, so against an IDLE agent — one producing no activity events
at all — the loop never got an iteration, the deadline never fired, and the
thread lived indefinitely holding an SSE connection. Measured on the
acceptance instance: four live watcher threads and four held connections for a
single agent. `_parse_sse_events` now reports keepalive comments rather than
swallowing them, and `MeshClient.stream_activity(..., heartbeat=True)` passes
them through as `None`, so an idle stream still gives the loop a turn to check
its clock and its stop switch. Alerts are unaffected: that consumer has no
deadline and filters heartbeats out.

⚠ **A newer turn stops the previous watcher rather than stacking beside it.**
Swapping in a new render finalized the old one but left its thread running
against the same agent until its own deadline. Each render now carries a
`stop_event` that the swap sets and the loop checks on every tick.

`/watch`'s pane watcher does **not** have this shape and needed no equivalent
fix: it drives its own loop (`ws.recv` with a drain deadline, then
`stop_event.wait(refresh_s)`) and checks its max duration at the top of every
iteration regardless of traffic. A test pins that so it stays true.

When a user sends a prompt to an agent via Telegram, the bot starts an activity watcher that tails the agent's real-time execution stream (`GET /agents/{agent}/activity/stream`). As the agent executes tools (`Bash`, `Read`, `Edit`, `Grep`, etc.) and transitions between states (`input`, `tool`, `output`), the bot maintains a live-updating message using Telegram's `editMessageText`:

```text
🛠 Activity (architect)
1. ✓ 💬 input received
2. ✓ Read
3. ✓ Grep
4. ⏳ Bash

⏳ still working… (updated just now)
```

- **Tool-Names Privacy Invariant:** Respects `HLD.md` §8 privacy boundaries — events carry tool names and lifecycle markers only, never arguments, parameters, file paths, or shell strings.
- **In-Place Updates & Throttling:** Edits are throttled to at most ~1/sec to comply with Telegram rate limits, keeping total rendered characters well below Telegram's 4096 cap via sliding window truncation for long execution runs. Redundant edits with identical text are skipped.
- **Completion & Coexistence with Replies:** When the agent finishes (when `ReplyPusher` delivers the reply from the mailbox), the activity message is finalized in place (`🛠 Activity (architect) · completed (4 steps)`), and the full final response (and voice note if enabled) is delivered as a fresh new message.
- **Disabling:** Disable live activity streaming by passing `--no-activity-push` or setting `NO_ACTIVITY_PUSH=1`.

---

## 2d. `/watch` — Live-Tail an Agent's Terminal (Rolling `editMessageText`)

A clearer alternative to guessing what an agent is doing from tool names: one
Telegram message showing an actual slice of the agent's tmux pane, refreshed
in place. This **replaces** an earlier "diff-triggered liveness pulse" design
(a `⏳ still working…` timestamp line bolted onto the activity message above)
that operators found unclear — this shows the content itself instead.

**Trigger.** Tap **👁 Watch** on the sticky menu (or send `/watch`) to pick an
agent from an inline list, or send `/watch <agent>` directly. One watch runs
per chat; starting a new one (any agent) replaces whatever was already
running there. `/unwatch`, or the **⏹ Stop watching** button attached to the
live message itself, ends it early.

**Source.** Reuses the Session WebSocket door's existing capture-pane
mechanism (`ws://HOST:8081/session`, `LLD-session.md` §2–3) — the bot has no
local tmux access at all, by design (`HLD.md` §7: `:8081` is terminal bytes
for a *person*, not a data path an app parses, except this one sanctioned
"watching an agent work" case). Rather than reconstructing a screen
client-side from the live `%output` diff stream, the bot asks for one fresh
snapshot per refresh tick via a small session-door protocol addition —
`{"subscribe": [agent], "mode": "read-only", "refresh": true}` — which
re-runs `capture-pane` for an already-subscribed agent instead of requiring
an unsubscribe/resubscribe round trip that would risk losing a live diff in
the gap.

**Content window.** Every `capture-pane` snapshot is up to the pane's full
120×32 screen (`LLD-tmux-host.md` §3); most of that is either stale
scrollback or the CLI's own input chrome, neither of which is "what the agent
just said". The bot looks at the last `PANE_WATCH_TAIL_LINES` rows (default
`12`) and crops the bottom `PANE_WATCH_CHROME_DEFAULT` rows (default `4`) —
net, roughly rows `[bottom-12 .. bottom-4]` — then trims rows from either edge
of what's left that are blank or match `_is_transient_chrome_line` (below).

⚠ **Chrome height is not the same across claude/codex/agy, and the bot
cannot ask which one an agent runs** (`GET /agents/{agent}` carries
`port_type` and presence, never `cli` — only `office peers -v`'s
`framework=<cli>` field reads that, from a Redis key (`resource: "launch"`)
the api door does not expose). Measured against
three live panes: claude and agy's Antigravity CLI both draw a 4-row bottom
*structural* chrome (separator / input line / separator / hint), Codex draws
5 (separator / blank / input box / footer status line). The default of `4`
is correct for two of three; `PANE_WATCH_CHROME_OVERRIDES` (same
`"name=value"` exceptions shape as `entrypoint.sh`'s `AGENT_CLIS`) lets an
operator who knows which agents run Codex correct those by name rather than
the bot guessing or mis-cropping silently.

⚠ **A second, state-dependent kind of chrome sits just above the structural
kind, and no fixed offset can crop it correctly.** A CLI's own
thinking-spinner ("✻ Churned for 20s", "Boogieing… (17s · ↓ 639 tokens)") and
update-nag banner ("✔ Update installed · Restart to update") are present or
absent depending on whether the agent is mid-turn or an update just shipped —
not on which CLI it is. Widening the crop only shifts where the leak lands;
first shipped (`PANE_WATCH_TAIL_LINES` `10`, no filtering) it leaked on a
codex pane and, checked again on a claude pane that happened to have both a
spinner and an update banner up at once, it was leaking there too. Filtered
by content instead (`_is_transient_chrome_line`, `bot.py`): update-banner
phrasing, a line ending in `tokens)`, claude's `done HH:MM AM/PM` completion
stamp, and a short (≤60 char) line matching `<word(s)> for Ns[ Ns]` — capped
in length so a genuine reply that happens to mention a duration mid-sentence
isn't mistaken for a spinner line. agy's own spinner text is unconfirmed —
none of the three lanes has captured it mid-turn yet — so it may need a
pattern added once it leaks somewhere.

**Refresh & stop conditions.** Polls every `PANE_WATCH_REFRESH_SECONDS`
(default `2.0`). Stops on any of: the **⏹ Stop watching** button or
`/unwatch`; `PANE_WATCH_MAX_DURATION_SECONDS` elapsing (default `600`, a
safety net against a forgotten watch); or the watched agent transitioning
from `working` to `idle` presence (`GET /agents/{agent}`) — a transition, not
a snapshot, so starting a watch on an already-idle agent doesn't end it
immediately. On any stop, the message is edited once more with the
last-known pane content plus a reason line, its **⏹ Stop watching** button
removed, and it is not touched again — the final state is what's left
behind, matching a normal Telegram message rather than one that looks
perpetually "live" after the bot has moved on.

---

## 2e. Receiving a Photo

Sending a photo used to be silently dropped — `_dispatch_update` only ever
looked at `message.text`, and a photo update carries `message.photo`/
`message.caption` instead, so it fell through with no reply and no record
anywhere. It now downloads the photo and sends a real `Attachment`-kind
envelope (`docs/CONTRACTS.md`) — file bytes on the bus, `content_base64`,
not a path shared out of band. An earlier version of this feature (before
the Attachment kind existed) saved the file under the agent's own workdir
and sent a plain `Message` naming the path; that's gone now that there's a
real envelope kind to send it as, and the tmux opener does the filesystem
work instead (`/workdir/<recipient>/attachments/<stream_id>/`, entirely its
own to create/write/clean up — confirmed directly with the tmux lane rather
than assumed. This client never touches a filesystem for a received photo
at all).

- **Routing is identical to a text message.** The caption is the
  `Attachment` envelope's `caption` field — the persistent chat target (🎯
  Message agent) unless the caption starts with `@agent`, which overrides
  the destination for this photo only, exactly like [§2a's
  `@mention`](#2a-menu-a-pinned-keyboard-not-a-one-off-message) (same
  validation, `_validate_mention_target`, shared with the text path rather
  than reimplemented). An unknown `@mention` target is refused before any
  download is attempted.
- **Largest available size**, `photo[-1]` — Telegram's own smallest-to-largest
  ordering for the `PhotoSize` array. This is the best *compressed* copy the
  bot ever sees: Telegram recompresses every "photo" upload itself, so full
  original quality isn't available through this path at all — a
  **"document"** upload preserves it instead, a different message shape,
  see [§2g](#2g-receiving-a-document-uncompressed). `mime_type` is always
  `image/jpeg` for the same reason — no content-sniffing needed.
- **`filename`** comes from Telegram's own `getFile` `file_path` (its
  basename, e.g. `file_123.jpg`), still run through the same basename
  validation `docs/CONTRACTS.md` specifies rather than trusted outright —
  falls back to a generated name if that ever fails.
- **Shares its download/validate/send mechanics with §2g's "document" path**
  (`_send_incoming_file_as_attachment`) — the only real difference between
  the two is *what's already known about the upload*: a photo has no name
  or MIME type of its own (both are supplied here, `image/jpeg` and a
  `file_path`-derived name), a document already carries both from Telegram.
- **A defensive size check against two different ceilings, not one applied
  twice.** `TELEGRAM_MAX_FILE_BYTES` (20MB) is what Telegram will let a bot
  download at all, checked against `PhotoSize`'s own reported `file_size`
  before downloading anything. `ATTACHMENT_MAX_BYTES` (10MB,
  `docs/CONTRACTS.md`'s decoded-content cap) is checked against the actual
  downloaded byte count — smaller than Telegram's own ceiling, so a photo
  between the two downloads fine and must still be refused here rather than
  left to the api's own `422`.
- **`blocked` presence is checked before any download work happens** — same
  as a text prompt, so a blocked agent doesn't cause a photo to be fetched
  for nothing.
- A **Telegram album** (several photos sent together) arrives as *separate*
  updates sharing a `media_group_id`, not one message with several photos —
  handled without any special casing, one `Attachment` envelope per photo.
  ⚠ Unlike a `Message`, **an `Attachment` is never combined into a burst**
  (`docs/CONTRACTS.md`) — an album reaching one agent shows up as N separate
  pastes, not one coalesced paste the way consecutive `Message`s would.

---

## 2f. Sending a File (the other direction)

`office send-file` lets an agent send this bot a real file — `ReplyPusher`
(§1) is what actually delivers it, since it already owns reading `telegram`'s
mailbox. Until this shipped, an `Attachment`-kind mailbox entry hit
`render_reply`, which only ever reads `payload.text` — absent on an
`Attachment` payload — so it silently rendered as `"<agent> sent a message"`
instead of the file ever reaching Telegram. `kind: "Attachment"` is now
checked before falling through to the ordinary text-reply path at all.

- **`content_base64` is decoded and forwarded via `sendDocument`**
  (`TelegramClient.send_document`, same multipart shape `send_voice` already
  used) — `filename` and `mime_type` come straight from the envelope
  payload, `caption` is prefixed with the sending agent (`from <agent>: ...`,
  or just `from <agent>` with none) the same way a text reply is prefixed
  with its source.
- **The full validation contract is re-enforced here, not just "is there
  something to send."** `docs/CONTRACTS.md` promises an api-type client
  "validates and decodes the same payload contract" the api door and the
  tmux opener do — the same defense-in-depth reasoning the opener's own
  re-check is built on: this envelope already passed the api door once in
  the normal flow, but a direct bus caller bypasses that door entirely and
  would otherwise reach this code with no real validation at all. Checked,
  reusing the same functions/constants the send side
  (`handle_photo_message`) already defined rather than a second copy of the
  rules:
  - `filename` against the spec's basename rules (`_valid_attachment_filename`)
  - `mime_type` against the spec's grammar and length (`_valid_attachment_mime_type`)
    — a missing one is **rejected**, not defaulted to
    `application/octet-stream`; it's one of the three required fields
  - `caption`, when present, against the 65,536-UTF-8-byte cap
    (`ATTACHMENT_MAX_CAPTION_BYTES`)
  - the decoded byte count against `ATTACHMENT_MAX_BYTES` (10MB) — the
    same cap the send side checks, here applied to what an agent sent in
  - **the payload's closed shape** (`ATTACHMENT_ALLOWED_PAYLOAD_KEYS`) — an
    envelope carrying any field beyond `filename`/`mime_type`/
    `content_base64`/`caption` is rejected outright, matching "no other
    field is accepted"
- **A failure is reported back to the chat, never silently dropped** — any
  rejection above, a `content_base64` that fails strict decoding, or
  Telegram's own `sendDocument` call failing (file too large, rejected
  type, network error) all produce a plain-text error message in the chat,
  mirroring the receiving side's rule (`handle_photo_message`) rather than
  leaving the operator to wonder why nothing arrived.
- **Telegram's own caption limit (1024 chars) is enforced client-side**,
  same truncation `send_voice` already applies — smaller than the bus's own
  65,536-UTF-8-byte cap (`docs/CONTRACTS.md`), so a long caption that's
  perfectly valid on the bus still gets cut for Telegram specifically.

---

## 2g. Receiving a Document (Uncompressed)

Telegram's normal "photo" picker always recompresses before this bot ever
sees it — Telegram's own behavior, not something fixable here. Sending as a
**document** instead (send as file, not as photo) skips that recompression,
so this is the path for anyone who actually needs the original bytes —
`message.document` is a single object with its own `file_id`/`file_name`/
`mime_type`, not an array of `PhotoSize` like `message.photo`, and not
always JPEG.

- **Shares every mechanic beyond that with [§2e's photo
  path](#2e-receiving-a-photo)** (`_send_incoming_file_as_attachment`) —
  `@mention` routing, `blocked` presence, both size ceilings
  (`TELEGRAM_MAX_FILE_BYTES` for the download, `ATTACHMENT_MAX_BYTES` for
  what the bus accepts), and sending the real `Attachment` envelope are all
  the identical code path a photo already used, not a second copy of it.
- **`filename` and `mime_type` come from Telegram directly** (`file_name`,
  `mime_type`) instead of being derived or hardcoded the way a photo's are
  — still run through the same validation as any other Attachment
  (`_valid_attachment_filename`/`_valid_attachment_mime_type`) rather than
  trusted outright, since Telegram's own metadata isn't guaranteed to be
  spec-shaped (e.g. a `mime_type` with a `; charset=...` parameter, which
  the closed grammar in `docs/CONTRACTS.md` doesn't allow). An invalid
  `mime_type` falls back to `application/octet-stream` rather than
  refusing the file outright — the content is still fine even if the
  reported type isn't — and `mime_type` is genuinely optional in Telegram's
  own API, so a missing one gets the same fallback. An invalid `file_name`
  falls back to a generated name, same idea as a photo's fallback but with
  no extension assumed (a document could be anything).

---

## 2h. `/run` — an Exception to "`Command` Is Not Exposed", Unrestricted by Default

`/run <agent> <command>` sends a `Command`-kind envelope instead of the
`Message`-kind shorthand every other text path uses: `command_opener` pastes
the text raw, with no `[message from telegram]` wrapper, so a native CLI
slash command (e.g. Claude Code's `/clear`) is interpreted by the underlying
CLI instead of read as chat text saying "/clear". One-off, same as
[`@mention`](#2a-menu-a-pinned-keyboard-not-a-one-off-message) —
`chat_target_agent` is never touched by this.

⚠ **Unrestricted by default (`RUN_ALLOWED_COMMANDS`/`--run-allowed-commands`
empty), not bounded to a fixed set.** An earlier draft here (a small,
fixed allowlist of native CLI slash commands, `/clear,/compact`) was too
narrow in practice — the user hit it immediately trying `/run architect
/context` — and, on reflection, wasn't actually adding a real boundary: this
bot is already locked to one `TELEGRAM_CHAT_ID` (§1), and the agent it runs
on already has permissions skipped, so whoever holds that chat already has
the practical equivalent of a live terminal to it. An allowlist here would
have been restricting that same operator from themselves, not protecting
against anyone else. What remains structural, allowlist or not:

- **Single-line only, always checked.** `command_opener` pastes the command
  with one trailing newline appended. A command's own text carrying an
  embedded newline would submit it as one line and then paste a second,
  completely independent line of raw input right after it, on delivery.
  Refused with "`/run commands must be a single line.`" regardless of
  whether an allowlist is configured.
- **Presence-gated and activity-visible exactly like a normal prompt** — a
  `blocked` target agent is refused the same way `handle_user_prompt`
  refuses one, and the live activity progress message (§2c) starts the
  same way too, since a slash command can still trigger tool calls.
- **No per-operator restriction beyond the existing single
  `allowed_chat_id` gate** every command already goes through — there is
  no concept of distinct "operators" for one `chat_id` to be restricted
  relative to another yet. Revisit if/when that lands.

**The allowlist mechanism itself still works** for anyone who wants to bound
`/run` — a shared chat, a CLI whose commands need vetting, whatever the
reason. Set `RUN_ALLOWED_COMMANDS`/`--run-allowed-commands` to a
comma-separated list and it's enforced as an **exact, whole-string** match —
`/clear extra` is not `/clear` and is refused, not passed through with an
argument attached — with a plain-text reply naming what *is* allowed,
never silently dropped. Global, not per-CLI or per-agent, when set: `GET
/agents/{agent}` exposes no field for which CLI an agent runs (the same
limitation `PANE_WATCH_CHROME_OVERRIDES`, §2d, exists for).

---

## 3. Documentation Gaps in `docs/API.md`

Built strictly against [`docs/API.md`](../../docs/API.md). The following gaps and ambiguities were encountered:

1. **Presence `blocked` State Omission in §5 Header**:
   Section 5 under `GET /agents/{agent}` (line 248) states: *"returns queue depths and presence status (working, idle, unknown)"*. It omitted `blocked` as a possible presence state in that section, even though `blocked` is a critical presence state documented in `CONTRACTS.md` and `HLD.md`.

2. **Re-enrolment Idempotency Behavior**:
   Sections 3 and 5 document `POST /agents/host/envelopes` with `StartAgent` and `port_type: "api"` for enrolling application clients, but do not state whether re-enrolling an already enrolled client (e.g. upon client restart) is idempotent or what HTTP status/body is returned.

3. **Task Board Ticket Schema Variability**:
   Section 5 gives an example response for `GET /agents/{agent}/board` with task objects containing `id`, `title`, `description`, `created_by`, `status`, `created_ts`, and `priority`. However, for legacy tasks or raw string items, `API.md` does not explain whether task items can be non-dict objects or strings.

4. **SSE Event Stream Reconnection (Header vs Query Parameter)**:
   Section 4 documents both `?after=<cursor>` (query parameter) and `Last-Event-ID` (HTTP header) for resuming SSE event streams. It does not specify precedence if both are present or note browser `EventSource` constraints (where custom headers cannot be set).

5. **Activity Feed Event Kinds & Schema**:
   Section 5 notes that `tool` events carry a `tool` string (e.g. `"tool": "Bash"`), but does not explicitly enumerate all valid values for `kind` or state whether `tool` is null/absent for non-tool event kinds (`input`, `output`).

6. **No reverse/tail query on `GET /alerts` (or any stream endpoint)**:
   Without `after`, `GET /alerts?limit=N` returns the *oldest* `N` stored entries (`xrange(min="-")`), not the most recent — unintuitive for a "show me recent alerts" one-shot. A client wanting the tail must fetch up to the retention cap and slice client-side, as this bot does. Worth a line in API.md's §4a gotchas.

7. **The two newest watchdog alerts never reach `GET /alerts`**:
   `doing_duration` and `todo_duration` ("unpicked ticket") are delivered as a direct `Message` to the lead's tmux pane (`_notify_lead`), not written to the alerts stream `_alert` writes to. API.md's Watchdog Alerts Feed section reads as if it covers "alerts produced by `modules.watchdog`" generally; it does not mention this split, and nothing in the REST/SSE surface exposes those two kinds at all.

8. **The account/profile registry has no REST endpoint**:
   `available_profiles` (`bus/accounts.py`) — what `office profiles` reads and what `StartAgent`'s `profile` field is validated against (`control/openers.py`) — is direct-Redis only. A REST client can `StartAgent` with a `profile` and get a clear `422` naming the valid accounts if it guesses wrong, but cannot list them ahead of time to offer a picker. `office usage` is the same shape (direct Redis, no REST equivalent).

## TLS

`--api-url https://<host>:8080` reaches a door serving TLS. Certificates are
verified by default:

```bash
--ca-cert /path/to/ca.pem     # verify against this CA        (H_MESH_CA_CERT)
--insecure                    # skip verification entirely    (H_MESH_INSECURE=1)
```

⚠ **`--insecure` is for the self-signed certificate `setup.sh` generates.** It
keeps the traffic encrypted but stops authenticating the door, so it says
nothing about who answered. Prefer `--ca-cert` wherever the certificate has an
issuer worth checking.

⚠ **Neither option touches the Telegram Bot API.** `api.telegram.org` is a
public host with a real certificate and stays verified — the context is handed
to the h-mesh client only.
