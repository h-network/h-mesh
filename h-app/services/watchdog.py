"""Thin launcher for the watchdog daemon. The actual logic lives in
modules.watchdog.service -- this file only wires environment into it.
"""

from modules.watchdog.service import main

if __name__ == "__main__":
    main()
