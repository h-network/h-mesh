"""Thin launcher for session's WebSocket daemon. The actual logic lives in
modules.session.app -- this file only wires environment into it and
runs the service.
"""

from modules.session.app import SessionSettings, run_session


def main() -> None:
    settings = SessionSettings.from_env()
    run_session(settings)


if __name__ == "__main__":
    main()
