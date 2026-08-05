"""SQLAlchemy 2 ORM models for GridBrief AI (PRD §6.1, §6.2).

These are read/write mirrors of the tables created by migrations/*.sql —
the SQL files are the source of truth for schema (constraints/indexes);
these classes exist so repository.py and the rest of the app get typed,
ergonomic access instead of hand-written SQL everywhere.
"""
from __future__ import annotations

from datetime import date, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    Date,
    Double,
    ForeignKey,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    base_url: Mapped[str | None] = mapped_column(Text)

    raw_items: Mapped[list[RawItem]] = relationship(back_populates="source")


class RawItem(Base):
    __tablename__ = "raw_items"
    __table_args__ = (UniqueConstraint("source_id", "source_ref", name="raw_items_source_ref_key"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
    )
    source_ref: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    url: Mapped[str | None] = mapped_column(Text)
    raw_hash: Mapped[str | None] = mapped_column(Text)
    ingested_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))

    source: Mapped[Source] = relationship(back_populates="raw_items")


class Timeseries(Base):
    __tablename__ = "timeseries"
    __table_args__ = (
        UniqueConstraint(
            "iso", "metric", "settlement_point", "ts", name="timeseries_observation_key"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    iso: Mapped[str] = mapped_column(Text, nullable=False)
    metric: Mapped[str] = mapped_column(Text, nullable=False)
    settlement_point: Mapped[str] = mapped_column(Text, nullable=False, default="")
    ts: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    value: Mapped[float] = mapped_column(Double, nullable=False)
    unit: Mapped[str] = mapped_column(Text, nullable=False)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("sources.id", ondelete="RESTRICT"), nullable=False
    )


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("sources.id", ondelete="RESTRICT"), nullable=False
    )
    source_ref: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    text: Mapped[str | None] = mapped_column(Text)
    topic: Mapped[str | None] = mapped_column(Text)
    importance: Mapped[float | None] = mapped_column(Double)
    chunk_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), default=list)

    chunks: Mapped[list[Chunk]] = relationship(back_populates="document")


class Chunk(Base):
    __tablename__ = "chunks"

    chunk_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    iso: Mapped[str] = mapped_column(Text, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding = mapped_column(Vector(768), nullable=False)
    source: Mapped[str | None] = mapped_column(Text)
    topic: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    url: Mapped[str | None] = mapped_column(Text)

    document: Mapped[Document] = relationship(back_populates="chunks")


class Edition(Base):
    __tablename__ = "editions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    iso: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    cycle_date: Mapped[date] = mapped_column(Date, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))
    status: Mapped[str] = mapped_column(Text, default="draft")
    markdown: Mapped[str | None] = mapped_column(Text)
    html: Mapped[str | None] = mapped_column(Text)
    json: Mapped[dict | None] = mapped_column(JSONB)


class EditionClaim(Base):
    __tablename__ = "edition_claims"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    edition_id: Mapped[int] = mapped_column(
        ForeignKey("editions.id", ondelete="CASCADE"), nullable=False
    )
    claim_text: Mapped[str] = mapped_column(Text, nullable=False)
    cited_chunk_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), default=list)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    groundedness: Mapped[float | None] = mapped_column(Double)


class EvalRun(Base):
    __tablename__ = "eval_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    edition_id: Mapped[int | None] = mapped_column(ForeignKey("editions.id", ondelete="CASCADE"))
    metric: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[float | None] = mapped_column(Double)
    detail_json: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))


class IngestionWatermark(Base):
    __tablename__ = "ingestion_watermarks"
    __table_args__ = (UniqueConstraint("source_id", name="ingestion_watermarks_source_key"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
    )
    last_success_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    window_end: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    status: Mapped[str | None] = mapped_column(Text)
    detail_json: Mapped[dict | None] = mapped_column(JSONB)


class IngestionRun(Base):
    __tablename__ = "ingestion_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    status: Mapped[str | None] = mapped_column(Text)
    inserted: Mapped[int] = mapped_column(default=0)
    updated: Mapped[int] = mapped_column(default=0)
    skipped: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None] = mapped_column(Text)


class BreakingTrigger(Base):
    __tablename__ = "breaking_triggers"
    __table_args__ = (UniqueConstraint("fingerprint", name="breaking_triggers_fingerprint_key"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_ref: Mapped[str] = mapped_column(Text, nullable=False)
    topic: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    fired_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))
    cooldown_until: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
