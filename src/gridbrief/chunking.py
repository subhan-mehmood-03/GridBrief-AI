"""Deterministic token-based document chunking for semantic retrieval."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

DEFAULT_MAX_TOKENS = 500
DEFAULT_OVERLAP_TOKENS = 60

_HORIZONTAL_SPACE_RE = re.compile(r"[ \t]+")
_EXTRA_BLANK_LINES_RE = re.compile(r"\n{3,}")


class TokenizerLike(Protocol):
    """Small tokenizer interface needed by the chunker."""

    def encode(
        self,
        text: str,
        *,
        add_special_tokens: bool = False,
    ) -> list[int]:
        """Convert text to token IDs."""

    def decode(
        self,
        token_ids: list[int],
        *,
        skip_special_tokens: bool = True,
    ) -> str:
        """Convert token IDs back to text."""


@dataclass(frozen=True, slots=True)
class ChunkDraft:
    """A chunk prepared for embedding and database storage."""

    position: int
    text: str
    token_count: int


def normalize_text(value: str | None) -> str:
    """Normalize whitespace while preserving paragraph boundaries."""

    if not value:
        return ""

    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    lines = [
        _HORIZONTAL_SPACE_RE.sub(" ", line).strip()
        for line in normalized.split("\n")
    ]
    normalized = "\n".join(lines)
    normalized = _EXTRA_BLANK_LINES_RE.sub("\n\n", normalized)

    return normalized.strip()


def chunk_document(
    *,
    title: str | None,
    text: str | None,
    tokenizer: TokenizerLike,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
) -> list[ChunkDraft]:
    """Split a document into deterministic overlapping token windows.

    The title is added to every chunk so semantic retrieval retains the
    document's main subject. Short documents remain as one chunk.
    """

    if max_tokens <= 0:
        raise ValueError("max_tokens must be greater than zero")

    if overlap_tokens < 0:
        raise ValueError("overlap_tokens cannot be negative")

    if overlap_tokens >= max_tokens:
        raise ValueError("overlap_tokens must be smaller than max_tokens")

    clean_title = normalize_text(title)
    clean_body = normalize_text(text)

    if not clean_title and not clean_body:
        return []

    prefix = f"Title: {clean_title}\n\n" if clean_title else ""
    prefix_token_ids = tokenizer.encode(
        prefix,
        add_special_tokens=False,
    )

    if len(prefix_token_ids) >= max_tokens:
        raise ValueError(
            "document title is too long to leave room for chunk text"
        )

    body_token_ids = tokenizer.encode(
        clean_body,
        add_special_tokens=False,
    )

    body_budget = max_tokens - len(prefix_token_ids)

    if not body_token_ids:
        return [
            ChunkDraft(
                position=0,
                text=normalize_text(prefix),
                token_count=len(prefix_token_ids),
            )
        ]

    step_size = body_budget - overlap_tokens
    if step_size <= 0:
        raise ValueError(
            "overlap_tokens leaves no room for new document content"
        )

    chunks: list[ChunkDraft] = []
    start = 0
    position = 0

    while start < len(body_token_ids):
        end = min(start + body_budget, len(body_token_ids))
        body_window = body_token_ids[start:end]

        decoded_body = tokenizer.decode(
            body_window,
            skip_special_tokens=True,
        )

        chunk_text = normalize_text(f"{prefix}{decoded_body}")

        if chunk_text:
            chunks.append(
                ChunkDraft(
                    position=position,
                    text=chunk_text,
                    token_count=(
                        len(prefix_token_ids) + len(body_window)
                    ),
                )
            )
            position += 1

        if end >= len(body_token_ids):
            break

        start += step_size

    return chunks
