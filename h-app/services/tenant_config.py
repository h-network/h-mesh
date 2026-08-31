"""Durable per-tenant config for the interactive setup wizard.

setup.sh's wizard collects the agent roster, CLI/account choices, and local
model provider settings once, interactively -- but it (and h-mesh upgrade,
and h-mesh start) run again later, in a different shell, with none of that
in the ambient environment. This is where it survives: one KEY=VALUE file
per tenant under h-mesh's own state dir, read back as prompt defaults on the
next setup.sh run ("blank keeps existing", same as the reference project's tenant .env)
and merged into the daemon environment so PROVIDER_*/CLAUDE_OAUTH_TOKEN_*
are there for `services.daemons.start_daemons` regardless of which shell
started it.

setup.sh never parses or writes this file itself -- it shells out to this
module's `get`/`set` subcommands, so there is exactly one place that knows
the file's format.
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from core.config import state_dir


def tenant_env_path(tenant: str) -> Path:
    return state_dir() / tenant / "env"


def read_tenant_env(tenant: str) -> dict[str, str]:
    path = tenant_env_path(tenant)
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def write_tenant_env(tenant: str, values: dict[str, str]) -> None:
    """Rewrite the whole file. Keys with an empty/None value are omitted --
    absent means "not configured", not "configured as empty string"; see
    modules/office/cli.py and reconciler.py, which both rely on that
    distinction for optional per-agent settings."""
    path = tenant_env_path(tenant)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{key}={value}" for key, value in values.items() if value]
    content = "\n".join(lines)
    if content:
        content += "\n"
    path.write_text(content)
    path.chmod(0o600)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="h-mesh tenant-config")
    sub = parser.add_subparsers(dest="command", required=True)

    p_get = sub.add_parser("get", help="print one value (or a default) from the tenant's config")
    p_get.add_argument("tenant")
    p_get.add_argument("key")
    p_get.add_argument("default", nargs="?", default="")

    sub.add_parser(
        "set", help="replace the tenant's whole config with KEY=VALUE lines read from stdin"
    ).add_argument("tenant")

    args = parser.parse_args(argv)

    if args.command == "get":
        values = read_tenant_env(args.tenant)
        print(values.get(args.key, args.default))
    elif args.command == "set":
        values: dict[str, str] = {}
        for line in sys.stdin:
            line = line.rstrip("\n")
            if not line or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key] = value
        write_tenant_env(args.tenant, values)


if __name__ == "__main__":
    main()
