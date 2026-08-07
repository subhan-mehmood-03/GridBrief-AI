from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi.testclient import TestClient

from gridbrief.adapters.ercot import ERCOTAdapter
from gridbrief.ai import _causal_claim_supported, understand_question
from gridbrief.analytics import ObservationEvidence, get_dart_window_summary
from gridbrief.normalization import build_raw_item, normalize_item
from gridbrief.web import create_app


def _observation(identifier: int, hour: int, minute: int, value: float) -> ObservationEvidence:
    return ObservationEvidence(
        observation_id=identifier,
        timestamp=datetime(2026, 8, 7, hour, minute, tzinfo=UTC),
        value=value,
        unit="$/MWh",
        source_id=1,
    )


def test_question_router_resolves_dart_location_window_and_persona() -> None:
    plan = understand_question(
        "What was the 48h average DART at Houston Hub?",
        role="market_analyst",
    )
    assert plan.subject == "dart_spread"
    assert plan.location == "HB_HOUSTON"
    assert plan.window_hours == 48
    assert plan.statistic == "average"
    assert plan.persona == "market_analyst"


def test_question_router_resolves_followup_location() -> None:
    plan = understand_question(
        "What about DA there?",
        role="market_analyst",
        history=[{"role": "user", "content": "What is RT at North Hub?"}],
    )
    assert plan.subject == "spp_da"
    assert plan.location == "HB_NORTH"


def test_dart_pairs_delivery_hour_and_preserves_da_minus_rt(monkeypatch) -> None:
    def fake_load(*, metric, **kwargs):
        del kwargs
        if metric == "spp_rt":
            return (_observation(1, 10, 0, 20.0), _observation(2, 10, 15, 40.0))
        return (_observation(3, 10, 0, 25.0),)

    monkeypatch.setattr("gridbrief.analytics._load_observations", fake_load)
    summary = get_dart_window_summary(
        settlement_point="HB_NORTH",
        hours=24,
        end=datetime(2026, 8, 7, 11, tzinfo=UTC),
    )
    assert summary.latest_real_time_price == 30.0
    assert summary.latest_day_ahead_price == 25.0
    assert summary.latest_dart == -5.0
    assert summary.as_dict()["evidence"]["formula"] == "day_ahead_price - real_time_price"


def test_rt_and_da_normalize_to_collision_safe_metrics() -> None:
    rows = []
    for market in ("spp_rt", "spp_da"):
        item = build_raw_item(
            source="ercot",
            source_ref=f"{market}:HB_NORTH:2026-08-07T10:00:00+00:00",
            published_at="2026-08-07T10:00:00+00:00",
            kind="timeseries",
            payload={"dataset": market, "Location": "HB_NORTH", "SPP": 25.0},
            url="https://ercot.com",
        )
        rows.extend(normalize_item(item).timeseries)
    assert [row.metric for row in rows] == ["spp_rt", "spp_da"]
    assert len({(row.metric, row.settlement_point, row.ts) for row in rows}) == 2


def test_ercot_adapter_requests_both_price_markets(monkeypatch) -> None:
    calls = []

    class Frame:
        def __init__(self, rows=None):
            self.rows = rows or []

        def to_dict(self, orient):
            assert orient == "records"
            return self.rows

    class Ercot:
        def get_load(self, date):
            del date
            return Frame()

        def get_fuel_mix(self, date):
            del date
            return Frame()

        def get_spp(self, date, *, market, **kwargs):
            del kwargs
            calls.append((date, market))
            return Frame(
                [
                    {
                        "Interval Start": "2026-08-07T10:00:00+00:00",
                        "Location": "HB_NORTH",
                        "SPP": 25.0,
                    }
                ]
            )

    markets = SimpleNamespace(REAL_TIME_15_MIN="RT", DAY_AHEAD_HOURLY="DA")
    monkeypatch.setitem(
        __import__("sys").modules, "gridstatus", SimpleNamespace(Ercot=Ercot, Markets=markets)
    )
    items = ERCOTAdapter().fetch(
        datetime(2026, 8, 7, 0, tzinfo=UTC),
        datetime(2026, 8, 7, 23, tzinfo=UTC),
    )
    assert [call[1] for call in calls] == ["RT", "DA"]
    assert {item.payload["dataset"] for item in items} == {"spp_rt", "spp_da"}
    assert len({item.source_ref for item in items}) == 2


def test_ask_invalid_payload_always_returns_string() -> None:
    response = TestClient(create_app()).post("/api/ask", json={"question": {"bad": "shape"}})
    body = response.json()
    assert response.status_code == 200
    assert isinstance(body["answer"], str)
    assert "[object Object]" not in body["answer"]
    assert "validation" not in body["answer"].lower()


def test_causal_verifier_rejects_unstated_cause() -> None:
    evidence = {7: "Prices increased during the latest interval."}
    assert not _causal_claim_supported("Prices increased because of outages. [cite:7]", evidence)
    assert _causal_claim_supported("Prices increased during the interval. [cite:7]", evidence)
