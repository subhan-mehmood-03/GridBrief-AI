from __future__ import annotations

from datetime import UTC, datetime, timedelta
from math import isclose

import pytest

from gridbrief.analytics import calculate_price_spreads
from gridbrief.chart_series import get_chart_series
from gridbrief.data_lab import query_data_lab
from gridbrief.fuel_mix import (
    _apply_source_priority,
    _calculate_weighted_values,
    _FuelObservation,
)
from gridbrief.retrieval import _normalize_query


def _utc(
    year: int,
    month: int,
    day: int,
    hour: int = 0,
    minute: int = 0,
) -> datetime:
    return datetime(
        year,
        month,
        day,
        hour,
        minute,
        tzinfo=UTC,
    )


def test_price_spread_signs_and_evidence() -> None:
    summary = calculate_price_spreads(
        settlement_point="HB_NORTH",
        real_time_price=31.75,
        day_ahead_price=24.50,
        real_time_observation_id=101,
        day_ahead_observation_id=202,
    )

    assert summary.rt_minus_da == 7.25
    assert summary.dart == -7.25
    assert summary.rt_minus_da == -summary.dart

    evidence = summary.as_dict()["evidence"]

    assert evidence["rt_minus_da_formula"] == ("real_time_price - day_ahead_price")
    assert evidence["dart_formula"] == ("day_ahead_price - real_time_price")
    assert evidence["real_time_observation_id"] == 101
    assert evidence["day_ahead_observation_id"] == 202


def test_price_spread_rejects_incomplete_evidence() -> None:
    with pytest.raises(
        ValueError,
        match="must be provided together",
    ):
        calculate_price_spreads(
            settlement_point="HB_NORTH",
            real_time_price=31.75,
            day_ahead_price=24.50,
            real_time_observation_id=101,
        )


def test_fuel_source_priority_uses_eia_before_ercot() -> None:
    observations = (
        _FuelObservation(
            observation_id=1,
            timestamp=_utc(2026, 8, 1, 0),
            value=10.0,
            unit="MW",
            source_id=1,
            source_name="eia",
        ),
        _FuelObservation(
            observation_id=2,
            timestamp=_utc(2026, 8, 1, 1),
            value=20.0,
            unit="MW",
            source_id=1,
            source_name="eia",
        ),
        _FuelObservation(
            observation_id=3,
            timestamp=_utc(2026, 8, 1, 2),
            value=30.0,
            unit="MW",
            source_id=1,
            source_name="eia",
        ),
        _FuelObservation(
            observation_id=4,
            timestamp=_utc(2026, 8, 1, 1, 30),
            value=25.0,
            unit="MW",
            source_id=6,
            source_name="ercot",
        ),
        _FuelObservation(
            observation_id=5,
            timestamp=_utc(2026, 8, 1, 1, 35),
            value=27.0,
            unit="MW",
            source_id=6,
            source_name="ercot",
        ),
    )

    selected = _apply_source_priority(
        fuel="solar",
        observations=observations,
    )

    assert tuple(observation.observation_id for observation in selected) == (1, 2, 4, 5)

    assert tuple(observation.source_name for observation in selected) == (
        "eia",
        "eia",
        "ercot",
        "ercot",
    )


def test_time_weighted_average_uses_interval_duration() -> None:
    window_start = _utc(2026, 8, 1, 0)
    window_end = _utc(2026, 8, 1, 2)

    observations = (
        _FuelObservation(
            observation_id=1,
            timestamp=window_start,
            value=10.0,
            unit="MW",
            source_id=1,
            source_name="eia",
        ),
        _FuelObservation(
            observation_id=2,
            timestamp=_utc(2026, 8, 1, 1),
            value=20.0,
            unit="MW",
            source_id=1,
            source_name="eia",
        ),
    )

    average, covered_seconds, source_hours = _calculate_weighted_values(
        observations=observations,
        window_start=window_start,
        window_end=window_end,
    )

    assert isclose(
        average,
        15.0,
        rel_tol=1e-12,
    )
    assert covered_seconds == 7_200
    assert source_hours == (("eia", 2.0),)


def test_retrieval_query_normalization() -> None:
    assert _normalize_query("  heat    advisory   near El Paso  ") == "heat advisory near El Paso"

    with pytest.raises(
        ValueError,
        match="query cannot be empty",
    ):
        _normalize_query("   ")


def test_chart_service_rejects_unsupported_window() -> None:
    with pytest.raises(
        ValueError,
        match="hours must be one of",
    ):
        get_chart_series(
            metric="system_load",
            settlement_point="ERCOT",
            hours=12,
        )


def test_data_lab_rejects_range_over_seven_days() -> None:
    end = _utc(2026, 8, 6, 12)
    start = end - timedelta(hours=169)

    with pytest.raises(
        ValueError,
        match="cannot exceed 168 hours",
    ):
        query_data_lab(
            metric="system_load",
            settlement_point="ERCOT",
            start=start,
            end=end,
        )
