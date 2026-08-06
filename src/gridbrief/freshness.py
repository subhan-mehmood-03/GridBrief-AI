"""Per-source ingestion freshness and health reporting."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Literal

SOURCE_FRESHNESS_SLA_MINUTES = {"ercot": 60, "eia": 1_800, "nws": 60, "rss": 60}


@dataclass(frozen=True)
class FreshnessStatus:
    source: str
    status: Literal["fresh", "stale", "missing", "error"]
    age_minutes: float | None
    sla_minutes: int
    last_success_at: datetime | None
    detail: str

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["last_success_at"] = (
            self.last_success_at.isoformat() if self.last_success_at else None
        )
        return result


def source_freshness(
    source: str, watermark: Any | None, *, now: datetime | None = None
) -> FreshnessStatus:
    sla = SOURCE_FRESHNESS_SLA_MINUTES[source]
    if watermark is None or watermark.last_success_at is None:
        return FreshnessStatus(source, "missing", None, sla, None, "No successful ingest recorded")
    last_success = _utc(watermark.last_success_at)
    age = max(0.0, ((_utc(now or datetime.now(UTC)) - last_success).total_seconds() / 60))
    if watermark.status not in {"ok", "success"}:
        return FreshnessStatus(
            source,
            "error",
            age,
            sla,
            last_success,
            f"Latest watermark status is {watermark.status!r}",
        )
    status: Literal["fresh", "stale"] = "fresh" if age <= sla else "stale"
    return FreshnessStatus(
        source, status, age, sla, last_success, f"{age:.1f} minutes old; SLA is {sla} minutes"
    )


def all_source_freshness(repo: Any, *, now: datetime | None = None) -> list[FreshnessStatus]:
    statuses: list[FreshnessStatus] = []
    for source_name in SOURCE_FRESHNESS_SLA_MINUTES:
        source = repo.get_source_by_name(source_name)
        watermark = repo.get_watermark(source.id) if source is not None else None
        statuses.append(source_freshness(source_name, watermark, now=now))
    return statuses


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
