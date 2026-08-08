"""Persisted quality evaluation and release-gate reporting."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from gridbrief.db import session_scope
from gridbrief.models import Edition
from gridbrief.quality import evaluate_edition
from gridbrief.repository import Repository


def evaluate_latest_editions() -> dict[str, object]:
    with session_scope() as session:
        editions = list(session.scalars(select(Edition).order_by(Edition.generated_at.desc())))
        latest = {}
        for edition in editions:
            latest.setdefault(edition.role, edition)
        reports = []
        repository = Repository(session)
        for edition in latest.values():
            report = evaluate_edition(session, edition.id)
            reports.append({"role": edition.role, **report.as_dict()})
            for metric in (
                "citation_coverage",
                "groundedness",
                "hallucination_rate",
                "source_attribution_precision",
            ):
                repository.add_eval_run(
                    edition_id=edition.id,
                    metric=metric,
                    value=float(getattr(report, metric)),
                    detail_json={"passed": report.passed},
                    created_at=datetime.now(UTC),
                )
    return {
        "passed": bool(reports) and all(report["passed"] for report in reports),
        "evaluated_at": datetime.now(UTC).isoformat(),
        "editions": reports,
    }
