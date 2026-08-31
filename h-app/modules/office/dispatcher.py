"""Unified ``h-mesh`` command dispatcher.

This is a routing layer over the existing command implementations.  The
standalone ``h-mesh-*`` entry points remain supported and authoritative.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from modules.office import cli as office_cli


@dataclass(frozen=True)
class _Route:
    description: str
    module: str
    callable_name: str = "main"
    accepts_argv: bool = False


_SERVICE_ROUTES: dict[str, _Route] = {
    "api": _Route("run the API service", "services.api"),
    "openshell-port": _Route(
        "run the OpenShell port", "modules.openshell.port", accepts_argv=True
    ),
    "session": _Route("run the session service", "services.session"),
    "switch": _Route("run the message switch", "core.service"),
    "tmux-port": _Route("run the tmux port", "modules.tmux.port", accepts_argv=True),
    "tmux-reconciler": _Route(
        "run the tmux reconciler", "services.tmux_reconciler"
    ),
    "watchdog": _Route("run the watchdog service", "services.watchdog"),
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="h-mesh",
        description="Run h-mesh office commands and services.",
    )
    subcommands = parser.add_subparsers(dest="command", metavar="COMMAND")
    for name in office_cli._COMMANDS:
        subcommands.add_parser(name, help=office_cli._DESCRIPTIONS[name], add_help=False)
    for name, route in _SERVICE_ROUTES.items():
        subcommands.add_parser(name, help=route.description, add_help=False)
    return parser


def _load(route: _Route) -> Callable[..., None]:
    module = importlib.import_module(route.module)
    return getattr(module, route.callable_name)


def _dispatch(argv: list[str]) -> None:
    parser = _parser()
    if not argv:
        parser.print_help()
        return
    if argv[0] in ("-h", "--help"):
        parser.parse_args(argv)
        return

    command, remainder = argv[0], argv[1:]
    if command in office_cli._DISPATCH:
        office_cli.main([command, *remainder])
        return

    route = _SERVICE_ROUTES.get(command)
    if route is None:
        parser.error(f"unknown command: {command}")
    target = _load(route)
    if route.accepts_argv:
        target(remainder)
        return
    if remainder:
        parser.error(f"{command} does not accept arguments")
    target()


def main(argv: Sequence[str] | None = None) -> None:
    """Dispatch one h-mesh subcommand without changing its implementation."""

    _dispatch(list(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    main()
