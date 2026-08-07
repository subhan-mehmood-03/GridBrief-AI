"""Bounded and citable chart-series services for GridBrief."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from gridbrief.analytics import ALLOWED_WINDOW_HOURS
from gridbrief.config import get_settings
from gridbrief.db import get_session_factory
from gridbrief.models import Source, Timeseries

DEFAULT_MAX_POINTS = 2_500
ABSOLUTE_MAX_POINTS = 5_000


@dataclass(frozen=True, slots=True)
class ChartPoint:
    """One exact observation displayed in a chart."""

    observation_id: int
    timestamp: datetime
    value: float
    source_id: int
    source_name: str

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-safe chart point."""

        return {
            "observation_id": self.observation_id,
            "timestamp": self.timestamp.isoformat(),
            "value": self.value,
            "source_id": self.source_id,
            "source": self.source_name,
        }


@dataclass(frozen=True, slots=True)
class ChartSeries:
    """A bounded chart-ready time series with provenance."""

    iso: str
    metric: str
    settlement_point: str
    window_hours: int
    requested_from: datetime
    requested_to: datetime
    data_from: datetime
    data_to: datetime
    unit: str
    point_count: int
    truncated: bool
    points: tuple[ChartPoint, ...]
    source_ids: tuple[int, ...]
    source_names: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        """Return the chart-series API contract."""

        return {
            "iso": self.iso,
            "metric": self.metric,
            "settlement_point": self.settlement_point,
            "window_hours": self.window_hours,
            "from": self.requested_from.isoformat(),
            "to": self.requested_to.isoformat(),
            "data_from": self.data_from.isoformat(),
            "data_to": self.data_to.isoformat(),
            "unit": self.unit,
            "point_count": self.point_count,
            "truncated": self.truncated,
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
        }


def _normalize_required_text(
    value: str,
    *,
    field_name: str,
) -> str:
    """Normalize and validate a required text value."""

    normalized = value.strip()

    if not normalized:
        raise ValueError(f"{field_name} cannot be empty")

    return normalized


def _normalize_window_end(
    value: datetime | None,
) -> datetime:
    """Return a timezone-aware UTC endpoint."""

    if value is None:
        return datetime.now(UTC)

    if value.tzinfo is None:
        raise ValueError("window end must be timezone-aware")

    return value.astimezone(UTC)


def _validate_window_hours(hours: int) -> None:
    """Validate the product's supported chart windows."""

    if hours not in ALLOWED_WINDOW_HOURS:
        allowed = ", ".join(str(value) for value in sorted(ALLOWED_WINDOW_HOURS))
        raise ValueError(f"hours must be one of: {allowed}")


def _validate_max_points(max_points: int) -> None:
    """Keep chart responses within a safe bound."""

    if max_points <= 0:
        raise ValueError("max_points must be greater than zero")

    if max_points > ABSOLUTE_MAX_POINTS:
        raise ValueError(f"max_points cannot be greater than {ABSOLUTE_MAX_POINTS}")


def get_chart_series(
    *,
    metric: str,
    settlement_point: str,
    hours: int,
    end: datetime | None = None,
    max_points: int = DEFAULT_MAX_POINTS,
) -> ChartSeries:
    """Return exact chronological observations for a chart."""

    normalized_metric = _normalize_required_text(
        metric,
        field_name="metric",
    )
    normalized_location = _normalize_required_text(
        settlement_point,
        field_name="settlement_point",
    )
    _validate_window_hours(hours)
    _validate_max_points(max_points)

    settings = get_settings()
    window_end = _normalize_window_end(end)
    window_start = window_end - timedelta(hours=hours)

    statement = (
        select(
            Timeseries,
            Source.name,
        )
        .join(
            Source,
            Source.id == Timeseries.source_id,
        )
        .where(
            Timeseries.iso == settings.iso,
            Timeseries.metric == normalized_metric,
            Timeseries.settlement_point == normalized_location,
            Timeseries.ts >= window_start,
            Timeseries.ts <= window_end,
        )
        .order_by(
            Timeseries.ts.desc(),
            Timeseries.id.desc(),
        )
        .limit(max_points + 1)
    )

    session_factory = get_session_factory()

    with session_factory() as session:
        rows = session.execute(statement).all()

    if not rows:
        raise LookupError(
            f"No chart observations were found for {normalized_metric} at {normalized_location}."
        )

    truncated = len(rows) > max_points
    selected_rows = list(reversed(rows[:max_points]))

    units = {row.unit for row, _ in selected_rows}

    if len(units) != 1:
        raise ValueError("The chart observations contain mixed units.")

    points = tuple(
        ChartPoint(
            observation_id=row.id,
            timestamp=row.ts.astimezone(UTC),
            value=float(row.value),
            source_id=row.source_id,
            source_name=source_name,
        )
        for row, source_name in selected_rows
    )

    sources = sorted(
        {
            (
                point.source_id,
                point.source_name,
            )
            for point in points
        }
    )

    return ChartSeries(
        iso=settings.iso,
        metric=normalized_metric,
        settlement_point=normalized_location,
        window_hours=hours,
        requested_from=window_start,
        requested_to=window_end,
        data_from=points[0].timestamp,
        data_to=points[-1].timestamp,
        unit=next(iter(units)),
        point_count=len(points),
        truncated=truncated,
        points=points,
        source_ids=tuple(source_id for source_id, _ in sources),
        source_names=tuple(source_name for _, source_name in sources),
    )


def chart_spec(
    *,
    metric: str,
    settlement_point: str,
    hours: int,
    end: datetime | None = None,
    max_points: int = DEFAULT_MAX_POINTS,
) -> dict[str, object]:
    """Return a line-chart contract without drawing pixels."""

    series = get_chart_series(
        metric=metric,
        settlement_point=settlement_point,
        hours=hours,
        end=end,
        max_points=max_points,
    )

    return {
        "kind": "line",
        "x_field": "timestamp",
        "y_field": "value",
        "series": series.as_dict(),
    }
