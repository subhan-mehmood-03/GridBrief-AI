from datetime import UTC, datetime, timedelta

import pytest

from gridbrief.graph import planner
from gridbrief.pipeline import generate_edition


def _state(role: str) -> dict:
    end = datetime(2026, 8, 7, 18, tzinfo=UTC)
    return {
        "iso": "ERCOT",
        "role": role,
        "edition_mode": "scheduled_daily",
        "window_start": end - timedelta(hours=24),
        "window_end": end,
        "classified": [
            {
                "source_ref": "weather-1",
                "topic": "weather",
                "importance": 0.9,
                "title": "Severe weather alert",
            }
        ],
    }


def test_grid_operations_plan_keeps_operational_sections_without_grid_documents() -> None:
    sections = [row["title"] for row in planner(_state("grid_operations"))["plan"]["sections"]]

    assert sections[0] == "Grid Conditions"
    assert "Weather Impact" in sections


def test_market_plan_keeps_price_section_for_structured_evidence() -> None:
    sections = [row["title"] for row in planner(_state("market_analyst"))["plan"]["sections"]]

    assert sections[0] == "Prices"


def test_generation_initializes_langchain_core_globals() -> None:
    from langchain_core.globals import (
        get_debug,
        get_llm_cache,
        get_verbose,
        set_debug,
        set_verbose,
    )

    set_debug(True)
    set_verbose(True)

    with pytest.raises(ValueError, match="role must be one of"):
        generate_edition(role="invalid")  # type: ignore[arg-type]

    assert get_debug() is False
    assert get_verbose() is False
    assert get_llm_cache() is None
