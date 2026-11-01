"""Launch the sending office's reply relay; no credentials in process arguments."""

import os
import signal

import redis

from core.logging import configure_logging
from modules.interoffice.relay import InterofficeRelay
from modules.interoffice.store import Store
from services.daemon_identity import require_daemon_identity


def main():
    pod, tenant = require_daemon_identity()
    configure_logging()
    r = redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"))
    relay = InterofficeRelay(Store(
        r, pod=pod, tenant=tenant,
        allow_plaintext=os.environ.get("H_MESH_ALLOW_PLAINTEXT") == "1",
    ))
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: relay.stop.set())
    try:
        relay.run_forever()
    finally:
        r.close()


if __name__ == "__main__":
    main()
