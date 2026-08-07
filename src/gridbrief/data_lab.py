"""Validated Data Lab queries and matching CSV export."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import StringIO

from sqlalchemy import func, select

from gridbrief.config import get_settings
from gridbrief.db import get_session_factory
from gridbrief.models import Source, Timeseries

MAX_RANGE_HOURS = 168
DEFAULT_MAX_POINTS = 5_000
ABSOLUTE_MAX_POINTS = 5_000


@dataclass(frozen=True, slots=True)
class DataLabPoint:
    """One exact time-series observation."""

    observation_id: int
    timestamp: datetime
    value: float
    source_id: int
    source_name: str

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-safe observation."""

        return {
            "observation_id": self.observation_id,
            "timestamp": self.timestamp.isoformat(),
            "value": self.value,
            "source_id": self.source_id,
            "source": self.source_name,
        }


@dataclass(frozen=True, slots=True)
class DataLabResult:
    """A bounded Data Lab response with full-window statistics."""

    iso: str
    metric: str
    settlement_point: str
    requested_from: datetime
    requested_to: datetime
    unit: str
    total_point_count: int
    returned_point_count: int
    truncated: bool
    latest: DataLabPoint
    average: float
    minimum: DataLabPoint
    maximum: DataLabPoint
    points: tuple[DataLabPoint, ...]
    source_ids: tuple[int, ...]
    source_names: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        """Return the Data Lab API contract."""

        return {
            "iso": self.iso,
            "metric": self.metric,
            "settlement_point": self.settlement_point,
            "from": self.requested_from.isoformat(),
            "to": self.requested_to.isoformat(),
            "unit": self.unit,
            "total_point_count": self.total_point_count,
            "returned_point_count": self.returned_point_count,
            "truncated": self.truncated,
            "statistics": {
                "latest": self.latest.as_dict(),
                "average": self.average,
                "minimum": self.minimum.as_dict(),
                "maximum": self.maximum.as_dict(),
            },
            "sources": [
                {
                    "source_id": source_id,
                    "name": source_name,
                }
                for source_id, source_name in zip(
                    self.source_ids,
                    self.source_names,
                    strict=True,
                )
            ],
            "points": [point.as_dict() for point in self.points],
            "evidence": {
                "kind": "data_lab_timeseries",
                "observation_ids": [point.observation_id for point in self.points],
                "source_ids": list(self.source_ids),
            },
        }


def _normalize_required_text(
    value: str,
    *,
    field_name: str,
) -> str:
    """Normalize and validate a required text argument."""

    normalized = value.strip()

    if not normalized:
        raise ValueError(f"{field_name} cannot be empty")

    return normalized


def _normalize_timestamp(
    value: datetime,
    *,
    field_name: str,
) -> datetime:
    """Require a timezone-aware timestamp and convert it to UTC."""

    if value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware")

    return value.astimezone(UTC)


def _validate_range(
    *,
    start: datetime,
    end: datetime,
) -> tuple[datetime, datetime]:
    """Validate and normalize a bounded Data Lab range."""

    normalized_start = _normalize_timestamp(
        start,
        field_name="start",
    )
    normalized_end = _normalize_timestamp(
        end,
        field_name="end",
    )

    if normalized_end <= normalized_start:
        raise ValueError("end must be later than start")

    maximum_duration = timedelta(hours=MAX_RANGE_HOURS)

    if normalized_end - normalized_start > maximum_duration:
        raise ValueError(f"Data Lab range cannot exceed {MAX_RANGE_HOURS} hours")

    return normalized_start, normalized_end


def _validate_max_points(max_points: int) -> None:
    """Keep Data Lab responses within a safe point limit."""

    if max_points <= 0:
        raise ValueError("max_points must be greater than zero")

    if max_points > ABSOLUTE_MAX_POINTS:
        raise ValueError(f"max_points cannot be greater than {ABSOLUTE_MAX_POINTS}")


def _to_point(
    row: Timeseries,
    source_name: str,
) -> DataLabPoint:
    """Convert one database row into a Data Lab point."""

    return DataLabPoint(
        observation_id=row.id,
        timestamp=row.ts.astimezone(UTC),
        value=float(row.value),
        source_id=row.source_id,
        source_name=source_name,
    )


def query_data_lab(
    *,
    metric: str,
    settlement_point: str,
    start: datetime,
    end: datetime,
    max_points: int = DEFAULT_MAX_POINTS,
) -> DataLabResult:
    """Return bounded exact points and full-window statistics."""

    normalized_metric = _normalize_required_text(
        metric,
        field_name="metric",
    )
    normalized_location = _normalize_required_text(
        settlement_point,
        field_name="settlement_point",
    )
    normalized_start, normalized_end = _validate_range(
        start=start,
        end=end,
    )
    _validate_max_points(max_points)

    settings = get_settings()

    conditions = (
        Timeseries.iso == settings.iso,
        Timeseries.metric == normalized_metric,
        Timeseries.settlement_point == normalized_location,
        Timeseries.ts >= normalized_start,
        Timeseries.ts <= normalized_end,
    )

    points_statement = (
        select(
            Timeseries,
            Source.name,
        )
        .join(
            Source,
            Source.id == Timeseries.source_id,
        )
        .where(*conditions)
        .order_by(
            Timeseries.ts.desc(),
            Timeseries.id.desc(),
        )
        .limit(max_points + 1)
    )

    aggregate_statement = select(
        func.count(Timeseries.id),
        func.avg(Timeseries.value),
    ).where(*conditions)

    latest_statement = (
        select(
            Timeseries,
            Source.name,
        )
        .join(
            Source,
            Source.id == Timeseries.source_id,
        )
        .where(*conditions)
        .order_by(
            Timeseries.ts.desc(),
            Timeseries.id.desc(),
        )
        .limit(1)
    )

    minimum_statement = (
        select(
            Timeseries,
            Source.name,
        )
        .join(
            Source,
            Source.id == Timeseries.source_id,
        )
        .where(*conditions)
        .order_by(
            Timeseries.value.asc(),
            Timeseries.ts.asc(),
            Timeseries.id.asc(),
        )
        .limit(1)
    )

    maximum_statement = (
        select(
            Timeseries,
            Source.name,
        )
        .join(
            Source,
            Source.id == Timeseries.source_id,
        )
        .where(*conditions)
        .order_by(
            Timeseries.value.desc(),
            Timeseries.ts.asc(),
            Timeseries.id.asc(),
        )
        .limit(1)
    )

    units_statement = select(Timeseries.unit).where(*conditions).distinct()

    sources_statement = (
        select(
            Source.id,
            Source.name,
        )
        .join(
            Timeseries,
            Timeseries.source_id == Source.id,
        )
        .where(*conditions)
        .distinct()
        .order_by(Source.id.asc())
    )

    session_factory = get_session_factory()

    with session_factory() as session:
        rows = session.execute(points_statement).all()

        total_count, average_value = session.execute(aggregate_statement).one()

        latest_row = session.execute(latest_statement).first()

        minimum_row = session.execute(minimum_statement).first()

        maximum_row = session.execute(maximum_statement).first()

        units = tuple(session.scalars(units_statement).all())

        sources = tuple(session.execute(sources_statement).all())

    if (
        not rows
        or latest_row is None
        or minimum_row is None
        or maximum_row is None
        or average_value is None
    ):
        raise LookupError(
            f"No Data Lab observations were found for {normalized_metric} at {normalized_location}."
        )

    if len(units) != 1:
        raise ValueError("The selected observations contain mixed units.")

    truncated = len(rows) > max_points
    selected_rows = list(reversed(rows[:max_points]))

    points = tuple(_to_point(row, source_name) for row, source_name in selected_rows)

    latest = _to_point(
        latest_row[0],
        latest_row[1],
    )
    minimum = _to_point(
        minimum_row[0],
        minimum_row[1],
    )
    maximum = _to_point(
        maximum_row[0],
        maximum_row[1],
    )

    return DataLabResult(
        iso=settings.iso,
        metric=normalized_metric,
        settlement_point=normalized_location,
        requested_from=normalized_start,
        requested_to=normalized_end,
        unit=units[0],
        total_point_count=int(total_count),
        returned_point_count=len(points),
        truncated=truncated,
        latest=latest,
        average=float(average_value),
        minimum=minimum,
        maximum=maximum,
        points=points,
        source_ids=tuple(source_id for source_id, _ in sources),
        source_names=tuple(source_name for _, source_name in sources),
    )


def export_data_lab_csv(
    *,
    metric: str,
    settlement_point: str,
    start: datetime,
    end: datetime,
    max_points: int = DEFAULT_MAX_POINTS,
) -> str:
    """Export the matching bounded Data Lab observations as CSV."""

    result = query_data_lab(
        metric=metric,
        settlement_point=settlement_point,
        start=start,
        end=end,
        max_points=max_points,
    )

    output = StringIO(newline="")
    writer = csv.writer(
        output,
        lineterminator="\n",
    )

    writer.writerow(
        (
            "iso",
            "metric",
            "settlement_point",
            "timestamp",
            "value",
            "unit",
            "observation_id",
            "source_id",
            "source",
        )
    )

    for point in result.points:
        writer.writerow(
            (
                result.iso,
                result.metric,
                result.settlement_point,
                point.timestamp.isoformat(),
                point.value,
                result.unit,
                point.observation_id,
                point.source_id,
                point.source_name,
            )
        )

    return output.getvalue()
