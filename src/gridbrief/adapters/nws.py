"""National Weather Service alerts and hourly forecast adapter."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from gridbrief.normalization import RawItem, build_raw_item, parse_timestamp

BASE_URL = "https://api.weather.gov"
FORECAST_POINTS = {
    "AUSTIN": (30.2672, -97.7431),
    "DALLAS": (32.7767, -96.7970),
    "HOUSTON": (29.7604, -95.3698),
    "MIDLAND": (31.9973, -102.0779),
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
        with urlopen(Request(url, headers=self.headers), timeout=self.timeout) as response:  # noqa: S310
            return json.load(response)

    def fetch(self, since: datetime, until: datetime) -> list[RawItem]:
        items = self._fetch_alerts(since, until)
        for location, (latitude, longitude) in FORECAST_POINTS.items():
            metadata = self._get(f"{BASE_URL}/points/{latitude:.4f},{longitude:.4f}")
            forecast_url = metadata.get("properties", {}).get("forecastHourly")
            if not forecast_url:
                continue
            periods = self._get(forecast_url).get("properties", {}).get("periods", [])
            for period in periods:
                start = parse_timestamp(period.get("startTime"))
                if start is None or not since <= start <= until:
                    continue
                ref = f"forecast:{location}:{start.isoformat()}"
                items.append(
                    build_raw_item(
                        source=self.name,
                        source_ref=ref,
                        published_at=start,
                        kind="timeseries",
                        payload={"dataset": "forecast", "location": location, **period},
                        url=forecast_url,
                    )
                )
        return items

    def _fetch_alerts(self, since: datetime, until: datetime) -> list[RawItem]:
        url = f"{BASE_URL}/alerts?{urlencode({'area': 'TX', 'limit': 500})}"
        features = self._get(url).get("features", [])
        items: list[RawItem] = []
        for feature in features:
            props = feature.get("properties", {})
            published = parse_timestamp(props.get("sent") or props.get("effective"))
            if published is None or not since <= published <= until:
                continue
            identifier = props.get("id") or feature.get("id")
            text = "\n\n".join(
                part
                for part in (
                    props.get("headline"),
                    props.get("description"),
                    props.get("instruction"),
                )
                if part
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
        return items


def _severity_score(severity: str | None) -> float:
    return {"Extreme": 1.0, "Severe": 0.85, "Moderate": 0.6, "Minor": 0.35}.get(
        severity or "", 0.25
    )
