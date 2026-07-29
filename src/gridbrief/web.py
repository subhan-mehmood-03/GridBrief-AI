"""FastAPI application wiring; routes will be expanded with product features."""

import os

from fastapi import FastAPI

from gridbrief.config import get_settings


def create_app() -> FastAPI:
    """Create the web application with safe operational probes."""

    settings = get_settings()
    app = FastAPI(title="GridBrief AI", version="0.1.0")

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/ready")
    def ready() -> dict[str, str]:
        return {"status": "not_ready", "detail": "Feature services are not implemented yet."}

    @app.get("/api/status")
    def status() -> dict[str, str | bool]:
        return {
            "iso": settings.iso,
            "public_mode": settings.public_mode,
            "automatic_refresh": settings.automatic_refresh,
        }

    return app


app = create_app()


def main() -> None:
    """Start the FastAPI application for the ``gridbrief-web`` command."""

    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))

