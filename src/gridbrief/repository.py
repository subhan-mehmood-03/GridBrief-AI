"""Transactional repository for GridBrief AI (PRD §6, §5.1).

Every write here is an idempotent upsert keyed on the unique constraints
defined in migrations/*.sql, so running ingestion twice never creates
duplicate rows (PRD §5.1: "de-dup on raw_hash/source_ref; upsert
time-series on (iso, metric, settlement_point, ts) and documents on
source_ref").

Downstream teammates should only touch the database through this class:
Person 3 (ingestion) writes through it, Person 4/5 (retrieval/AI) read
through it, Person 6 (web) never talks to the database directly.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from .models import (
    BreakingTrigger,
    Chunk,
    Document,
    Edition,
    EditionClaim,
    EvalRun,
    IngestionRun,
    IngestionWatermark,
    RawItem,
    Source,
    Timeseries,
)


class Repository:
    """Wraps a single SQLAlchemy Session. Callers own the transaction
    boundary via db.session_scope(); every method here just adds
    statements to that session.
    """

    def __init__(self, session: Session):
        self.session = session

    def _returning_one(self, stmt):
        """Execute an ON CONFLICT ... RETURNING statement and make sure the
        object we hand back reflects the just-written row. Needed because
        the ORM identity map can otherwise keep serving pre-update
        attribute values for a row that already existed (the DO UPDATE
        branch of an upsert)."""
        obj = self.session.execute(stmt).scalar_one()
        self.session.refresh(obj)
        return obj

    # ---------------------------------------------------------------- sources
    def upsert_source(self, name: str, kind: str, base_url: str | None = None) -> Source:
        stmt = (
            pg_insert(Source)
            .values(name=name, kind=kind, base_url=base_url)
            .on_conflict_do_update(
                index_elements=[Source.name],
                set_={"kind": kind, "base_url": base_url},
            )
            .returning(Source)
        )
        return self._returning_one(stmt)

    def get_source_by_name(self, name: str) -> Source | None:
        return self.session.execute(select(Source).where(Source.name == name)).scalar_one_or_none()

    # ------------------------------------------------------------- raw_items
    def upsert_raw_item(
        self,
        *,
        source_id: int,
        source_ref: str,
        kind: str,
        published_at: datetime | None,
        url: str | None,
        raw_hash: str | None,
        ingested_at: datetime,
    ) -> RawItem:
        stmt = (
            pg_insert(RawItem)
            .values(
                source_id=source_id,
                source_ref=source_ref,
                kind=kind,
                published_at=published_at,
                url=url,
                raw_hash=raw_hash,
                ingested_at=ingested_at,
            )
            .on_conflict_do_update(
                index_elements=[RawItem.source_id, RawItem.source_ref],
                set_={
                    "published_at": published_at,
                    "url": url,
                    "raw_hash": raw_hash,
                },
            )
            .returning(RawItem)
        )
        return self._returning_one(stmt)

    # ------------------------------------------------------------ timeseries
    def upsert_timeseries_point(
        self,
        *,
        iso: str,
        metric: str,
        settlement_point: str,
        ts: datetime,
        value: float,
        unit: str,
        source_id: int,
    ) -> Timeseries:
        stmt = (
            pg_insert(Timeseries)
            .values(
                iso=iso,
                metric=metric,
                settlement_point=settlement_point or "",
                ts=ts,
                value=value,
                unit=unit,
                source_id=source_id,
            )
            .on_conflict_do_update(
                index_elements=[
                    Timeseries.iso,
                    Timeseries.metric,
                    Timeseries.settlement_point,
                    Timeseries.ts,
                ],
                # revisions: ERCOT/EIA restate values, so a later ingest
                # of the same key overwrites value/unit/source (PRD §5.1).
                set_={"value": value, "unit": unit, "source_id": source_id},
            )
            .returning(Timeseries)
        )
        return self._returning_one(stmt)

    def upsert_timeseries_batch(self, rows: Sequence[dict[str, Any]]) -> int:
        """Bulk version of upsert_timeseries_point. Returns rows written."""
        count = 0
        for row in rows:
            self.upsert_timeseries_point(**row)
            count += 1
        return count

    def get_timeseries(
        self,
        *,
        metric: str,
        settlement_point: str | None,
        start: datetime,
        end: datetime,
    ) -> list[Timeseries]:
        stmt = select(Timeseries).where(
            Timeseries.metric == metric,
            Timeseries.ts >= start,
            Timeseries.ts <= end,
        )
        if settlement_point is not None:
            stmt = stmt.where(Timeseries.settlement_point == settlement_point)
        stmt = stmt.order_by(Timeseries.ts)
        return list(self.session.execute(stmt).scalars().all())

    # ------------------------------------------------------------- documents
    def upsert_document(
        self,
        *,
        source_id: int,
        source_ref: str | None,
        title: str | None,
        url: str | None,
        published_at: datetime | None,
        text: str | None,
        topic: str | None,
        importance: float | None,
    ) -> Document:
        if source_ref is None:
            doc = Document(
                source_id=source_id,
                source_ref=None,
                title=title,
                url=url,
                published_at=published_at,
                text=text,
                topic=topic,
                importance=importance,
            )
            self.session.add(doc)
            self.session.flush()
            return doc

        stmt = (
            pg_insert(Document)
            .values(
                source_id=source_id,
                source_ref=source_ref,
                title=title,
                url=url,
                published_at=published_at,
                text=text,
                topic=topic,
                importance=importance,
            )
            .on_conflict_do_update(
                index_elements=[Document.source_id, Document.source_ref],
                index_where=Document.source_ref.isnot(None),
                set_={
                    "title": title,
                    "url": url,
                    "published_at": published_at,
                    "text": text,
                    "topic": topic,
                    "importance": importance,
                },
            )
            .returning(Document)
        )
        return self._returning_one(stmt)

    def set_document_chunk_ids(self, document_id: int, chunk_ids: Iterable[int]) -> None:
        doc = self.session.get(Document, document_id)
        if doc is None:
            raise ValueError(f"document {document_id} not found")
        doc.chunk_ids = list(chunk_ids)

    def get_recent_documents(
        self,
        *,
        iso: str,
        start: datetime,
        end: datetime,
        topic: str | None = None,
        limit: int = 50,
    ) -> list[Document]:
        """Return recent citable documents, optionally constrained by topic.

        ``iso`` is resolved through indexed chunks because documents are ISO-neutral.
        Documents without chunks remain eligible so a newly ingested item can still
        participate before the next indexing run.
        """
        stmt = (
            select(Document)
            .outerjoin(Chunk, Chunk.document_id == Document.id)
            .where(
                Document.published_at >= start,
                Document.published_at <= end,
                or_(Chunk.iso == iso, Chunk.iso.is_(None)),
            )
            .distinct()
            .order_by(Document.importance.desc().nullslast(), Document.published_at.desc())
            .limit(limit)
        )
        if topic is not None:
            stmt = stmt.where(Document.topic == topic)
        return list(self.session.execute(stmt).scalars().all())

    # ---------------------------------------------------------------- chunks
    def add_chunk(
        self,
        *,
        document_id: int,
        iso: str,
        text: str,
        embedding: Sequence[float],
        source: str | None,
        topic: str | None,
        published_at: datetime | None,
        url: str | None,
    ) -> Chunk:
        chunk = Chunk(
            document_id=document_id,
            iso=iso,
            text=text,
            embedding=list(embedding),
            source=source,
            topic=topic,
            published_at=published_at,
            url=url,
        )
        self.session.add(chunk)
        self.session.flush()
        return chunk

    # ----------------------------------------------------------- watermarks
    def get_watermark(self, source_id: int) -> IngestionWatermark | None:
        stmt = select(IngestionWatermark).where(IngestionWatermark.source_id == source_id)
        return self.session.execute(stmt).scalar_one_or_none()

    def upsert_watermark(
        self,
        *,
        source_id: int,
        last_success_at: datetime | None,
        window_end: datetime | None,
        status: str | None,
        detail_json: dict | None = None,
    ) -> IngestionWatermark:
        stmt = (
            pg_insert(IngestionWatermark)
            .values(
                source_id=source_id,
                last_success_at=last_success_at,
                window_end=window_end,
                status=status,
                detail_json=detail_json,
            )
            .on_conflict_do_update(
                index_elements=[IngestionWatermark.source_id],
                set_={
                    "last_success_at": last_success_at,
                    "window_end": window_end,
                    "status": status,
                    "detail_json": detail_json,
                },
            )
            .returning(IngestionWatermark)
        )
        return self._returning_one(stmt)

    # --------------------------------------------------------- ingestion runs
    def start_ingestion_run(self, *, source_id: int, started_at: datetime) -> IngestionRun:
        run = IngestionRun(source_id=source_id, started_at=started_at, status="running")
        self.session.add(run)
        self.session.flush()
        return run

    def finish_ingestion_run(
        self,
        run_id: int,
        *,
        completed_at: datetime,
        status: str,
        inserted: int = 0,
        updated: int = 0,
        skipped: int = 0,
        error: str | None = None,
    ) -> IngestionRun:
        run = self.session.get(IngestionRun, run_id)
        if run is None:
            raise ValueError(f"ingestion run {run_id} not found")
        run.completed_at = completed_at
        run.status = status
        run.inserted = inserted
        run.updated = updated
        run.skipped = skipped
        run.error = error
        return run

    # ------------------------------------------------------- breaking triggers
    def fire_breaking_trigger(
        self,
        *,
        source_ref: str,
        topic: str,
        severity: str,
        fingerprint: str,
        fired_at: datetime,
        cooldown_until: datetime | None,
    ) -> tuple[BreakingTrigger, bool]:
        """Idempotent per PRD §8.1: re-polling and minor revisions never
        re-fire. Returns (row, created) so callers can tell a fresh fire
        from a duplicate no-op.
        """
        existing = self.session.execute(
            select(BreakingTrigger).where(BreakingTrigger.fingerprint == fingerprint)
        ).scalar_one_or_none()
        if existing is not None:
            return existing, False

        trigger = BreakingTrigger(
            source_ref=source_ref,
            topic=topic,
            severity=severity,
            fingerprint=fingerprint,
            fired_at=fired_at,
            cooldown_until=cooldown_until,
        )
        self.session.add(trigger)
        self.session.flush()
        return trigger, True

    def get_active_breaking_cooldown(
        self, *, topic: str, fired_at: datetime
    ) -> BreakingTrigger | None:
        stmt = (
            select(BreakingTrigger)
            .where(
                BreakingTrigger.topic == topic,
                BreakingTrigger.cooldown_until.isnot(None),
                BreakingTrigger.cooldown_until > fired_at,
            )
            .order_by(BreakingTrigger.fired_at.desc())
            .limit(1)
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def delete_breaking_trigger(self, trigger_id: int) -> None:
        trigger = self.session.get(BreakingTrigger, trigger_id)
        if trigger is not None:
            self.session.delete(trigger)

    # ------------------------------------------------------------- editions
    def save_edition(
        self,
        *,
        iso: str,
        role: str,
        cycle_date,
        generated_at: datetime,
        status: str,
        markdown: str | None,
        html: str | None,
        json: dict | None,
    ) -> Edition:
        edition = Edition(
            iso=iso,
            role=role,
            cycle_date=cycle_date,
            generated_at=generated_at,
            status=status,
            markdown=markdown,
            html=html,
            json=json,
        )
        self.session.add(edition)
        self.session.flush()
        return edition

    def add_edition_claim(
        self,
        *,
        edition_id: int,
        claim_text: str,
        cited_chunk_ids: Sequence[int],
        verified: bool,
        groundedness: float | None,
    ) -> EditionClaim:
        claim = EditionClaim(
            edition_id=edition_id,
            claim_text=claim_text,
            cited_chunk_ids=list(cited_chunk_ids),
            verified=verified,
            groundedness=groundedness,
        )
        self.session.add(claim)
        self.session.flush()
        return claim

    def add_eval_run(
        self,
        *,
        edition_id: int | None,
        metric: str,
        value: float | None,
        detail_json: dict | None,
        created_at: datetime,
    ) -> EvalRun:
        run = EvalRun(
            edition_id=edition_id,
            metric=metric,
            value=value,
            detail_json=detail_json,
            created_at=created_at,
        )
        self.session.add(run)
        self.session.flush()
        return run

    def get_latest_edition(self, *, role: str) -> Edition | None:
        stmt = (
            select(Edition)
            .where(Edition.role == role)
            .order_by(Edition.generated_at.desc())
            .limit(1)
        )
        return self.session.execute(stmt).scalar_one_or_none()
