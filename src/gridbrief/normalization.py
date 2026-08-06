"""Canonical data contracts and source-to-row normalization (PRD §18)."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal


@dataclass(frozen=True)
class RawItem:
    source: str
    source_ref: str
    published_at: datetime | None
    kind: Literal["timeseries", "document"]
    payload: dict[str, Any]
    url: str | None
    raw_hash: str


@dataclass(frozen=True)
class TimeseriesRow:
    iso: str
    metric: str
    settlement_point: str
    ts: datetime
    value: float
    unit: str


@dataclass(frozen=True)
class DocumentRow:
    source_ref: str
    title: str | None
    url: str | None
    published_at: datetime | None
    text: str | None
    topic: str | None
    importance: float | None


@dataclass(frozen=True)
class NormalizedItem:
    raw: RawItem
    timeseries: list[TimeseriesRow] = field(default_factory=list)
    document: DocumentRow | None = None


@dataclass(frozen=True)
class MetricSpec:
    label: str
    unit: str
    expected_cadence_minutes: int
    freshness_sla_minutes: int
    allowed_locations: tuple[str, ...]
    source_priority: tuple[str, ...]
    chart_eligible: bool = True


METRIC_REGISTRY: dict[str, MetricSpec] = {
    "spp": MetricSpec(
        "Real-time settlement point price",
        "$/MWh",
        15,
        60,
        ("HB_NORTH", "HB_SOUTH", "HB_WEST", "HB_HOUSTON"),
        ("ercot",),
    ),
    "system_load": MetricSpec("ERCOT system load", "MW", 15, 60, ("ERCOT",), ("ercot", "eia")),
    "net_generation": MetricSpec("Net generation", "MW", 60, 1_800, ("ERCOT",), ("eia",)),
    "interchange_net": MetricSpec("Net interchange", "MW", 60, 1_800, ("ERCOT",), ("eia",)),
    "weather_temperature_forecast": MetricSpec(
        "NWS temperature forecast",
        "°F",
        60,
        180,
        ("AUSTIN", "DALLAS", "HOUSTON", "MIDLAND"),
        ("nws",),
    ),
    "weather_wind_forecast": MetricSpec(
        "NWS wind forecast",
        "mph",
        60,
        180,
        ("AUSTIN", "DALLAS", "HOUSTON", "MIDLAND"),
        ("nws",),
    ),
}
EIA_FUEL_NAMES = {
    "BAT": "battery_storage",
    "COL": "coal",
    "NG": "natural_gas",
    "NUC": "nuclear",
    "OIL": "petroleum",
    "OTH": "other",
    "SUN": "solar",
    "WAT": "hydro",
    "WND": "wind",
}


def build_raw_item(
    *,
    source: str,
    source_ref: str,
    published_at: datetime | str | None,
    kind: Literal["timeseries", "document"],
    payload: dict[str, Any],
    url: str | None,
) -> RawItem:
    timestamp = parse_timestamp(published_at)
    canonical = json.dumps(
        {"source": source, "source_ref": source_ref, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return RawItem(
        source=source,
        source_ref=source_ref,
        published_at=timestamp,
        kind=kind,
        payload=payload,
        url=url,
        raw_hash=hashlib.sha256(canonical.encode()).hexdigest(),
    )


def parse_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def normalize_item(item: RawItem) -> NormalizedItem:
    if item.kind == "document":
        payload = item.payload
        return NormalizedItem(
            raw=item,
            document=DocumentRow(
                source_ref=item.source_ref,
                title=_text(payload.get("title")),
                url=item.url,
                published_at=item.published_at,
                text=_text(payload.get("text")),
                topic=_text(payload.get("topic")),
                importance=_number(payload.get("importance")),
            ),
        )

    normalizers = {"ercot": _normalize_ercot, "eia": _normalize_eia, "nws": _normalize_nws}
    try:
        rows = normalizers[item.source](item)
    except KeyError as exc:
        raise ValueError(f"No timeseries normalizer for source {item.source!r}") from exc
    return NormalizedItem(raw=item, timeseries=_unique_rows(rows))


def _normalize_ercot(item: RawItem) -> list[TimeseriesRow]:
    payload = item.payload
    timestamp = item.published_at
    if timestamp is None:
        return []
    dataset = payload.get("dataset")
    if dataset == "spp":
        return _row("spp", payload.get("Location") or "ERCOT", timestamp, payload.get("SPP"))
    ignored = {"dataset", "Time", "Interval Start", "Interval End"}
    rows: list[TimeseriesRow] = []
    if dataset == "load":
        for name, value in payload.items():
            if name in ignored:
                continue
            location = "ERCOT" if name in {"Load", "TOTAL", "ERCOT"} else _location(name)
            rows.extend(_row("system_load", location, timestamp, value))
    elif dataset == "fuel_mix":
        for fuel, value in payload.items():
            if fuel in ignored:
                continue
            metric = f"fuel_mix_{_slug(fuel)}"
            _register_dynamic(metric, f"{fuel} generation", "MW", 15, 60)
            rows.extend(_row(metric, "ERCOT", timestamp, value))
    return rows


def _normalize_eia(item: RawItem) -> list[TimeseriesRow]:
    payload = item.payload
    timestamp = item.published_at
    if timestamp is None:
        return []
    value = payload.get("value")
    if payload.get("dataset") == "fuel-type-data":
        fuel_code = str(payload.get("fueltype") or "").upper()
        fuel = EIA_FUEL_NAMES.get(
            fuel_code,
            _slug(payload.get("type-name") or payload.get("fueltype-name") or fuel_code),
        )
        metric = f"fuel_mix_{fuel or 'unknown'}"
        label = f"{fuel.replace('_', ' ').title()} generation"
        _register_dynamic(metric, label, "MW", 60, 1_800)
        return _row(metric, "ERCOT", timestamp, value)
    metric = {"D": "system_load", "NG": "net_generation", "TI": "interchange_net"}.get(
        payload.get("type")
    )
    return _row(metric, "ERCOT", timestamp, value) if metric else []


def _normalize_nws(item: RawItem) -> list[TimeseriesRow]:
    payload = item.payload
    timestamp = item.published_at
    if timestamp is None or payload.get("dataset") != "forecast":
        return []
    location = _location(payload.get("location") or "TEXAS")
    temperature = _number(payload.get("temperature"))
    if temperature is not None and payload.get("temperatureUnit") == "C":
        temperature = temperature * 9 / 5 + 32
    rows = _row("weather_temperature_forecast", location, timestamp, temperature)
    wind_match = re.search(r"-?\d+(?:\.\d+)?", str(payload.get("windSpeed", "")))
    if wind_match:
        rows.extend(_row("weather_wind_forecast", location, timestamp, wind_match.group()))
    return rows


def _row(metric: str | None, location: str, ts: datetime, value: Any) -> list[TimeseriesRow]:
    number = _number(value)
    if metric is None or number is None or metric not in METRIC_REGISTRY:
        return []
    return [TimeseriesRow("ERCOT", metric, location, ts, number, METRIC_REGISTRY[metric].unit)]


def _unique_rows(rows: list[TimeseriesRow]) -> list[TimeseriesRow]:
    keyed = {(row.iso, row.metric, row.settlement_point, row.ts): row for row in rows}
    return list(keyed.values())


def _register_dynamic(metric: str, label: str, unit: str, cadence: int, sla: int) -> None:
    METRIC_REGISTRY.setdefault(
        metric, MetricSpec(label, unit, cadence, sla, ("ERCOT",), ("ercot", "eia"))
    )


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _slug(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")


def _location(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", str(value).strip().upper()).strip("_")


def _text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None
