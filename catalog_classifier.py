"""Live catalogue classifier.

Groq is intentionally NOT used here. The AI extraction layer supplies only
plain service names; this module resolves them against the live database
catalogue using deterministic, conservative matching.
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

# A fuzzy candidate is accepted only when it is genuinely strong.
FUZZY_CONFIDENCE_GATE = 0.70
TOKEN_OVERLAP_GATE = 0.80

# These are grammatical/function words only. Catalogue/domain words are never
# removed here, so the matcher remains independent of business categories.
STOP_WORDS = {
    # Armenian
    "և", "ու", "կամ", "համար", "մեջ", "վրա", "հետ", "առանց", "ըստ",
    "է", "են", "էին", "լինելու",
    # Russian
    "и", "или", "для", "по", "на", "в", "во", "с", "со", "без", "из",
    # English
    "and", "or", "for", "on", "in", "with", "without", "of", "the",
}

# Armenian inflection/plural endings. Longest forms must be removed first.
# This is intentionally a small, conservative normalizer, not a full
# Armenian morphological stemmer.
ARMENIAN_SUFFIXES = (
    "ներով",
    "ներից",
    "ներին",
    "ների",
    "երով",
    "երից",
    "երին",
    "երի",
    "ներ",
    "եր",
    "ի",
)


def _normalize_text(value: Any) -> str:
    """Lowercase, clean punctuation/whitespace, and keep Unicode letters."""
    if value is None:
        return ""

    text = str(value).lower().strip()
    text = re.sub(r"[^\w\s]+", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def _stem_token(token: str) -> str:
    """Apply conservative Armenian inflection stripping to one token."""
    token = _normalize_text(token)
    if not token:
        return ""

    # Never reduce very short words; stripping one or two characters from
    # them creates more false positives than useful matches.
    if len(token) <= 4:
        return token

    for suffix in ARMENIAN_SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 4:
            return token[: -len(suffix)]

    return token


def _tokens(value: Any) -> set[str]:
    """Return normalized, stop-word-free token stems."""
    text = _normalize_text(value)
    if not text:
        return set()

    result = set()
    for token in text.split():
        if token in STOP_WORDS:
            continue
        stem = _stem_token(token)
        if stem and stem not in STOP_WORDS:
            result.add(stem)
    return result


def _root_token_match(service_tokens: set[str], category_tokens: set[str]) -> bool:
    """True when a meaningful service root is contained in a category token."""
    if not service_tokens or not category_tokens:
        return False

    for service_token in service_tokens:
        if len(service_token) < 4:
            continue

        for category_token in category_tokens:
            if service_token == category_token:
                return True

            # Root containment is checked only after both sides have been
            # normalized/stemmed, avoiding raw substring false positives.
            if len(service_token) >= 5 and service_token in category_token:
                return True
            if len(category_token) >= 5 and category_token in service_token:
                return True

    return False


def _token_overlap_ratio(service_tokens: set[str], category_tokens: set[str]) -> float:
    """Coverage of the service's meaningful tokens by category tokens."""
    if not service_tokens or not category_tokens:
        return 0.0

    intersection = service_tokens & category_tokens
    return len(intersection) / len(service_tokens)


def _direct_match_score(service: Any, category: Any) -> float:
    """Return 1.0 for a high-confidence exact/root/token match, else 0."""
    service_tokens = _tokens(service)
    category_tokens = _tokens(category)

    if not service_tokens or not category_tokens:
        return 0.0

    # Direct root match is high confidence.
    if _root_token_match(service_tokens, category_tokens):
        return 1.0

    # More than 80% of the meaningful service tokens must occur in the
    # category. With a one-token service this requires an exact token match.
    if _token_overlap_ratio(service_tokens, category_tokens) > TOKEN_OVERLAP_GATE:
        return 1.0

    return 0.0


def _fuzzy_score(service: Any, category: Any) -> float:
    """SequenceMatcher score over normalized/stemmed token text."""
    service_tokens = _tokens(service)
    category_tokens = _tokens(category)

    if not service_tokens or not category_tokens:
        return 0.0

    left = " ".join(sorted(service_tokens))
    right = " ".join(sorted(category_tokens))
    return SequenceMatcher(None, left, right).ratio()


def _safe_int(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


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


def get_match_ratio(str1: Any, str2: Any) -> float:
    """Backward-compatible public similarity helper."""
    direct = _direct_match_score(str1, str2)
    if direct:
        return direct
    return _fuzzy_score(str1, str2)


def _admin_telegram_id() -> int | None:
    return _safe_int(os.getenv("ADMIN_TELEGRAM_ID", "").strip())


def _already_alerted(
    *,
    admin_telegram_id: int,
    service_name: str,
    application_id: int | None,
) -> bool:
    """Avoid duplicate alerts for the same unresolved service/application."""
    if application_id is None:
        return False

    try:
        import json

        service_filter = json.dumps(
            [{"service_name": str(service_name)}],
            ensure_ascii=False,
        )
        row = platform_db.one(
            """SELECT n.id
               FROM notifications n
               JOIN users u ON u.id=n.user_id
               WHERE (u.id=%s OR u.telegram_id=%s)
                 AND n.kind='catalog_unclassified_service'
                 AND COALESCE(n.data_json->>'application_id','')=%s
                 AND n.data_json->'services' @> %s::jsonb
               LIMIT 1""",
            (
                admin_telegram_id,
                admin_telegram_id,
                str(application_id),
                service_filter,
            ),
        )
        return bool(row)
    except Exception:
        logger.exception("Could not check duplicate catalogue alert.")
        return False


def _resolve_application_id(telegram_id: int | None) -> int | None:
    """Resolve the latest application when the caller has not supplied its ID."""
    if telegram_id is None:
        return None

    try:
        row = platform_db.one(
            """SELECT a.id
               FROM partner_applications a
               JOIN partners p ON p.id=a.partner_id
               WHERE p.user_id=%s
               ORDER BY a.id DESC
               LIMIT 1""",
            (int(telegram_id),),
        )
        return _safe_int(row.get("id")) if row else None
    except Exception:
        logger.exception("Could not resolve application ID for Telegram user %s.", telegram_id)
        return None


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
    if application_id is None:
        application_id = _resolve_application_id(telegram_id)
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
    Conservative two-stage classification against the live catalogue.

    Stage 1:
        Exact/root/token match. A direct match is accepted immediately.

    Stage 2:
        SequenceMatcher. A candidate is accepted only when its score is
        >= FUZZY_CONFIDENCE_GATE. Otherwise the service remains unclassified.

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
            direct_match = None
            direct_score = 0.0

            # -----------------------------
            # Stage 1: exact/root/token
            # -----------------------------
            for row in catalog_rows:
                for category_name in (
                    row.get("category_am"),
                    row.get("category_ru"),
                    row.get("category_en"),
                ):
                    score = _direct_match_score(service, category_name)
                    if score > direct_score:
                        direct_score = score
                        direct_match = row

            if (
                direct_match is not None
                and direct_match.get("category_id") is not None
                and direct_match.get("master_id") is not None
            ):
                final_classified.append(
                    {
                        "service_name": service,
                        "subcategory_id": direct_match["category_id"],
                        "direction_id": direct_match["master_id"],
                    }
                )
                logger.info(
                    "Catalogue direct-match: '%s' -> category=%s direction=%s confidence=1.00",
                    service,
                    direct_match["category_id"],
                    direct_match["master_id"],
                )
                continue

            # -----------------------------
            # Stage 2: strict fuzzy search
            # -----------------------------
            best_match = None
            max_score = 0.0

            for row in catalog_rows:
                current_max = max(
                    _fuzzy_score(service, row.get("category_am", "")),
                    _fuzzy_score(service, row.get("category_ru", "")),
                    _fuzzy_score(service, row.get("category_en", "")),
                )

                if current_max > max_score:
                    max_score = current_max
                    best_match = row

            # IMPORTANT: never select "the best of the bad candidates".
            if (
                best_match is not None
                and max_score >= FUZZY_CONFIDENCE_GATE
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
                    "Catalogue fuzzy-match: '%s' -> category=%s direction=%s score=%.2f",
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
                    "Service '%s' remains unclassified: best fuzzy score %.2f < gate %.2f.",
                    service,
                    max_score,
                    FUZZY_CONFIDENCE_GATE,
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
