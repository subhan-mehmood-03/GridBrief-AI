"""Deterministic edition quality metrics and PRD release gates."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from sqlalchemy import select

from gridbrief.models import Edition, EditionClaim

CITATION_RE = re.compile(r"\[(?:cite|calc):[^\]]+\]")


@dataclass(frozen=True)
class QualityReport:
    edition_id: int
    claim_count: int
    citation_coverage: float
    groundedness: float
    hallucination_rate: float
    source_attribution_precision: float
    passed: bool

    def as_dict(self) -> dict[str, int | float | bool]:
        return asdict(self)


def evaluate_edition(session, edition_id: int) -> QualityReport:
    edition = session.get(Edition, edition_id)
    if edition is None:
        raise ValueError(f"edition {edition_id} was not found")
    claims = list(
        session.scalars(select(EditionClaim).where(EditionClaim.edition_id == edition_id))
    )
    count = len(claims)
    if not count:
        return QualityReport(edition_id, 0, 0.0, 0.0, 1.0, 0.0, False)
    cited = sum(
        bool(claim.cited_chunk_ids or CITATION_RE.search(claim.claim_text)) for claim in claims
    )
    verified = sum(bool(claim.verified) for claim in claims)
    grounded_values = [claim.groundedness for claim in claims if claim.groundedness is not None]
    groundedness = (
        sum(grounded_values) / len(grounded_values) if grounded_values else verified / count
    )
    coverage = cited / count
    hallucination = 1 - verified / count
    attribution = (
        sum(
            bool(claim.verified and (claim.cited_chunk_ids or CITATION_RE.search(claim.claim_text)))
            for claim in claims
        )
        / cited
        if cited
        else 0.0
    )
    return QualityReport(
        edition_id,
        count,
        coverage,
        groundedness,
        hallucination,
        attribution,
        coverage >= 0.90 and groundedness >= 0.90 and hallucination <= 0.05,
    )
