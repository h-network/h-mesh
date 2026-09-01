#!/usr/bin/env python3
"""Application adapter for the payload-ack scenario: verify payloads and
acknowledge them. Ported from the reference implementation's
payload-ack-port.py — same custody events, same checksum-verify-then-Ack
shape, adapted to h-mesh's module names (core.envelope/core.keys/
core.channels instead of the reference's bus/doors modules).
"""
import argparse
import hashlib
import os
import sys
import time

os.environ["H_MESH_WRITER"] = "payload-port"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import redis  # noqa: E402

from core.channels import _emit_for_recipient, send  # noqa: E402
from core.envelope import parse  # noqa: E402
from core.keys import prefix  # noqa: E402
from core.logging import log_record  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pod", required=True)
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--count", type=int, required=True)
    ap.add_argument("--prefix", default="payload-")
    ap.add_argument("--idle-exit", type=float, default=30)
    args = ap.parse_args()

    r = redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"))
    keys = [
        prefix(args.pod, args.tenant, f"{args.prefix}{i}", "ingress")
        for i in range(1, args.count + 1)
    ]
    last = time.time()
    handled = 0
    while True:
        item = r.blpop(keys, timeout=2)
        if item is None:
            if time.time() - last > args.idle_exit:
                break
            continue
        last = time.time()
        key, raw = item
        agent = (key.decode() if isinstance(key, bytes) else key).split(":")[-2]
        try:
            envelope = parse(raw)
        except Exception as exc:
            _emit_for_recipient("payload-port", "dead_lettered", {}, agent, str(exc))
            continue
        _emit_for_recipient("payload-port", "received", envelope, agent)
        _emit_for_recipient("payload-port", "opened", envelope, agent)
        payload = envelope.get("payload", {})
        if envelope.get("kind") == "Message":
            marker = payload.get("marker")
            checksum = payload.get("checksum")
            expected = (
                hashlib.sha256(marker.encode()).hexdigest()
                if isinstance(marker, str)
                else ""
            )
            if checksum != expected:
                _emit_for_recipient(
                    "payload-port", "payload_invalid", envelope, agent,
                    "marker checksum mismatch",
                )
                continue
            _emit_for_recipient("payload-port", "payload_verified", envelope, agent)
            ack_id = send(
                r,
                pod=args.pod,
                tenant=args.tenant,
                source=agent,
                destination=envelope["l2"]["source"],
                kind="Ack",
                correlation_id=envelope["stream_id"],
                payload={"ack_for": envelope["stream_id"], "marker": marker, "checksum": checksum},
                module="payload-port",
            )
            log_record(
                "payload-port", "ack_sent",
                stream_id=ack_id,
                correlation_id=envelope["stream_id"],
                source=agent,
                destination=envelope["l2"]["source"],
            )
        elif envelope.get("kind") == "Ack":
            _emit_for_recipient("payload-port", "ack_verified", envelope, agent)
            _emit_for_recipient("payload-port", "ack_opened", envelope, agent)
        handled += 1
    print(f"payload-port: handled {handled}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
