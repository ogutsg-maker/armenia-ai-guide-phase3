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
    Deterministic two-pass batch classification against the live catalogue.

    Pass 1 finds the strongest candidate for every service across the full
    catalogue and collects strong master_id votes (score >= 0.70).

    A batch master context is selected only when there are at least two
    strong votes, the winner is strictly ahead of every other master_id,
    and the winner has at least 50% of all strong votes.

    Pass 2 then searches strictly inside that dynamic master_id when a
    context exists. Final acceptance requires confidence >= 0.70 and,
    for non-near-exact matches (< 0.95), a margin of at least 0.10.

    If there is no physical second candidate, second_score is 0.0 because
    there is no competing catalogue row. Failed matches remain unclassified
    and trigger the existing admin notification.
    """
    services = [
        str(service).strip()
        for service in (extracted_services or [])
        if str(service).strip()
    ]

    if not services:
        return []

    safe = [
        {
            "service_name": service,
            "subcategory_id": None,
            "direction_id": None,
        }
        for service in services
    ]

    try:
        catalog_rows = await get_catalog(db)
        if not catalog_rows:
            logger.error("Live catalogue is empty or unavailable.")
            return safe

        def row_score(service: str, row: dict[str, Any]) -> float:
            return max(
                get_match_ratio(service, row.get("category_am", "")),
                get_match_ratio(service, row.get("category_ru", "")),
                get_match_ratio(service, row.get("category_en", "")),
            )

        def ranked_candidates(
            service: str,
            rows: list[dict[str, Any]],
        ) -> list[tuple[float, dict[str, Any]]]:
            ranked = [
                (row_score(service, row), row)
                for row in rows
                if row.get("category_id") is not None
                and row.get("master_id") is not None
            ]
            ranked.sort(key=lambda item: item[0], reverse=True)
            return ranked

        # ==============================================================
        # PASS 1 — collect strong direction votes
        # ==============================================================
        strong_votes: list[int] = []

        for service in services:
            ranked = ranked_candidates(service, catalog_rows)

            if not ranked:
                logger.warning(
                    "Catalogue pass-1: no candidates for service='%s'.",
                    service,
                )
                continue

            best_score, best_row = ranked[0]

            logger.info(
                "Catalogue pass-1: service='%s' best_category=%s "
                "best_category_am='%s' master_id=%s score=%.4f",
                service,
                best_row.get("category_id"),
                best_row.get("category_am"),
                best_row.get("master_id"),
                best_score,
            )

            if best_score >= FUZZY_CONFIDENCE_GATE:
                master_id = _safe_int(best_row.get("master_id"))
                if master_id is not None:
                    strong_votes.append(master_id)

        # ==============================================================
        # MAJORITY VOTE — derive dynamic batch context
        # ==============================================================
        batch_master_context: int | None = None

        if len(strong_votes) >= 2:
            vote_counts: dict[int, int] = {}

            for master_id in strong_votes:
                vote_counts[master_id] = vote_counts.get(master_id, 0) + 1

            ordered_votes = sorted(
                vote_counts.items(),
                key=lambda item: item[1],
                reverse=True,
            )

            winner_id, winner_votes = ordered_votes[0]
            second_votes = ordered_votes[1][1] if len(ordered_votes) > 1 else 0

            if (
                winner_votes > second_votes
                and winner_votes / len(strong_votes) >= 0.50
            ):
                batch_master_context = winner_id

            logger.info(
                "Catalogue majority vote: votes=%s winner=%s "
                "winner_votes=%s total_strong_votes=%s context=%s",
                vote_counts,
                winner_id,
                winner_votes,
                len(strong_votes),
                batch_master_context,
            )
        else:
            logger.info(
                "Catalogue majority vote: insufficient strong votes=%s; "
                "batch_master_context=None",
                strong_votes,
            )

        # ==============================================================
        # PASS 2 — strict dynamic context filtering
        # ==============================================================
        filtered_rows = (
            [
                row
                for row in catalog_rows
                if _safe_int(row.get("master_id")) == batch_master_context
            ]
            if batch_master_context is not None
            else catalog_rows
        )

        logger.info(
            "Catalogue pass-2 context=%s candidate_rows=%s",
            batch_master_context,
            len(filtered_rows),
        )

        final_classified: list[dict[str, Any]] = []
        unresolved: list[tuple[str, float]] = []

        for service in services:
            ranked = ranked_candidates(service, filtered_rows)

            if ranked:
                best_score, best_row = ranked[0]
                # No physical second candidate means no competing candidate.
                second_score = ranked[1][0] if len(ranked) > 1 else 0.0
            else:
                best_score = 0.0
                second_score = 0.0
                best_row = None

            margin = best_score - second_score

            confidence_ok = best_score >= FUZZY_CONFIDENCE_GATE

            # Near-exact matches do not need a margin check.
            margin_required = best_score < 0.95
            margin_ok = (not margin_required) or (margin >= 0.10)

            accepted = (
                best_row is not None
                and best_row.get("category_id") is not None
                and best_row.get("master_id") is not None
                and confidence_ok
                and margin_ok
            )

            logger.info(
                "Catalogue pass-2: service='%s' best_category=%s "
                "best_category_am='%s' direction=%s best=%.4f second=%.4f "
                "margin=%.4f confidence_ok=%s margin_ok=%s context=%s",
                service,
                best_row.get("category_id") if best_row else None,
                best_row.get("category_am") if best_row else None,
                best_row.get("master_id") if best_row else None,
                best_score,
                second_score,
                margin,
                confidence_ok,
                margin_ok,
                batch_master_context,
            )

            if accepted:
                final_classified.append(
                    {
                        # Keep the original extracted phrase.
                        "service_name": service,
                        # Resolve the actual live catalogue row.
                        "subcategory_id": best_row["category_id"],
                        "direction_id": best_row["master_id"],
                        "subcategory_name_am": best_row.get("category_am"),
                        "subcategory_name_ru": best_row.get("category_ru"),
                        "subcategory_name_en": best_row.get("category_en"),
                    }
                )

                logger.info(
                    "Catalogue accepted: '%s' -> '%s' "
                    "(category=%s direction=%s score=%.4f margin=%.4f)",
                    service,
                    best_row.get("category_am"),
                    best_row.get("category_id"),
                    best_row.get("master_id"),
                    best_score,
                    margin,
                )
            else:
                unresolved.append((service, best_score))

                # Preserve the dynamic majority direction as context, but
                # never invent a subcategory_id when gates fail.
                final_classified.append(
                    {
                        "service_name": service,
                        "subcategory_id": None,
                        "direction_id": batch_master_context,
                        "subcategory_name_am": None,
                        "subcategory_name_ru": None,
                        "subcategory_name_en": None,
                    }
                )

                logger.warning(
                    "Catalogue rejected: '%s' best=%.4f second=%.4f "
                    "margin=%.4f confidence_ok=%s margin_ok=%s context=%s",
                    service,
                    best_score,
                    second_score,
                    margin,
                    confidence_ok,
                    margin_ok,
                    batch_master_context,
                )

        await _notify_unclassified_services(
            services=unresolved,
            telegram_id=telegram_id,
            application_id=application_id,
        )

        return final_classified

    except Exception as exc:
        logger.error(
            "classify_services_batch failed: %s",
            exc,
            exc_info=True,
        )
        return safe
