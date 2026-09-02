"""Read-only access to the tenant registry."""

from .keys import prefix


def members(r, *, pod: str, tenant: str) -> set[str]:
    values = r.hkeys(prefix(pod, tenant, resource="registry"))
    return {value.decode() if isinstance(value, bytes) else value for value in values}


def member_types(r, *, pod: str, tenant: str) -> dict[str, str]:
    """Return one registry snapshot preserving each participant's port type."""
    values = r.hgetall(prefix(pod, tenant, resource="registry"))
    return {
        key.decode() if isinstance(key, bytes) else key:
        value.decode() if isinstance(value, bytes) else value
        for key, value in values.items()
    }


def is_member(r, *, pod: str, tenant: str, agent: str) -> bool:
    return bool(r.hexists(prefix(pod, tenant, resource="registry"), agent))


def port_type(r, *, pod: str, tenant: str, agent: str) -> str | None:
    value = r.hget(prefix(pod, tenant, resource="registry"), agent)
    if isinstance(value, bytes):
        return value.decode()
    return value
