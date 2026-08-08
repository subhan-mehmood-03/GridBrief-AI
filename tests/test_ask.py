from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi.testclient import TestClient

from gridbrief.adapters.ercot import ERCOTAdapter
from gridbrief.ai import (
    _causal_claim_supported,
    _direct_answer,
    _narrative_answer,
    _narrative_evidence,
    ask_gridbrief,
    understand_question,
)
from gridbrief.analytics import ObservationEvidence, get_dart_window_summary
from gridbrief.llm import LLMError
from gridbrief.normalization import build_raw_item, normalize_item
from gridbrief.web import _RATE_BUCKETS, create_app


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


def test_dam_timeline_questions_do_not_route_to_price() -> None:
    questions = (
        "time deadline for ercot DA",
        "When does the ERCOT DAM close?",
        "What is the day-ahead submission cutoff?",
        "When are ERCOT DAM results posted?",
    )
    for question in questions:
        plan = understand_question(question, role="market_analyst")
        assert plan.subject == "ercot_dam_timeline"
        result = _direct_answer(plan)
        assert "10:00 a.m." in result["answer"]
        assert "1:30 p.m." in result["answer"]
        assert "latest day-ahead" not in result["answer"].lower()
        assert len(result["sources"]) == 2


def test_rtm_timeline_questions_do_not_route_to_price() -> None:
    questions = (
        "time deadline for RT ercot",
        "When does the ERCOT real-time market close?",
        "What is the RTM submission cutoff?",
        "What time is real time scheduled?",
        "What is the timing for ERCOT RT?",
    )
    for question in questions:
        plan = understand_question(question, role="market_analyst")
        assert plan.subject == "ercot_rtm_timeline"
        result = _direct_answer(plan)
        assert "does not have one daily submission deadline" in result["answer"]
        assert "five minutes" in result["answer"]
        assert "15-minute" in result["answer"]
        assert "latest real-time" not in result["answer"].lower()
        assert len(result["sources"]) == 1


def test_question_router_resolves_two_hub_comparison() -> None:
    plan = understand_question(
        "Compare real-time prices at North and South hubs",
        role="market_analyst",
    )
    assert plan.subject == "spp_rt"
    assert plan.location == "HB_NORTH"
    assert plan.comparison_location == "HB_SOUTH"


def test_question_router_covers_required_operational_vocabulary() -> None:
    cases = {
        "How much has reserve changed over 48h?": (
            "available_capacity_reserve",
            "SYSTEM",
            48,
            "change",
        ),
        "What are forced outages?": ("outages_unplanned", "SYSTEM", 24, "latest"),
        "Are batteries charging or discharging?": ("storage_net_output", "SYSTEM", 24, "latest"),
        "What is grid frequency?": ("grid_frequency", "SYSTEM", 24, "latest"),
        "What is tomorrow's load forecast?": ("load_forecast", "SYSTEM", 24, "latest"),
        "What is Houston humidity?": (
            "weather_relative_humidity_forecast",
            "COAST",
            24,
            "latest",
        ),
        "What is the seven day average wind output?": ("wind_gen", "SYSTEM", 168, "average"),
        "What is demand right now?": ("system_load", "ERCOT", 24, "latest"),
        "What is driving the fuel mix?": ("fuel_mix_summary", "ERCOT", 24, "latest"),
        "Summarize current reliability risks.": (
            "reliability_summary",
            "ERCOT",
            24,
            "latest",
        ),
    }
    for question, expected in cases.items():
        plan = understand_question(question, role="grid_operations")
        assert (plan.subject, plan.location, plan.window_hours, plan.statistic) == expected


def test_composite_answers_use_structured_evidence(monkeypatch) -> None:
    values = {
        "fuel_mix_natural_gas": 40_000.0,
        "fuel_mix_wind": 20_000.0,
        "fuel_mix_nuclear": 5_000.0,
        "fuel_mix_solar": 10_000.0,
        "available_capacity_reserve": 8_000.0,
        "outages_unplanned": 2_000.0,
        "outages_total": 3_000.0,
        "grid_frequency": 60.0,
    }

    def fake_summary(*, metric, **kwargs):
        del kwargs
        if metric not in values:
            raise LookupError
        return SimpleNamespace(
            as_dict=lambda: {
                "latest": {
                    "observation_id": list(values).index(metric) + 1,
                    "timestamp": datetime.now(UTC).isoformat(),
                    "value": values[metric],
                    "unit": "Hz" if metric == "grid_frequency" else "MW",
                    "source_id": 6,
                }
            }
        )

    monkeypatch.setattr("gridbrief.ai.get_metric_window_summary", fake_summary)
    fuel = _direct_answer(
        understand_question("What is driving the fuel mix?", role="market_analyst")
    )
    reliability = _direct_answer(
        understand_question("Summarize current reliability risks.", role="grid_operations")
    )
    assert "natural gas" in fuel["answer"].lower()
    assert "[calc:" in fuel["answer"]
    assert fuel["sources"]
    assert "available reserve" in reliability["answer"].lower()
    assert reliability["sources"]


def test_narrative_fallback_is_clean_cited_and_verified(monkeypatch) -> None:
    evidence = [
        {
            "document_id": 7,
            "title": "Severe Thunderstorm Warning for North Texas",
            "text": (
                "GRIDBRIEF_ALERT_METADATA Severity: Severe END_GRIDBRIEF_ALERT_METADATA "
                "Severe Thunderstorm Warning for North Texas."
            ),
            "source": "nws",
            "published_at": "2026-08-07T20:00:00+00:00",
            "url": "https://api.weather.gov/alerts/7",
        }
    ]
    monkeypatch.setattr("gridbrief.ai._narrative_evidence", lambda *args, **kwargs: evidence)
    monkeypatch.setattr(
        "gridbrief.ai.get_llm_client",
        lambda: SimpleNamespace(available=False),
    )
    plan = understand_question("What weather risks affect reliability?", role="general")
    result = _narrative_answer("What weather risks affect reliability?", plan)
    assert result["answer"] == "Severe Thunderstorm Warning for North Texas [cite:7]"
    assert "GRIDBRIEF_ALERT_METADATA" not in result["answer"]
    assert result["unsupported_removed"] == 0
    assert result["model"] == "deterministic"


def test_remote_model_failure_falls_back_to_verified_evidence(monkeypatch) -> None:
    evidence = [
        {
            "document_id": 9,
            "title": "Flood Warning for Corpus Christi",
            "text": "A Flood Warning is active for Corpus Christi.",
            "source": "nws",
            "published_at": "2026-08-08T01:00:00+00:00",
            "url": "https://api.weather.gov/alerts/9",
        }
    ]
    client = SimpleNamespace(
        available=True,
        complete_json=lambda **kwargs: (_ for _ in ()).throw(LLMError("provider failed")),
    )
    monkeypatch.setattr("gridbrief.ai._narrative_evidence", lambda *args, **kwargs: evidence)
    monkeypatch.setattr("gridbrief.ai.get_llm_client", lambda: client)
    plan = understand_question("What current weather risks exist?", role="general")
    result = _narrative_answer("What current weather risks exist?", plan)
    assert result["answer"] == "Flood Warning for Corpus Christi [cite:9]"
    assert result["model"] == "deterministic"


def test_empty_vector_results_use_recent_document_fallback(monkeypatch) -> None:
    document = SimpleNamespace(
        id=17,
        chunk_ids=[],
        title="Current Texas weather alert",
        text="A current weather alert is active in Texas.",
        published_at=datetime.now(UTC),
        url="https://api.weather.gov/alerts/17",
    )
    repository = SimpleNamespace(get_recent_documents=lambda **kwargs: [document])

    class Context:
        def __enter__(self):
            return object()

        def __exit__(self, *args):
            return None

    context = Context()
    monkeypatch.setattr("gridbrief.ai.vector_search", lambda *args, **kwargs: [])
    monkeypatch.setattr("gridbrief.ai.session_scope", lambda: context)
    monkeypatch.setattr("gridbrief.ai.Repository", lambda session: repository)
    plan = understand_question("What current weather risks exist?", role="general")
    evidence = _narrative_evidence("What current weather risks exist?", plan)
    assert [item["document_id"] for item in evidence] == [17]


def test_missing_evidence_returns_specific_safe_low_confidence_answer(monkeypatch) -> None:
    monkeypatch.setattr("gridbrief.ai._narrative_evidence", lambda *args, **kwargs: [])
    result = ask_gridbrief("What is moon output in ERCOT?")
    assert isinstance(result["answer"], str)
    assert result["confidence"]["level"] == "low"
    assert result["sources"] == {}
    assert "[object Object]" not in result["answer"]


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
    _RATE_BUCKETS.clear()
    response = TestClient(create_app()).post("/api/ask", json={"question": {"bad": "shape"}})
    body = response.json()
    assert response.status_code == 200
    assert isinstance(body["answer"], str)
    assert "[object Object]" not in body["answer"]
    assert "validation" not in body["answer"].lower()


def test_ask_rate_limit_always_returns_readable_answer_contract(monkeypatch) -> None:
    _RATE_BUCKETS.clear()
    monkeypatch.setattr(
        "gridbrief.web.ask_gridbrief",
        lambda *args, **kwargs: {
            "answer": "Grounded answer.",
            "sources": {},
            "confidence": {"level": "high", "score": 0.9, "reason": "test"},
        },
    )
    client = TestClient(create_app())
    responses = [client.post("/api/ask", json={"question": "Current load?"}) for _ in range(21)]
    body = responses[-1].json()
    assert responses[-1].status_code == 429
    assert isinstance(body["answer"], str)
    assert "too many" in body["answer"].lower()
    _RATE_BUCKETS.clear()


def test_causal_verifier_rejects_unstated_cause() -> None:
    evidence = {7: "Prices increased during the latest interval."}
    assert not _causal_claim_supported("Prices increased because of outages. [cite:7]", evidence)
    assert _causal_claim_supported("Prices increased during the interval. [cite:7]", evidence)
