"""National Weather Service alerts and hourly forecast adapter."""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from gridbrief.normalization import RawItem, build_raw_item, parse_timestamp

BASE_URL = "https://api.weather.gov"
FORECAST_POINTS = {
    "COAST": (29.7604, -95.3698),
    "EAST": (32.3513, -95.3011),
    "FAR_WEST": (31.7619, -106.4850),
    "NORTH": (33.9137, -98.4934),
    "NORTH_C": (32.7767, -96.7970),
    "SOUTH_C": (30.2672, -97.7431),
    "SOUTH": (27.8006, -97.3964),
    "WEST": (32.4487, -99.7331),
}
DIRECTION_DEGREES = {
    name: index * 22.5
    for index, name in enumerate(
        (
            "N",
            "NNE",
            "NE",
            "ENE",
            "E",
            "ESE",
            "SE",
            "SSE",
            "S",
            "SSW",
            "SW",
            "WSW",
            "W",
            "WNW",
            "NW",
            "NNW",
        )
    )
}


class NWSAdapter:
    name = "nws"
    kind = "nws_api"
    base_url = BASE_URL

    def __init__(self, contact_email: str, timeout: float = 30.0):
        self.timeout = timeout
        self.headers = {
            "Accept": "application/geo+json",
            "User-Agent": f"GridBrief-AI/0.1 ({contact_email})",
        }

    def _get(self, url: str) -> dict[str, Any]:
        for attempt in range(3):
            try:
                request = Request(url, headers=self.headers)
                with urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                    return json.load(response)
            except (OSError, ValueError):
                if attempt == 2:
                    raise
                time.sleep(0.5 * (2**attempt))
        raise RuntimeError("NWS request failed")

    def fetch(self, since: datetime, until: datetime) -> list[RawItem]:
        items = self._fetch_alerts(since, until)
        for location, (latitude, longitude) in FORECAST_POINTS.items():
            try:
                metadata = self._get(f"{BASE_URL}/points/{latitude:.4f},{longitude:.4f}")
                forecast_url = metadata.get("properties", {}).get("forecastHourly")
                if not forecast_url:
                    continue
                forecast = self._get(forecast_url).get("properties", {})
            except (OSError, ValueError):
                # NWS forecast offices fail independently. Preserve alerts and every healthy
                # regional forecast instead of rejecting the entire Texas refresh.
                continue
            updated = parse_timestamp(forecast.get("updated")) or datetime.now(UTC)
            periods = forecast.get("periods", [])
            for period in periods:
                start = parse_timestamp(period.get("startTime"))
                if start is None or not until <= start <= until + timedelta(hours=72):
                    continue
                for metric, value, unit in _forecast_values(period):
                    if value is None:
                        continue
                    items.append(
                        build_raw_item(
                            source=self.name,
                            source_ref=f"forecast:{location}:{metric}:{start.isoformat()}",
                            published_at=updated,
                            kind="timeseries",
                            payload={
                                "dataset": "forecast",
                                "metric": metric,
                                "settlement_point": location,
                                "ts": start.isoformat(),
                                "value": value,
                                "unit": unit,
                            },
                            url=forecast_url,
                        )
                    )
        return items

    def _fetch_alerts(self, since: datetime, until: datetime) -> list[RawItem]:
        url = f"{BASE_URL}/alerts?{urlencode({'area': 'TX', 'limit': 500})}"
        features = self._get(url).get("features", [])
        items: list[RawItem] = []
        active_refs: list[str] = []
        for feature in features:
            props = feature.get("properties", {})
            published = parse_timestamp(props.get("sent") or props.get("effective"))
            if published is None or published > until:
                continue
            identifier = props.get("id") or feature.get("id")
            if not identifier:
                continue
            active_refs.append(str(identifier))
            metadata = "\n".join(
                f"{label}: {value}"
                for label, value in (
                    ("Severity", props.get("severity")),
                    ("Areas", props.get("areaDesc")),
                    ("Effective", props.get("effective")),
                    ("Onset", props.get("onset")),
                    ("Expires", props.get("expires")),
                    ("Ends", props.get("ends")),
                )
                if value
            )
            narrative = "\n\n".join(
                part
                for part in (
                    props.get("headline"),
                    props.get("description"),
                    props.get("instruction"),
                )
                if part
            )
            text = (
                f"GRIDBRIEF_ALERT_METADATA\n{metadata}\nEND_GRIDBRIEF_ALERT_METADATA\n\n"
                f"{narrative}"
            )
            items.append(
                build_raw_item(
                    source=self.name,
                    source_ref=str(identifier),
                    published_at=published,
                    kind="document",
                    payload={
                        "title": props.get("headline") or props.get("event"),
                        "text": re.sub(r"\n{3,}", "\n\n", text).strip(),
                        "topic": "weather",
                        "importance": _severity_score(props.get("severity")),
                        "event": props.get("event"),
                        "area": props.get("areaDesc"),
                    },
                    url=feature.get("id"),
                )
            )
        items.append(
            build_raw_item(
                source=self.name,
                source_ref="active-alert-snapshot",
                published_at=until,
                kind="document",
                payload={
                    "title": "NWS active alert snapshot",
                    "text": json.dumps(active_refs),
                    "topic": "weather_snapshot",
                    "importance": 0.0,
                },
                url=url,
            )
        )
        return items


def _severity_score(severity: str | None) -> float:
    return {"Extreme": 1.0, "Severe": 0.85, "Moderate": 0.6, "Minor": 0.35}.get(
        severity or "", 0.25
    )


def _forecast_values(period: dict[str, Any]) -> list[tuple[str, float | None, str]]:
    temperature = _number(period.get("temperature"))
    if temperature is not None and period.get("temperatureUnit") == "C":
        temperature = temperature * 9 / 5 + 32
    dewpoint = _nested_number(period.get("dewpoint"))
    if dewpoint is not None:
        dewpoint = dewpoint * 9 / 5 + 32
    wind_values = [
        float(value) for value in re.findall(r"\d+(?:\.\d+)?", str(period.get("windSpeed") or ""))
    ]
    return [
        ("weather_temperature_forecast", temperature, "°F"),
        ("weather_dew_point_forecast", dewpoint, "°F"),
        ("weather_relative_humidity_forecast", _nested_number(period.get("relativeHumidity")), "%"),
        (
            "weather_precip_probability_forecast",
            _nested_number(period.get("probabilityOfPrecipitation")),
            "%",
        ),
        (
            "weather_wind_speed_forecast",
            sum(wind_values) / len(wind_values) if wind_values else None,
            "mph",
        ),
        (
            "weather_wind_direction_forecast",
            DIRECTION_DEGREES.get(str(period.get("windDirection") or "").upper()),
            "degrees",
        ),
    ]


def _nested_number(value: Any) -> float | None:
    return _number(value.get("value")) if isinstance(value, dict) else _number(value)


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
