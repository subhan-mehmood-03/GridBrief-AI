"""RSS adapter for official energy-industry feeds."""

from __future__ import annotations

import html
import os
import re
from calendar import timegm
from datetime import UTC, datetime
from typing import Any

from gridbrief.normalization import RawItem, build_raw_item, parse_timestamp

EIA_TODAY_IN_ENERGY = "https://www.eia.gov/rss/todayinenergy.xml"


class RSSAdapter:
    name = "rss"
    kind = "rss"
    base_url = EIA_TODAY_IN_ENERGY

    def __init__(self, feed_urls: dict[str, str] | None = None):
        ercot_feed = os.getenv("GRIDBRIEF_ERCOT_RSS_URL")
        self.feed_urls = feed_urls or {
            "eia_today_in_energy": EIA_TODAY_IN_ENERGY,
            **({"ercot_notices": ercot_feed} if ercot_feed else {}),
        }

    def fetch(self, since: datetime, until: datetime) -> list[RawItem]:
        try:
            import feedparser
        except ImportError as exc:  # pragma: no cover - environment failure
            raise RuntimeError("RSS ingestion requires the feedparser package") from exc

        items: list[RawItem] = []
        for feed_name, feed_url in self.feed_urls.items():
            parsed = feedparser.parse(feed_url)
            if parsed.bozo and not parsed.entries:
                raise RuntimeError(f"Unable to parse RSS feed {feed_url}: {parsed.bozo_exception}")
            for entry in parsed.entries:
                published = _entry_time(entry)
                if published is None or not since <= published <= until:
                    continue
                link = entry.get("link")
                source_ref = entry.get("id") or link or f"{feed_name}:{published.isoformat()}"
                summary = entry.get("summary") or entry.get("description") or ""
                items.append(
                    build_raw_item(
                        source=self.name,
                        source_ref=str(source_ref),
                        published_at=published,
                        kind="document",
                        payload={
                            "feed": feed_name,
                            "title": entry.get("title"),
                            "text": _plain_text(summary),
                            "topic": "grid" if feed_name == "ercot_notices" else "energy_news",
                            "importance": 0.8 if feed_name == "ercot_notices" else 0.45,
                        },
                        url=link,
                    )
                )
        return items


def _entry_time(entry: dict[str, Any]) -> datetime | None:
    structured = entry.get("published_parsed") or entry.get("updated_parsed")
    if structured:
        return datetime.fromtimestamp(timegm(structured), tz=UTC)
    return parse_timestamp(entry.get("published") or entry.get("updated"))


def _plain_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value))).strip()
