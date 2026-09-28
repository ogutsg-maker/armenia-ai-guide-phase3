"""Live catalogue classifier.

Groq is intentionally NOT used here. The AI extraction layer supplies only
plain service names; this module resolves them against the live database
catalogue using deterministic multilingual string matching.
"""
from __future__ import annotations

import logging
import os
import re
from difflib import SequenceMatcher
from typing import Any

import data_core
import platform_db

logger = logging.getLogger(__name__)

MATCH_THRESHOLD = 0.45


async def get_catalog(db=None) -> list[dict]:
    """Read the active catalogue through the existing Data Core DB layer."""
    rows = []
    try:
        for row in data_core.rows(
            """SELECT m.id AS master_id,m.name_am AS master_am,
                      m.name_ru AS master_ru,m.name_en AS master_en,
                      m.slug AS master_slug,
                      c.id AS category_id,c.name_am AS category_am,
                      c.name_ru AS category_ru,c.name_en AS category_en,
                      c.slug AS category_slug
               FROM master_categories m
               JOIN categories c ON c.master_category_id=m.id
               WHERE m.is_active=TRUE AND c.is_active=TRUE
               ORDER BY m.id,c.id"""
        ):
            rows.append(dict(row))
    except Exception:
        logger.exception("Failed to load live catalogue.")
    return rows


def _normalize_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).lower().strip())


def get_match_ratio(str1: Any, str2: Any) -> float:
    """Multilingual deterministic string similarity."""
    s1 = _normalize_text(str1)
    s2 = _normalize_text(str2)

    if not s1 or not s2:
        return 0.0

    if s1 in s2 or s2 in s1:
        return 0.95

    tokens1 = set(s1.split())
    tokens2 = set(s2.split())
    if tokens1.intersection(tokens2):
        return 0.85

    return SequenceMatcher(None, s1, s2).ratio()


def _safe_int(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _admin_telegram_id() -> int | None:
    return _safe_int(os.getenv("ADMIN_TELEGRAM_ID", "").strip())


def _already_alerted(
    *,
    admin_telegram_id: int,
    service_name: str,
    application_id: int | None,
) -> bool:
    """Avoid duplicate alerts for the same unresolved service/application."""
    try:
        row = platform_db.one(
            """SELECT n.id
               FROM notifications n
               JOIN users u ON u.id=n.user_id
               WHERE (u.id=%s OR u.telegram_id=%s)
                 AND n.kind='catalog_unclassified_service'
                 AND n.data_json->>'service_name'=%s
                 AND COALESCE(n.data_json->>'application_id','')=%s
               LIMIT 1""",
            (
                admin_telegram_id,
                admin_telegram_id,
                str(service_name),
                str(application_id or ""),
            ),
        )
        return bool(row)
    except Exception:
        logger.exception("Could not check duplicate catalogue alert.")
        return False


async def _notify_unclassified_services(
    *,
    services: list[tuple[str, float]],
    telegram_id: int | None,
    application_id: int | None,
) -> None:
    """Create one best-effort admin notification for this classification batch."""
    if not services:
        return

    admin_id = _admin_telegram_id()
    if not admin_id:
        logger.warning("Catalog alert skipped: ADMIN_TELEGRAM_ID is not configured.")
        return

    unique = []
    seen = set()
    for service_name, score in services:
        key = _normalize_text(service_name)
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append((service_name, score))

    if application_id is not None:
        unique = [
            (name, score)
            for name, score in unique
            if not _already_alerted(
                admin_telegram_id=admin_id,
                service_name=name,
                application_id=application_id,
            )
        ]

    if not unique:
        return

    body_lines = [
        "🚨 Նոր չդասակարգված ծառայություն",
        "",
        "Կատալոգում համապատասխան գրառում չի գտնվել։",
    ]
    body_lines.extend(f"• {name} · score={score:.2f}" for name, score in unique)
    body_lines.extend(
        [
            "",
            f"📨 Հայտ: #{application_id}" if application_id else "📨 Հայտ: Ն/Դ",
            f"👤 Telegram: {telegram_id}" if telegram_id else "👤 Telegram: Ն/Դ",
            "",
            "💡 Ստուգեք հայտը և անհրաժեշտության դեպքում ավելացրեք/վերանայեք կատալոգի ծառը։",
        ]
    )

    try:
        notification = platform_db.create_notification(
            admin_id,
            title="🚨 Նոր չդասակարգված ծառայություն",
            body="\n".join(body_lines),
            kind="catalog_unclassified_service",
            audience="admin",
            data={
                "application_id": application_id,
                "telegram_id": telegram_id,
                "service_name": unique[0][0],
                "services": [
                    {"service_name": name, "score": round(score, 4)}
                    for name, score in unique
                ],
            },
            delivered=False,
        )
        if notification:
            logger.warning(
                "Admin catalogue alert created: application=%s services=%s",
                application_id,
                [name for name, _ in unique],
            )
    except Exception:
        logger.exception("Failed to create admin catalogue alert.")


async def classify_services_batch(
    db,
    extracted_services: list[str],
    telegram_id: int | None = None,
    application_id: int | None = None,
) -> list[dict[str, Any]]:
    """
    Classify AI-extracted service names against the live catalogue.

    Groq is never called here.
    """
    services = [
        str(service).strip()
        for service in (extracted_services or [])
        if str(service).strip()
    ]

    if not services:
        return []

    safe = [
        {"service_name": service, "subcategory_id": None, "direction_id": None}
        for service in services
    ]

    try:
        catalog_rows = await get_catalog(db)
        if not catalog_rows:
            logger.error("Live catalogue is empty or unavailable.")
            return safe

        final_classified = []
        unresolved = []

        for service in services:
            best_match = None
            max_score = 0.0

            for row in catalog_rows:
                current_max = max(
                    get_match_ratio(service, row.get("category_am", "")),
                    get_match_ratio(service, row.get("category_ru", "")),
                    get_match_ratio(service, row.get("category_en", "")),
                )

                if current_max > max_score:
                    max_score = current_max
                    best_match = row

            if (
                best_match is not None
                and max_score >= MATCH_THRESHOLD
                and best_match.get("category_id") is not None
                and best_match.get("master_id") is not None
            ):
                final_classified.append(
                    {
                        "service_name": service,
                        "subcategory_id": best_match["category_id"],
                        "direction_id": best_match["master_id"],
                    }
                )
                logger.info(
                    "Catalogue auto-match: '%s' -> category=%s direction=%s score=%.2f",
                    service,
                    best_match["category_id"],
                    best_match["master_id"],
                    max_score,
                )
            else:
                final_classified.append(
                    {
                        "service_name": service,
                        "subcategory_id": None,
                        "direction_id": None,
                    }
                )
                unresolved.append((service, max_score))
                logger.warning(
                    "Service '%s' is unclassified (best score %.2f).",
                    service,
                    max_score,
                )

        await _notify_unclassified_services(
            services=unresolved,
            telegram_id=telegram_id,
            application_id=application_id,
        )

        return final_classified

    except Exception as exc:
        logger.error("classify_services_batch failed: %s", exc, exc_info=True)
        return safe
