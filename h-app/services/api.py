"""Thin launcher for the API module's uvicorn server."""

import uvicorn

from core.logging import configure_logging
from modules.api.server import ApiSettings, create_app


def main() -> None:
    # Before uvicorn.run: its own dictConfig sets disable_existing_loggers
    # False and never touches the root logger, so this threshold survives it.
    # ⚠ It does not reach uvicorn's own loggers -- those take their level from
    # uvicorn's `log_level` argument, not from root.
    configure_logging()
    settings = ApiSettings.from_env()
    settings.validate()
    kwargs = {}
    if settings.api_tls_cert and settings.api_tls_key:
        kwargs["ssl_certfile"] = settings.api_tls_cert
        kwargs["ssl_keyfile"] = settings.api_tls_key
    uvicorn.run(
        create_app(settings=settings),
        host=settings.api_bind,
        port=settings.api_port,
        **kwargs,
    )


if __name__ == "__main__":
    main()
