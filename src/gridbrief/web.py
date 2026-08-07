"""FastAPI application wiring; routes will be expanded with product features."""

import os
from typing import Any, Literal

from fastapi import Body, FastAPI
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from gridbrief.ai import ask_gridbrief
from gridbrief.config import get_settings


class AskHistoryItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=2_000)


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    question: str = Field(min_length=1, max_length=1_000)
    role: Literal["general", "market_analyst", "grid_operations"] = "general"
    history: list[AskHistoryItem] = Field(default_factory=list, max_length=8)


def _invalid_ask_response() -> dict[str, Any]:
    return {
        "answer": "Please provide a concise GridBrief question and valid conversation history.",
        "sources": {},
        "model": "deterministic",
        "chart_metric": None,
        "query_plan": {},
        "confidence": {"level": "low", "score": 0.0, "reason": "Invalid request."},
        "verification": {"claims_checked": 0, "unsupported_removed": 0, "passed": False},
        "as_of": None,
    }


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

    @app.post("/api/ask")
    def ask(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        """Grounded Ask AI endpoint with a stable, always-string answer contract."""
        try:
            request = AskRequest.model_validate(payload)
        except ValidationError:
            return _invalid_ask_response()
        response = ask_gridbrief(
            request.question,
            role=request.role,
            history=[item.model_dump() for item in request.history],
        )
        response["answer"] = str(response.get("answer") or "Evidence was insufficient to answer.")
        if "[object Object]" in response["answer"] or response["answer"].lstrip().startswith("{"):
            response = _invalid_ask_response()
        return response

    return app


app = create_app()


def main() -> None:
    """Start the FastAPI application for the ``gridbrief-web`` command."""

    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
