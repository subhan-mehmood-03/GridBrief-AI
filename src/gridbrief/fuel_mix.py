"""Source-prioritized, time-weighted fuel-mix analytics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from gridbrief.analytics import ALLOWED_WINDOW_HOURS
from gridbrief.config import get_settings
from gridbrief.db import get_session_factory
from gridbrief.models import Source, Timeseries

SOURCE_CADENCE_SECONDS = {
    "ercot": 300,
    "eia": 3600,
}

FUEL_METRIC_CANDIDATES: dict[
    str,
    tuple[tuple[str, str], ...],
] = {
    "coal": (
        ("fuel_mix_coal_and_lignite", "ercot"),
        ("fuel_mix_coal", "eia"),
    ),
    "hydro": (
        ("fuel_mix_hydro", "ercot"),
        ("fuel_mix_hydro", "eia"),
    ),
    "natural_gas": (
        ("fuel_mix_natural_gas", "ercot"),
        ("fuel_mix_natural_gas", "eia"),
    ),
    "nuclear": (
        ("fuel_mix_nuclear", "ercot"),
        ("fuel_mix_nuclear", "eia"),
    ),
    "other": (
        ("fuel_mix_other", "ercot"),
        ("fuel_mix_other", "eia"),
    ),
    "solar": (
        ("fuel_mix_solar", "ercot"),
        ("fuel_mix_solar", "eia"),
    ),
    "storage": (
        ("fuel_mix_power_storage", "ercot"),
        ("fuel_mix_battery_storage", "eia"),
    ),
    "wind": (
        ("fuel_mix_wind", "ercot"),
        ("fuel_mix_wind", "eia"),
    ),
}


@dataclass(frozen=True, slots=True)
class _FuelObservation:
    """Internal normalized fuel-mix observation."""

    observation_id: int
    timestamp: datetime
    value: float
    unit: str
    source_id: int
    source_name: str


@dataclass(frozen=True, slots=True)
class FuelMixMetricSummary:
    """Time-weighted summary for one canonical fuel category."""

    fuel: str
    canonical_metric: str
    settlement_point: str
    window_hours: int
    window_start: datetime
    window_end: datetime
    unit: str
    latest_value: float
    latest_timestamp: datetime
    time_weighted_average: float
    covered_hours: float
    coverage_ratio: float
    point_count: int
    source_ids: tuple[int, ...]
    source_names: tuple[str, ...]
    observation_ids: tuple[int, ...]
    source_hours: tuple[tuple[str, float], ...]

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-safe calculation and evidence contract."""

        result: dict[str, object] = {
            "fuel": self.fuel,
            "metric": self.canonical_metric,
            "settlement_point": self.settlement_point,
            "window_hours": self.window_hours,
            "from": self.window_start.isoformat(),
            "to": self.window_end.isoformat(),
            "unit": self.unit,
            "latest": {
                "value": self.latest_value,
                "timestamp": self.latest_timestamp.isoformat(),
            },
            "time_weighted_average": self.time_weighted_average,
            "covered_hours": self.covered_hours,
            "coverage_ratio": self.coverage_ratio,
            "point_count": self.point_count,
            "source_hours": dict(self.source_hours),
            "evidence": {
                "kind": "time_weighted_fuel_mix",
                "source_ids": list(self.source_ids),
                "source_names": list(self.source_names),
                "observation_ids": list(self.observation_ids),
                "source_priority": [
                    source_name for _, source_name in FUEL_METRIC_CANDIDATES[self.fuel]
                ],
            },
        }

        if self.fuel == "storage":
            result["storage_sign"] = {
                "positive": "discharging",
                "negative": "charging",
            }

        return result


def _normalize_window_end(
    value: datetime | None,
) -> datetime:
    """Return a timezone-aware UTC window endpoint."""

    if value is None:
        return datetime.now(UTC)

    if value.tzinfo is None:
        raise ValueError("window end must be timezone-aware")

    return value.astimezone(UTC)


def _validate_request(
    *,
    fuel: str,
    hours: int,
    settlement_point: str,
) -> tuple[str, str]:
    """Validate and normalize public function arguments."""

    normalized_fuel = fuel.strip().lower()
    normalized_location = settlement_point.strip()

    if normalized_fuel not in FUEL_METRIC_CANDIDATES:
        supported = ", ".join(sorted(FUEL_METRIC_CANDIDATES))
        raise ValueError(f"fuel must be one of: {supported}")

    if hours not in ALLOWED_WINDOW_HOURS:
        supported_windows = ", ".join(str(value) for value in sorted(ALLOWED_WINDOW_HOURS))
        raise ValueError(f"hours must be one of: {supported_windows}")

    if not normalized_location:
        raise ValueError("settlement_point cannot be empty")

    return normalized_fuel, normalized_location


def _load_observations(
    *,
    fuel: str,
    settlement_point: str,
    window_start: datetime,
    window_end: datetime,
) -> tuple[_FuelObservation, ...]:
    """Load candidate source rows for one fuel category."""

    settings = get_settings()
    candidates = FUEL_METRIC_CANDIDATES[fuel]
    metric_names = {metric for metric, _ in candidates}
    candidate_pairs = set(candidates)

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
            Timeseries.metric.in_(metric_names),
            Timeseries.settlement_point == settlement_point,
            Timeseries.ts >= window_start,
            Timeseries.ts <= window_end,
        )
        .order_by(
            Timeseries.ts.asc(),
            Timeseries.id.asc(),
        )
    )

    session_factory = get_session_factory()

    with session_factory() as session:
        rows = session.execute(statement).all()

    return tuple(
        _FuelObservation(
            observation_id=row.id,
            timestamp=row.ts.astimezone(UTC),
            value=float(row.value),
            unit=row.unit,
            source_id=row.source_id,
            source_name=source_name,
        )
        for row, source_name in rows
        if (
            row.metric,
            source_name,
        )
        in candidate_pairs
    )


def _apply_source_priority(
    *,
    fuel: str,
    observations: tuple[_FuelObservation, ...],
) -> tuple[_FuelObservation, ...]:
    """Use higher-priority sources after their coverage begins."""

    selected: list[_FuelObservation] = []

    for _, source_name in reversed(FUEL_METRIC_CANDIDATES[fuel]):
        candidate_rows = [
            observation for observation in observations if observation.source_name == source_name
        ]

        if not candidate_rows:
            continue

        candidate_start = min(observation.timestamp for observation in candidate_rows)

        selected = [
            observation for observation in selected if observation.timestamp < candidate_start
        ]
        selected.extend(candidate_rows)
        selected.sort(
            key=lambda observation: (
                observation.timestamp,
                observation.observation_id,
            )
        )

    return tuple(selected)


def _calculate_weighted_values(
    *,
    observations: tuple[_FuelObservation, ...],
    window_start: datetime,
    window_end: datetime,
) -> tuple[
    float,
    float,
    tuple[tuple[str, float], ...],
]:
    """Calculate an interval-weighted average and coverage."""

    weighted_total = 0.0
    covered_seconds = 0.0
    source_seconds: dict[str, float] = {}

    for index, observation in enumerate(observations):
        expected_seconds = SOURCE_CADENCE_SECONDS[observation.source_name]
        maximum_supported_gap = expected_seconds * 1.5

        if index + 1 < len(observations):
            next_timestamp = observations[index + 1].timestamp
            observed_gap = (next_timestamp - observation.timestamp).total_seconds()

            if observed_gap <= maximum_supported_gap:
                segment_end = next_timestamp
            else:
                segment_end = observation.timestamp + timedelta(seconds=expected_seconds)
        else:
            segment_end = observation.timestamp + timedelta(seconds=expected_seconds)

        segment_start = max(
            observation.timestamp,
            window_start,
        )
        segment_end = min(
            segment_end,
            window_end,
        )

        duration_seconds = max(
            0.0,
            (segment_end - segment_start).total_seconds(),
        )

        if duration_seconds == 0:
            continue

        weighted_total += observation.value * duration_seconds
        covered_seconds += duration_seconds
        source_seconds[observation.source_name] = (
            source_seconds.get(
                observation.source_name,
                0.0,
            )
            + duration_seconds
        )

    if covered_seconds == 0:
        raise LookupError("The selected observations provide no time-weighted coverage.")

    source_hours = tuple(
        (
            source_name,
            seconds / 3600,
        )
        for source_name, seconds in sorted(source_seconds.items())
    )

    return (
        weighted_total / covered_seconds,
        covered_seconds,
        source_hours,
    )


def get_fuel_mix_metric_summary(
    *,
    fuel: str,
    hours: int,
    settlement_point: str = "ERCOT",
    end: datetime | None = None,
) -> FuelMixMetricSummary:
    """Return source-prioritized, time-weighted fuel analytics."""

    normalized_fuel, normalized_location = _validate_request(
        fuel=fuel,
        hours=hours,
        settlement_point=settlement_point,
    )

    window_end = _normalize_window_end(end)
    window_start = window_end - timedelta(hours=hours)

    observations = _load_observations(
        fuel=normalized_fuel,
        settlement_point=normalized_location,
        window_start=window_start,
        window_end=window_end,
    )
    selected = _apply_source_priority(
        fuel=normalized_fuel,
        observations=observations,
    )

    if not selected:
        raise LookupError(
            f"No fuel-mix observations were found for {normalized_fuel} during the selected window."
        )

    units = {observation.unit for observation in selected}

    if len(units) != 1:
        raise ValueError("The selected fuel-mix observations contain mixed units.")

    (
        weighted_average,
        covered_seconds,
        source_hours,
    ) = _calculate_weighted_values(
        observations=selected,
        window_start=window_start,
        window_end=window_end,
    )

    latest = max(
        selected,
        key=lambda observation: (
            observation.timestamp,
            observation.observation_id,
        ),
    )
    requested_seconds = hours * 3600

    return FuelMixMetricSummary(
        fuel=normalized_fuel,
        canonical_metric=(FUEL_METRIC_CANDIDATES[normalized_fuel][0][0]),
        settlement_point=normalized_location,
        window_hours=hours,
        window_start=window_start,
        window_end=window_end,
        unit=next(iter(units)),
        latest_value=latest.value,
        latest_timestamp=latest.timestamp,
        time_weighted_average=weighted_average,
        covered_hours=covered_seconds / 3600,
        coverage_ratio=min(
            covered_seconds / requested_seconds,
            1.0,
        ),
        point_count=len(selected),
        source_ids=tuple(sorted({observation.source_id for observation in selected})),
        source_names=tuple(sorted({observation.source_name for observation in selected})),
        observation_ids=tuple(observation.observation_id for observation in selected),
        source_hours=source_hours,
    )
