"""Incremental pgvector indexing for citable GridBrief documents."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from gridbrief.chunking import ChunkDraft, TokenizerLike, chunk_document
from gridbrief.config import get_settings
from gridbrief.db import get_session_factory, session_scope
from gridbrief.models import Chunk, Document, Source
from gridbrief.repository import Repository

EXPECTED_EMBEDDING_DIMENSION = 768
DEFAULT_BATCH_SIZE = 16


@dataclass(frozen=True, slots=True)
class IndexSummary:
    """Summary returned after an indexing or dry-run operation."""

    documents_seen: int
    documents_indexed: int
    documents_skipped: int
    chunks_planned: int
    chunks_written: int
    dry_run: bool

    def as_dict(self) -> dict[str, int | bool]:
        """Return a JSON-safe representation."""

        return {
            "documents_seen": self.documents_seen,
            "documents_indexed": self.documents_indexed,
            "documents_skipped": self.documents_skipped,
            "chunks_planned": self.chunks_planned,
            "chunks_written": self.chunks_written,
            "dry_run": self.dry_run,
        }


@dataclass(frozen=True, slots=True)
class _DocumentPlan:
    """Prepared indexing work for one document."""

    document_id: int
    source_name: str
    topic: str | None
    published_at: datetime | None
    url: str | None
    chunks: tuple[ChunkDraft, ...]
    existing_chunk_ids: tuple[int, ...]
    needs_index: bool
    chunk_ids_need_repair: bool


def _chunks_are_current(
    *,
    existing: tuple[Chunk, ...],
    drafts: tuple[ChunkDraft, ...],
    iso: str,
    source_name: str,
    topic: str | None,
    published_at: datetime | None,
    url: str | None,
) -> bool:
    """Check whether stored chunks already match text and metadata."""

    if len(existing) != len(drafts):
        return False

    return all(
        stored.text == draft.text
        and stored.iso == iso
        and stored.source == source_name
        and stored.topic == topic
        and stored.published_at == published_at
        and stored.url == url
        for stored, draft in zip(existing, drafts, strict=True)
    )


def _build_plans(
    *,
    session: Session,
    tokenizer: TokenizerLike,
    iso: str,
    force: bool,
) -> list[_DocumentPlan]:
    """Prepare deterministic work plans without changing the database."""

    rows = session.execute(
        select(Document, Source.name)
        .join(Source, Document.source_id == Source.id)
        .order_by(Document.id)
    ).all()

    plans: list[_DocumentPlan] = []

    for document, source_name in rows:
        drafts = tuple(
            chunk_document(
                title=document.title,
                text=document.text,
                tokenizer=tokenizer,
            )
        )

        existing = tuple(
            session.execute(
                select(Chunk).where(Chunk.document_id == document.id).order_by(Chunk.chunk_id)
            )
            .scalars()
            .all()
        )

        existing_chunk_ids = tuple(chunk.chunk_id for chunk in existing)

        is_current = _chunks_are_current(
            existing=existing,
            drafts=drafts,
            iso=iso,
            source_name=source_name,
            topic=document.topic,
            published_at=document.published_at,
            url=document.url,
        )

        needs_index = force or not is_current

        stored_document_ids = tuple(document.chunk_ids or ())
        chunk_ids_need_repair = not needs_index and stored_document_ids != existing_chunk_ids

        plans.append(
            _DocumentPlan(
                document_id=document.id,
                source_name=source_name,
                topic=document.topic,
                published_at=document.published_at,
                url=document.url,
                chunks=drafts,
                existing_chunk_ids=existing_chunk_ids,
                needs_index=needs_index,
                chunk_ids_need_repair=chunk_ids_need_repair,
            )
        )

    return plans


def index_documents(
    *,
    dry_run: bool = False,
    force: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
    show_progress: bool = True,
) -> IndexSummary:
    """Chunk, embed, and incrementally index stored documents.

    Unchanged documents are skipped. Changed documents have their old
    chunks replaced inside one transaction so rerunning the command does
    not create duplicate chunks.
    """

    if batch_size <= 0:
        raise ValueError("batch_size must be greater than zero")

    settings = get_settings()

    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(settings.embedding_model)
    dimension = model.get_sentence_embedding_dimension()

    if dimension != EXPECTED_EMBEDDING_DIMENSION:
        raise ValueError(
            "embedding dimension mismatch: "
            f"expected {EXPECTED_EMBEDDING_DIMENSION}, got {dimension}"
        )

    session_factory = get_session_factory()

    with session_factory() as session:
        plans = _build_plans(
            session=session,
            tokenizer=model.tokenizer,
            iso=settings.iso,
            force=force,
        )

    changed_plans = [plan for plan in plans if plan.needs_index]

    chunk_texts = [chunk.text for plan in changed_plans for chunk in plan.chunks]

    if dry_run:
        return IndexSummary(
            documents_seen=len(plans),
            documents_indexed=len(changed_plans),
            documents_skipped=len(plans) - len(changed_plans),
            chunks_planned=len(chunk_texts),
            chunks_written=0,
            dry_run=True,
        )

    if chunk_texts:
        embeddings = model.encode(
            chunk_texts,
            batch_size=batch_size,
            show_progress_bar=show_progress,
            normalize_embeddings=True,
            convert_to_numpy=True,
        )

        if embeddings.ndim != 2:
            raise ValueError("embedding model returned an invalid array")

        if embeddings.shape[1] != EXPECTED_EMBEDDING_DIMENSION:
            raise ValueError(
                "generated embedding dimension mismatch: "
                f"expected {EXPECTED_EMBEDDING_DIMENSION}, "
                f"got {embeddings.shape[1]}"
            )
    else:
        embeddings = None

    chunks_written = 0
    embedding_offset = 0

    with session_scope() as session:
        repository = Repository(session)

        for plan in changed_plans:
            session.execute(delete(Chunk).where(Chunk.document_id == plan.document_id))
            session.flush()

            new_chunk_ids: list[int] = []

            for draft in plan.chunks:
                if embeddings is None:
                    raise RuntimeError("embeddings are missing for prepared chunks")

                embedding = embeddings[embedding_offset].tolist()
                embedding_offset += 1

                stored_chunk = repository.add_chunk(
                    document_id=plan.document_id,
                    iso=settings.iso,
                    text=draft.text,
                    embedding=embedding,
                    source=plan.source_name,
                    topic=plan.topic,
                    published_at=plan.published_at,
                    url=plan.url,
                )

                new_chunk_ids.append(stored_chunk.chunk_id)
                chunks_written += 1

            repository.set_document_chunk_ids(
                plan.document_id,
                new_chunk_ids,
            )

        for plan in plans:
            if plan.chunk_ids_need_repair:
                repository.set_document_chunk_ids(
                    plan.document_id,
                    plan.existing_chunk_ids,
                )

    return IndexSummary(
        documents_seen=len(plans),
        documents_indexed=len(changed_plans),
        documents_skipped=len(plans) - len(changed_plans),
        chunks_planned=len(chunk_texts),
        chunks_written=chunks_written,
        dry_run=False,
    )
