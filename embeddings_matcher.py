"""Semantic catalogue matching using OpenAI embeddings + PostgreSQL/pgvector.

Groq remains responsible for extracting service text only. This module never
accepts category IDs from an LLM.
"""
from __future__ import annotations

import os
from functools import lru_cache

from openai import AsyncOpenAI

import data_core


EMBEDDING_MODEL = os.getenv(
    "CATALOG_EMBEDDING_MODEL",
    "text-embedding-3-small",
).strip() or "text-embedding-3-small"
EMBEDDING_DIMENSIONS = int(os.getenv("CATALOG_EMBEDDING_DIMENSIONS", "1536"))
DEFAULT_THRESHOLD = float(os.getenv("CATALOG_EMBEDDING_THRESHOLD", "0.72"))


@lru_cache(maxsize=1)
def _client() -> AsyncOpenAI:
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "OPENAI_API_KEY is required for semantic catalogue matching"
        )
    return AsyncOpenAI(api_key=key)


def _embedding_text(name_am: str = "", name_ru: str = "", name_en: str = "") -> str:
    parts = []
    for value in (name_am, name_ru, name_en):
        value = " ".join(str(value or "").split())
        if value and value not in parts:
            parts.append(value)
    return " | ".join(parts)


async def create_embedding(text: str) -> list[float]:
    value = " ".join(str(text or "").split()).strip()
    if not value:
        raise ValueError("Cannot embed empty text")

    response = await _client().embeddings.create(
        model=EMBEDDING_MODEL,
        input=value,
    )
    vector = list(response.data[0].embedding)

    if len(vector) != EMBEDDING_DIMENSIONS:
        raise RuntimeError(
            f"Embedding dimension mismatch: expected {EMBEDDING_DIMENSIONS}, "
            f"got {len(vector)}"
        )
    return vector


async def match_service_to_catalog(
    service_name: str,
    threshold: float = DEFAULT_THRESHOLD,
) -> dict | None:
    """Return the best live DB category using cosine similarity."""
    vector = await create_embedding(service_name)
    vector_literal = "[" + ",".join(f"{float(x):.10f}" for x in vector) + "]"

    row = data_core.one(
        """
        SELECT
            c.id AS category_id,
            c.master_category_id AS master_id,
            c.name_am AS category_am,
            c.name_ru AS category_ru,
            c.name_en AS category_en,
            m.name_am AS master_am,
            m.name_ru AS master_ru,
            m.name_en AS master_en,
            1 - (c.embedding <=> %s::vector) AS similarity
        FROM categories c
        JOIN master_categories m ON m.id = c.master_category_id
        WHERE c.is_active = TRUE
          AND m.is_active = TRUE
          AND c.embedding IS NOT NULL
        ORDER BY c.embedding <=> %s::vector
        LIMIT 1
        """,
        (vector_literal, vector_literal),
    )

    if not row:
        return None

    similarity = float(row.get("similarity") or 0)
    if similarity < float(threshold):
        return None

    result = dict(row)
    result["similarity"] = similarity
    return result
