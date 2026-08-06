"""ERCOT structured-data adapter backed by gridstatus."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from gridbrief.normalization import RawItem, build_raw_item, parse_timestamp

BASE_URL = "https://www.ercot.com"
MARKET_TIMEZONE = ZoneInfo("America/Chicago")


class ERCOTAdapter:
    name = "ercot"
    kind = "ercot_api"
    base_url = BASE_URL

    def fetch(self, since: datetime, until: datetime) -> list[RawItem]:
        try:
            from gridstatus import Ercot, Markets
        except ImportError as exc:  # pragma: no cover - environment failure
            raise RuntimeError(
                "ERCOT ingestion requires gridstatus; install gridbrief[ercot]"
            ) from exc

        ercot = Ercot()
        items: list[RawItem] = []
        day = since.astimezone(MARKET_TIMEZONE).date()
        last_day = until.astimezone(MARKET_TIMEZONE).date()
        while day <= last_day:
            date_text = day.isoformat()
            frames = [("load", ercot.get_load(date_text))]
            if day >= datetime.now(UTC).astimezone(MARKET_TIMEZONE).date() - timedelta(days=1):
                frames.append(("fuel_mix", ercot.get_fuel_mix(date_text)))
            for dataset, frame in frames:
                items.extend(_frame_items(dataset, frame, since, until))
            day += timedelta(days=1)

        latest_spp = ercot.get_spp(
            "latest",
            market=Markets.REAL_TIME_15_MIN,
            locations=["HB_NORTH", "HB_SOUTH", "HB_WEST", "HB_HOUSTON"],
            location_type="Trading Hub",
        )
        items.extend(_frame_items("spp", latest_spp, since, until))
        return items


def _frame_items(dataset: str, frame: Any, since: datetime, until: datetime) -> list[RawItem]:
    items: list[RawItem] = []
    for row in frame.to_dict(orient="records"):
        timestamp = parse_timestamp(row.get("Time") or row.get("Interval Start"))
        if timestamp is None or not since <= timestamp <= until:
            continue
        clean = {str(key): _json_value(value) for key, value in row.items()}
        location = clean.get("Location") or "ERCOT"
        items.append(
            build_raw_item(
                source="ercot",
                source_ref=f"{dataset}:{location}:{timestamp.isoformat()}",
                published_at=timestamp,
                kind="timeseries",
                payload={"dataset": dataset, **clean},
                url=BASE_URL,
            )
        )
    return items


def _json_value(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "item"):
        return value.item()
    return value
