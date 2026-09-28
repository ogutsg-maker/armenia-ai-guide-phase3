"""Live catalogue classifier.

Groq is intentionally NOT used here. The AI extraction layer supplies only
plain service names; this module resolves them against the live database
catalogue using deterministic, conservative matching.
"""
from __future__ import annotations

# json is required by the semantic resolver and payload serialization.
import json
import logging
import os
import re
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
    """
    Score a deterministic token/root match without allowing one generic word
    to dominate a multi-word service.

    Example:
        "լվացքի մեքենաներ" vs "Ավտոմեքենայի ախտորոշում"
        -> only "մեքենա" overlaps, so this is NOT a direct match.

        "լվացքի մեքենաներ" vs "Լվացքի մեքենաների վերանորոգում"
        -> both meaningful service tokens are covered, so this is a
           high-confidence direct match.
    """
    service_tokens = _tokens(service)
    category_tokens = _tokens(category)

    if not service_tokens or not category_tokens:
        return 0.0

    # A one-word service must not be accepted merely because its root
    # appears inside a longer catalogue word. This is critical for Armenian:
    # "հարդարում" must not become "Դիմահարդարում".
    if len(service_tokens) == 1:
        token = next(iter(service_tokens))
        return 1.0 if token in category_tokens else 0.0

    # A direct match requires coverage of the service's meaningful tokens.
    # A single shared generic/root token must never be enough for a
    # multi-token service.
    matched_tokens = 0
    for service_token in service_tokens:
        if len(service_token) < 4:
            continue

        matched = False
        for category_token in category_tokens:
            if service_token == category_token:
                matched = True
                break

            if len(service_token) >= 5 and service_token in category_token:
                matched = True
                break

            if len(category_token) >= 5 and category_token in service_token:
                matched = True
                break

        if matched:
            matched_tokens += 1

    coverage = matched_tokens / len(service_tokens)

    if coverage > TOKEN_OVERLAP_GATE:
        return 1.0

    return 0.0


def _fuzzy_score(service: Any, category: Any) -> float:
    """Legacy compatibility hook; fuzzy similarity is no longer a classifier."""
    return 0.0


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
               JOIN users u ON u.telegram_id=n.user_id
               WHERE u.telegram_id=%s
                 AND n.kind='catalog_unclassified_service'
                 AND COALESCE(n.data_json->>'application_id','')=%s
                 AND n.data_json->'services' @> %s::jsonb
               LIMIT 1""",
            (
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


async def _semantic_resolve_unresolved(
    *,
    services: list[str],
    candidate_rows: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Resolve disputed service phrases through one text-only Groq bridge.

    Groq sees only the disputed service names and live catalogue labels.
    Database IDs never enter the prompt and are resolved back to DB rows by
    Python after exact normalized text validation.
    """
    if not services or not candidate_rows:
        return {}

    candidates = []
    seen = set()
    for row in candidate_rows:
        name = str(
            row.get("category_am")
            or row.get("category_ru")
            or row.get("category_en")
            or ""
        ).strip()
        if not name:
            continue
        key = _normalize_text(name)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(name)

    if not candidates:
        return {}

    try:
        from groq import AsyncGroq

        api_key = os.getenv("GROQ_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("GROQ_API_KEY is not configured")

        model = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b").strip() or "openai/gpt-oss-20b"
        prompt = f"""Перед тобой спорные услуги мастера: {json.dumps(services, ensure_ascii=False)}.
А вот текстовый список доступных подкатегорий нашего живого каталога из БД для вычисленного направления: {json.dumps(candidates, ensure_ascii=False)}.

Проведи семантический анализ. Верни JSON, где для каждой услуги мастера сопоставлено СТРОГО ТЕКСТОВОЕ название категории из нашего списка, которая на 100% подходит по смыслу.

Примеры:
- "հարդարում" → "Դասավորում"
- "երեկոյան դիմահարդարում" → "Դիմահարդարում"

Правила:
- Используй только категории из предоставленного списка.
- Возвращай название категории ровно так, как оно написано в списке.
- Не генерируй ID.
- Не придумывай новые категории.
- Не выбирай категорию только из-за общего корня или одного общего слова.
- Если точного смыслового соответствия нет, верни null.

Верни только JSON:
{{"matches":[{{"service":"исходное название услуги","category":"точное название категории или null"}}]}}"""

        client = AsyncGroq(api_key=api_key)
        response = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            reasoning_effort="low",
            max_tokens=max(350, min(900, 180 + len(services) * 120)),
        )
        raw = (response.choices[0].message.content or "{}").strip()
        data = json.loads(raw)
        if not isinstance(data, dict):
            return {}

        logger.info(
            "Catalogue semantic raw result: services=%s result=%s",
            services,
            data,
        )

        allowed = {_normalize_text(x): x for x in candidates}
        result = {}
        raw_matches = data.get("matches")
        if not isinstance(raw_matches, list):
            raw_matches = []

        for item in raw_matches:
            if not isinstance(item, dict):
                continue
            service = str(item.get("service") or "").strip()
            category = str(item.get("category") or "").strip()
            if not service or not category:
                continue

            canonical = allowed.get(_normalize_text(category))
            if not canonical:
                continue

            result[_normalize_text(service)] = {"category_name": canonical}

        logger.info("Catalogue semantic accepted proposals: %s", result)
        return result
    except Exception:
        logger.exception("Semantic catalogue resolution failed.")
        return {}


async def classify_services_batch(
    db,
    extracted_services: list[dict],
    telegram_id: int | None = None,
    application_id: int | None = None,
) -> list[dict[str, Any]]:
    """Resolve services semantically against live catalog labels.

    Groq selects a canonical live catalog label. Python validates that exact
    label and resolves the real category/direction IDs. Fuzzy matching is never
    used as the final classifier.
    """
    services = []
    for item in extracted_services or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        price = item.get("price")
        price_type = str(item.get("price_type") or "fixed").strip().lower()
        if price_type not in {"fixed", "from"}:
            price_type = "fixed"
        services.append({"name": name, "price": price, "price_type": price_type})

    if not services:
        return []

    try:
        catalog_rows = await get_catalog(db)
        if not catalog_rows:
            logger.error("Live catalogue is empty or unavailable.")
            return [
                {"service_name": x["name"], "price": x["price"], "price_type": x["price_type"],
                 "subcategory_id": None, "direction_id": None}
                for x in services
            ]

        semantic = await _semantic_resolve_unresolved(
            services=[x["name"] for x in services],
            candidate_rows=catalog_rows,
        )
        by_name = {}
        for row in catalog_rows:
            for key in ("category_am", "category_ru", "category_en"):
                value = row.get(key)
                if value:
                    by_name[_normalize_text(value)] = row

        result = []
        unresolved = []
        for item in services:
            proposal = semantic.get(_normalize_text(item["name"])) or {}
            row = by_name.get(_normalize_text(proposal.get("category_name") or ""))
            base = {"service_name": item["name"], "price": item["price"], "price_type": item["price_type"]}
            if row and row.get("category_id") is not None and row.get("master_id") is not None:
                result.append({
                    **base,
                    "subcategory_id": row["category_id"],
                    "direction_id": row["master_id"],
                    "subcategory_name_am": row.get("category_am"),
                    "subcategory_name_ru": row.get("category_ru"),
                    "subcategory_name_en": row.get("category_en"),
                    "catalog_match_status": "semantic_confirmed",
                })
            else:
                result.append({
                    **base,
                    "subcategory_id": None,
                    "direction_id": None,
                    "subcategory_name_am": None,
                    "subcategory_name_ru": None,
                    "subcategory_name_en": None,
                    "catalog_match_status": "needs_admin_review",
                })
                unresolved.append((item["name"], 0.0))

        await _notify_unclassified_services(
            services=unresolved,
            telegram_id=telegram_id,
            application_id=application_id,
        )
        return result
    except Exception as exc:
        logger.error("classify_services_batch failed: %s", exc, exc_info=True)
        return [
            {"service_name": x["name"], "price": x["price"], "price_type": x["price_type"],
             "subcategory_id": None, "direction_id": None}
            for x in services
        ]

