"""ERCOT structured-data adapter backed by gridstatus."""

from __future__ import annotations

import logging
import math
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from gridbrief.normalization import RawItem, build_raw_item, parse_timestamp

BASE_URL = "https://www.ercot.com"
MARKET_TIMEZONE = ZoneInfo("America/Chicago")
HUBS = ["HB_NORTH", "HB_SOUTH", "HB_WEST", "HB_HOUSTON"]
LOGGER = logging.getLogger(__name__)


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
        today = until.astimezone(MARKET_TIMEZONE).date().isoformat()
        calls = {
            "load": lambda: ercot.get_load("latest"),
            "fuel_mix": lambda: ercot.get_fuel_mix("latest"),
            "wind": lambda: ercot.get_wind_actual_and_forecast_hourly("latest"),
            "solar": lambda: ercot.get_solar_actual_and_forecast_hourly("latest"),
            "outages": lambda: ercot.get_reported_outages("latest"),
            "system_conditions": lambda: ercot.get_real_time_system_conditions("latest"),
            "storage": lambda: ercot.get_energy_storage_resources("latest"),
            "lmp": lambda: ercot.get_lmp("latest"),
            "spp_rt": lambda: ercot.get_spp(
                "latest", market=Markets.REAL_TIME_15_MIN, locations=HUBS
            ),
            "spp_da": lambda: ercot.get_spp(
                "latest", market=Markets.DAY_AHEAD_HOURLY, locations=HUBS
            ),
            "load_forecast": lambda: ercot.get_load_forecast("latest"),
            "adequacy": lambda: ercot.get_short_term_system_adequacy("latest"),
            "as_prices": lambda: ercot.get_as_prices(today),
            "weather_zone_load": lambda: ercot.get_load_by_weather_zone(today),
            "forecast_zone_load": lambda: ercot.get_load_by_forecast_zone(today),
        }
        frames: list[tuple[str, Any]] = []
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {name: executor.submit(call) for name, call in calls.items()}
            for name, future in futures.items():
                try:
                    frames.append((name, future.result()))
                except Exception as exc:
                    LOGGER.warning("Optional ERCOT dataset %s failed: %s", name, exc)
        if not frames:
            raise RuntimeError("Every requested ERCOT public dataset failed")
        items: list[RawItem] = []
        for dataset, frame in frames:
            items.extend(_canonical_items(dataset, frame, since, until))
        return items


def _canonical_items(dataset: str, frame: Any, since: datetime, until: datetime) -> list[RawItem]:
    items: list[RawItem] = []
    for row in frame.to_dict(orient="records"):
        timestamp = parse_timestamp(row.get("Interval Start") or row.get("Time"))
        if timestamp is None:
            continue
        # Forecast datasets legitimately extend past the retrieval timestamp.
        if timestamp < since and dataset not in {"wind", "solar", "load_forecast"}:
            continue
        base_location = str(row.get("Location") or "SYSTEM")
        metrics: list[tuple[str, Any, str, str]] = []

        def add(metric: str, value: Any, unit: str, location: str | None = None) -> None:
            try:
                number = float(value)
            except (TypeError, ValueError):
                return
            if math.isfinite(number):
                metrics.append((metric, number, unit, location or base_location))

        if dataset == "load":
            add("system_load", row.get("Load"), "MW", "ERCOT")
        elif dataset == "fuel_mix":
            for column in (
                "Coal and Lignite",
                "Hydro",
                "Nuclear",
                "Power Storage",
                "Solar",
                "Wind",
                "Natural Gas",
                "Other",
            ):
                add(f"fuel_mix_{_slug(column)}", row.get(column), "MW", "ERCOT")
        elif dataset in {"wind", "solar"}:
            prefix = dataset
            add(f"{prefix}_gen", row.get("GEN SYSTEM WIDE"), "MW")
            add(
                f"{prefix}_forecast",
                row.get("STWPF SYSTEM WIDE" if dataset == "wind" else "STPPF SYSTEM WIDE"),
                "MW",
            )
        elif dataset == "outages":
            add("outages_total", row.get("Combined Total"), "MW")
            add("outages_unplanned", row.get("Combined Unplanned"), "MW")
            add("outages_planned", row.get("Combined Planned"), "MW")
        elif dataset in {"spp_rt", "spp_da"}:
            add(dataset, row.get("SPP"), "$/MWh")
        elif dataset == "lmp" and str(row.get("Location Type")) == "Trading Hub":
            add("lmp", row.get("LMP"), "$/MWh")
        elif dataset == "system_conditions":
            for column, metric, unit in (
                ("Current Frequency", "grid_frequency", "Hz"),
                ("Actual System Demand", "actual_system_demand", "MW"),
                ("Average Net Load", "average_net_load", "MW"),
                ("Total System Capacity excluding Ancillary Services", "system_capacity", "MW"),
                ("Current System Inertia", "system_inertia", "MWs"),
            ):
                add(metric, row.get(column), unit)
        elif dataset == "storage":
            for column, metric in (
                ("Total Charging", "storage_charging"),
                ("Total Discharging", "storage_discharging"),
                ("Net Output", "storage_net_output"),
            ):
                add(metric, row.get(column), "MW")
        elif dataset in {"load_forecast", "weather_zone_load", "forecast_zone_load"}:
            metric = dataset
            ignored = {"Time", "Interval Start", "Interval End", "Publish Time"}
            for column, value in row.items():
                if column not in ignored:
                    add(
                        metric,
                        value,
                        "MW",
                        "SYSTEM" if column in {"System Total", "TOTAL"} else str(column),
                    )
        elif dataset == "adequacy":
            for column, metric in (
                ("Available Capacity Generation", "available_capacity_generation"),
                ("Available Capacity Reserve", "available_capacity_reserve"),
                ("Capacity Reg Up Total", "as_capacity_reg_up"),
                ("Capacity Reg Down Total", "as_capacity_reg_down"),
                ("Capacity RRS Total", "as_capacity_rrs"),
                ("Capacity ECRS Total", "as_capacity_ecrs"),
                ("Capacity NSPIN Total", "as_capacity_nspin"),
            ):
                add(metric, row.get(column), "MW")
        elif dataset == "as_prices":
            for column, metric in (
                ("Non-Spinning Reserves", "as_price_nspin"),
                ("Regulation Down", "as_price_reg_down"),
                ("Regulation Up", "as_price_reg_up"),
                ("Responsive Reserves", "as_price_rrs"),
                ("ERCOT Contingency Reserve Service", "as_price_ecrs"),
            ):
                add(metric, row.get(column), "$/MWh")
        for metric, value, unit, location in metrics:
            items.append(
                build_raw_item(
                    source="ercot",
                    source_ref=f"{dataset}:{metric}:{location}:{timestamp.isoformat()}",
                    published_at=timestamp,
                    kind="timeseries",
                    payload={
                        "dataset": dataset,
                        "metric": metric,
                        "settlement_point": location,
                        "ts": timestamp.isoformat(),
                        "value": value,
                        "unit": unit,
                    },
                    url=BASE_URL,
                )
            )
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


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
