"""SDK port: one-off Claude Agent SDK query() delivery, no persistent session."""

_DELIVERY_EXPORTS = {"deliver_sdk", "main"}


def __getattr__(name: str):
    """Load delivery code (and the claude_agent_sdk import it carries) lazily."""
    if name not in _DELIVERY_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from . import port

    return getattr(port, name)


__all__ = ["deliver_sdk", "main"]
