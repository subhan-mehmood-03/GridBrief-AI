"""FastAPI website and public JSON contract for GridBrief AI."""

from __future__ import annotations

import csv
import io
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from fastapi import Body, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select

from gridbrief.ai import ask_gridbrief, generate_edition_json
from gridbrief.config import get_settings
from gridbrief.db import session_scope
from gridbrief.models import Edition, IngestionWatermark, Source, Timeseries
from gridbrief.repository import Repository

LOGGER = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).with_name("web_static")
ROLES = ("general", "market_analyst", "grid_operations")
METRICS = {
    "system_load",
    "spp_rt",
    "spp_da",
    "fuel_mix_wind",
    "fuel_mix_solar",
    "fuel_mix_battery_storage",
    "operating_reserves",
    "outages",
    "frequency",
    "temperature",
}


class AskHistoryItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=2_000)


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    question: str = Field(min_length=1, max_length=1_000)
    role: Literal["general", "market_analyst", "grid_operations"] = "general"
    history: list[AskHistoryItem] = Field(default_factory=list, max_length=8)


def _invalid_ask_response(
    message: str = "Please enter a concise GridBrief question.",
) -> dict[str, Any]:
    return {
        "answer": message,
        "sources": {},
        "model": "deterministic",
        "chart_metric": None,
        "query_plan": {},
        "confidence": {"level": "low", "score": 0.0, "reason": message},
        "verification": {"claims_checked": 0, "unsupported_removed": 0, "passed": False},
        "as_of": None,
    }


def _point(row: Timeseries) -> dict[str, Any]:
    return {
        "ts": row.ts.isoformat(),
        "value": float(row.value),
        "unit": row.unit,
        "location": row.settlement_point or "ERCOT",
        "source_id": row.source_id,
    }


def _series(metric: str, hours: int, location: str | None = None) -> list[dict[str, Any]]:
    end = datetime.now(UTC)
    with session_scope() as session:
        rows = Repository(session).get_timeseries(
            metric=metric, settlement_point=location, start=end - timedelta(hours=hours), end=end
        )
        return [_point(row) for row in rows[-5000:]]


def _summary(points: list[dict[str, Any]]) -> dict[str, Any]:
    values = [point["value"] for point in points]
    if not values:
        return {"current": None, "average": None, "min": None, "max": None, "coverage": 0}
    return {
        "current": values[-1],
        "average": sum(values) / len(values),
        "min": min(values),
        "max": max(values),
        "coverage": len(values),
    }


def _edition_payload(edition: Edition) -> dict[str, Any]:
    payload = dict(edition.json or {})
    payload.update(
        {
            "id": edition.id,
            "role": edition.role,
            "status": edition.status,
            "generated_at": edition.generated_at.isoformat(),
            "data_as_of": payload.get("data_as_of") or edition.generated_at.isoformat(),
        }
    )
    payload.setdefault("sections", [])
    payload.setdefault("sources", [])
    payload.setdefault("quality", {"citation_coverage": 1.0, "groundedness": 1.0})
    return payload


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="GridBrief AI", version="0.2.0")

    @app.middleware("http")
    async def secure_responses(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        return response

    @app.exception_handler(Exception)
    async def readable_error(_request: Request, exc: Exception):
        LOGGER.exception("Public request failed", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content={"detail": "GridBrief could not load this data. Please try again."},
        )

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/ready")
    def ready() -> JSONResponse:
        try:
            with session_scope() as session:
                edition = session.execute(select(Edition.id).limit(1)).first()
                point = session.execute(select(Timeseries.id).limit(1)).first()
            if not edition or not point:
                return JSONResponse(
                    status_code=503,
                    content={
                        "status": "not_ready",
                        "detail": "Core data or an edition is missing.",
                    },
                )
            return JSONResponse(content={"status": "ready"})
        except Exception:
            return JSONResponse(
                status_code=503,
                content={"status": "not_ready", "detail": "The data service is unavailable."},
            )

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        with session_scope() as session:
            rows = session.execute(
                select(Source, IngestionWatermark).outerjoin(
                    IngestionWatermark, IngestionWatermark.source_id == Source.id
                )
            ).all()
        sources = [
            {
                "name": source.name,
                "kind": source.kind,
                "status": watermark.status if watermark else "unknown",
                "last_success_at": watermark.last_success_at.isoformat()
                if watermark and watermark.last_success_at
                else None,
                "window_end": watermark.window_end.isoformat()
                if watermark and watermark.window_end
                else None,
            }
            for source, watermark in rows
        ]
        return {
            "iso": settings.iso,
            "timezone": settings.timezone,
            "sources": sources,
            "warnings": [
                f"{s['name']} has no current watermark" for s in sources if s["status"] != "success"
            ],
        }

    @app.get("/api/config")
    def config() -> dict[str, Any]:
        return {
            "ask_available": bool(settings.groq_api_key),
            "generation_available": not settings.public_mode,
            "public_mode": settings.public_mode,
            "iso": settings.iso,
            "timezone": settings.timezone,
        }

    @app.get("/api/metrics")
    def metrics(hours: int = Query(24, ge=1, le=168)) -> dict[str, Any]:
        end = datetime.now(UTC)
        data = {metric: _series(metric, hours) for metric in sorted(METRICS)}
        return {
            "from": (end - timedelta(hours=hours)).isoformat(),
            "to": end.isoformat(),
            "hours": hours,
            "metrics": data,
        }

    @app.get("/api/intelligence")
    def intelligence(hours: int = Query(24, ge=1, le=168)) -> dict[str, Any]:
        metric_data = {name: _series(name, hours) for name in METRICS}
        summaries = {name: _summary(points) for name, points in metric_data.items()}
        prices = {point["location"]: point for point in metric_data["spp_rt"][-50:]}
        return {
            "hours": hours,
            "kpis": summaries,
            "operations": {
                "load": summaries["system_load"],
                "reserves": summaries["operating_reserves"],
                "outages": summaries["outages"],
            },
            "fuel_mix": {
                key: {"summary": summaries[key], "points": metric_data[key]}
                for key in ("fuel_mix_wind", "fuel_mix_solar", "fuel_mix_battery_storage")
            },
            "market": {
                "regional_prices": list(prices.values()),
                "rt": summaries["spp_rt"],
                "da": summaries["spp_da"],
            },
            "risks": [],
            "regional": [],
        }

    @app.get("/api/daily-use")
    def daily_use() -> dict[str, Any]:
        load = _summary(_series("system_load", 24))
        rt = _summary(_series("spp_rt", 24))
        priorities = []
        if load["current"] is not None:
            priorities.append(
                {
                    "severity": "watch",
                    "title": "Track system demand",
                    "detail": f"Current load is {load['current']:,.0f} MW.",
                }
            )
        if rt["max"] is not None:
            priorities.append(
                {
                    "severity": "info",
                    "title": "Review price range",
                    "detail": f"24-hour RT maximum is {rt['max']:,.2f} $/MWh.",
                }
            )
        return {"priorities": priorities, "anomalies": [], "calendar": [], "threshold_alerts": []}

    @app.get("/api/edition/latest")
    def latest_edition(role: str = Query("general")) -> dict[str, Any]:
        if role not in ROLES:
            raise HTTPException(
                422, detail="Role must be general, market_analyst, or grid_operations."
            )
        with session_scope() as session:
            edition = Repository(session).get_latest_edition(role=role)
            if not edition:
                raise HTTPException(404, detail="No edition is available for this persona yet.")
            return _edition_payload(edition)

    @app.get("/api/editions")
    def editions(limit: int = Query(10, ge=1, le=50)) -> dict[str, Any]:
        with session_scope() as session:
            rows = (
                session.execute(select(Edition).order_by(Edition.generated_at.desc()).limit(limit))
                .scalars()
                .all()
            )
            return {
                "editions": [
                    {
                        "id": row.id,
                        "role": row.role,
                        "status": row.status,
                        "generated_at": row.generated_at.isoformat(),
                        "cycle_date": row.cycle_date.isoformat(),
                    }
                    for row in rows
                ]
            }

    @app.get("/api/data/series")
    def data_series(
        metric: str, hours: int = Query(24, ge=1, le=720), location: str | None = None
    ) -> dict[str, Any]:
        if metric not in METRICS:
            raise HTTPException(422, detail="Select a supported GridBrief metric.")
        points = _series(metric, hours, location)
        return {
            "metric": metric,
            "location": location or "all",
            "hours": hours,
            "points": points,
            "summary": _summary(points),
        }

    @app.get("/api/data/export.csv")
    def export_csv(metric: str, hours: int = Query(24, ge=1, le=720), location: str | None = None):
        if metric not in METRICS:
            raise HTTPException(422, detail="Select a supported GridBrief metric.")
        output = io.StringIO()
        writer = csv.DictWriter(
            output, fieldnames=("timestamp", "metric", "location", "value", "unit", "source_id")
        )
        writer.writeheader()
        for point in _series(metric, hours, location):
            writer.writerow(
                {
                    "timestamp": point["ts"],
                    "metric": metric,
                    **{key: point[key] for key in ("location", "value", "unit", "source_id")},
                }
            )
        return Response(
            output.getvalue(),
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="gridbrief-{metric}-{hours}h.csv"'
            },
        )

    @app.post("/api/ask")
    def ask(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        try:
            request = AskRequest.model_validate(payload)
        except ValidationError:
            return _invalid_ask_response()
        try:
            response = ask_gridbrief(
                request.question,
                role=request.role,
                history=[item.model_dump() for item in request.history[-6:]],
            )
        except Exception:
            LOGGER.exception("Ask AI failed")
            return _invalid_ask_response("Ask AI is temporarily unavailable. Please try again.")
        answer = response.get("answer")
        if (
            not isinstance(answer, str)
            or "[object Object]" in answer
            or answer.lstrip().startswith(("{", "["))
        ):
            return _invalid_ask_response(
                "Ask AI returned an unreadable response. Please try again."
            )
        response["answer"] = answer
        return response

    @app.post("/api/generate")
    def generate(
        payload: dict[str, Any] = Body(default_factory=dict), x_admin_key: str | None = Header(None)
    ) -> dict[str, Any]:
        if settings.public_mode and (
            not settings.admin_api_key or x_admin_key != settings.admin_api_key.get_secret_value()
        ):
            raise HTTPException(403, detail="Generation requires an administrator key.")
        role = payload.get("role", "general")
        if role not in ROLES:
            raise HTTPException(422, detail="Select a valid persona.")
        return generate_edition_json(role=role, scheduled=False)

    @app.get("/api/automation")
    def automation() -> dict[str, Any]:
        return {
            "owner": "web" if settings.automatic_refresh else "external",
            "automatic_refresh": settings.automatic_refresh,
            "detail": "GitHub Actions owns scheduled refreshes."
            if not settings.automatic_refresh
            else "The web process owns scheduled refreshes.",
        }

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
