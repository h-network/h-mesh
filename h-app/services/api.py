"""Thin launcher for the API module's uvicorn server."""

import uvicorn

from modules.api.server import ApiSettings, create_app


def main() -> None:
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
