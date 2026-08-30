"""Smoke-test that core.service.Switch actually starts and steps against a real Redis.

Run with REDIS_URL pointed at a throwaway Redis (a fresh DB or a CI service
container) -- this connects for real, unlike the unit tests, which use fakes.
"""

import os
import sys

import redis

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.service import Switch  # noqa: E402


def main() -> None:
    url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
    pod = os.environ.get("POD", "ci-smoke")
    tenant = os.environ.get("TENANT", "ci-smoke")
    r = redis.Redis.from_url(url)
    r.ping()
    switch = Switch(r, pod=pod, tenant=tenant)
    switch.step(timeout=1)
    print("switch started and stepped cleanly")


if __name__ == "__main__":
    main()
