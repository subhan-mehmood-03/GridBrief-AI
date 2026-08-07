"""Deterministic window analytics for GridBrief time-series data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from statistics import fmean

from gridbrief.config import get_settings
from gridbrief.db import get_session_factory
from gridbrief.repository import Repository

ALLOWED_WINDOW_HOURS = frozenset({24, 48, 168})


@dataclass(frozen=True, slots=True)
class ObservationEvidence:
    """One source observation used in a deterministic calculation."""

    observation_id: int
    timestamp: datetime
    value: float
    unit: str
    source_id: int

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-safe evidence record."""

        return {
            "observation_id": self.observation_id,
            "timestamp": self.timestamp.isoformat(),
            "value": self.value,
            "unit": self.unit,
            "source_id": self.source_id,
        }


@dataclass(frozen=True, slots=True)
class MetricWindowSummary:
    """Deterministic statistics for one metric and location."""

    iso: str
    metric: str
    settlement_point: str
    window_hours: int
    window_start: datetime
    window_end: datetime
    point_count: int
    unit: str
    latest: ObservationEvidence
    average: float
    minimum: ObservationEvidence
    maximum: ObservationEvidence
    source_ids: tuple[int, ...]
    observation_ids: tuple[int, ...]

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-safe analytics and evidence contract."""

        return {
            "iso": self.iso,
            "metric": self.metric,
            "settlement_point": self.settlement_point,
            "window_hours": self.window_hours,
            "from": self.window_start.isoformat(),
            "to": self.window_end.isoformat(),
            "point_count": self.point_count,
            "unit": self.unit,
            "latest": self.latest.as_dict(),
            "average": self.average,
            "minimum": self.minimum.as_dict(),
            "maximum": self.maximum.as_dict(),
            "evidence": {
                "kind": "timeseries_calculation",
                "source_ids": list(self.source_ids),
                "observation_ids": list(self.observation_ids),
            },
        }


def _validate_window_hours(hours: int) -> None:
    """Require one of the product's supported chart windows."""

    if hours not in ALLOWED_WINDOW_HOURS:
        allowed = ", ".join(str(value) for value in sorted(ALLOWED_WINDOW_HOURS))
        raise ValueError(f"hours must be one of: {allowed}")


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


def _normalize_window_end(
    value: datetime | None,
) -> datetime:
    """Return a timezone-aware UTC window end."""

    if value is None:
        return datetime.now(UTC)

    if value.tzinfo is None:
        raise ValueError("window end must be timezone-aware")

    return value.astimezone(UTC)


def _load_observations(
    *,
    metric: str,
    settlement_point: str,
    window_start: datetime,
    window_end: datetime,
    iso: str,
) -> tuple[ObservationEvidence, ...]:
    """Load observations through the shared repository interface."""

    session_factory = get_session_factory()

    with session_factory() as session:
        repository = Repository(session)

        rows = repository.get_timeseries(
            metric=metric,
            settlement_point=settlement_point,
            start=window_start,
            end=window_end,
        )

        observations = tuple(
            ObservationEvidence(
                observation_id=row.id,
                timestamp=row.ts.astimezone(UTC),
                value=float(row.value),
                unit=row.unit,
                source_id=row.source_id,
            )
            for row in rows
            if row.iso == iso
        )

    return observations


@dataclass(frozen=True, slots=True)
class PriceSpreadSummary:
    """Explicit RT-DA and DART results for one settlement point."""

    settlement_point: str
    real_time_price: float
    day_ahead_price: float
    unit: str
    rt_minus_da: float
    dart: float
    real_time_observation_id: int | None
    day_ahead_observation_id: int | None

    def as_dict(self) -> dict[str, object]:
        """Return spread values with explicit calculation evidence."""

        return {
            "settlement_point": self.settlement_point,
            "unit": self.unit,
            "real_time_price": self.real_time_price,
            "day_ahead_price": self.day_ahead_price,
            "rt_minus_da": self.rt_minus_da,
            "dart": self.dart,
            "evidence": {
                "rt_minus_da_formula": ("real_time_price - day_ahead_price"),
                "dart_formula": ("day_ahead_price - real_time_price"),
                "real_time_observation_id": (self.real_time_observation_id),
                "day_ahead_observation_id": (self.day_ahead_observation_id),
            },
        }


def get_metric_window_summary(
    *,
    metric: str,
    settlement_point: str,
    hours: int,
    end: datetime | None = None,
) -> MetricWindowSummary:
    """Calculate latest, average, minimum, and maximum values."""

    _validate_window_hours(hours)

    normalized_metric = _normalize_required_text(
        metric,
        field_name="metric",
    )
    normalized_location = _normalize_required_text(
        settlement_point,
        field_name="settlement_point",
    )

    settings = get_settings()
    window_end = _normalize_window_end(end)
    window_start = window_end - timedelta(hours=hours)

    observations = _load_observations(
        metric=normalized_metric,
        settlement_point=normalized_location,
        window_start=window_start,
        window_end=window_end,
        iso=settings.iso,
    )

    if not observations:
        raise LookupError(
            "No observations were found for "
            f"{normalized_metric} at {normalized_location} "
            f"during the selected {hours}-hour window."
        )

    units = {observation.unit for observation in observations}

    if len(units) != 1:
        raise ValueError("The selected observations contain mixed units.")

    latest = max(
        observations,
        key=lambda observation: (
            observation.timestamp,
            observation.observation_id,
        ),
    )
    minimum = min(
        observations,
        key=lambda observation: (
            observation.value,
            observation.timestamp,
        ),
    )
    maximum = max(
        observations,
        key=lambda observation: (
            observation.value,
            observation.timestamp,
        ),
    )

    return MetricWindowSummary(
        iso=settings.iso,
        metric=normalized_metric,
        settlement_point=normalized_location,
        window_hours=hours,
        window_start=window_start,
        window_end=window_end,
        point_count=len(observations),
        unit=next(iter(units)),
        latest=latest,
        average=float(fmean(observation.value for observation in observations)),
        minimum=minimum,
        maximum=maximum,
        source_ids=tuple(sorted({observation.source_id for observation in observations})),
        observation_ids=tuple(observation.observation_id for observation in observations),
    )


def calculate_price_spreads(
    *,
    settlement_point: str,
    real_time_price: float,
    day_ahead_price: float,
    unit: str = "$/MWh",
    real_time_observation_id: int | None = None,
    day_ahead_observation_id: int | None = None,
) -> PriceSpreadSummary:
    """Calculate both spread conventions from explicit RT and DA inputs."""

    normalized_location = _normalize_required_text(
        settlement_point,
        field_name="settlement_point",
    )
    normalized_unit = _normalize_required_text(
        unit,
        field_name="unit",
    )

    has_real_time_id = real_time_observation_id is not None
    has_day_ahead_id = day_ahead_observation_id is not None

    if has_real_time_id != has_day_ahead_id:
        raise ValueError("real-time and day-ahead observation IDs must be provided together")

    normalized_real_time = float(real_time_price)
    normalized_day_ahead = float(day_ahead_price)

    return PriceSpreadSummary(
        settlement_point=normalized_location,
        real_time_price=normalized_real_time,
        day_ahead_price=normalized_day_ahead,
        unit=normalized_unit,
        rt_minus_da=(normalized_real_time - normalized_day_ahead),
        dart=(normalized_day_ahead - normalized_real_time),
        real_time_observation_id=real_time_observation_id,
        day_ahead_observation_id=day_ahead_observation_id,
    )
