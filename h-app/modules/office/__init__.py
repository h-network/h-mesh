"""office: the agent-facing CLI and its receiving lifecycle port.

`cli.py` is the sending half; `port.py` is the receiving half.
"""

def __getattr__(name: str):
    """Load receiving-port code only when its public entrypoint is requested."""
    if name != "deliver_office":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from .port import deliver_office

    return deliver_office


__all__ = ["deliver_office"]
