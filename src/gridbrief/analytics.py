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


@dataclass(frozen=True, slots=True)
class DartWindowSummary:
    """Hour-aligned deterministic DA-minus-RT price-spread evidence."""

    settlement_point: str
    window_hours: int
    window_start: datetime
    window_end: datetime
    paired_hours: int
    coverage: float
    latest_hour: datetime
    latest_real_time_price: float
    latest_day_ahead_price: float
    latest_dart: float
    average_dart: float
    unit: str
    real_time_observation_ids: tuple[int, ...]
    day_ahead_observation_ids: tuple[int, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "settlement_point": self.settlement_point,
            "window_hours": self.window_hours,
            "from": self.window_start.isoformat(),
            "to": self.window_end.isoformat(),
            "paired_hours": self.paired_hours,
            "coverage": self.coverage,
            "latest_hour": self.latest_hour.isoformat(),
            "latest_real_time_price": self.latest_real_time_price,
            "latest_day_ahead_price": self.latest_day_ahead_price,
            "latest_dart": self.latest_dart,
            "average_dart": self.average_dart,
            "unit": self.unit,
            "evidence": {
                "formula": "day_ahead_price - real_time_price",
                "real_time_metric": "spp_rt",
                "day_ahead_metric": "spp_da",
                "real_time_observation_ids": list(self.real_time_observation_ids),
                "day_ahead_observation_ids": list(self.day_ahead_observation_ids),
            },
        }


def get_dart_window_summary(
    *,
    settlement_point: str,
    hours: int,
    end: datetime | None = None,
) -> DartWindowSummary:
    """Pair DA hourly prices with the mean RT price in the same delivery hour."""
    _validate_window_hours(hours)
    location = _normalize_required_text(settlement_point, field_name="settlement_point")
    window_end = _normalize_window_end(end)
    window_start = window_end - timedelta(hours=hours)
    settings = get_settings()
    real_time = _load_observations(
        metric="spp_rt",
        settlement_point=location,
        window_start=window_start,
        window_end=window_end,
        iso=settings.iso,
    )
    day_ahead = _load_observations(
        metric="spp_da",
        settlement_point=location,
        window_start=window_start,
        window_end=window_end,
        iso=settings.iso,
    )
    if not real_time:
        raise LookupError(f"No real-time SPP observations were found for {location}.")
    if not day_ahead:
        raise LookupError(f"No day-ahead SPP observations were found for {location}.")
    units = {row.unit for row in (*real_time, *day_ahead)}
    if len(units) != 1:
        raise ValueError("The paired RT and DA observations contain mixed units.")

    def delivery_hour(timestamp: datetime) -> datetime:
        return timestamp.astimezone(UTC).replace(minute=0, second=0, microsecond=0)

    rt_by_hour: dict[datetime, list[ObservationEvidence]] = {}
    for observation in real_time:
        rt_by_hour.setdefault(delivery_hour(observation.timestamp), []).append(observation)
    da_by_hour: dict[datetime, ObservationEvidence] = {}
    for observation in day_ahead:
        hour = delivery_hour(observation.timestamp)
        current = da_by_hour.get(hour)
        if current is None or observation.observation_id > current.observation_id:
            da_by_hour[hour] = observation
    common_hours = sorted(set(rt_by_hour) & set(da_by_hour))
    if not common_hours:
        raise LookupError(f"No hour-aligned RT and DA SPP observations were found for {location}.")
    hourly = []
    for hour in common_hours:
        rt_rows = rt_by_hour[hour]
        rt_average = float(fmean(row.value for row in rt_rows))
        da_row = da_by_hour[hour]
        hourly.append((hour, rt_average, da_row.value, da_row.value - rt_average))
    latest_hour, latest_rt, latest_da, latest_dart = hourly[-1]
    return DartWindowSummary(
        settlement_point=location,
        window_hours=hours,
        window_start=window_start,
        window_end=window_end,
        paired_hours=len(hourly),
        coverage=min(1.0, len(hourly) / hours),
        latest_hour=latest_hour,
        latest_real_time_price=latest_rt,
        latest_day_ahead_price=latest_da,
        latest_dart=latest_dart,
        average_dart=float(fmean(row[3] for row in hourly)),
        unit=next(iter(units)),
        real_time_observation_ids=tuple(
            row.observation_id for hour in common_hours for row in rt_by_hour[hour]
        ),
        day_ahead_observation_ids=tuple(da_by_hour[hour].observation_id for hour in common_hours),
    )
