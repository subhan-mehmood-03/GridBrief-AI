"""LangGraph skeleton for PRD §7/§8 edition generation."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import date, datetime
from html import escape
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from gridbrief.db import session_scope
from gridbrief.llm import LLMError, get_llm_client
from gridbrief.repository import Repository
from gridbrief.retrieval import SearchFilters, vector_search

EditionMode = Literal["scheduled_daily", "on_demand", "breaking"]
Role = Literal["general", "market_analyst", "grid_operations"]
TOPICS = ("grid", "prices", "renewables", "weather", "policy", "market")
VALID_MODES = ("scheduled_daily", "on_demand", "breaking")
VALID_ROLES = ("general", "market_analyst", "grid_operations")


class GraphState(TypedDict, total=False):
    iso: str
    role: Role
    edition_mode: EditionMode
    window_start: datetime
    window_end: datetime
    cycle_date: date
    raw_items: list[dict[str, Any]]
    classified: list[dict[str, Any]]
    plan: dict[str, Any]
    retrieved: dict[str, Any]
    drafts: dict[str, str]
    verification: dict[str, Any]
    revision_count: int
    edition: dict[str, Any]
    edition_id: int


KEYWORDS = {
    "grid": ("load", "reserve", "outage", "constraint", "eea", "advisory", "frequency"),
    "prices": ("price", "lmp", "spp", "dam", "rtm", "scarcity", "ordc"),
    "renewables": ("wind", "solar", "renewable", "fuel mix", "storage", "battery"),
    "weather": ("weather", "heat", "cold", "storm", "warning", "temperature", "nws"),
    "policy": ("puct", "policy", "regulatory", "filing", "rule", "legislation"),
    "market": ("market", "ancillary", "spread", "clearing", "trading"),
}

SECTION_TOPIC = {
    "Executive Summary": None,
    "Grid Conditions": "grid",
    "Prices": "prices",
    "Renewables & Fuel Mix": "renewables",
    "Weather Impact": "weather",
    "Policy / Regulatory": "policy",
    "Notable Events": None,
    "Outlook": "market",
}

ROLE_ORDER = {
    "general": list(SECTION_TOPIC),
    "market_analyst": [
        "Prices",
        "Executive Summary",
        "Notable Events",
        "Renewables & Fuel Mix",
        "Grid Conditions",
        "Outlook",
    ],
    "grid_operations": [
        "Grid Conditions",
        "Executive Summary",
        "Notable Events",
        "Weather Impact",
        "Renewables & Fuel Mix",
        "Outlook",
    ],
}

ROLE_WEIGHTS = {
    "general": defaultdict(lambda: 1.0),
    "market_analyst": defaultdict(lambda: 0.7, prices=1.5, market=1.4, renewables=1.1),
    "grid_operations": defaultdict(lambda: 0.7, grid=1.5, weather=1.25, renewables=1.1),
}


def _item_text(item: dict[str, Any]) -> str:
    return " ".join(str(item.get(key, "")) for key in ("title", "text", "payload", "source_ref"))


def _heuristic_classification(item: dict[str, Any]) -> tuple[str, float]:
    text = _item_text(item).lower()
    scores = {topic: sum(word in text for word in words) for topic, words in KEYWORDS.items()}
    topic = max(TOPICS, key=lambda name: (scores[name], -TOPICS.index(name)))
    hits = scores[topic]
    urgency = sum(
        token in text
        for token in ("emergency", "eea", "warning", "record", "scarcity", "unplanned")
    )
    importance = min(1.0, 0.35 + hits * 0.12 + urgency * 0.18)
    return topic, round(importance, 2)


def triage(state: GraphState) -> dict[str, Any]:
    """Classify every candidate into the PRD taxonomy and score importance."""
    items = state.get("raw_items", [])
    if not items:
        return {"classified": []}
    classified: list[dict[str, Any]] = []
    client = get_llm_client()
    if client.available:
        try:
            response = client.complete_json(
                system=(
                    "Classify GridBrief items. Return a JSON object with key items. "
                    "Each item contains index, topic from the supplied taxonomy, "
                    "and importance from 0 through 1. Taxonomy: grid, prices, "
                    "renewables, weather, policy, market."
                ),
                user=json.dumps(
                    [{"index": i, "text": _item_text(item)[:3000]} for i, item in enumerate(items)]
                ),
            )
            by_index = {int(row["index"]): row for row in response.get("items", [])}
        except (LLMError, TypeError, ValueError, KeyError):
            by_index = {}
    else:
        by_index = {}
    for index, item in enumerate(items):
        fallback_topic, fallback_importance = _heuristic_classification(item)
        result = by_index.get(index, {})
        topic = result.get("topic", fallback_topic)
        if topic not in TOPICS:
            topic = fallback_topic
        try:
            importance = max(0.0, min(1.0, float(result.get("importance", fallback_importance))))
        except (TypeError, ValueError):
            importance = fallback_importance
        classified.append({**item, "topic": topic, "importance": round(importance, 3)})
    return {"classified": classified}


def planner(state: GraphState) -> dict[str, Any]:
    """Dynamically select items and sections using persona-weighted relevance."""
    role = state["role"]
    mode = state["edition_mode"]
    ranked = sorted(
        state.get("classified", []),
        key=lambda item: float(item["importance"]) * ROLE_WEIGHTS[role][item["topic"]],
        reverse=True,
    )
    if mode == "breaking":
        triggering = [
            item
            for item in ranked
            if item["importance"] >= 0.9 and item["topic"] in {"grid", "prices"}
        ]
        selected = (triggering or ranked)[:5]
        sections = ["Breaking Update"]
    else:
        selected = ranked[:12]
        present = {item["topic"] for item in selected if item["importance"] >= 0.45}
        sections = []
        for section in ROLE_ORDER[role]:
            topic = SECTION_TOPIC[section]
            include = section == "Executive Summary" or topic in present
            if section == "Notable Events":
                include = any(item["importance"] >= 0.75 for item in selected)
            if section == "Outlook":
                include = bool(selected)
            if include:
                sections.append(section)
    plan = {
        "iso": state["iso"],
        "role": role,
        "edition_mode": mode,
        "window_start": state["window_start"].isoformat(),
        "window_end": state["window_end"].isoformat(),
        "sections": [
            {
                "title": section,
                "topic": SECTION_TOPIC.get(section),
                "item_refs": [
                    item.get("source_ref", item.get("id", index))
                    for index, item in enumerate(selected)
                    if section in {"Executive Summary", "Notable Events", "Breaking Update"}
                    or item["topic"] == SECTION_TOPIC.get(section)
                ][:5],
            }
            for section in sections
        ],
        "selected_item_count": len(selected),
    }
    return {"plan": plan}


def retriever(state: GraphState) -> dict[str, Any]:
    """Build section grounding bundles through Person 4 search and Person 2 repository."""
    bundles: dict[str, Any] = {}
    metrics_by_topic = {
        "grid": ("system_load",),
        "prices": ("spp_rt", "spp_da"),
        "renewables": ("fuel_mix_wind", "fuel_mix_solar", "fuel_mix_battery_storage"),
        "market": ("as_price_regup",),
    }
    for section in state["plan"]["sections"]:
        title = section["title"]
        topic = section.get("topic")
        query = f"{state['iso']} {title} developments"
        try:
            chunks = [
                result.as_dict()
                for result in vector_search(
                    query,
                    SearchFilters(
                        iso=state["iso"],
                        topic=topic,
                        published_after=state["window_start"],
                        published_before=state["window_end"],
                    ),
                    k=5,
                )
            ]
        except Exception as exc:
            chunks = []
            search_error = str(exc)
        else:
            search_error = None
        if not chunks:
            try:
                with session_scope() as session:
                    documents = Repository(session).get_recent_documents(
                        iso=state["iso"],
                        start=state["window_start"],
                        end=state["window_end"],
                        topic=topic,
                        limit=5,
                    )
                    chunks = [
                        {
                            "chunk_id": document.chunk_ids[0] if document.chunk_ids else None,
                            "document_id": document.id,
                            "title": document.title,
                            "text": document.text or "",
                            "score": 0.0,
                            "source": None,
                            "topic": document.topic,
                            "published_at": (
                                document.published_at.isoformat()
                                if document.published_at is not None
                                else None
                            ),
                            "url": document.url,
                        }
                        for document in documents
                        if document.text
                    ]
            except Exception as exc:
                fallback_error = str(exc)
            else:
                fallback_error = None
        else:
            fallback_error = None
        timeseries: list[dict[str, Any]] = []
        metrics = metrics_by_topic.get(topic, ())
        if metrics:
            try:
                with session_scope() as session:
                    repository = Repository(session)
                    rows = [
                        row
                        for metric in metrics
                        for row in repository.get_timeseries(
                            metric=metric,
                            settlement_point=None,
                            start=state["window_start"],
                            end=state["window_end"],
                        )
                    ]
                timeseries = [
                    {
                        "observation_id": row.id,
                        "source_id": row.source_id,
                        "metric": row.metric,
                        "settlement_point": row.settlement_point,
                        "ts": row.ts.isoformat(),
                        "value": float(row.value),
                        "unit": row.unit,
                    }
                    for row in rows
                ]
            except Exception as exc:
                repository_error = str(exc)
            else:
                repository_error = None
        else:
            repository_error = None
        bundles[title] = {
            "chunks": chunks,
            "timeseries": timeseries,
            "errors": [
                error for error in (search_error, fallback_error, repository_error) if error
            ],
        }
    return {"retrieved": bundles}


def _evidence_prompt(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "document_id": chunk["document_id"],
            "title": chunk.get("title"),
            "text": chunk["text"],
            "published_at": chunk.get("published_at"),
            "url": chunk.get("url"),
        }
        for chunk in bundle.get("chunks", [])
        if chunk.get("text") and chunk.get("document_id") is not None
    ]


def _structured_evidence(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    return [row for row in bundle.get("timeseries", []) if row.get("observation_id") is not None]


def _fallback_draft(
    title: str, evidence: list[dict[str, Any]], observations: list[dict[str, Any]]
) -> str:
    bullets = []
    for item in evidence[:3]:
        sentence = re.split(r"(?<=[.!?])\s+", " ".join(item["text"].split()))[0]
        if sentence:
            bullets.append(f"- {sentence} [cite:{item['document_id']}]")
    labels = {
        "spp_rt": "Real-time SPP",
        "spp_da": "Day-ahead SPP",
        "system_load": "System load",
        "fuel_mix_wind": "Wind generation",
        "fuel_mix_solar": "Solar generation",
        "fuel_mix_battery_storage": "Battery storage output",
    }
    latest: dict[tuple[str, str | None], dict[str, Any]] = {}
    for row in observations:
        key = (row["metric"], row.get("settlement_point"))
        if key not in latest or row["ts"] > latest[key]["ts"]:
            latest[key] = row
    selected_observations: list[dict[str, Any]] = []
    for metric in dict.fromkeys(row["metric"] for row in latest.values()):
        selected_observations.extend(
            row for row in latest.values() if row["metric"] == metric
        )
    metric_counts: defaultdict[str, int] = defaultdict(int)
    balanced_observations = []
    for row in selected_observations:
        if metric_counts[row["metric"]] < 2:
            balanced_observations.append(row)
            metric_counts[row["metric"]] += 1
    for row in balanced_observations[:6]:
        label = labels.get(row["metric"], row["metric"].replace("_", " ").title())
        location = f" at {row['settlement_point']}" if row.get("settlement_point") else ""
        bullets.append(
            f"- {label}{location} was {row['value']:g} {row['unit']} as of {row['ts']} "
            f"[calc:obs-{row['observation_id']}]"
        )
    return "\n".join(bullets)


def writer(state: GraphState) -> dict[str, Any]:
    """Draft sections from their evidence bundles and no other factual input."""
    drafts = dict(state.get("drafts", {}))
    verification = state.get("verification", {})
    retrying = bool(verification and any(not row["passed"] for row in verification.values()))
    client = get_llm_client()
    for section in state["plan"]["sections"]:
        title = section["title"]
        if retrying and verification.get(title, {}).get("passed", False):
            continue
        bundle = state["retrieved"].get(title, {})
        evidence = _evidence_prompt(bundle)
        observations = _structured_evidence(bundle)
        unsupported = verification.get(title, {}).get("unsupported_claims", [])
        if client.available and (evidence or observations):
            try:
                response = client.complete_json(
                    system=(
                        "You write one GridBrief section using ONLY the supplied evidence. "
                        "Every factual sentence must end with [cite:doc_id] for document evidence "
                        "or [calc:obs-observation_id] for a structured observation, and appear on "
                        "its own line. Citation IDs must occur in the supplied evidence. "
                        "Never use outside knowledge, infer a "
                        "number, or invent a citation. Return JSON with a single 'draft' string."
                    ),
                    user=json.dumps(
                        {
                            "section": title,
                            "persona": state["role"],
                            "evidence": evidence,
                            "structured_observations": observations,
                            "claims_to_remove_or_repair": unsupported,
                        }
                    ),
                )
                draft = str(response.get("draft", "")).strip()
            except (LLMError, AttributeError, TypeError):
                draft = _fallback_draft(title, evidence, observations)
        else:
            draft = _fallback_draft(title, evidence, observations)
        drafts[title] = draft
    return {
        "drafts": drafts,
        "revision_count": state.get("revision_count", 0) + (1 if retrying else 0),
    }


CITATION_RE = re.compile(r"\[cite:(\d+)\]")
CALC_RE = re.compile(r"\[calc:obs-(\d+)\]")


def _claims(draft: str) -> list[str]:
    return [
        line.strip().removeprefix("- ")
        for line in draft.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _claim_supported(
    claim: str, evidence_by_document: dict[int, str], evidence_by_observation: dict[int, str]
) -> bool:
    cited = [int(value) for value in CITATION_RE.findall(claim)]
    observations = [int(value) for value in CALC_RE.findall(claim)]
    if not cited and not observations:
        return False
    if any(document_id not in evidence_by_document for document_id in cited):
        return False
    if any(observation_id not in evidence_by_observation for observation_id in observations):
        return False
    marker_free_claim = CALC_RE.sub("", CITATION_RE.sub("", claim))
    claim_words = {
        word
        for word in re.findall(r"[a-z0-9]+", marker_free_claim.lower())
        if len(word) > 3
    }
    evidence_words = {
        word
        for document_id in cited
        for word in re.findall(r"[a-z0-9]+", evidence_by_document[document_id].lower())
    }
    evidence_words.update(
        word
        for observation_id in observations
        for word in re.findall(r"[a-z0-9]+", evidence_by_observation[observation_id].lower())
    )
    claim_numbers = set(re.findall(r"[-+]?\d[\d,.]*%?", marker_free_claim))
    evidence_numbers = {
        number
        for document_id in cited
        for number in re.findall(r"[-+]?\d[\d,.]*%?", evidence_by_document[document_id])
    }
    evidence_numbers.update(
        number
        for observation_id in observations
        for number in re.findall(r"[-+]?\d[\d,.]*%?", evidence_by_observation[observation_id])
    )
    if not claim_numbers <= evidence_numbers:
        return False
    return not claim_words or len(claim_words & evidence_words) / len(claim_words) >= 0.45


def verifier(state: GraphState) -> dict[str, Any]:
    """Check each claim against only the documents cited in that claim."""
    verification: dict[str, Any] = {}
    for title, draft in state.get("drafts", {}).items():
        evidence = {
            int(chunk["document_id"]): chunk["text"]
            for chunk in state["retrieved"].get(title, {}).get("chunks", [])
            if chunk.get("document_id") is not None and chunk.get("text")
        }
        observation_evidence = {
            int(row["observation_id"]): json.dumps(row, sort_keys=True)
            for row in state["retrieved"].get(title, {}).get("timeseries", [])
            if row.get("observation_id") is not None
        }
        claims = _claims(draft)
        unsupported = [
            claim
            for claim in claims
            if not _claim_supported(claim, evidence, observation_evidence)
        ]
        client = get_llm_client()
        if client.available and claims:
            try:
                response = client.complete_json(
                    system=(
                        "Verify claims strictly against only their cited evidence. Return JSON "
                        "with unsupported_claims containing exact claim strings that are not "
                        "fully entailed, cite a missing source, or state an unsupported number."
                    ),
                    user=json.dumps(
                        {
                            "claims": claims,
                            "evidence_by_document": evidence,
                            "evidence_by_observation": observation_evidence,
                        }
                    ),
                )
                model_unsupported = response.get("unsupported_claims", [])
                unsupported.extend(
                    claim
                    for claim in model_unsupported
                    if claim in claims and claim not in unsupported
                )
            except (LLMError, AttributeError, TypeError):
                pass
        verification[title] = {
            "passed": not unsupported,
            "unsupported_claims": unsupported,
            "claim_count": len(claims),
        }
    return {"verification": verification}


def _route_after_verification(state: GraphState) -> str:
    failed = any(not result["passed"] for result in state.get("verification", {}).values())
    if failed and state.get("revision_count", 0) < 2:
        return "writer"
    return "editor"


def _supported_claims(state: GraphState) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []
    for title, draft in state.get("drafts", {}).items():
        unsupported = set(
            state.get("verification", {}).get(title, {}).get("unsupported_claims", [])
        )
        chunk_by_document = {
            int(chunk["document_id"]): chunk.get("chunk_id")
            for chunk in state["retrieved"].get(title, {}).get("chunks", [])
            if chunk.get("document_id") is not None
        }
        for claim in _claims(draft):
            if claim in unsupported:
                continue
            document_ids = [int(value) for value in CITATION_RE.findall(claim)]
            observation_ids = [int(value) for value in CALC_RE.findall(claim)]
            claims.append(
                {
                    "section": title,
                    "text": claim,
                    "document_ids": document_ids,
                    "observation_ids": observation_ids,
                    "chunk_ids": [
                        chunk_by_document[document_id]
                        for document_id in document_ids
                        if chunk_by_document.get(document_id) is not None
                    ],
                }
            )
    return claims


def editor(state: GraphState) -> dict[str, Any]:
    """Drop unsupported claims, assemble sources, and persist edition + claims."""
    claims = _supported_claims(state)
    if not claims:
        raise RuntimeError("generation produced no supported cited claims; edition was not saved")
    sections = []
    for planned in state["plan"]["sections"]:
        title = planned["title"]
        section_claims = [claim["text"] for claim in claims if claim["section"] == title]
        if section_claims:
            sections.append({"title": title, "claims": section_claims})
    document_ids = {doc_id for claim in claims for doc_id in claim["document_ids"]}
    sources_by_id = {
        int(chunk["document_id"]): {
            "document_id": int(chunk["document_id"]),
            "title": chunk.get("title"),
            "publisher": chunk.get("source"),
            "published_at": chunk.get("published_at"),
            "url": chunk.get("url"),
        }
        for bundle in state["retrieved"].values()
        for chunk in bundle.get("chunks", [])
        if chunk.get("document_id") in document_ids
    }
    observation_ids = {obs_id for claim in claims for obs_id in claim["observation_ids"]}
    observations_by_id = {
        int(row["observation_id"]): row
        for bundle in state["retrieved"].values()
        for row in bundle.get("timeseries", [])
        if row.get("observation_id") in observation_ids
    }
    markdown_parts = [f"# {state['role'].replace('_', ' ').title()} GridBrief"]
    for section in sections:
        markdown_parts.extend([f"\n## {section['title']}", *section["claims"]])
    if sources_by_id:
        markdown_parts.append("\n## Sources")
        for source in sources_by_id.values():
            label = source["title"] or f"Document {source['document_id']}"
            url = source["url"] or ""
            markdown_parts.append(f"- [{source['document_id']}] [{label}]({url})")
    if observations_by_id:
        markdown_parts.append("\n## Structured Data Sources")
        for observation in observations_by_id.values():
            location = observation.get("settlement_point") or state["iso"]
            markdown_parts.append(
                f"- [calc:obs-{observation['observation_id']}] "
                f"{observation['metric']} at {location}, {observation['ts']}"
            )
    markdown = "\n".join(markdown_parts)
    edition_json = {
        "iso": state["iso"],
        "role": state["role"],
        "edition_mode": state["edition_mode"],
        "data_as_of": state["window_end"].isoformat(),
        "sections": sections,
        "sources": list(sources_by_id.values()),
        "structured_sources": list(observations_by_id.values()),
        "revision_count": state.get("revision_count", 0),
    }
    html = "<article><pre>" + escape(markdown) + "</pre></article>"
    with session_scope() as session:
        repository = Repository(session)
        saved = repository.save_edition(
            iso=state["iso"],
            role=state["role"],
            cycle_date=state["cycle_date"],
            generated_at=state["window_end"],
            status="published",
            markdown=markdown,
            html=html,
            json=edition_json,
        )
        for claim in claims:
            repository.add_edition_claim(
                edition_id=saved.id,
                claim_text=claim["text"],
                cited_chunk_ids=claim["chunk_ids"],
                verified=True,
                groundedness=1.0,
            )
        edition_id = saved.id
    return {"edition": edition_json, "edition_id": edition_id}


def build_generation_graph():
    graph = StateGraph(GraphState)
    graph.add_node("triage", triage)
    graph.add_node("planner", planner)
    graph.add_node("retriever", retriever)
    graph.add_node("writer", writer)
    graph.add_node("verifier", verifier)
    graph.add_node("editor", editor)
    graph.add_edge(START, "triage")
    graph.add_edge("triage", "planner")
    graph.add_edge("planner", "retriever")
    graph.add_edge("retriever", "writer")
    graph.add_edge("writer", "verifier")
    graph.add_conditional_edges(
        "verifier",
        _route_after_verification,
        {"writer": "writer", "editor": "editor"},
    )
    graph.add_edge("editor", END)
    return graph.compile()
