"""Public entry points for the three GridBrief generation modes."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from gridbrief.db import session_scope
from gridbrief.graph import (
    VALID_MODES,
    VALID_ROLES,
    EditionMode,
    GraphState,
    Role,
    build_generation_graph,
    planner,
    triage,
)
from gridbrief.repository import Repository


def _refresh_on_demand() -> list[str]:
    """Refresh each source independently; partial success is useful and expected."""
    from gridbrief.config import get_settings
    from gridbrief.ingestion import SUPPORTED_SOURCES, ingest_source

    settings = get_settings()
    if not settings.automatic_refresh:
        return []
    refreshed = []
    priority = ("nws", "rss", "eia", "ercot")
    for source in (name for name in priority if name in SUPPORTED_SOURCES):
        try:
            ingest_source(source, hours=24, settings=settings)
        except Exception:
            continue
        refreshed.append(source)
    return refreshed


def _load_candidate_documents(*, iso: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    with session_scope() as session:
        documents = Repository(session).get_recent_documents(
            iso=iso,
            start=start,
            end=end,
            limit=50,
        )
        return [
            {
                "id": document.id,
                "source_ref": document.source_ref,
                "title": document.title,
                "text": document.text,
                "url": document.url,
                "published_at": (
                    document.published_at.isoformat() if document.published_at is not None else None
                ),
                "topic": document.topic,
                "importance": document.importance,
            }
            for document in documents
        ]


def _load_structured_candidates(
    *, iso: str, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    metric_topics = {
        "spp_rt": ("prices", 0.9),
        "spp_da": ("prices", 0.9),
        "system_load": ("grid", 0.8),
        "fuel_mix_wind": ("renewables", 0.7),
        "fuel_mix_solar": ("renewables", 0.7),
        "fuel_mix_battery_storage": ("renewables", 0.65),
    }
    candidates = []
    with session_scope() as session:
        repository = Repository(session)
        for metric, (topic, importance) in metric_topics.items():
            rows = repository.get_timeseries(
                metric=metric,
                settlement_point=None,
                start=start,
                end=end,
            )
            latest_by_location = {}
            for row in rows:
                if row.iso == iso:
                    current = latest_by_location.get(row.settlement_point)
                    if current is None or (row.ts, row.id) > (current.ts, current.id):
                        latest_by_location[row.settlement_point] = row
            for row in latest_by_location.values():
                candidates.append(
                    {
                        "id": f"observation-{row.id}",
                        "source_ref": f"timeseries:{metric}:{row.settlement_point}:{row.id}",
                        "title": f"{metric} at {row.settlement_point}",
                        "text": (
                            f"{metric} {row.value} {row.unit} at {row.settlement_point} "
                            f"as of {row.ts.isoformat()}"
                        ),
                        "topic": topic,
                        "importance": importance,
                    }
                )
    return candidates


def _candidates_are_fresh(candidates: list[dict[str, Any]], now: datetime) -> bool:
    from gridbrief.config import get_settings

    threshold = now - timedelta(minutes=get_settings().freshness_minutes)
    return any(
        item.get("published_at") and datetime.fromisoformat(str(item["published_at"])) >= threshold
        for item in candidates
    )


def generate_edition(
    *,
    role: Role = "general",
    edition_mode: EditionMode = "on_demand",
    raw_items: list[dict[str, Any]] | None = None,
    iso: str = "ERCOT",
    now: datetime | None = None,
    window_start: datetime | None = None,
) -> GraphState:
    """Run one graph for scheduled, on-demand, or breaking generation."""
    if role not in VALID_ROLES:
        raise ValueError(f"role must be one of: {', '.join(VALID_ROLES)}")
    if edition_mode not in VALID_MODES:
        raise ValueError(f"edition_mode must be one of: {', '.join(VALID_MODES)}")
    window_end = now or datetime.now(UTC)
    if window_end.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    start = window_start or window_end - timedelta(hours=24)
    if start.tzinfo is None:
        raise ValueError("window_start must be timezone-aware")
    if start >= window_end:
        raise ValueError("window_start must be before now")
    candidates = raw_items
    if candidates is None:
        candidates = [
            *_load_candidate_documents(iso=iso, start=start, end=window_end),
            *_load_structured_candidates(iso=iso, start=start, end=window_end),
        ]
    if edition_mode == "on_demand" and not _candidates_are_fresh(candidates, window_end):
        _refresh_on_demand()
        candidates = [
            *_load_candidate_documents(iso=iso, start=start, end=window_end),
            *_load_structured_candidates(iso=iso, start=start, end=window_end),
        ]
    initial: GraphState = {
        "iso": iso,
        "role": role,
        "edition_mode": edition_mode,
        "window_start": start,
        "window_end": window_end,
        "cycle_date": window_end.date(),
        "raw_items": candidates,
        "classified": [],
        "plan": {},
        "retrieved": {},
        "drafts": {},
        "verification": {},
        "revision_count": 0,
        "edition": {},
    }
    return build_generation_graph().invoke(initial)


def _breaking_severity(item: dict[str, Any], topic: str, importance: float) -> str | None:
    from gridbrief.config import get_settings

    text = " ".join(str(item.get(key, "")) for key in ("title", "text", "payload")).lower()
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else item
    settings = get_settings()

    def numeric_value(*keys: str) -> float | None:
        for key in keys:
            try:
                if payload.get(key) is not None:
                    return float(payload[key])
            except (AttributeError, TypeError, ValueError):
                continue
        return None

    for level in ("eea 3", "eea3", "eea 2", "eea2", "eea 1", "eea1", "watch"):
        if level in text:
            return level.replace(" ", "").upper()
    if topic == "prices" and any(term in text for term in ("scarcity", "ordc", "price spike")):
        return "SCARCITY"
    price = numeric_value("rtm_spp", "spp", "price", "value")
    if topic == "prices" and price is not None and price >= settings.breaking_price_threshold:
        return "PRICE_THRESHOLD"
    if "unplanned" in text and "outage" in text:
        outage_mw = numeric_value("outage_mw", "capacity_mw", "mw")
        if outage_mw is None or outage_mw >= settings.breaking_outage_threshold_mw:
            return "MAJOR_OUTAGE"
    if topic == "weather" and any(term in text for term in ("warning", "extreme", "storm")):
        return "EXTREME_WEATHER"
    if topic in {"grid", "prices"} and importance >= 0.9:
        return "HIGH"
    return None


def process_breaking_item(
    item: dict[str, Any],
    *,
    role: Role = "general",
    iso: str = "ERCOT",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Apply §8.1 predicate, fingerprint idempotency, and one-hour topic cooldown."""
    fired_at = now or datetime.now(UTC)
    classified = triage({"raw_items": [item]}).get("classified", [])
    if not classified:
        return {"fired": False, "reason": "not_actionable"}
    candidate = classified[0]
    topic = candidate["topic"]
    severity = _breaking_severity(candidate, topic, float(candidate["importance"]))
    if severity is None:
        return {"fired": False, "reason": "below_threshold", "topic": topic}
    source_ref = str(candidate.get("source_ref") or candidate.get("id") or "unknown")
    fingerprint = hashlib.sha256(
        json.dumps(
            {"source_ref": source_ref, "topic": topic, "severity": severity},
            sort_keys=True,
        ).encode()
    ).hexdigest()
    with session_scope() as session:
        repository = Repository(session)
        cooldown = repository.get_active_breaking_cooldown(topic=topic, fired_at=fired_at)
        duplicate, created = repository.fire_breaking_trigger(
            source_ref=source_ref,
            topic=topic,
            severity=severity,
            fingerprint=fingerprint,
            fired_at=fired_at,
            cooldown_until=fired_at + timedelta(hours=1),
        )
        if not created:
            return {
                "fired": False,
                "reason": "duplicate",
                "trigger_id": duplicate.id,
                "topic": topic,
            }
        if cooldown is not None:
            session.delete(duplicate)
            return {
                "fired": False,
                "reason": "cooldown",
                "trigger_id": cooldown.id,
                "topic": topic,
            }
        trigger_id = duplicate.id
    try:
        state = generate_edition(
            role=role,
            edition_mode="breaking",
            raw_items=[candidate],
            iso=iso,
            now=fired_at,
        )
    except Exception:
        with session_scope() as session:
            Repository(session).delete_breaking_trigger(trigger_id)
        raise
    return {
        "fired": True,
        "reason": "published",
        "trigger_id": trigger_id,
        "topic": topic,
        "edition_id": state["edition_id"],
    }


def plan_edition(**kwargs: Any) -> dict[str, Any]:
    """Convenience entry point used by CLI/API layers during this milestone."""
    # A plan intentionally stops before retrieval so the CLI can preview it
    # even when the database or embedding model is unavailable.
    role = kwargs.get("role", "general")
    edition_mode = kwargs.get("edition_mode", "on_demand")
    now = kwargs.get("now") or datetime.now(UTC)
    start = kwargs.get("window_start") or now - timedelta(hours=24)
    state: GraphState = {
        "iso": kwargs.get("iso", "ERCOT"),
        "role": role,
        "edition_mode": edition_mode,
        "window_start": start,
        "window_end": now,
        "cycle_date": now.date(),
        "raw_items": kwargs.get("raw_items") or [],
    }
    state.update(triage(state))
    state.update(planner(state))
    return state["plan"]
