"""Citable semantic retrieval using sentence-transformers and pgvector."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache

from sentence_transformers import SentenceTransformer
from sqlalchemy import select

from gridbrief.config import get_settings
from gridbrief.db import get_session_factory
from gridbrief.models import Chunk, Document

EXPECTED_EMBEDDING_DIMENSION = 768
DEFAULT_RESULT_COUNT = 5
MAX_RESULT_COUNT = 50
QUERY_INSTRUCTION = (
    "Represent this sentence for searching relevant passages: "
)


@dataclass(frozen=True, slots=True)
class SearchFilters:
    """Optional metadata filters for semantic retrieval."""

    iso: str | None = None
    source: str | None = None
    topic: str | None = None
    published_after: datetime | None = None
    published_before: datetime | None = None


@dataclass(frozen=True, slots=True)
class SearchResult:
    """One citable semantic-search result."""

    chunk_id: int
    document_id: int
    title: str | None
    text: str
    score: float
    source: str | None
    topic: str | None
    published_at: datetime | None
    url: str | None

    def as_dict(self) -> dict[str, int | float | str | None]:
        """Return a JSON-safe evidence record."""

        return {
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "title": self.title,
            "text": self.text,
            "score": self.score,
            "source": self.source,
            "topic": self.topic,
            "published_at": (
                self.published_at.isoformat()
                if self.published_at is not None
                else None
            ),
            "url": self.url,
        }


@lru_cache(maxsize=1)
def _get_embedding_model() -> SentenceTransformer:
    """Load and cache the configured embedding model."""

    settings = get_settings()
    model = SentenceTransformer(settings.embedding_model)
    dimension = model.get_sentence_embedding_dimension()

    if dimension != EXPECTED_EMBEDDING_DIMENSION:
        raise ValueError(
            "embedding dimension mismatch: "
            f"expected {EXPECTED_EMBEDDING_DIMENSION}, got {dimension}"
        )

    return model


def _normalize_query(query: str) -> str:
    """Validate and normalize a user search query."""

    normalized = " ".join(query.split())

    if not normalized:
        raise ValueError("query cannot be empty")

    return normalized


def _encode_query(query: str) -> list[float]:
    """Create a normalized embedding for semantic passage retrieval."""

    model = _get_embedding_model()
    instructed_query = f"{QUERY_INSTRUCTION}{query}"

    embedding = model.encode(
        instructed_query,
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=False,
    )

    if embedding.ndim != 1:
        raise ValueError("embedding model returned an invalid query vector")

    if embedding.shape[0] != EXPECTED_EMBEDDING_DIMENSION:
        raise ValueError(
            "query embedding dimension mismatch: "
            f"expected {EXPECTED_EMBEDDING_DIMENSION}, "
            f"got {embedding.shape[0]}"
        )

    return embedding.tolist()


def search(
    query: str,
    filters: SearchFilters | None = None,
    k: int = DEFAULT_RESULT_COUNT,
) -> list[SearchResult]:
    """Return the most relevant citable chunks from pgvector."""

    if k <= 0:
        raise ValueError("k must be greater than zero")

    if k > MAX_RESULT_COUNT:
        raise ValueError(
            f"k cannot be greater than {MAX_RESULT_COUNT}"
        )

    settings = get_settings()

    if settings.retrieval_backend != "pgvector":
        raise ValueError(
            "semantic search currently requires "
            "GRIDBRIEF_RETRIEVAL_BACKEND=pgvector"
        )

    normalized_query = _normalize_query(query)
    query_embedding = _encode_query(normalized_query)
    active_filters = filters or SearchFilters()
    iso = active_filters.iso or settings.iso

    distance = Chunk.embedding.cosine_distance(
        query_embedding
    ).label("distance")

    statement = (
        select(
            Chunk,
            Document.title,
            distance,
        )
        .join(
            Document,
            Document.id == Chunk.document_id,
        )
        .where(Chunk.iso == iso)
    )

    if active_filters.source is not None:
        statement = statement.where(
            Chunk.source == active_filters.source
        )

    if active_filters.topic is not None:
        statement = statement.where(
            Chunk.topic == active_filters.topic
        )

    if active_filters.published_after is not None:
        statement = statement.where(
            Chunk.published_at >= active_filters.published_after
        )

    if active_filters.published_before is not None:
        statement = statement.where(
            Chunk.published_at <= active_filters.published_before
        )

    statement = statement.order_by(
        distance.asc(),
        Chunk.chunk_id.asc(),
    ).limit(k)

    session_factory = get_session_factory()

    with session_factory() as session:
        rows = session.execute(statement).all()

    results: list[SearchResult] = []

    for chunk, title, distance_value in rows:
        score = 1.0 - float(distance_value)

        results.append(
            SearchResult(
                chunk_id=chunk.chunk_id,
                document_id=chunk.document_id,
                title=title,
                text=chunk.text,
                score=score,
                source=chunk.source,
                topic=chunk.topic,
                published_at=chunk.published_at,
                url=chunk.url,
            )
        )

    return results


def vector_search(
    query: str,
    filters: SearchFilters | None = None,
    k: int = DEFAULT_RESULT_COUNT,
) -> list[SearchResult]:
    """Agent-tool alias for the shared semantic-search interface."""

    return search(
        query=query,
        filters=filters,
        k=k,
    )
