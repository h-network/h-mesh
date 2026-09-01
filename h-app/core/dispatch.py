"""Generic port_type dispatch: the switch's kick target looks here to decide
who actually handles a delivery. The switch itself never imports this --
only the process/callback the switch's kick invokes does.
"""

import importlib
import logging
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, Callable, Union

from .keys import prefix
from .logging import log_record

logger = logging.getLogger(__name__)

HandlerSpec = Union[Callable[..., Any], tuple[str, str]]

# GAP: empty on purpose. Each entry is (module_path, attribute_name) for a
# port_type this process can dispatch to -- none exist in h-mesh yet (no
# tmux/api/openshell port modules are built). Whoever builds a port module
# registers it via register_type(), or a future setup step seeds real
# defaults here. Do not fabricate paths to modules that don't exist.
_DEFAULT_REGISTRY: dict[str, HandlerSpec] = {}

_REGISTRY: dict[str, HandlerSpec] = dict(_DEFAULT_REGISTRY)

DELIVERY_LOCK_LEASE_MS = 10_000
DELIVERY_LOCK_ACQUIRE_TIMEOUT = 60.0
DELIVERY_LOCK_POLL_INTERVAL = 0.05

_RENEW_DELIVERY_LOCK = """
-- core delivery lock renew v1
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""

_RELEASE_DELIVERY_LOCK = """
-- core delivery lock release v1
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""

_DRAIN_INGRESS = """
-- core unroutable ingress drain v1
local key = KEYS[1]
local items = redis.call('LRANGE', key, 0, -1)
if #items > 0 then
    redis.call('DEL', key)
end
return items
"""


def register_type(port_type_name: str, handler: HandlerSpec) -> None:
    """Register or override a delivery handler for a port_type.

    A lazy (module_path, attr_name) spec is imported once here to reject an
    invalid registration immediately. The original spec is retained and
    imported again on every lookup so delivery resolves the current attribute.
    """
    if not port_type_name:
        raise ValueError("port_type name must be non-empty")
    resolved = handler
    if isinstance(handler, tuple) and len(handler) == 2:
        module_path, attr_name = handler
        try:
            mod = importlib.import_module(module_path)
            resolved = getattr(mod, attr_name)
        except (ImportError, AttributeError, TypeError) as exc:
            raise ValueError(
                f"invalid delivery handler for port_type {port_type_name!r}: "
                f"cannot resolve {module_path}.{attr_name}"
            ) from exc
    if not callable(resolved):
        raise ValueError(
            f"invalid delivery handler for port_type {port_type_name!r}: "
            "handler must be callable or a resolvable (module_path, attribute_name) spec"
        )
    _REGISTRY[port_type_name] = handler


def unregister_type(port_type_name: str) -> None:
    """Remove a port_type from the registry."""
    _REGISTRY.pop(port_type_name, None)


def reset_registry() -> None:
    """Reset the registry to its default (empty) state -- mainly for tests."""
    global _REGISTRY
    _REGISTRY = dict(_DEFAULT_REGISTRY)


def get_handler(port_type_name: str) -> Callable[..., Any] | None:
    """Look up and resolve the delivery handler for a given port_type.

    Resolves a lazy (module_path, attr_name) spec on every call. Registration
    has already imported it once for validation; lookup imports it again to
    resolve the current attribute.
    """
    if not port_type_name or port_type_name not in _REGISTRY:
        return None
    spec = _REGISTRY[port_type_name]
    if callable(spec):
        return spec
    if isinstance(spec, (tuple, list)) and len(spec) == 2:
        module_path, attr_name = spec
        try:
            mod = importlib.import_module(module_path)
            handler = getattr(mod, attr_name)
        except (ImportError, AttributeError) as exc:
            logger.error(
                "failed to import delivery handler %s.%s for port_type %r: %s",
                module_path, attr_name, port_type_name, exc,
            )
            return None
        if not callable(handler):
            logger.error(
                "delivery handler %s.%s for port_type %r is not callable",
                module_path, attr_name, port_type_name,
            )
            return None
        return handler
    return None


def dead_letter_unroutable(
    r, *, pod: str, tenant: str, agent: str, port_type_name: str | None,
) -> None:
    """Drain and dead-letter everything queued for a destination with no handler."""
    ingress_key = prefix(pod, tenant, agent, "ingress")
    dead_key = prefix(pod, tenant, agent, "dead")
    items = r.eval(_DRAIN_INGRESS, 1, ingress_key)
    for raw in items:
        r.rpush(dead_key, raw)


def dispatch_ingress(r, *, pod: str, tenant: str, agent: str) -> None:
    """Resolve one destination's port_type and hand off to its handler.

    Checks the generic paused marker first. With no registered handler for
    the destination's port_type, everything queued for it is dead-lettered
    instead of silently dropped or retried forever.
    """
    paused_key = prefix(pod, tenant, agent=agent, resource="paused")
    if r.get(paused_key):
        return

    registry_key = prefix(pod, tenant, resource="registry")
    raw_port_type = r.hget(registry_key, agent)
    agent_port_type = raw_port_type.decode() if isinstance(raw_port_type, bytes) else raw_port_type

    handler = get_handler(agent_port_type) if agent_port_type else None
    if handler is None:
        dead_letter_unroutable(r, pod=pod, tenant=tenant, agent=agent, port_type_name=agent_port_type)
        return

    handler(r=r, pod=pod, tenant=tenant, agent=agent)


@contextmanager
def delivery_lock(
    r,
    *,
    pod: str,
    tenant: str,
    agent: str,
    lease_ms: int = DELIVERY_LOCK_LEASE_MS,
    acquire_timeout: float = DELIVERY_LOCK_ACQUIRE_TIMEOUT,
    poll_interval: float = DELIVERY_LOCK_POLL_INTERVAL,
):
    """Serialize delivery with an owned, renewable Redis lease.

    A killed holder's key expires, while a live holder renews until it leaves
    the context. Compare-and-delete prevents an expired holder from removing a
    successor's lease if its ``finally`` block runs late.
    """
    if lease_ms <= 0 or acquire_timeout < 0 or poll_interval <= 0:
        raise ValueError("delivery lock timings must be positive")
    lock_key = prefix(pod, tenant, agent=agent, resource="delivering")
    token = uuid.uuid4().hex
    deadline = time.monotonic() + acquire_timeout
    while not r.set(lock_key, token, nx=True, px=lease_ms):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            reason = f"delivery lock acquisition timed out after {acquire_timeout:.3f}s"
            log_record(
                "dispatch", "delivery_lock_timeout", destination=agent, reason=reason,
            )
            raise TimeoutError(reason)
        time.sleep(min(poll_interval, remaining))

    stop_renewal = threading.Event()
    lease_lost = threading.Event()

    def renew() -> None:
        interval = max(lease_ms / 3000.0, 0.001)
        while not stop_renewal.wait(interval):
            try:
                if not r.eval(_RENEW_DELIVERY_LOCK, 1, lock_key, token, lease_ms):
                    lease_lost.set()
                    return
            except Exception:
                # A later renewal can still preserve ownership after a short
                # Redis fault. The TTL remains the authority meanwhile.
                continue

    renewal = threading.Thread(
        target=renew, name=f"delivery-lock-{agent}", daemon=True,
    )
    renewal.start()
    try:
        yield
    finally:
        stop_renewal.set()
        renewal.join(timeout=max(lease_ms / 1000.0, 0.1))
        try:
            r.eval(_RELEASE_DELIVERY_LOCK, 1, lock_key, token)
        finally:
            if lease_lost.is_set():
                log_record(
                    "dispatch",
                    "delivery_lock_lost",
                    destination=agent,
                    reason="delivery lock lease was lost before handler completed",
                )


def run_delivery_kick(
    agent: str, *, pod: str, tenant: str, r,
) -> None:
    """Serialize one delivery to a destination so a second kick can't race it.

    Acquires and renews a bounded Redis lease before calling
    dispatch_ingress(), then releases only the lease it owns.
    """
    with delivery_lock(r, pod=pod, tenant=tenant, agent=agent):
        dispatch_ingress(r, pod=pod, tenant=tenant, agent=agent)
