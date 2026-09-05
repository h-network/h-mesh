"""webui port: relays Progress/Message envelopes to connected browser tabs."""

_DELIVERY_EXPORTS = {"deliver_webui", "main"}


def __getattr__(name: str):
    if name not in _DELIVERY_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from . import port

    return getattr(port, name)


__all__ = ["deliver_webui", "main"]
