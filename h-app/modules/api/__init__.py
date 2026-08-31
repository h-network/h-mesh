"""HTTP API endpoint and mailbox delivery port."""

__all__ = ["ApiSettings", "create_app", "deliver_api"]


def __getattr__(name: str):
    """Keep port dispatch from eagerly importing the HTTP server stack."""
    if name == "deliver_api":
        from .port import deliver_api

        return deliver_api
    if name in {"ApiSettings", "create_app"}:
        from .server import ApiSettings, create_app

        return {"ApiSettings": ApiSettings, "create_app": create_app}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
