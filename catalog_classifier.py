"""Live-catalogue batch classifier."""
from __future__ import annotations

import json
import logging
import os

from groq import AsyncGroq

logger = logging.getLogger(__name__)


async def classify_services_batch(db, extracted_services: list[str]) -> list[dict]:
    services = [str(s).strip() for s in (extracted_services or []) if str(s).strip()]
    if not services:
        return []

    safe = [
        {"service_name": s, "subcategory_id": None, "direction_id": None}
        for s in services
    ]

    try:
        catalog_rows = await get_catalog(db)
        if not catalog_rows:
            logger.error("Live catalogue is empty or unavailable.")
            return safe

        clean_catalog = [
            {
                "id": row["category_id"],
                "name_am": row["category_am"],
                "name_ru": row["category_ru"],
                "direction_id": row["master_id"],
            }
            for row in catalog_rows
        ]
        catalog_lookup = {
            int(row["category_id"]): int(row["master_id"])
            for row in catalog_rows
            if row.get("category_id") is not None and row.get("master_id") is not None
        }

        prompt = f"""Ты — эксперт-классификатор каталога услуг.
Услуги мастера:
{json.dumps(services, ensure_ascii=False)}

Живой каталог:
{json.dumps(clean_catalog, ensure_ascii=False)}

Для КАЖДОЙ услуги выбери наиболее подходящую подкатегорию из каталога.
Учитывай армянский и русский языки, синонимы, формы слов и контекст ремонта.
Если точного совпадения нет, выбери наиболее близкую логическую подкатегорию
из предоставленного каталога.

Верни ТОЛЬКО JSON:
{{
  "classified_services": [
    {{
      "service_name": "оригинальное имя услуги",
      "subcategory_id": 123,
      "direction_id": 19
    }}
  ]
}}

subcategory_id и direction_id должны быть только из предоставленного каталога.
Нельзя придумывать ID. Для каждой входящей услуги должен быть результат."""

        key = os.getenv("GROQ_API_KEY", "").strip()
        if not key:
            logger.error("GROQ_API_KEY is not configured.")
            return safe

        client = AsyncGroq(api_key=key)
        response = await client.chat.completions.create(
            model="openai/gpt-oss-20b",
            reasoning_effort="low",
            temperature=0,
            response_format={"type": "json_object"},
            max_tokens=max(500, len(services) * 120),
            messages=[{"role": "user", "content": prompt}],
        )

        raw = (response.choices[0].message.content if response.choices else "") or ""
        data = json.loads(raw)
        ai_items = data.get("classified_services", [])
        if not isinstance(ai_items, list):
            raise ValueError("Invalid classified_services response")

        by_name = {}
        for item in ai_items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("service_name") or "").strip()
            if name:
                by_name[name] = item

        result = []
        for service in services:
            item = by_name.get(service)
            try:
                sub_id = int(item["subcategory_id"])
                direction_id = int(item["direction_id"])
            except (KeyError, TypeError, ValueError):
                sub_id = direction_id = None

            valid = (
                sub_id is not None
                and direction_id is not None
                and sub_id in catalog_lookup
                and catalog_lookup[sub_id] == direction_id
            )

            if not valid:
                logger.warning("Service '%s' failed catalogue ID validation.", service)
                sub_id = direction_id = None

            result.append({
                "service_name": service,
                "subcategory_id": sub_id,
                "direction_id": direction_id,
            })

        return result

    except Exception as exc:
        logger.error("classify_services_batch failed: %s", exc, exc_info=True)
        return safe
