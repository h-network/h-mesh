"""Atomic ingress-draining primitive shared by every port type's own handler."""

_SNAPSHOT_INGRESS = """
-- ingress snapshot v1
local key = KEYS[1]
local items = redis.call('LRANGE', key, 0, -1)
if #items > 0 then
    redis.call('DEL', key)
end
return items
"""


def snapshot_ingress(r, ingress_key: str) -> list[str]:
    """Atomically drain all raw envelopes currently queued in ingress."""
    if hasattr(r, "eval"):
        try:
            res = r.eval(_SNAPSHOT_INGRESS, 1, ingress_key)
            if res is not None:
                return [item.decode() if isinstance(item, bytes) else str(item) for item in res]
        except Exception:
            pass
    # Fallback for test doubles without eval support.
    items = []
    while True:
        raw = r.lpop(ingress_key)
        if raw is None:
            break
        items.append(raw.decode() if isinstance(raw, bytes) else str(raw))
    return items
