"""Generate/update embeddings for every active catalogue subcategory.

Run once after applying the vector migration:
    python scripts/index_catalog_embeddings.py
"""
from __future__ import annotations

import asyncio
import os

from openai import AsyncOpenAI

import data_core


MODEL = os.getenv("CATALOG_EMBEDDING_MODEL", "text-embedding-3-small")
DIMENSIONS = int(os.getenv("CATALOG_EMBEDDING_DIMENSIONS", "1536"))


def build_text(row: dict) -> str:
    values = []
    for key in ("name_am", "name_ru", "name_en"):
        value = " ".join(str(row.get(key) or "").split()).strip()
        if value and value not in values:
            values.append(value)
    return " | ".join(values)


async def main() -> None:
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("OPENAI_API_KEY is required")

    client = AsyncOpenAI(api_key=key)
    rows = data_core.rows(
        """
        SELECT id,name_am,name_ru,name_en
        FROM categories
        WHERE is_active=TRUE
        ORDER BY id
        """
    )

    batch_size = 64
    for start in range(0, len(rows), batch_size):
        batch = rows[start:start + batch_size]
        inputs = [build_text(row) for row in batch]

        response = await client.embeddings.create(
            model=MODEL,
            input=inputs,
        )

        for row, item, embedding in zip(batch, inputs, response.data):
            vector = list(embedding.embedding)
            if len(vector) != DIMENSIONS:
                raise RuntimeError(
                    f"category {row['id']}: expected {DIMENSIONS}, got {len(vector)}"
                )

            literal = "[" + ",".join(f"{float(x):.10f}" for x in vector) + "]"
            data_core.execute(
                """
                UPDATE categories
                SET embedding=%s::vector,
                    embedding_model=%s,
                    embedding_source=%s,
                    embedding_updated_at=NOW()
                WHERE id=%s
                """,
                (literal, MODEL, item, int(row["id"])),
            )

        print(f"embedded {min(start + batch_size, len(rows))}/{len(rows)}")


if __name__ == "__main__":
    asyncio.run(main())
