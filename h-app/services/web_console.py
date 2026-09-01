"""Thin launcher for the web console and Mini App gateway. The actual logic
lives in clients.web.server -- this file only wires environment into it and
starts the server.
"""

from clients.web.server import main as server_main
from core.logging import configure_logging


def main() -> None:
    # ⚠ This launcher, not clients/web/server.py itself: nothing under clients/
    # imports core, deliberately, so running that server directly still gets
    # stdlib logging's unconfigured default.
    configure_logging()
    server_main()


if __name__ == "__main__":
    main()
