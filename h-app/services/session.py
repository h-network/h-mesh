"""Thin launcher for session's WebSocket daemon. The actual logic lives in
modules.session.app -- this file only wires environment into it and
runs the service.
"""

from core.logging import configure_logging
from modules.session.app import SessionSettings, run_session


def main() -> None:
    configure_logging()
    settings = SessionSettings.from_env()
    run_session(settings)


if __name__ == "__main__":
    main()
