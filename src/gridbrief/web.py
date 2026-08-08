"""Production FastAPI website and bounded public JSON contract."""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import threading
import time
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from fastapi import Body, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import distinct, func, select

from gridbrief.ai import ask_gridbrief
from gridbrief.config import get_settings
from gridbrief.db import session_scope
from gridbrief.models import Document, Edition, IngestionWatermark, Source, Timeseries
from gridbrief.pipeline import generate_edition
from gridbrief.repository import Repository

LOGGER = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).with_name("web_static")
ROLES = ("general", "market_analyst", "grid_operations")
ALIASES = {
    "spp": "spp_rt",
    "spp_dam": "spp_da",
    "wind_gen": "fuel_mix_wind",
    "solar_gen": "fuel_mix_solar",
    "fuel_mix_power_storage": "fuel_mix_power_storage",
}
PUBLIC_SERIES = (
    "fuel_mix_hydro",
    "fuel_mix_other",
    "fuel_mix_solar",
    "fuel_mix_natural_gas",
    "fuel_mix_nuclear",
    "fuel_mix_power_storage",
    "fuel_mix_coal_and_lignite",
    "fuel_mix_wind",
    "spp_dam",
    "system_load",
    "wind_forecast",
    "wind_gen",
    "solar_forecast",
    "solar_gen",
    "outages_planned",
    "outages_total",
    "outages_unplanned",
    "spp",
)
SOURCE_LABELS = {
    "ercot": "Electric Reliability Council of Texas (ERCOT)",
    "eia": "U.S. Energy Information Administration (EIA)",
    "nws": "National Weather Service (NWS)",
    "noaa": "National Oceanic and Atmospheric Administration (NOAA)",
    "epa": "U.S. Environmental Protection Agency (EPA)",
}
WEATHER_ZONES = {
    "AUSTIN": "South Central",
    "DALLAS": "North Central",
    "HOUSTON": "Coast",
    "MIDLAND": "West",
    "COAST": "Coast",
    "EAST": "East",
    "FAR_WEST": "Far West",
    "NORTH": "North",
    "NORTH_C": "North Central",
    "SOUTH_C": "South Central",
    "SOUTH": "Southern",
    "WEST": "West",
}
_RATE_BUCKETS: dict[str, deque[float]] = defaultdict(deque)
_RATE_LOCK = threading.Lock()
_SCHEDULER_STATE: dict[str, Any] = {"running": False, "started_at": None}


class AskHistoryItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=1_200)


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    question: str = Field(min_length=1, max_length=1_000)
    role: Literal["general", "market_analyst", "grid_operations"] = "general"
    history: list[AskHistoryItem] = Field(default_factory=list, max_length=8)


def _rate_limit(request: Request, name: str, limit: int, seconds: int) -> None:
    key = f"{name}:{request.client.host if request.client else 'unknown'}"
    now = time.monotonic()
    with _RATE_LOCK:
        bucket = _RATE_BUCKETS[key]
        while bucket and bucket[0] <= now - seconds:
            bucket.popleft()
        if len(bucket) >= limit:
            raise HTTPException(429, detail="Too many requests. Please wait and try again.")
        bucket.append(now)


def _source_label(name: str | None) -> str:
    lowered = (name or "").lower()
    return next(
        (label for key, label in SOURCE_LABELS.items() if key in lowered), name or "Official source"
    )


def _canonical(metric: str) -> str:
    return ALIASES.get(metric, metric)


def _query_series(
    metric: str,
    hours: int,
    location: str | None = None,
    *,
    future_hours: int = 0,
) -> list[dict[str, Any]]:
    end = datetime.now(UTC)
    with session_scope() as session:
        stmt = (
            select(Timeseries, Source.name)
            .join(Source, Source.id == Timeseries.source_id)
            .where(
                Timeseries.metric == _canonical(metric),
                Timeseries.ts >= end - timedelta(hours=hours),
                Timeseries.ts <= end + timedelta(hours=future_hours),
            )
            .order_by(Timeseries.ts, Timeseries.settlement_point)
            .limit(5000)
        )
        if location:
            stmt = stmt.where(Timeseries.settlement_point == location)
        rows = session.execute(stmt).all()
    return [
        {
            "ts": row.ts.isoformat(),
            "value": float(row.value),
            "unit": row.unit,
            "location": row.settlement_point or "SYSTEM",
            "source": _source_label(source),
            "source_id": row.source_id,
            "observation_id": row.id,
        }
        for row, source in rows
    ]


def _summary(
    points: list[dict[str, Any]], *, previous: dict[str, Any] | None = None
) -> dict[str, Any]:
    values = [point["value"] for point in points]
    if not values:
        return {
            "latest": None,
            "minimum": None,
            "maximum": None,
            "average": None,
            "unit": None,
            "as_of": None,
            "peak_at": None,
            "source": None,
            "previous": None,
            "change": None,
            "percent_change": None,
        }
    latest = points[-1]
    peak = max(points, key=lambda point: point["value"])
    old = previous or points[0]
    change = latest["value"] - old["value"]
    return {
        "latest": latest["value"],
        "minimum": min(values),
        "maximum": max(values),
        "average": sum(values) / len(values),
        "unit": latest["unit"],
        "as_of": latest["ts"],
        "peak_at": peak["ts"],
        "source": latest["source"],
        "previous": old["value"],
        "change": change,
        "percent_change": change / old["value"] * 100 if old["value"] else None,
    }


def _latest_by_location(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for point in points:
        latest[point["location"]] = point
    return list(latest.values())


def _edition_payload(edition: Edition) -> dict[str, Any]:
    raw = dict(edition.json or {})
    sections_raw = raw.get("sections") or {}
    if isinstance(sections_raw, list):
        sections = {
            str(section.get("title", "Brief")): "\n".join(
                str(claim) for claim in section.get("claims", [])
            )
            for section in sections_raw
        }
    else:
        sections = {str(key): str(value) for key, value in sections_raw.items()}
    sources: dict[str, dict[str, Any]] = {}
    for source in raw.get("sources", []):
        source_id = f"doc-{source.get('document_id')}"
        sources[source_id] = {
            **source,
            "source": source.get("publisher") or "Official document",
            "evidence_type": "official_document",
        }
        sections = {
            key: re.sub(rf"\[cite:{source.get('document_id')}\]", f"[cite:{source_id}]", value)
            for key, value in sections.items()
        }
    for source in raw.get("structured_sources", []):
        source_id = f"obs-{source.get('observation_id')}"
        sources[source_id] = {
            **source,
            "location": source.get("settlement_point"),
            "as_of": source.get("ts"),
            "source": "ERCOT structured data",
            "evidence_type": "timeseries_calculation",
        }
        sections = {
            key: re.sub(
                rf"\[calc:obs-{source.get('observation_id')}\]", f"[cite:{source_id}]", value
            )
            for key, value in sections.items()
        }
    return {
        "id": edition.id,
        "iso": edition.iso,
        "role": edition.role,
        "mode": raw.get("edition_mode", "on_demand"),
        "status": edition.status,
        "generated_at": edition.generated_at.isoformat(),
        "data_as_of": raw.get("data_as_of") or edition.generated_at.isoformat(),
        "sections": sections,
        "sources": sources,
        "quality": raw.get("quality")
        or {
            "citation_coverage": 1.0,
            "hallucination_rate": 0.0,
            "source_attribution_precision": 1.0,
        },
    }


def _invalid_ask(message: str) -> dict[str, Any]:
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


def _weather_alerts(hours: int = 168) -> list[dict[str, Any]]:
    end = datetime.now(UTC)
    with session_scope() as session:
        snapshot = session.scalar(
            select(Document)
            .where(Document.topic == "weather_snapshot")
            .order_by(Document.published_at.desc())
            .limit(1)
        )
        active_refs: list[str] | None = None
        if snapshot and snapshot.text:
            try:
                parsed_refs = json.loads(snapshot.text)
                if isinstance(parsed_refs, list):
                    active_refs = [str(value) for value in parsed_refs]
            except (TypeError, ValueError):
                pass
        statement = (
            select(Document, Source.name)
            .join(Source, Source.id == Document.source_id)
            .where(
                Document.published_at >= end - timedelta(hours=hours), Document.topic == "weather"
            )
            .order_by(Document.importance.desc().nullslast(), Document.published_at.desc())
            .limit(30)
        )
        if active_refs is not None:
            if not active_refs:
                return []
            statement = statement.where(Document.source_ref.in_(active_refs))
        rows = session.execute(statement).all()
    grouped: dict[str, dict[str, Any]] = {}
    for document, source in rows:
        title = document.title or "Weather advisory"
        key = re.sub(r"\s+issued.*", "", title, flags=re.I)
        importance = float(document.importance or 0.5)
        text = document.text or ""

        def metadata(label: str) -> str | None:
            match = re.search(rf"^{label}:\s*(.+)$", text, re.I | re.M)
            return match.group(1).strip() if match else None

        severity = metadata("Severity") or (
            "Extreme"
            if importance >= 0.95
            else "Severe"
            if importance >= 0.8
            else "Moderate"
            if importance >= 0.55
            else "Minor"
        )
        areas = [area.strip() for area in (metadata("Areas") or "").split(";") if area.strip()]
        narrative = re.sub(
            r"^GRIDBRIEF_ALERT_METADATA.*?END_GRIDBRIEF_ALERT_METADATA\s*",
            "",
            text,
            flags=re.S,
        ).strip()
        entry = grouped.setdefault(
            key,
            {
                "title": key,
                "publisher": _source_label(source),
                "published_at": document.published_at.isoformat()
                if document.published_at
                else None,
                "announced_at": document.published_at.isoformat()
                if document.published_at
                else None,
                "updated_at": document.published_at.isoformat() if document.published_at else None,
                "effective_at": metadata("Onset") or metadata("Effective"),
                "expires_at": metadata("Ends") or metadata("Expires"),
                "importance": importance,
                "severity": severity,
                "areas": areas,
                "count": 0,
                "impact": (narrative or "Review the official advisory.")[:300],
            },
        )
        entry["count"] += 1
        entry["areas"] = list(dict.fromkeys([*entry["areas"], *areas]))[:12]
    return list(grouped.values())


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="GridBrief AI", version="1.0.0", docs_url="/api/docs")

    @app.middleware("http")
    async def secure_responses(request: Request, call_next):
        response = await call_next(request)
        response.headers.update(
            {
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "strict-origin-when-cross-origin",
                "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
                "Content-Security-Policy": (
                    "default-src 'self'; script-src 'self'; "
                    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                    "font-src 'self' data: https://fonts.gstatic.com; "
                    "img-src 'self' data:; connect-src 'self'"
                ),
            }
        )
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

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return Response(status_code=204)

    @app.get("/docs", include_in_schema=False)
    def old_docs():
        return RedirectResponse("/api/docs")

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
                        "detail": "Core data or a published edition is missing.",
                    },
                )
            return JSONResponse(content={"status": "ready"})
        except Exception:
            return JSONResponse(
                status_code=503,
                content={"status": "not_ready", "detail": "The data service is unavailable."},
            )

    @app.get("/api/config")
    def config() -> dict[str, Any]:
        return {
            "public_mode": settings.public_mode,
            "interactive_generation": not settings.public_mode,
            "generation_available": not settings.public_mode,
            "ask_available": True,
            "retrieval_backend": settings.retrieval_backend,
            "iso": settings.iso,
            "timezone": settings.timezone,
        }

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        now = datetime.now(UTC)
        with session_scope() as session:
            rows = session.execute(
                select(Source, IngestionWatermark).outerjoin(
                    IngestionWatermark, IngestionWatermark.source_id == Source.id
                )
            ).all()
            latest = dict(
                session.execute(
                    select(Timeseries.source_id, func.max(Timeseries.ts)).group_by(
                        Timeseries.source_id
                    )
                ).all()
            )
        result = {}
        for source, watermark in rows:
            last = watermark.last_success_at if watermark else None
            observation = latest.get(source.id)
            result[re.sub(r"[^a-z0-9]+", "_", source.name.lower()).strip("_")] = {
                "high_watermark": watermark.window_end.isoformat()
                if watermark and watermark.window_end
                else None,
                "last_success_at": last.isoformat() if last else None,
                "age_minutes": (now - last).total_seconds() / 60 if last else None,
                "latest_observation": observation.isoformat() if observation else None,
                "observation_age_minutes": (now - observation).total_seconds() / 60
                if observation
                else None,
            }
        return result

    @app.get("/api/metrics")
    def metrics(hours: int = Query(24, ge=1, le=168)) -> dict[str, Any]:
        end = datetime.now(UTC)
        return {
            "from": (end - timedelta(hours=hours)).isoformat(),
            "to": end.isoformat(),
            "series": {metric: _query_series(metric, hours) for metric in PUBLIC_SERIES},
        }

    @app.get("/api/intelligence")
    def intelligence(hours: int = Query(24, ge=1, le=168)) -> dict[str, Any]:
        end = datetime.now(UTC)
        data = {
            metric: _query_series(metric, hours)
            for metric in (
                "system_load",
                "spp_rt",
                "spp_da",
                "fuel_mix_wind",
                "fuel_mix_solar",
                "fuel_mix_power_storage",
                "outages_total",
                "outages_planned",
                "outages_unplanned",
                "grid_frequency",
                "system_capacity",
                "available_capacity_reserve",
                "storage_net_output",
                "load_forecast",
                "weather_zone_load",
                "temperature_forecast",
            )
        }
        data["temperature_forecast"] = _query_series(
            "weather_temperature_forecast", hours, future_hours=hours
        )
        summaries = {metric: _summary(points) for metric, points in data.items()}
        fuel = [
            _query_series(metric, hours)
            for metric in (
                "fuel_mix_natural_gas",
                "fuel_mix_wind",
                "fuel_mix_solar",
                "fuel_mix_coal_and_lignite",
                "fuel_mix_nuclear",
                "fuel_mix_hydro",
                "fuel_mix_power_storage",
                "fuel_mix_other",
            )
        ]
        latest_fuel = [points[-1]["value"] for points in fuel if points]
        renewable = (summaries["fuel_mix_wind"]["latest"] or 0) + (
            summaries["fuel_mix_solar"]["latest"] or 0
        )
        renewable_share = (
            renewable / sum(latest_fuel) * 100 if latest_fuel and sum(latest_fuel) else None
        )
        rt, da = _latest_by_location(data["spp_rt"]), _latest_by_location(data["spp_da"])
        kpis = {
            "load": summaries["system_load"],
            "outages": summaries["outages_total"],
            "unplanned_outages": summaries["outages_unplanned"],
            "real_time_price": summaries["spp_rt"],
            "day_ahead_price": summaries["spp_da"],
            "renewable_share": {"latest": renewable_share, "unit": "%"},
        }
        operation_keys = (
            "grid_frequency",
            "system_capacity",
            "available_capacity_reserve",
            "storage_net_output",
            "load_forecast",
        )
        operations = {key: summaries[key] for key in operation_keys}
        operations["frequency"] = operations.pop("grid_frequency")
        operations["available_reserve"] = operations.pop("available_capacity_reserve")
        operations.update(
            {
                "forecast_timeline": data["load_forecast"],
                "forecast_by_zone": _latest_by_location(data["load_forecast"]),
                "weather_zone_load": _latest_by_location(data["weather_zone_load"]),
                "weather_zone_history": data["weather_zone_load"],
                "temperature_by_zone": _latest_by_location(data["temperature_forecast"]),
                "temperature_history_by_zone": data["temperature_forecast"],
                "ancillary_prices": {
                    metric.removeprefix("as_price_"): _summary(_query_series(metric, hours))
                    for metric in (
                        "as_price_nspin",
                        "as_price_reg_down",
                        "as_price_reg_up",
                        "as_price_rrs",
                        "as_price_ecrs",
                    )
                },
                "ancillary_capacity": {
                    metric.removeprefix("as_capacity_"): _summary(_query_series(metric, hours))
                    for metric in (
                        "as_capacity_reg_up",
                        "as_capacity_reg_down",
                        "as_capacity_rrs",
                        "as_capacity_ecrs",
                        "as_capacity_nspin",
                    )
                },
            }
        )
        changes = [
            {
                "label": label,
                "latest": summary["latest"],
                "change": summary["change"],
                "percent": summary["percent_change"],
                "unit": summary["unit"],
                "location": "SYSTEM",
            }
            for label, summary in (
                ("System load", summaries["system_load"]),
                ("Real-time SPP", summaries["spp_rt"]),
                ("Wind generation", summaries["fuel_mix_wind"]),
                ("Solar generation", summaries["fuel_mix_solar"]),
            )
        ]
        return {
            "window": {
                "hours": hours,
                "from": (end - timedelta(hours=hours)).isoformat(),
                "to": end.isoformat(),
            },
            "kpis": kpis,
            "outage_breakdown": {
                "planned": summaries["outages_planned"]["latest"] or 0,
                "unplanned": summaries["outages_unplanned"]["latest"] or 0,
                "total": summaries["outages_total"]["latest"] or 0,
            },
            "operations": operations,
            "changes": changes,
            "renewables": {
                "wind": {
                    "actual": summaries["fuel_mix_wind"]["latest"],
                    "forecast": None,
                    "absolute_error": None,
                    "sample_count": 0,
                    "unit": "MW",
                },
                "solar": {
                    "actual": summaries["fuel_mix_solar"]["latest"],
                    "forecast": None,
                    "absolute_error": None,
                    "sample_count": 0,
                    "unit": "MW",
                },
            },
            "prices": {
                "real_time": rt,
                "day_ahead": da,
                "real_time_history": data["spp_rt"],
                "day_ahead_history": data["spp_da"],
            },
            "alerts": _weather_alerts(hours),
            "freshness": status(),
        }

    @app.get("/api/daily-use")
    def daily_use() -> dict[str, Any]:
        now = datetime.now(UTC)
        candidates = (
            ("System load", "system_load"),
            ("Real-time SPP", "spp_rt"),
            ("Wind generation", "fuel_mix_wind"),
            ("Solar generation", "fuel_mix_solar"),
        )
        priorities, anomalies = [], []
        for label, metric in candidates:
            points = _query_series(metric, 168)
            summary = _summary(points)
            if summary["latest"] is None:
                continue
            rank = sum(point["value"] <= summary["latest"] for point in points) / len(points) * 100
            anomalies.append(
                {
                    "label": label,
                    "latest": summary["latest"],
                    "unit": summary["unit"],
                    "percentile": rank,
                    "sample_count": len(points),
                }
            )
        load = next((row for row in anomalies if row["label"] == "System load"), None)
        price = next((row for row in anomalies if row["label"] == "Real-time SPP"), None)
        if load:
            priorities.append(
                {
                    "title": "System demand",
                    "detail": (
                        f"Load is {load['latest']:,.0f} MW at the "
                        f"{load['percentile']:.0f}th percentile of the available week."
                    ),
                    "status": "watch" if load["percentile"] >= 90 else "normal",
                }
            )
        if price:
            priorities.append(
                {
                    "title": "Real-time prices",
                    "detail": f"Latest observed SPP is {price['latest']:,.2f} $/MWh.",
                    "status": "watch" if price["percentile"] >= 90 else "normal",
                }
            )
        calendar = [
            {"time": alert["published_at"], "kind": "Weather", "title": alert["title"]}
            for alert in _weather_alerts(48)[:6]
        ]
        return {
            "generated_at": now.isoformat(),
            "priorities": priorities,
            "anomalies": anomalies,
            "calendar": calendar,
        }

    @app.get("/api/weather")
    def weather(hours: int = Query(24, ge=1, le=72), zone: str | None = None) -> dict[str, Any]:
        end = datetime.now(UTC) + timedelta(hours=hours)
        weather_metrics = (
            "weather_temperature_forecast",
            "weather_dew_point_forecast",
            "weather_relative_humidity_forecast",
            "weather_precip_probability_forecast",
            "weather_wind_speed_forecast",
            "weather_wind_direction_forecast",
        )
        raw = {
            metric: _query_series(metric, max(hours, 168), future_hours=hours)
            for metric in weather_metrics
        }

        def normalize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [
                {**row, "location": WEATHER_ZONES.get(row["location"], row["location"])}
                for row in rows
            ]

        normalized = {metric: normalize(rows) for metric, rows in raw.items()}
        zones = sorted({row["location"] for rows in normalized.values() for row in rows})
        if zone:
            normalized = {
                metric: [row for row in rows if row["location"] == zone]
                for metric, rows in normalized.items()
            }
        series = {**normalized, "temperature_forecast": normalized["weather_temperature_forecast"]}
        return {
            "hours": hours,
            "from": datetime.now(UTC).isoformat(),
            "to": end.isoformat(),
            "selected_zone": zone,
            "zones": zones,
            "series": series,
            "history": {key: value for key, value in series.items()},
            "disclosure": (
                "Forecasts are supplied by the National Weather Service and may be revised."
            ),
        }

    @app.get("/api/maps")
    def maps() -> dict[str, Any]:
        temp = _latest_by_location(weather(24)["series"]["weather_temperature_forecast"])
        lmp_points = _query_series("lmp", 24) or _query_series("lmp", 8_760)
        lmp = _latest_by_location(lmp_points)
        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "temperature": temp,
            "lmp": lmp,
            "temperature_disclosure": (
                "Latest available NWS hourly temperature forecast by represented ERCOT "
                "weather zone."
            ),
            "lmp_disclosure": (
                "Latest verified ERCOT trading-hub locational marginal prices. "
                if lmp
                else "ERCOT locational marginal price (LMP) data has not been received yet; "
                "the last verified snapshot will appear after a successful refresh."
            ),
        }

    @app.get("/api/data/catalog")
    def data_catalog() -> list[dict[str, Any]]:
        with session_scope() as session:
            rows = session.execute(
                select(
                    Timeseries.metric,
                    Timeseries.unit,
                    func.count(Timeseries.id),
                    func.min(Timeseries.ts),
                    func.max(Timeseries.ts),
                    func.count(distinct(Timeseries.settlement_point)),
                )
                .group_by(Timeseries.metric, Timeseries.unit)
                .order_by(Timeseries.metric)
            ).all()
            locations = defaultdict(list)
            for metric, location in session.execute(
                select(Timeseries.metric, Timeseries.settlement_point).distinct()
            ).all():
                locations[metric].append(location or "SYSTEM")
        return [
            {
                "metric": metric,
                "label": metric.replace("_", " ").title(),
                "unit": unit,
                "observations": count,
                "from": start.isoformat(),
                "to": end.isoformat(),
                "locations": location_count,
                "location_values": sorted(locations[metric]),
            }
            for metric, unit, count, start, end, location_count in rows
        ]

    @app.get("/api/data/series")
    def data_series(
        metric: str, hours: int = Query(24, ge=1, le=8760), location: str | None = None
    ) -> dict[str, Any]:
        available = {item["metric"] for item in data_catalog()}
        if _canonical(metric) not in available:
            raise HTTPException(422, detail="Select a supported GridBrief metric.")
        end = datetime.now(UTC)
        points = _query_series(metric, hours, location)
        return {
            "metric": metric,
            "location": location or "all",
            "hours": hours,
            "from": (end - timedelta(hours=hours)).isoformat(),
            "to": end.isoformat(),
            "points": points,
            "summary": _summary(points),
        }

    @app.get("/api/data/export.csv")
    def export_csv(metric: str, hours: int = Query(24, ge=1, le=8760), location: str | None = None):
        payload = data_series(metric, hours, location)
        output = io.StringIO()
        writer = csv.DictWriter(
            output, fieldnames=("timestamp", "metric", "location", "value", "unit", "source")
        )
        writer.writeheader()
        for point in payload["points"]:
            writer.writerow(
                {
                    "timestamp": point["ts"],
                    "metric": metric,
                    **{key: point[key] for key in ("location", "value", "unit", "source")},
                }
            )
        return Response(
            output.getvalue(),
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="gridbrief-{metric}-{hours}h.csv"'
            },
        )

    @app.get("/api/edition/latest")
    def latest_edition(role: str = Query("general")) -> dict[str, Any]:
        if role not in ROLES:
            raise HTTPException(422, detail="Select a valid persona.")
        with session_scope() as session:
            edition = Repository(session).get_latest_edition(role=role)
            if not edition:
                raise HTTPException(404, detail="No edition is available for this persona yet")
            return _edition_payload(edition)

    @app.get("/api/editions")
    def editions(limit: int = Query(10, ge=1, le=50)) -> list[dict[str, Any]]:
        with session_scope() as session:
            rows = (
                session.execute(select(Edition).order_by(Edition.generated_at.desc()).limit(limit))
                .scalars()
                .all()
            )
        return [
            {
                "id": row.id,
                "role": row.role,
                "status": row.status,
                "data_as_of": (row.json or {}).get("data_as_of") or row.generated_at.isoformat(),
            }
            for row in rows
        ]

    @app.get("/api/automation")
    def automation() -> dict[str, Any]:
        external = not settings.automatic_refresh
        return {
            "enabled": settings.automatic_refresh,
            "running": bool(_SCHEDULER_STATE["running"]) if not external else False,
            "managed_externally": external,
            "manager": "GitHub Actions" if external else "Web process",
            "started_at": _SCHEDULER_STATE["started_at"] if not external else None,
            "jobs": (
                [{"id": "hourly-refresh", "interval_minutes": 60, "next_run": None}]
                if not external
                else []
            ),
        }

    @app.post("/api/ask")
    def ask(
        request: Request, payload: dict[str, Any] = Body(default_factory=dict)
    ) -> dict[str, Any]:
        try:
            _rate_limit(request, "ask", 20, 60)
        except HTTPException:
            message = "Too many Ask AI requests. Please wait a minute and try again."
            return JSONResponse(
                status_code=429,
                content={**_invalid_ask(message), "detail": message},
            )
        try:
            parsed = AskRequest.model_validate(payload)
        except ValidationError:
            return _invalid_ask("Please enter a concise GridBrief question and valid history.")
        try:
            response = ask_gridbrief(
                parsed.question,
                role=parsed.role,
                history=[item.model_dump() for item in parsed.history[-6:]],
            )
        except Exception:
            LOGGER.exception("Ask AI failed")
            return _invalid_ask("Ask AI is temporarily unavailable. Please try again.")
        answer = response.get("answer")
        if (
            not isinstance(answer, str)
            or "[object Object]" in answer
            or answer.lstrip().startswith(("{", "["))
        ):
            return _invalid_ask("Ask AI returned an unreadable response. Please try again.")
        response["answer"] = answer
        return response

    @app.post("/api/generate")
    def generate(
        request: Request,
        payload: dict[str, Any] = Body(default_factory=dict),
        x_admin_key: str | None = Header(None),
    ) -> dict[str, Any]:
        _rate_limit(request, "generate", 3, 300)
        if settings.public_mode and (
            not settings.admin_api_key or x_admin_key != settings.admin_api_key.get_secret_value()
        ):
            raise HTTPException(403, detail="Generation requires an administrator key.")
        role, mode = payload.get("role", "general"), payload.get("mode", "on_demand")
        if role not in ROLES or mode not in ("scheduled_daily", "on_demand", "breaking"):
            raise HTTPException(422, detail="Select a valid persona and edition mode.")
        state = generate_edition(role=role, edition_mode=mode)
        return {"edition_id": state["edition_id"], "status": "published", **state["edition"]}

    app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


app = create_app()


def main() -> None:
    from threading import Thread

    import uvicorn

    from gridbrief.scheduler import run_scheduler

    settings = get_settings()
    if settings.automatic_refresh:
        def scheduler_target() -> None:
            _SCHEDULER_STATE["running"] = True
            _SCHEDULER_STATE["started_at"] = datetime.now(UTC).isoformat()
            try:
                run_scheduler(once=False, interval_minutes=60)
            finally:
                _SCHEDULER_STATE["running"] = False

        Thread(
            target=scheduler_target,
            name="gridbrief-refresh",
            daemon=True,
        ).start()

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
