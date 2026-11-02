import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Set

from core.config import state_path
from core.keys import validate_segment
from core.logging import log_record
from lib.paths import build_pane_path, get_agent_workdir, get_workdir_root, resolve_venv_bin
from lib.profile_env import resolve_claude_profile_env
from services.claude_statusline import install_statusline


# Seconds between the paste and the Enter. `paste-buffer -p` only emits the
# bracket markers when the application has asked for bracketed paste mode; a
# CLI that never does gets the old behaviour, and this delay is what that case
# still relies on. 0.5 is the margin decision across CLIs (Claude Code Ink,
# codex, agy) to ensure Enter keystrokes are never swallowed into input boxes.
ENTER_DELAY = float(os.environ.get("PASTE_ENTER_DELAY", "0.5"))
OFFICE_TOOLS_CMD = os.environ.get("OFFICE_TOOLS", "h-mesh-office")
OFFICE_TOOLS_ENV = f"OFFICE_TOOLS={OFFICE_TOOLS_CMD}"


class AmbientTmuxError(RuntimeError):
    """Refused to drive a tmux server we were not explicitly pointed at."""


class EmptyRosterError(RuntimeError):
    """Refusing to reap non-empty session when roster is empty without explicit override."""


class TmuxCommandError(RuntimeError):
    """A tmux command failed, distinct from a valid empty result."""

    def __init__(self, command: str, code: int, stderr: str):
        self.command = command
        self.code = code
        self.stderr = stderr
        super().__init__(f"{command} failed ({code}): {stderr or 'no error output'}")


def require_isolated_tmux(socket: str | None = None) -> None:
    """Refuse to touch whatever tmux server happens to be ambient.

    With no explicit socket, no TMUX_SOCKET, and no TMUX_TMPDIR, tmux would
    drive /tmp/tmux-$UID/default (or $TMUX when running inside an existing session)
    — which, for anything developed inside an office, is the office's own server.
    A reconcile then deletes every window not in the roster it was given, and a
    control-mode client can drive every pane on it. That has destroyed this
    office repeatedly, both times with a warning already written in the docs.

    The container always sets TMUX_TMPDIR, so this costs nothing in production
    and stops the accident everywhere else.
    """
    if not (socket or os.environ.get("TMUX_SOCKET") or os.environ.get("TMUX_TMPDIR")):
        inside = " You are inside a tmux session right now." if os.environ.get("TMUX") else ""
        raise AmbientTmuxError(
            "refusing to use the ambient tmux server: neither TMUX_TMPDIR nor an "
            "explicit socket is set, so this would drive /tmp/tmux-$UID/default."
            + inside
            + " Set TMUX_TMPDIR=$(mktemp -d) for a scratch server, or pass socket=."
        )

    explicit_socket = socket or os.environ.get("TMUX_SOCKET")
    tmux_env = os.environ.get("TMUX")
    if explicit_socket and tmux_env:
        ambient_socket = tmux_env.split(",")[0]
        same_server = False
        try:
            if os.path.realpath(explicit_socket) == os.path.realpath(ambient_socket):
                same_server = True
        except Exception:
            pass
        if not same_server and explicit_socket == ambient_socket:
            same_server = True

        if same_server:
            raise AmbientTmuxError(
                f"refusing to use the ambient tmux server: explicit socket '{explicit_socket}' "
                f"matches the ambient session socket ($TMUX). "
                f"Set TMUX_TMPDIR or TMUX_SOCKET to an isolated location."
            )


def run_tmux(*args: str, socket: str | None = None, input_data: str | None = None) -> tuple[int, str, str]:
    require_isolated_tmux(socket)
    cmd = ["tmux"]
    explicit_socket = socket or os.environ.get("TMUX_SOCKET")
    if explicit_socket:
        cmd.extend(["-S", explicit_socket])
    cmd.extend(args)
    # Strip TMUX and TMUX_PANE so tmux computes its own socket path under TMUX_TMPDIR
    # without preferring the ambient outer session.
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
    proc = subprocess.run(cmd, input=input_data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def list_windows(session_name: str, socket: str | None = None) -> Set[str]:
    ret, stdout, stderr = run_tmux("list-windows", "-t", session_name, "-F", "#{window_name}", socket=socket)
    if ret != 0:
        raise TmuxCommandError("list-windows", ret, stderr)
    return {w for w in stdout.splitlines() if w}


def generate_agents_md(
    agent_name: str,
    tenant: str = "default",
    lead: str | None = None,
    office_cmd: str | None = None,
    operator_entrance: str | None = None,
    enrolled_entrances: set[str] | list[str] | None = None,
    raise_on_invalid: bool = True,
) -> str:
    cmd = office_cmd or os.environ.get("OFFICE_TOOLS", "h-mesh-office")
    entrance = operator_entrance if operator_entrance is not None else os.environ.get("OPERATOR_ENTRANCE")

    valid_entrance: str | None = None
    rejected_entrance: str | None = None
    unverified_entrance: str | None = None

    if entrance:
        if enrolled_entrances is not None:
            if entrance in enrolled_entrances:
                valid_entrance = entrance
            else:
                if raise_on_invalid:
                    raise ValueError(
                        f"Configured operator entrance {entrance!r} is not an enrolled participant "
                        f"in this office (enrolled: {sorted(list(enrolled_entrances))!r})"
                    )
                else:
                    rejected_entrance = entrance
        else:
            try:
                log_record(
                    "tmux", "warning",
                    reason=f"Configured operator entrance {entrance!r} could not be validated "
                           "(enrolled participants unavailable); omitting operator authority rule from guide",
                )
            except Exception:
                pass
            unverified_entrance = entrance

    if lead and agent_name == lead:
        lead_sentence = (
            "You are the lead of this office. The other agents follow your direction, "
            "and yours is the account that decides when something is done.\n\n"
            f"Before you hand out work, check `{cmd} status`. An agent that is `blocked` "
            "will not receive it — hold the work and say so. Do not try to fix the agent.\n\n"
            f"`{cmd} clone-to-all` only ever reuses a copy it made during that same call "
            "as a local source for the next agent — never a target that already existed "
            "before the call started. Mixing a pre-existing agent into the same `-a` list "
            "as fresh hires is safe: the existing one is skipped, not reused as a source.\n\n"
        )
    elif lead:
        lead_sentence = f"{lead} is the lead of this office. Their direction is the office's direction.\n\n"
    else:
        lead_sentence = ""

    if valid_entrance:
        entrance_peers_desc = f"the operator's external entrance (`{valid_entrance}`)"
    elif rejected_entrance:
        entrance_peers_desc = f"external entrances (configured entrance `{rejected_entrance}` is not currently enrolled)"
    elif unverified_entrance:
        entrance_peers_desc = f"external entrances (configured entrance `{unverified_entrance}` unverified)"
    else:
        entrance_peers_desc = "external entrances"

    return f"""You are **{agent_name}**, an agent in this office.

{lead_sentence}Everything about your situation is in your environment:

    $AGENT_NAME      who you are
    $TENANT          the office you are in
    $OFFICE_TOOLS    the commands available to you

Run any of those with --help. To see your tmux colleagues:

    {cmd} peers

A message arrives in your terminal as `[message from <name>] …` — reply by name,
whether or not `peers` lists it:

    {cmd} send -a <name> "one quoted argument"
    {cmd} send -a <name> --stdin      < the body on stdin
    {cmd} send -a <name> --file PATH  the body from a file

⚠ The body is ONE argument. Unquoted words after the first are rejected, not
sent — and anything long or multi-line belongs on --stdin or --file, which are
never shell-parsed. `send` prints the destination and the bytes it accepted;
if that byte count is not what you meant to send, it did not arrive intact.
This directory is yours; work in it.

You have a task board. Nothing will notify you about it — check it yourself:

    {cmd} list        titles waiting for you
    {cmd} take        take the next one, and it prints in full
    {cmd} done --outcome completed
                      when ordinary work is finished; use passed or failed
                      for a review verdict
    {cmd} return      put the open ticket back in todo without cancelling it
    {cmd} show ID     read a ticket in full without taking or changing it
    {cmd} retitle --title TEXT [ID]
                      correct your own open ticket without changing its identity

Take a ticket *before* you start work, not after. `doing` is how the office
knows what you are on.

## Working in this office

This directory is your own clone — work only here. Do not read or write
another agent's files or state directly; `{cmd} send` is how you reach
another agent, not a shortcut around it.

Always reply through `{cmd} send`, not just in your own terminal output.
Text you write as your own response is only visible to whoever is directly
watching that pane — it never reaches the sender unless you also send it.

A short reply that reads like the last line of a conversation — a
confirmation, an acknowledgment, "nice," "got it" — is the highest-risk
moment for skipping this, not a low-risk one. It feels complete as text alone
precisely because it reads like a natural close. Before treating a turn as
finished: did an {cmd} send call actually execute in this turn, not "did I
mean to."

That said, "always reply via {cmd} send" means a substantive message must
actually reach its recipient — it does not mean every message needs a reply
forever. Once an exchange is down to pure closing acknowledgments ("thanks",
"got it", "noted") with nothing new in either message, let it end. Replying
to an acknowledgment with another acknowledgment is a loop, not courtesy —
watchdog will flag it if it keeps going.

Push your own branch, never `main` or `develop` directly — name it
`<your-agent-name>/short-description`. The lead opens the pull request into
`develop` and merges it once CI passes; you don't need `gh`/PR access
yourself, only enough git credential to push your own branch (SSH or
whatever's already set up). `develop` moves to `main` on its own release
cadence, separately from any individual merge.

Before you ask the lead to merge an h-mesh branch, run the authoritative local
suite from the installed development environment:

    python -m tools.run_tests

Report both the collected count and the passed count. Topic-branch pushes do
not run authoritative CI. An old branch can still display a green result from
the workflow stored in that old commit; that result is non-authoritative.

Your git identity is your own agent name — that is how the office knows
which agent did which work. No co-author line, no "generated with" trailer,
no other identity in a commit: only the one you are authenticated as.

When a ticket is done, send the lead ONE line saying it is done, naming the
ticket (title or id), then `{cmd} done`. No summary, no recap of changes.

Long-running work: don't go silent. Background it, or answer a check-in
with real status — not silence until you're finished.

Never dump a credential's actual value to check whether it is set — check
presence only. This office's shells carry real, live credentials; `env |
grep` prints secrets, not just names.

Anything destructive, ambiguous, or outside a ticket's stated scope: ask
the lead first, don't guess.

Read a ticket's claimed cause skeptically — check it against the real
system, logs, or code before building the fix it describes. The ticket can
be wrong.

A behaviour change ships with its docs, in the same branch — not as a
follow-up.

One ticket, one branch, one focused change. Don't fold unrelated work into
a branch just because you're already in it.
"""


def ensure_claude_project_trusted(cwd: str, profile: str | None = None) -> None:
    """⚠ Trust is written where the CLI will read it, which the profile decides.

    An agent with `profile=work` runs with CLAUDE_CONFIG_DIR=~/.claude-work and
    reads its trust from there. Writing to ~/.claude.json trusted a directory for
    an account the agent does not use — so it met the "Yes, I trust this folder"
    picker and sat on it, unreachable, while presence read `idle`.
    """
    try:
        home_dir = os.environ.get("HOME", os.path.expanduser("~"))
        if profile:
            config_path = os.path.join(home_dir, f".claude-{profile}", ".claude.json")
            os.makedirs(os.path.dirname(config_path), exist_ok=True)
        else:
            config_path = os.path.join(home_dir, ".claude.json")
        data = {}
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}

        if "projects" not in data or not isinstance(data["projects"], dict):
            data["projects"] = {}

        if cwd not in data["projects"] or not isinstance(data["projects"][cwd], dict):
            data["projects"][cwd] = {}

        data["projects"][cwd]["hasTrustDialogAccepted"] = True
        data["projects"][cwd]["hasCompletedProjectOnboarding"] = True

        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as exc:
        # ⚠ Never raise into a delivery path — but never vanish either.
        # Silence here is how the profile-blind trust bug hid: seeding
        # failed, every profiled agent sat at a picker unreachable, and
        # presence read `idle` because idle is what a prompt looks like.
        log_record("tmux", "error",
                   reason=f"claude trust seeding failed for {cwd}: {exc}")


def ensure_codex_project_trusted(cwd: str, profile: str | None = None) -> None:
    """Same as the Claude one: CODEX_HOME moves with the profile."""
    try:
        home_dir = os.environ.get("HOME", os.path.expanduser("~"))
        codex_dir = os.path.join(home_dir, f".codex-{profile}" if profile else ".codex")
        os.makedirs(codex_dir, exist_ok=True)
        config_path = os.path.join(codex_dir, "config.toml")

        header = f'[projects."{cwd}"]'
        entry = f'{header}\ntrust_level = "trusted"\n'

        if not os.path.exists(config_path):
            with open(config_path, "w", encoding="utf-8") as f:
                f.write(entry)
            return

        with open(config_path, "r", encoding="utf-8") as f:
            content = f.read()

        if header in content:
            return

        with open(config_path, "a", encoding="utf-8") as f:
            if not content.endswith("\n"):
                f.write("\n")
            f.write(f"\n{entry}")
    except Exception as exc:
        # ⚠ Never raise into a delivery path — but never vanish either.
        # Silence here is how the profile-blind trust bug hid: seeding
        # failed, every profiled agent sat at a picker unreachable, and
        # presence read `idle` because idle is what a prompt looks like.
        log_record("tmux", "error",
                   reason=f"codex trust seeding failed for {cwd}: {exc}")


def ensure_agy_project_trusted(cwd: str) -> None:
    try:
        home_dir = os.environ.get("HOME", os.path.expanduser("~"))
        agy_dir = os.path.join(home_dir, ".gemini", "antigravity-cli")
        os.makedirs(agy_dir, exist_ok=True)
        config_path = os.path.join(agy_dir, "settings.json")

        data = {}
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}

        if not isinstance(data, dict):
            data = {}

        data["enableTelemetry"] = False

        workspaces = data.get("trustedWorkspaces", [])
        if not isinstance(workspaces, list):
            workspaces = []

        if cwd not in workspaces:
            workspaces.append(cwd)
        data["trustedWorkspaces"] = workspaces

        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as exc:
        # ⚠ Never raise into a delivery path — but never vanish either.
        # Silence here is how the profile-blind trust bug hid: seeding
        # failed, every profiled agent sat at a picker unreachable, and
        # presence read `idle` because idle is what a prompt looks like.
        log_record("tmux", "error",
                   reason=f"agy trust seeding failed for {cwd}: {exc}")


def window_env(
    agent_name: str,
    *,
    tenant: str = "default",
    cwd: str | None = None,
    profile: str | None = None,
    provider: dict | None = None,
    skip_permissions: bool | None = None,
    claude_tools: str | None = None,
    log_file: str | Path | None = None,
    venv_bin: str | Path | None = None,
) -> list[str]:
    """Single place where a window environment is constructed for all execution paths."""
    cwd = get_agent_workdir(agent_name, cwd)
    guide_path = f"{cwd}/AGENTS.md"
    # ⚠ Do not inherit ambient H_MESH_LOG_FILE from the caller's process.
    # The window's log path must resolve through `state_path` (which respects
    # `H_MESH_STATE_DIR`) or an explicit `log_file` override. Inheriting an
    # ambient H_MESH_LOG_FILE causes test windows and nested processes to write
    # into whatever production or parent office log happens to be in the environment.
    log_path = str(log_file) if log_file is not None else str(state_path("window.log.jsonl"))

    # ⚠ INJECT PATH DIRECTLY INTO THE CONSTRUCTED ENV.
    # A hired agent's CLI process is spawned directly by tmux without sourcing
    # rc files (~/.bashrc/~/.profile). Construct a complete PATH from known-required
    # locations (virtualenv bin, ~/.local/bin where h-agent is installed, ambient PATH,
    # and system directories) so all tools, h-agent, and python binaries are accessible.
    constructed_path = build_pane_path(venv_bin=venv_bin)

    env_vars = [
        "env",
        f"AGENT_NAME={agent_name}",
        f"TENANT={tenant}",
        f"H_MESH_LOG_FILE={log_path}",
        f"OFFICE_TOOLS={os.environ.get('OFFICE_TOOLS', 'h-mesh-office')}",
        f"AGENT_GUIDE={guide_path}",
        f"PATH={constructed_path}",
    ]
    # ⚠ PER WINDOW, KEYED TO THE PROFILE — never one token for the tenant.
    # A token in the container environment is inherited by EVERY window, which
    # is a single-account mechanism bolted onto a multi-account design: with two
    # accounts it either overrides both profiles with one identity or is ignored
    # where a profile already has credentials. Resolving it from the profile
    # here means the profile decides both which config dir and which
    # credential, so the two compose instead of competing. See
    # lib/profile_env.py for the shared resolution logic (also used by the
    # claude_sdk port, which needs the same mapping for its own one-off SDK
    # calls).
    profile_env = resolve_claude_profile_env(profile)
    env_vars.extend(f"{key}={value}" for key, value in profile_env.items())

    # ⚠ Both are `h-agent`'s own knobs (base image, not h-mesh's), threaded
    # through per window rather than left tenant-wide. Absent is not the same
    # as off/empty for either: `h-agent` reads `AGENT_SKIP_PERMISSIONS`
    # with a default of `1` and `AGENT_CLAUDE_TOOLS` with bash's `${VAR-default}`
    # (unset only), so leaving the variable out entirely is what preserves its
    # own default rather than this layer silently re-deciding it.
    if skip_permissions is not None:
        env_vars.append(f"AGENT_SKIP_PERMISSIONS={'1' if skip_permissions else '0'}")
    # ⚠ `""` is a real, distinct value here — "no restriction" — not "unset".
    # `h-agent` only falls back to its limited default when the variable is
    # completely absent, so a hire that asked for the full tool set must set
    # the variable to empty, never omit it.
    if claude_tools is not None:
        env_vars.append(f"AGENT_CLAUDE_TOOLS={claude_tools}")
    # A local model instead of the vendor's. claude reads these itself; nothing
    # in h-mesh talks to the model, so an agent on a local provider is an agent
    # like any other — same window, same paste, same activity file.
    #
    # ⚠ It uses NO account credential. The watchdog's credential check does not
    # apply to it, and a missing login is not a fault for this agent.
    # Do not seed it a profile expecting one.
    if provider:
        # ⚠ STATE THE INTENT; THE BASE IMAGE TRANSLATES IT. `h-agent` turns
        # these into the CLI's own variables, and it knows things h-mesh should
        # not have to: that claude wants the URL WITHOUT `/v1` because it appends
        # `/v1/messages` itself, that all three model tiers must carry the same
        # id or the others fall back to vendor names the local server does not
        # serve, and that inherited `ANTHROPIC_*` must be stripped first.
        #
        # ⚠ **AND IT REFUSES WHAT IT CANNOT DO.** `h-agent codex` and
        # `h-agent agy` with these set exit 3 instead of starting, rather than
        # silently falling back to the vendor: a codex/agy agent that thinks
        # it's on a local provider but is actually billing and talking to the
        # vendor is a cost and privacy difference the operator was never told
        # about. Delegating to h-agent buys that refusal instead of building
        # a second, separate local-provider translation here.
        url = (provider.get("url") or "").rstrip("/")
        if url:
            env_vars.append(f"AGENT_PROVIDER_URL={url}")
        if provider.get("token"):
            env_vars.append(f"AGENT_PROVIDER_TOKEN={provider['token']}")
        model = provider.get("model")
        if model:
            env_vars.append(f"AGENT_PROVIDER_MODEL={model}")
            if provider.get("small_model"):
                env_vars.append(f"AGENT_PROVIDER_SMALL_MODEL={provider['small_model']}")
    return env_vars


def has_session_history(
    agent: str,
    cli: str,
    profile: str | None = None,
    home_root: str | Path | None = None,
    cwd: str | None = None,
) -> bool:
    """Return True if prior session history exists for the given agent/CLI/profile."""
    home = Path(home_root) if home_root is not None else Path(os.environ.get("HOME", os.path.expanduser("~")))
    suffix = f"-{profile}" if profile else ""
    cwd = get_agent_workdir(agent, cwd)

    if cli == "claude":
        project_dir = home / f".claude{suffix}" / "projects" / cwd.replace("/", "-")
        if not project_dir.is_dir():
            return False
        for p in project_dir.glob("*.jsonl"):
            try:
                if p.is_file() and p.stat().st_size > 0:
                    return True
            except OSError:
                continue
        return False

    elif cli == "codex":
        sessions_dir = home / f".codex{suffix}" / "sessions"
        if not sessions_dir.is_dir():
            return False
        for p in sessions_dir.glob("**/rollout-*.jsonl"):
            if not p.is_file():
                continue
            try:
                with p.open("rb") as f:
                    raw = f.readline()
                record = json.loads(raw)
                if not isinstance(record, dict):
                    continue
                payload = record.get("payload")
                if (
                    record.get("type") == "session_meta"
                    and isinstance(payload, dict)
                    and payload.get("cwd") == cwd
                ):
                    return True
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
        return False

    elif cli == "agy":
        history_file = home / ".gemini" / "antigravity-cli" / "history.jsonl"
        if not history_file.is_file():
            return False
        try:
            with history_file.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                        if isinstance(record, dict) and record.get("workspace") == cwd:
                            return True
                    except json.JSONDecodeError:
                        continue
        except OSError:
            return False
        return False

    return False


class PurgeError(Exception):
    """A purge was refused or could not be completed safely."""


def _agy_history_rewrite(history_file: Path, cwd: str) -> int:
    """Drop this workspace's lines from agy's shared history file; return how many.

    The file is shared by every agy agent, so it is rewritten (temp file in the
    same directory, fsync, os.replace), never deleted. Lines that are not this
    workspace's, including ones that do not parse, are kept byte for byte.

    ⚠ agy does not take any lock of ours. A line appended between our read and
    the replace would be lost, so the file's size and mtime are compared just
    before the replace and the whole rewrite is retried if it moved. A write
    landing in the few microseconds between that check and os.replace is the
    residual window; it cannot be closed without agy's cooperation.
    """
    for _ in range(5):
        try:
            before = history_file.stat()
            raw = history_file.read_bytes()
        except FileNotFoundError:
            return 0
        kept: list[bytes] = []
        dropped = 0
        for line in raw.splitlines(keepends=True):
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                record = None
            if isinstance(record, dict) and record.get("workspace") == cwd:
                dropped += 1
            else:
                kept.append(line)
        if not dropped:
            return 0
        tmp = history_file.with_name(f".{history_file.name}.purge-{os.getpid()}")
        try:
            with tmp.open("wb") as f:
                f.write(b"".join(kept))
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, before.st_mode & 0o7777)
            now = history_file.stat()
            if (now.st_size, now.st_mtime_ns, now.st_ino) != (before.st_size, before.st_mtime_ns, before.st_ino):
                continue
            os.replace(tmp, history_file)
            return dropped
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
    raise PurgeError(f"{history_file} kept changing while it was being rewritten; nothing was removed, try again")


def purge_session_history(
    agent: str,
    cli: str,
    profile: str | None = None,
    home_root: str | Path | None = None,
    cwd: str | None = None,
) -> int:
    """Delete the agent's on-disk CLI session history; return how many sessions or lines went.

    Mirrors `has_session_history` location for location, but deletes. Every
    path is derived from (home, cli, profile, cwd) and checked to stay inside
    the CLI's own history root; nothing is globbed outside it. The caller is
    responsible for refusing a live agent: this function does not know about
    the registry.
    """
    if cli not in ("claude", "codex", "agy"):
        raise PurgeError(f"unknown cli {cli!r}")
    if profile:
        try:
            validate_segment(profile)
        except KeyError:
            raise PurgeError(f"invalid profile {profile!r}") from None
    home = Path(home_root) if home_root is not None else Path(os.environ.get("HOME", os.path.expanduser("~")))
    suffix = f"-{profile}" if profile else ""
    cwd = get_agent_workdir(agent, cwd)
    if not os.path.isabs(cwd) or os.path.normpath(cwd) != cwd or cwd == "/":
        raise PurgeError(f"refusing to purge for working directory {cwd!r}")

    if cli == "claude":
        projects = home / f".claude{suffix}" / "projects"
        project_dir = projects / cwd.replace("/", "-")
        if project_dir.parent != projects or project_dir.name in ("", ".", ".."):
            raise PurgeError(f"refusing history path {project_dir}")
        if project_dir.is_symlink():
            raise PurgeError(f"{project_dir} is a symlink; not following it")
        if not project_dir.is_dir():
            return 0
        sessions = sum(1 for p in project_dir.glob("*.jsonl") if p.is_file())
        shutil.rmtree(project_dir)
        return sessions

    if cli == "codex":
        sessions_dir = home / f".codex{suffix}" / "sessions"
        if sessions_dir.is_symlink() or not sessions_dir.is_dir():
            return 0
        removed = 0
        for root, _dirs, files in os.walk(sessions_dir, followlinks=False):
            for name in files:
                if not (name.startswith("rollout-") and name.endswith(".jsonl")):
                    continue
                p = Path(root) / name
                if p.is_symlink() or not p.is_file():
                    continue
                try:
                    with p.open("rb") as f:
                        record = json.loads(f.readline())
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(record, dict):
                    continue
                payload = record.get("payload")
                if (
                    record.get("type") == "session_meta"
                    and isinstance(payload, dict)
                    and payload.get("cwd") == cwd
                ):
                    p.unlink()
                    removed += 1
        return removed

    history_file = home / ".gemini" / "antigravity-cli" / "history.jsonl"
    if history_file.is_symlink() or not history_file.is_file():
        return 0
    return _agy_history_rewrite(history_file, cwd)


def start_agent_command(
    cli: str,
    resume: bool = False,
) -> list[str]:
    """Construct the h-agent argv list for the given CLI and resume mode."""
    if not resume:
        return ["h-agent", cli]

    if cli == "claude":
        return ["h-agent", "claude", "--continue"]
    elif cli == "codex":
        return ["h-agent", "codex", "resume", "--last"]
    elif cli == "agy":
        return ["h-agent", "agy", "--continue"]
    else:
        return ["h-agent", cli, "--continue"]


def _seed_profile_dirs(profile: str | None) -> None:
    """Populate a profiled CLI's config dir from the image's defaults.

    ⚠ **Unprofiled agents need nothing** — they read `~/.claude` directly, which
    the image already ships populated.

    `seedProfile` comes from the base image, which owns first-run state and CLI
    defaults; h-mesh only decides *which* profile an agent gets. It copies from
    the live config dir at runtime, so there is one source of truth rather than a
    copy here that drifts from the image.

    Never raises: a missing `seedProfile` means an older base, and an agent that
    starts against an unseeded dir is a visible failure, not a silent one.
    """
    if not profile:
        return
    for cli in ("claude", "codex"):
        try:
            result = subprocess.run(
                ["seedProfile", cli, profile],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                try:
                    log_record(
                        "tmux", "error",
                        reason=f"seedProfile {cli} {profile} exited {result.returncode}: "
                               f"{(result.stderr or '').strip()[:200]}",
                    )
                except Exception:
                    pass
        except FileNotFoundError:
            try:
                log_record("tmux", "error",
                           reason="seedProfile is not on PATH — the base image predates it, "
                                  f"so profile {profile!r} is unseeded and its agents may "
                                  "stop on a first-run dialog")
            except Exception:
                pass
            return
        except Exception as exc:
            try:
                log_record("tmux", "error",
                           reason=f"seedProfile {cli} {profile} failed: {exc}")
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Single guide file (issue #31): AGENTS.md only, CLAUDE.md only when we must.
#
# claude reads CLAUDE.md, and -- when a project has none of its own -- falls
# back to AGENTS.md (its `instructionFiles` setting, default
# `claude-md-or-agents-md`). Issue #31 verified this against Claude Code 2.1.302,
# which is the floor below; the operator also confirmed it live on 2.1.316.
# codex/agy read AGENTS.md directly.
#
# ⚠ A "no CLAUDE.md" guide is a guide-less claude agent if that fallback does
# not hold, which is worse than one harmless identical extra file. So the
# per-agent CLAUDE.md is dropped ONLY when `claude_reads_agents_md` can positively
# establish the fallback, and it FAILS CLOSED: anything unknown keeps the
# dual-write. See README "Agent guide" for what it can and cannot detect.
# ---------------------------------------------------------------------------

# The oldest claude the fallback was verified on (issue #31).
CLAUDE_AGENTS_MD_FLOOR = (2, 1, 302)
MANAGED_SETTINGS_DIR = "/etc/claude-code"
_INSTRUCTION_FILES_OK = frozenset({"claude-md-or-agents-md", "claude-md-and-agents-md"})
# Legacy spelling still honoured by claude while instructionFiles is default.
_PROJECT_INSTRUCTIONS_OK = frozenset({"agents-fallback", "both"})


_VERSION_TTL_SECONDS = 30.0
_version_cache: dict = {"at": None, "value": None}


def _claude_version() -> tuple[int, int, int] | None:
    """`claude --version` as (major, minor, patch), or None if unknowable.

    ⚠ Cached for a short TTL only, never for the process lifetime: in a fresh
    container the guide is written seconds after boot, when claude is still
    the version the installer put there, and claude self-updates about a
    minute later. A process-lifetime cache pinned the stale answer forever.
    """
    now = time.monotonic()
    if _version_cache["at"] is not None and now - _version_cache["at"] < _VERSION_TTL_SECONDS:
        return _version_cache["value"]
    value = None
    try:
        proc = subprocess.run(
            ["claude", "--version"], capture_output=True, text=True, timeout=5,
        )
        if proc.returncode == 0:
            match = re.search(r"(\d+)\.(\d+)\.(\d+)", proc.stdout or "")
            if match:
                value = tuple(int(g) for g in match.groups())
    except Exception:
        value = None
    _version_cache.update(at=now, value=value)
    return value  # type: ignore[return-value]


def _clear_version_cache() -> None:
    _version_cache.update(at=None, value=None)


_claude_version.cache_clear = _clear_version_cache  # type: ignore[attr-defined]


def _claude_config_dir(profile: str | None) -> Path:
    home_dir = os.environ.get("HOME", os.path.expanduser("~"))
    return Path(home_dir) / (f".claude-{profile}" if profile else ".claude")


def _settings_files(cwd: str, profile: str | None) -> list[Path]:
    """Every settings file claude merges that could override the fallback."""
    files = [
        _claude_config_dir(profile) / "settings.json",
        Path(cwd) / ".claude" / "settings.json",
        Path(cwd) / ".claude" / "settings.local.json",
    ]
    managed = Path(MANAGED_SETTINGS_DIR)
    files.append(managed / "managed-settings.json")
    try:
        files.extend(sorted((managed / "managed-settings.d").glob("*.json")))
    except OSError:
        pass
    return files


def claude_reads_agents_md(cwd: str, profile: str | None = None) -> tuple[bool, str]:
    """Can claude be relied on to load AGENTS.md in `cwd` without a CLAUDE.md?

    Returns (ok, reason). ⚠ FAILS CLOSED: False for anything not positively
    established, because False only costs one duplicate file.
    """
    version = _claude_version()
    if version is None:
        return False, "claude --version is unavailable"
    if version < CLAUDE_AGENTS_MD_FLOOR:
        return False, (
            f"claude {'.'.join(map(str, version))} is older than the tested "
            f"{'.'.join(map(str, CLAUDE_AGENTS_MD_FLOOR))}"
        )

    for path in _settings_files(cwd, profile):
        if not os.path.lexists(path):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            return False, f"settings file {path} is unreadable: {exc}"
        if not isinstance(data, dict):
            return False, f"settings file {path} is not a JSON object"
        if "instructionFiles" in data and not (
            isinstance(data["instructionFiles"], str) and data["instructionFiles"] in _INSTRUCTION_FILES_OK
        ):
            return False, f"{path} sets instructionFiles={data['instructionFiles']!r}"
        if "projectInstructions" in data and not (
            isinstance(data["projectInstructions"], str)
            and data["projectInstructions"] in _PROJECT_INSTRUCTIONS_OK
        ):
            return False, f"{path} sets projectInstructions={data['projectInstructions']!r}"

    # A CLAUDE.md in an ancestor directory may count as the project's "own".
    # Unverified either way, so it keeps the dual-write.
    for ancestor in Path(cwd).absolute().parents:
        if os.path.lexists(ancestor / "CLAUDE.md"):
            return False, f"{ancestor / 'CLAUDE.md'} exists in an ancestor directory"
    return True, "ok"


def _log(event: str, reason: str) -> None:
    """Guide housekeeping must never raise into a hire or the reconcile loop."""
    try:
        log_record("tmux", event, reason=reason)
    except Exception:
        pass


def _read_bytes(path: str) -> bytes | None:
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def _remove_generated_claude_md(cwd: str, proofs: list[bytes | None]) -> None:
    """Remove `<cwd>/CLAUDE.md` only if it is byte-identical to one of `proofs`.

    ⚠ NEVER delete a CLAUDE.md we cannot prove we generated: a human's file
    would be data loss. A different one is left in place and logged -- note
    claude then reads it INSTEAD of AGENTS.md.
    """
    path = os.path.join(cwd, "CLAUDE.md")
    if not os.path.lexists(path):
        return
    if os.path.islink(path) or not os.path.isfile(path):
        existing = None
    else:
        existing = _read_bytes(path)
    if existing is not None and any(existing == p for p in proofs if p is not None):
        try:
            os.remove(path)
            _log("guide_claude_md_removed", f"removed generated duplicate {path}")
        except OSError as exc:
            _log("error", f"could not remove {path}: {exc}")
        return
    _log(
        "error",
        f"{path} differs from the generated guide, so it was left in place; "
        "claude will read it instead of AGENTS.md",
    )


def migrate_agent_guide(cwd: str, profile: str | None = None) -> str:
    """Clean the duplicate CLAUDE.md of an agent whose guide is not being rewritten.

    The reconciler never recreates an existing window, so already-running
    agents would keep an old dual-write forever. This never rewrites AGENTS.md
    and removes CLAUDE.md only when the fallback is established AND CLAUDE.md
    is byte-identical to the AGENTS.md beside it. Returns a short status.
    """
    claude_md = os.path.join(cwd, "CLAUDE.md")
    if not os.path.lexists(claude_md):
        return "absent"
    ok, reason = claude_reads_agents_md(cwd, profile)
    if not ok:
        return f"kept: {reason}"
    agents = _read_bytes(os.path.join(cwd, "AGENTS.md"))
    if agents is None:
        return "kept: no AGENTS.md beside it"
    _remove_generated_claude_md(cwd, [agents])
    return "removed" if not os.path.lexists(claude_md) else "kept: differs from AGENTS.md"


def write_agent_guide(
    cwd: str, agent_name: str, tenant: str = "default", lead: str | None = None,
    profile: str | None = None, cli: str | None = None,
    operator_entrance: str | None = None,
    enrolled_entrances: set[str] | list[str] | None = None,
) -> None:
    try:
        os.makedirs(cwd, exist_ok=True)
        try:
            content = generate_agents_md(
                agent_name, tenant, lead=lead, operator_entrance=operator_entrance,
                enrolled_entrances=enrolled_entrances, raise_on_invalid=True,
            )
        except ValueError as val_err:
            try:
                log_record("tmux", "error", reason=f"guide write: {val_err}")
            except Exception:
                pass
            content = generate_agents_md(
                agent_name, tenant, lead=lead, operator_entrance=operator_entrance,
                enrolled_entrances=enrolled_entrances, raise_on_invalid=False,
            )

        # What an older dual-write left as AGENTS.md is the proof that a
        # CLAUDE.md beside it is ours, even if the template has changed since.
        previous_agents = _read_bytes(os.path.join(cwd, "AGENTS.md"))
        with open(os.path.join(cwd, "AGENTS.md"), "w", encoding="utf-8") as f:
            f.write(content)

        single, why = claude_reads_agents_md(cwd, profile)
        if single:
            _remove_generated_claude_md(cwd, [previous_agents, content.encode("utf-8")])
        else:
            # ⚠ Unchanged dual-write: the fallback cannot be relied on here.
            with open(os.path.join(cwd, "CLAUDE.md"), "w", encoding="utf-8") as f:
                f.write(content)
            _log("guide_dual_write", f"also wrote CLAUDE.md: {why}")

        # ⚠ SEED THE PROFILE DIR BEFORE TRUST, AND THE ORDER IS LOAD-BEARING.
        # A CLI pointed at CLAUDE_CONFIG_DIR=~/.claude-<profile> does NOT fall
        # back to ~/.claude for its settings or its first-run state — measured
        # in the base image, where `CLAUDE_CONFIG_DIR=~/.claude-test claude`
        # stops on the theme picker. So a profiled agent needs that directory
        # populated or it waits forever for a keypress and looks exactly like an
        # agent with nothing to say.
        #
        # ⚠ `seedProfile` never overwrites an existing file. The trust helpers
        # below CREATE `<dir>/.claude.json`, so running them first leaves a file
        # carrying trust and no `hasCompletedOnboarding` — which seedProfile then
        # declines to touch, and the picker returns. Seed first, always.
        _seed_profile_dirs(profile)

        ensure_claude_project_trusted(cwd, profile=profile)
        ensure_codex_project_trusted(cwd, profile=profile)
        ensure_agy_project_trusted(cwd)

        # ⚠ claude only. setup.sh/h-mesh upgrade cover the default account's
        # ~/.claude up front, but a profiled account's ~/.claude-<profile>
        # doesn't exist until an agent using that profile is hired -- this
        # is that profile's only chance to get it. Idempotent, so calling it
        # on every hire (not just the first) is cheap and correct.
        if cli == "claude":
            home_dir = os.environ.get("HOME", os.path.expanduser("~"))
            config_dir = Path(home_dir) / (f".claude-{profile}" if profile else ".claude")
            install_statusline(config_dir, log=lambda msg: None)
    except Exception as exc:
        # ⚠ Never raise into a delivery path — but never vanish either.
        # Silence here is how the profile-blind trust bug hid: seeding
        # failed, every profiled agent sat at a picker unreachable, and
        # presence read `idle` because idle is what a prompt looks like.
        try:
            log_record("tmux", "error",
                       reason=f"guide write failed for {cwd}: {exc}")
        except Exception:
            pass


def create_window(
    session_name: str,
    agent_name: str,
    command: list[str] | None = None,
    cwd: str | None = None,
    socket: str | None = None,
    lead: str | None = None,
    profile: str | None = None,
    tenant: str = "default",
    log_file: str | Path | None = None,
    cli: str | None = None,
    operator_entrance: str | None = None,
    enrolled_entrances: set[str] | list[str] | None = None,
) -> tuple[int, str, str]:
    """⚠ This writes the guide for every caller, so it needs the lead.

    Without the parameter it wrote a guide with no lead sentence *over* the one
    a caller had just written with it. Measured: the initial window (created by
    new-session, which does not come through here) named the lead; every other
    agent's guide had been silently overwritten and named nobody.
    """
    if agent_name != "__init__":
        cwd = get_agent_workdir(agent_name, cwd)

        try:
            os.makedirs(cwd, exist_ok=True)
        except OSError:
            pass

        write_agent_guide(
            cwd, agent_name, tenant=tenant, lead=lead, profile=profile, cli=cli,
            operator_entrance=operator_entrance,
            enrolled_entrances=enrolled_entrances,
        )

    # ⚠ Idempotent by name. tmux happily creates a second window with the same
    # name, and then refuses to resolve it: `tmux -t hq:<name>` answers
    # "can't find window" on an ambiguous target. Every delivery to that agent
    # fails from then on, silently.
    #
    # Measured: hiring an existing name three times left three windows called
    # `rehire` and made the agent unaddressable. Re-writing the guide above is
    # deliberate and harmless — it refreshes the lead sentence — but a second
    # window is not.
    try:
        current_windows = list_windows(session_name, socket=socket)
    except Exception:
        current_windows = set()
    if agent_name in current_windows:
        return 0, "", ""

    if not command:
        command = window_env(agent_name, tenant=tenant, cwd=cwd, profile=profile, log_file=log_file) + ["bash", "-il"]

    args = ["new-window", "-t", f"{session_name}:", "-n", agent_name]
    if cwd:
        args.extend(["-c", cwd])
    args.extend(command)
    ret, out, err = run_tmux(*args, socket=socket)
    if ret == 0:
        run_tmux("set-window-option", "-t", f"{session_name}:{agent_name}", "automatic-rename", "off", socket=socket)
        run_tmux("set-window-option", "-t", f"{session_name}:{agent_name}", "allow-rename", "off", socket=socket)
    return ret, out, err


def kill_window(session_name: str, window_name: str, socket: str | None = None) -> tuple[int, str, str]:
    ret, out, err = run_tmux("kill-window", "-t", f"{session_name}:{window_name}", socket=socket)
    if ret == 0:
        log_record("tmux", "window_killed", destination=window_name)
    return ret, out, err


def submit_text(
    session_name: str,
    agent_name: str,
    text: str,
    stream_id: str = "",
    socket: str | None = None,
) -> None:
    target = f"{session_name}:{agent_name}"
    nonce = os.urandom(8).hex()
    sid_part = f"_{stream_id}" if stream_id else ""
    buf_name = f"hmesh_{agent_name}{sid_part}_{nonce}"

    def checked(command: str, result: tuple[int, str, str]) -> None:
        code, _, stderr = result
        if code != 0:
            raise TmuxCommandError(command, code, stderr)

    try:
        checked(
            "load-buffer",
            run_tmux("load-buffer", "-b", buf_name, "-", socket=socket, input_data=text),
        )
        checked(
            "paste-buffer",
            run_tmux("paste-buffer", "-b", buf_name, "-p", "-d", "-t", target, socket=socket),
        )
        time.sleep(ENTER_DELAY)
        checked("send-keys", run_tmux("send-keys", "-t", target, "Enter", socket=socket))
    except Exception:
        run_tmux("delete-buffer", "-b", buf_name, socket=socket)
        raise
