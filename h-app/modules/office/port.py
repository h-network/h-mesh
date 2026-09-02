"""Office port: drain lifecycle envelopes made available by each kick."""

import os
import signal
import subprocess
import sys

import redis

from core.channels import DeadLetter, receive
from core.dispatch import delivery_lock
from core.keys import prefix
from core.logging import configure_logging
from lib.agentlifecycle.lifecycle import (
    ProvableLifecycleRejection, pause_agent, resume_agent, start_agent, stop_agent,
)
from modules.tmux.ops import kill_window, run_tmux


def _ensure_tmux(command: str, result: tuple[int, str, str]) -> None:
    code, _, stderr = result
    if code != 0:
        raise RuntimeError(f"{command} failed: {stderr}")


def _kick(agent: str) -> None:
    """Start one independent tmux-port delivery attempt for a resumed agent."""
    kwargs = {}
    custody_path = os.environ.get("H_MESH_LOG_FILE", "")
    fd_prefix = "/proc/self/fd/"
    if custody_path.startswith(fd_prefix):
        try:
            custody_fd = int(custody_path[len(fd_prefix):])
            os.fstat(custody_fd)
        except (OSError, ValueError):
            pass
        else:
            # The office port itself was kicked by the switch. Keep its
            # custody-only pipe alive through this nested tmux-port kick so
            # the child can write to the H_MESH_LOG_FILE it inherited.
            kwargs["pass_fds"] = (custody_fd,)
    subprocess.Popen([sys.executable, "-m", "modules.tmux.port", agent], **kwargs)


def _lifecycle_opener(operation, **kwargs):
    """Make only pre-mutation validation rejection explicit to receive().

    Lifecycle validates payload shape before its first desired-state write.
    ValueError therefore proves the effect did not begin; every other exception
    retains its UNKNOWN semantics and must reach receive() unchanged.
    """
    try:
        return operation(**kwargs)
    except ProvableLifecycleRejection as exc:
        raise DeadLetter(str(exc)) from exc


def deliver_office(
    r,
    pod: str,
    tenant: str,
    agent: str,
    session_name: str | None = None,
    socket: str | None = None,
    timeout: int = 0,
    blocking: bool = False,
    **kwargs,
) -> None:
    """Drain lifecycle envelopes addressed to the office port."""
    session_name = session_name or os.environ.get("TMUX_SESSION") or tenant
    socket = socket or os.environ.get("TMUX_SOCKET")

    def kill(target: str) -> None:
        _ensure_tmux("kill-window", kill_window(session_name, target, socket=socket))

    def interrupt(target: str) -> None:
        _ensure_tmux(
            "pause send-keys",
            run_tmux("send-keys", "-t", f"{session_name}:{target}", "C-c", socket=socket),
        )

    def resume(target: str) -> None:
        _ensure_tmux(
            "resume send-keys",
            run_tmux(
                "send-keys",
                "-t",
                f"{session_name}:{target}",
                "startAgent --resume",
                "Enter",
                socket=socket,
            ),
        )

    # Account discovery has not been ported to h-mesh. Returning None preserves
    # agentlifecycle's explicit "catalog unavailable" behavior while still
    # validating the profile as a safe key segment.
    def unavailable_profiles(_pod: str, _tenant: str) -> None:
        return None

    openers = {
        "StartAgent": lambda envelope: _lifecycle_opener(
            start_agent, r=r,
            pod=pod,
            tenant=tenant,
            envelope=envelope,
            replace_window=kill,
            available_profiles=unavailable_profiles,
        ),
        "StopAgent": lambda envelope: _lifecycle_opener(
            stop_agent, r=r, pod=pod, tenant=tenant, envelope=envelope, kill_window=kill
        ),
        "PauseAgent": lambda envelope: _lifecycle_opener(
            pause_agent, r=r, pod=pod, tenant=tenant, envelope=envelope, interrupt_window=interrupt
        ),
        "ResumeAgent": lambda envelope: _lifecycle_opener(
            resume_agent, r=r,
            pod=pod,
            tenant=tenant,
            envelope=envelope,
            resume_window=resume,
            kick_agent=_kick,
        ),
    }

    receive(
        r,
        pod=pod,
        tenant=tenant,
        agent=agent,
        openers=openers,
        timeout=timeout,
        blocking=blocking,
        module="office",
    )


def main(argv: list[str] | None = None) -> None:
    # First thing in the process, and only in the process: this is the entry
    # point, so it is the one place allowed to set the root logger's level.
    configure_logging()
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print("usage: python -m modules.office.port <agent>", file=sys.stderr)
        raise SystemExit(1)
    agent = args[0]
    pod = os.environ["POD"]
    tenant = os.environ["TENANT"]
    redis_url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")

    r = redis.Redis.from_url(redis_url)
    with delivery_lock(r, pod=pod, tenant=tenant, agent=agent):
        if r.get(prefix(pod, tenant, agent=agent, resource="paused")):
            return
        deliver_office(r, pod=pod, tenant=tenant, agent=agent)


if __name__ == "__main__":
    main()
