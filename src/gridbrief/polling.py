"""Incremental polling windows and rolling revision re-fetch policy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

ROLLING_REFETCH_DAYS = {"ercot": 3, "eia": 7, "nws": 0, "rss": 0}


@dataclass(frozen=True)
class PollWindow:
    since: datetime
    until: datetime
    watermark_at: datetime | None
    rolling_refetch_days: int


def polling_window(
    source: str,
    *,
    hours: int,
    watermark: Any | None,
    now: datetime | None = None,
) -> PollWindow:
    if hours <= 0:
        raise ValueError("hours must be greater than zero")
    until = _utc(now or datetime.now(UTC))
    requested_since = until - timedelta(hours=hours)
    watermark_at = _utc(watermark.window_end) if watermark and watermark.window_end else None
    rolling_days = ROLLING_REFETCH_DAYS[source]

    if watermark_at is None:
        since = requested_since
    elif rolling_days:
        since = min(requested_since, watermark_at - timedelta(days=rolling_days))
    else:
        since = max(requested_since, watermark_at - timedelta(minutes=5))
    return PollWindow(
        since=since, until=until, watermark_at=watermark_at, rolling_refetch_days=rolling_days
    )


def deduplicate_raw_items(items: list[Any]) -> tuple[list[Any], int]:
    """Remove duplicates within one fetch; database upserts handle prior runs."""
    unique: dict[tuple[str, str], Any] = {}
    for item in items:
        unique[(item.source, item.source_ref)] = item
    return list(unique.values()), len(items) - len(unique)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
