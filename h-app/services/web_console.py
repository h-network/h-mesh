"""Thin launcher for the web console and Mini App gateway. The actual logic
lives in clients.web.server -- this file only wires environment into it and
starts the server.
"""

from clients.web.server import main as server_main


def main() -> None:
    server_main()


if __name__ == "__main__":
    main()
