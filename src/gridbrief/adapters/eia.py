"""EIA API v2 adapter for ERCOT balancing-authority data."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from gridbrief.normalization import RawItem, build_raw_item

BASE_URL = "https://api.eia.gov/v2/electricity/rto"


class EIAAdapter:
    """Fetch EIA-930 demand, generation, interchange, and fuel data for ERCO."""

    name = "eia"
    kind = "eia_api"
    base_url = BASE_URL

    def __init__(self, api_key: str, timeout: float = 45.0):
        if not api_key:
            raise ValueError("GRIDBRIEF_EIA_API_KEY is required for EIA ingestion")
        self.api_key = api_key
        self.timeout = timeout

    def _get(self, route: str, params: list[tuple[str, str]]) -> list[dict[str, Any]]:
        query = urlencode([("api_key", self.api_key), *params])
        request = Request(
            f"{BASE_URL}/{route}/data/?{query}",
            headers={"Accept": "application/json", "User-Agent": "GridBrief-AI/0.1"},
        )
        with urlopen(request, timeout=self.timeout) as response:  # noqa: S310
            body = json.load(response)
        if "error" in body:
            raise RuntimeError(f"EIA API error: {body['error']}")
        return list(body.get("response", {}).get("data", []))

    def fetch(self, since: datetime, until: datetime) -> list[RawItem]:
        common = [
            ("frequency", "hourly"),
            ("facets[respondent][]", "ERCO"),
            ("start", since.strftime("%Y-%m-%dT%H")),
            ("end", until.strftime("%Y-%m-%dT%H")),
            ("sort[0][column]", "period"),
            ("sort[0][direction]", "asc"),
            ("length", "5000"),
        ]
        region_rows = self._get(
            "region-data",
            [
                ("data[]", "value"),
                ("facets[type][]", "D"),
                ("facets[type][]", "NG"),
                ("facets[type][]", "TI"),
                *common,
            ],
        )
        fuel_rows = self._get("fuel-type-data", [("data[]", "value"), *common])
        items: list[RawItem] = []
        for dataset, rows in (("region-data", region_rows), ("fuel-type-data", fuel_rows)):
            for row in rows:
                ref_parts = [
                    dataset,
                    str(row.get("period", "")),
                    str(row.get("type", "")),
                    str(row.get("fueltype", "")),
                ]
                items.append(
                    build_raw_item(
                        source=self.name,
                        source_ref=":".join(ref_parts),
                        published_at=row.get("period"),
                        kind="timeseries",
                        payload={"dataset": dataset, **row},
                        url=f"{BASE_URL}/{dataset}/data/",
                    )
                )
        return items
