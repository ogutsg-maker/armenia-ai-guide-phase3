"""Armenia AI Guide Data Core.

Single application-owned gateway between AI/application layers and PostgreSQL.
The existing Supabase/PostgreSQL schema remains the source of truth and is not
recreated by this module.

Rules:
- AI never receives SQL access.
- AI Context and AI Tools call Data Core only.
- Authorization/ownership checks belong here, not in prompts.
- New schema changes, when genuinely required, must be migrations; this layer
  does not create or reset the database.
"""
from __future__ import annotations

from typing import Any, Iterable
import json
import secrets
import os

import platform_db


# ---------------------------------------------------------------------------
# Low-level DB gateway
# ---------------------------------------------------------------------------
# These functions are intentionally the only DB primitives exported to the
# application architecture. Domain modules should prefer the named methods
# below, but the primitives keep the migration backwards-compatible while
# existing modules are moved incrementally.

def one(sql: str, params: Iterable[Any] = ()):
    return platform_db.one(sql, tuple(params))


def rows(sql: str, params: Iterable[Any] = ()):
    return platform_db.rows(sql, tuple(params))


def execute(sql: str, params: Iterable[Any] = (), returning: bool = False):
    return platform_db.execute(sql, tuple(params), returning)


def json_dump(value: Any) -> str:
    return platform_db.json_dump(value)


# ---------------------------------------------------------------------------
# Users / AI session identity
# ---------------------------------------------------------------------------

def get_user_by_telegram_id(telegram_id: int):
    return one(
        """
        SELECT *
        FROM users
        WHERE telegram_id=%s
        LIMIT 1
        """,
        (int(telegram_id),),
    )


def ensure_user_by_telegram_id(telegram_id: int):
    user = get_user_by_telegram_id(int(telegram_id))
    if user:
        return user
    return one(
        """
        INSERT INTO users (telegram_id)
        VALUES (%s)
        RETURNING *
        """,
        (int(telegram_id),),
    )



# ---------------------------------------------------------------------------
# Partner companies
# ---------------------------------------------------------------------------

def list_companies(partner_id: int):
    """Return companies owned by a partner for session restoration/cabinet."""
    return rows(
        """
        SELECT id, partner_id, name, description, phone, status, is_default
        FROM partner_businesses
        WHERE partner_id=%s
          AND status <> 'archived'
        ORDER BY is_default DESC, id
        """,
        (int(partner_id),),
    )


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------

def catalog_tree():
    return platform_db.catalog_tree()


def approved_partner_categories(partner_id: int):
    return platform_db.approved_partner_categories(int(partner_id))


def search_catalog(query: str = "", master_category_id: int | None = None, limit: int = 100):
    query = str(query or "").strip()
    params: list[Any] = []
    where = ["c.is_active=TRUE", "m.is_active=TRUE"]
    if query:
        where.append("""(
            c.name_am ILIKE %s OR c.name_ru ILIKE %s OR c.name_en ILIKE %s
            OR c.slug ILIKE %s OR m.name_am ILIKE %s OR m.name_ru ILIKE %s
            OR m.name_en ILIKE %s
        )""")
        params.extend([f"%{query}%"] * 7)
    if master_category_id is not None:
        where.append("c.master_category_id=%s")
        params.append(int(master_category_id))
    params.append(max(1, min(int(limit or 100), 500)))
    return rows(
        """SELECT c.id,c.master_category_id,c.name_am,c.name_ru,c.name_en,c.slug,
                  m.name_am AS master_name_am,m.name_ru AS master_name_ru,
                  m.name_en AS master_name_en
           FROM categories c
           JOIN master_categories m ON m.id=c.master_category_id
           WHERE """ + " AND ".join(where) + " ORDER BY c.id LIMIT %s",
        tuple(params),
    )


def _catalog_text(value: Any) -> str:
    """Normalize multilingual catalog text for backend matching."""
    import re
    text = str(value or "").casefold().strip()
    text = text.replace("ё", "е")
    text = re.sub(r"[^\w\s\u0530-\u058F\u0400-\u04FF-]+", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _catalog_tokens(value: Any) -> set[str]:
    return {x for x in _catalog_text(value).split() if len(x) >= 2}


def _catalog_match_score(query: str, candidate: str) -> float:
    """Conservative exact/phrase/token-root match for service categories."""
    q = _catalog_text(query)
    c = _catalog_text(candidate)
    if not q or not c:
        return 0.0
    if q == c:
        return 1.0

    def stem(token: str) -> str:
        suffixes = (
            "ներով", "ներին", "ներից", "ների", "երով", "ություն", "ությունների",
            "ության", "ություններ", "ական", "ային", "ը", "ի", "ին", "ից", "ով", "ներ",
            "ами", "ями", "ого", "ему", "ом", "ов", "ы", "и", "а", "я", "у", "ю", "е",
        )
        value = token
        for suffix in sorted(suffixes, key=len, reverse=True):
            if len(value) > len(suffix) + 2 and value.endswith(suffix):
                return value[:-len(suffix)]
        return value

    qt = _catalog_tokens(q)
    ct = _catalog_tokens(c)
    qs = {stem(t) for t in qt}
    cs = {stem(t) for t in ct}

    if qs == cs:
        return 0.96

    # For multi-word services, shared generic words such as «մազերի» are
    # insufficient by themselves. Require the action/service root to agree.
    if len(qs) >= 2 and len(cs) >= 2:
        specific_q = {t for t in qs if len(t) >= 4}
        specific_c = {t for t in cs if len(t) >= 4}
        if not (specific_q & specific_c):
            return 0.0

    overlap = len(qs & cs) / max(1, len(qs | cs))
    containment = 1.0 if q in c or c in q else 0.0

    # A short user phrase can legitimately omit the service action:
    # "холодильников" -> "Ремонт холодильников".
    # The caller handles the important safety decision dynamically by checking
    # whether the meaningful query root is unique in the LIVE catalogue.
    # This function therefore only reports lexical evidence; it never invents
    # a category on its own.
    return min(1.0, 0.78 * overlap + 0.22 * containment)

def _catalog_rankings(query: str, catalog: list[dict[str, Any]]) -> list[tuple[float, dict[str, Any]]]:
    """Rank live catalog entries conservatively; irrelevant entries stay low."""
    ranked: list[tuple[float, dict[str, Any]]] = []
    for cat in catalog:
        names = (cat.get("name_am"), cat.get("name_ru"), cat.get("name_en"), cat.get("slug"))
        score = max(
            (_catalog_match_score(query, name) for name in names if name),
            default=0.0,
        )
        ranked.append((float(score), cat))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked

def resolve_catalog_services(services: list[dict[str, Any]], limit: int = 500) -> list[dict[str, Any]]:
    """Resolve services using the live catalogue plus dynamic lexical evidence.

    For abbreviated phrases such as "холодильников", a single meaningful
    catalogue root is enough only when that root is unique across the live
    catalogue. This avoids hardcoded dictionaries while preventing generic
    words from becoming false categories.
    """
    catalog = search_catalog(limit=max(100, min(int(limit or 500), 500)))

    # Dynamic inverse-frequency index over the live catalogue. No category
    # names/IDs are hardcoded here.
    root_frequency: dict[str, int] = {}
    for cat in catalog:
        cat_roots = set()
        for field in ("name_am", "name_ru", "name_en", "slug"):
            for token in _catalog_tokens(cat.get(field)):
                root = token
                suffixes = (
                    "ներով", "ներին", "ներից", "ների", "երով", "ություն",
                    "ությունների", "ության", "ություններ", "ական", "ային",
                    "ը", "ի", "ին", "ից", "ով", "ներ",
                    "ами", "ями", "ого", "ему", "ом", "ов", "ы", "и",
                    "а", "я", "у", "ю", "е",
                )
                for suffix in sorted(suffixes, key=len, reverse=True):
                    if len(root) > len(suffix) + 2 and root.endswith(suffix):
                        root = root[:-len(suffix)]
                        break
                if len(root) >= 4:
                    cat_roots.add(root)
        for root in cat_roots:
            root_frequency[root] = root_frequency.get(root, 0) + 1
    resolved: list[dict[str, Any]] = []
    for index, service in enumerate(services):
        item = dict(service)
        name = str(item.get("name") or item.get("service_name") or "").strip()
        item["service_id"] = index + 1
        if not name:
            item.update({
                "category_id": None,
                "master_category_id": None,
                "catalog_match_score": 0.0,
                "catalog_second_score": 0.0,
                "catalog_match_margin": 0.0,
                "catalog_match_status": "unclassified",
                "catalog_options": [],
            })
            resolved.append(item)
            continue

        candidates = _catalog_rankings(name, catalog)

        # Abbreviated service names are common in partner speech. If the
        # meaningful root(s) from the user phrase occur in exactly one live
        # category, promote that candidate to high-confidence lexical match.
        # This is data-driven and therefore works for new catalogue entries
        # without adding Python dictionaries.
        query_roots = set()
        for token in _catalog_tokens(name):
            root = token
            suffixes = (
                "ами", "ями", "ого", "ему", "ом", "ов", "ы", "и", "а", "я",
                "у", "ю", "е", "ներով", "ներին", "ներից", "ների", "երով",
                "ություն", "ությունների", "ության", "ություններ", "ական",
                "ային", "ը", "ի", "ին", "ից", "ով", "ներ",
            )
            for suffix in sorted(suffixes, key=len, reverse=True):
                if len(root) > len(suffix) + 2 and root.endswith(suffix):
                    root = root[:-len(suffix)]
                    break
            if len(root) >= 4:
                query_roots.add(root)

        unique_candidates = []
        if query_roots:
            for idx, (score, cat) in enumerate(candidates):
                cat_roots = set()
                for field in ("name_am", "name_ru", "name_en", "slug"):
                    for token in _catalog_tokens(cat.get(field)):
                        root = token
                        suffixes = (
                            "ами", "ями", "ого", "ему", "ом", "ов", "ы", "и",
                            "а", "я", "у", "ю", "е", "ներով", "ներին", "ներից",
                            "ների", "երով", "ություն", "ությունների", "ության",
                            "ություններ", "ական", "ային", "ը", "ի", "ին", "ից",
                            "ով", "ներ",
                        )
                        for suffix in sorted(suffixes, key=len, reverse=True):
                            if len(root) > len(suffix) + 2 and root.endswith(suffix):
                                root = root[:-len(suffix)]
                                break
                        if len(root) >= 4:
                            cat_roots.add(root)
                shared_unique = [
                    root for root in query_roots
                    if root in cat_roots and root_frequency.get(root) == 1
                ]
                if shared_unique:
                    unique_candidates.append((idx, cat, shared_unique))

        if unique_candidates and len(unique_candidates) == 1:
            idx, unique_cat, _ = unique_candidates[0]
            candidates = [(1.0, unique_cat)] + [
                pair for n, pair in enumerate(candidates)
                if n != idx
            ]

        catalog_name = str(item.get("catalog_name") or "").strip()
        if catalog_name:
            exact = next(
                (cat for _, cat in candidates
                 if any(_catalog_text(catalog_name) == _catalog_text(cat.get(k))
                        for k in ("name_am", "name_ru", "name_en", "slug") if cat.get(k))),
                None,
            )
            if exact:
                candidates = [(1.0, exact)] + [
                    (score, cat) for score, cat in candidates
                    if int(cat.get("id") or 0) != int(exact.get("id") or 0)
                ]

        best_score, best = candidates[0] if candidates else (0.0, None)
        second_score = candidates[1][0] if len(candidates) > 1 else 0.0
        margin = float(best_score) - float(second_score)
        item["catalog_match_score"] = round(float(best_score), 4)
        item["catalog_second_score"] = round(float(second_score), 4)
        item["catalog_match_margin"] = round(float(margin), 4)

        if best and best_score >= 0.70 and margin >= 0.10:
            item.update({
                "category_id": int(best["id"]),
                "master_category_id": int(best["master_category_id"]),
                "category_name_am": best.get("name_am"),
                "category_name_ru": best.get("name_ru"),
                "category_name_en": best.get("name_en"),
                "master_name_am": best.get("master_name_am"),
                "master_name_ru": best.get("master_name_ru"),
                "master_name_en": best.get("master_name_en"),
                "catalog_match_status": "matched",
                "catalog_options": [],
            })
        elif best and best_score >= 0.70:
            options = []
            for score, cat in candidates[:8]:
                if score < 0.60 or (best_score - score) > 0.15:
                    continue
                options.append({
                    "category_id": int(cat["id"]),
                    "category_name_am": cat.get("name_am"),
                    "category_name_ru": cat.get("name_ru"),
                    "category_name_en": cat.get("name_en"),
                    "score": round(float(score), 4),
                })
                if len(options) >= 5:
                    break
            if len(options) >= 2:
                item.update({
                    "category_id": None,
                    "master_category_id": None,
                    "catalog_match_status": "ambiguous",
                    "catalog_options": options,
                })
            else:
                item.update({
                    "category_id": None,
                    "master_category_id": None,
                    "catalog_match_status": "unclassified",
                    "catalog_options": [],
                })
        else:
            item.update({
                "category_id": None,
                "master_category_id": None,
                "catalog_match_status": "unclassified",
                "catalog_options": [],
            })
        resolved.append(item)
    return resolved


def check_live_category_exists(category_id: int) -> bool:
    """Return whether a category currently exists and is active in the live catalogue."""
    try:
        cid = int(category_id)
    except (TypeError, ValueError):
        return False
    if cid <= 0:
        return False
    category = get_catalog_category(cid)
    return bool(category and category.get("is_active"))


def init_bulk_catalog_resolution(*, application_id: int, actor_user_id: int) -> dict[str, Any]:
    """Build the single pending_action contract for bulk category resolution."""
    if not is_admin(int(actor_user_id)):
        raise PermissionError("admin_required")
    app = get_application_full(int(application_id))
    if not app:
        raise ValueError("application_not_found")
    services = _application_payload_services(app)
    if not services:
        raise ValueError("application_services_missing")

    resolved = resolve_catalog_services(services, limit=500)
    pending = {
        "type": "bulk_resolve_categories",
        "application_id": int(application_id),
        "resolved_changes": [],
        "unresolved_ambiguities": [],
        "unclassified_services": [],
    }

    for item in resolved:
        service_id = int(item.get("service_id") or 0)
        service_name = str(item.get("name") or item.get("service_name") or "").strip()
        if item.get("catalog_match_status") == "matched" and item.get("category_id"):
            pending["resolved_changes"].append({
                "service_id": service_id,
                "service_name": service_name,
                "category_id": int(item["category_id"]),
                "category_name_am": item.get("category_name_am"),
            })
        elif item.get("catalog_match_status") == "ambiguous":
            pending["unresolved_ambiguities"].append({
                "service_id": service_id,
                "service_name": service_name,
                "options": [
                    {
                        "category_id": int(opt["category_id"]),
                        "category_name_am": opt.get("category_name_am"),
                    }
                    for opt in (item.get("catalog_options") or [])
                ],
            })
        else:
            pending["unclassified_services"].append({
                "service_id": service_id,
                "service_name": service_name,
                "reason": "no_reliable_match",
            })

    return {
        "ok": True,
        "pending_action": pending,
        "status": "needs_selection" if pending["unresolved_ambiguities"] else "ready_for_preview",
    }


def apply_pending_category_resolution(*, pending_action: dict[str, Any],
                                      confirmation_token: str,
                                      actor_user_id: int) -> dict[str, Any]:
    """Apply only resolved_changes from a pending category action."""
    if not is_admin(int(actor_user_id)):
        raise PermissionError("admin_required")
    if not confirmation_token or len(str(confirmation_token)) < 20:
        raise PermissionError("invalid_confirmation_token")
    if not isinstance(pending_action, dict) or pending_action.get("type") != "bulk_resolve_categories":
        raise ValueError("invalid_pending_action")
    if pending_action.get("unresolved_ambiguities"):
        raise ValueError("unresolved_ambiguities_remaining")

    mappings = []
    for change in pending_action.get("resolved_changes") or []:
        category_id = int(change.get("category_id") or 0)
        if not check_live_category_exists(category_id):
            raise ValueError("catalog_category_invalid")
        service_id = int(change.get("service_id") or 0)
        if service_id <= 0:
            raise ValueError("invalid_service_id")
        mappings.append({
            "service_index": service_id - 1,
            "category_id": category_id,
            "master_category_id": int((get_catalog_category(category_id) or {}).get("master_category_id") or 0),
        })

    if not mappings:
        return {
            "ok": True,
            "application_id": int(pending_action["application_id"]),
            "changes": [],
            "status": "no_catalog_changes",
            "message": "no_classified_services_to_apply",
        }

    return apply_catalog_resolution(
        application_id=int(pending_action["application_id"]),
        mappings=mappings,
        confirmation_token=str(confirmation_token),
        actor_user_id=int(actor_user_id),
    )

def admin_approve_application_document(*, application_id: int, actor_user_id: int) -> dict[str, Any]:
    """Approve the pending verification document attached to one application."""
    if not is_admin(int(actor_user_id)):
        raise PermissionError("admin_required")
    app = get_application_full(int(application_id))
    if not app:
        raise ValueError("application_not_found")
    status = str(app.get("status") or "").strip().lower()
    if status in {"approved", "rejected", "cancelled"}:
        raise ValueError("application_finalized")

    # Prefer the document explicitly attached to this application. Legacy rows
    # may have no application_id, so fall back to the company's current pending
    # document only when it is unambiguous.
    docs = rows(
        """SELECT id,status,application_id,business_id,partner_id,created_at
           FROM partner_verification_documents
           WHERE partner_id=%s
             AND (application_id=%s OR (application_id IS NULL AND business_id=%s))
           ORDER BY CASE WHEN application_id=%s THEN 0 ELSE 1 END, created_at DESC, id DESC
           LIMIT 10""",
        (
            int(app.get("partner_id") or 0),
            int(application_id),
            int(app.get("business_id") or 0),
            int(application_id),
        ),
    )
    if not docs:
        raise ValueError("verification_document_not_found")

    pending = [x for x in docs if str(x.get("status") or "").lower() == "pending"]
    doc = pending[0] if pending else docs[0]
    doc_status = str(doc.get("status") or "").lower()
    if doc_status == "approved":
        return {
            "ok": True,
            "application_id": int(application_id),
            "document_id": int(doc["id"]),
            "document_status": "approved",
            "status": status,
            "already_approved": True,
        }
    if doc_status != "pending":
        raise ValueError("verification_document_not_pending")

    updated = execute(
        """UPDATE partner_verification_documents
           SET status='approved', rejection_reason=NULL,
               reviewed_by=%s, reviewed_at=NOW()
           WHERE id=%s AND status='pending'
           RETURNING id,status,reviewed_at""",
        (int(actor_user_id), int(doc["id"])),
        returning=True,
    )
    if not updated:
        # Another admin may have approved it between the read and write.
        fresh = one(
            "SELECT id,status FROM partner_verification_documents WHERE id=%s",
            (int(doc["id"]),),
        )
        if fresh and str(fresh.get("status") or "").lower() == "approved":
            return {
                "ok": True,
                "application_id": int(application_id),
                "document_id": int(doc["id"]),
                "document_status": "approved",
                "status": status,
                "already_approved": True,
            }
        raise RuntimeError("document_approval_update_failed")

    # Keep the application in the moderation queue. The application itself is
    # NOT approved here; that remains a separate confirmed admin action.
    if status == "document_under_review":
        execute(
            """UPDATE partner_applications
               SET status='pending_admin', updated_at=NOW()
               WHERE id=%s AND status='document_under_review'""",
            (int(application_id),),
            returning=False,
        )

    return {
        "ok": True,
        "application_id": int(application_id),
        "document_id": int(doc["id"]),
        "document_status": "approved",
        "status": "pending_admin" if status == "document_under_review" else status,
    }


def _application_payload_services(application: dict[str, Any]) -> list[dict[str, Any]]:
    payload = application.get("payload_json") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {}
    if not isinstance(payload, dict):
        return []
    raw = payload.get("services") or []
    return [dict(x) for x in raw if isinstance(x, dict)]


def application_service_items(application_id: int) -> list[dict[str, Any]]:
    """Return backend-indexed application services with current live catalog labels."""
    app = get_application_full(int(application_id))
    if not app:
        return []
    result = []
    for index, service in enumerate(_application_payload_services(app)):
        category_id = service.get("category_id")
        category = get_catalog_category(int(category_id)) if category_id not in (None, "") else None
        result.append({
            "service_index": index,
            "service_id": index + 1,
            "name": str(service.get("name") or service.get("service_name") or "").strip(),
            "price": service.get("price"),
            "price_type": service.get("price_type"),
            "category_id": category_id,
            "master_category_id": service.get("master_category_id"),
            "category_name_am": (category or {}).get("name_am") or service.get("category_name_am"),
            "category_name_ru": (category or {}).get("name_ru") or service.get("category_name_ru"),
            "category_name_en": (category or {}).get("name_en") or service.get("category_name_en"),
        })
    return result


def admin_catalog_candidates(*, application_id: int, service_indexes: list[int] | None = None,
                              limit_per_service: int = 10, actor_user_id: int) -> dict[str, Any]:
    """Return only meaningful live-catalog candidates for human review."""
    if not is_admin(int(actor_user_id)):
        raise PermissionError("admin_required")
    app = get_application_full(int(application_id))
    if not app:
        raise ValueError("application_not_found")

    services = application_service_items(int(application_id))
    wanted = {int(x) for x in service_indexes} if isinstance(service_indexes, list) and service_indexes else None
    catalog = search_catalog(limit=500)
    per_service = max(1, min(int(limit_per_service or 10), 5))
    result = []

    for service in services:
        idx = int(service["service_index"])
        if wanted is not None and idx not in wanted:
            continue
        query = str(service.get("name") or "").strip()
        ranked = _catalog_rankings(query, catalog)
        best_score = ranked[0][0] if ranked else 0.0
        meaningful = [
            (score, cat)
            for score, cat in ranked
            if score >= 0.60 and (best_score - score) <= 0.15
        ]
        result.append({
            "service_index": idx,
            "service_id": int(service["service_id"]),
            "service_name": query,
            "current_category": service.get("category_name_am"),
            "candidates": [{
                "catalog_name": cat.get("name_am") or cat.get("name_ru") or cat.get("name_en"),
                "name_am": cat.get("name_am"),
                "name_ru": cat.get("name_ru"),
                "name_en": cat.get("name_en"),
                "master_name_am": cat.get("master_name_am"),
                "master_name_ru": cat.get("master_name_ru"),
                "master_name_en": cat.get("master_name_en"),
                "category_id": int(cat.get("id")),
                "score": round(float(score), 4),
            } for score, cat in meaningful[:per_service]],
        })
    return {"application_id": int(application_id), "items": result}


def prepare_application_service_price_update(*, application_id: int, service_index: int,
                                             price: float, actor_user_id: int) -> dict[str, Any]:
    """Validate an application service price change and return a confirmation action."""
    if not is_admin(int(actor_user_id)):
        raise PermissionError("admin_required")
    if price < 0:
        raise ValueError("invalid_service_price")
    app = get_application_full(int(application_id))
    if not app:
        raise ValueError("application_not_found")
    services = application_service_items(int(application_id))
    service = next((x for x in services if int(x["service_index"]) == int(service_index)), None)
    if not service:
        raise ValueError("service_not_found")
    token = secrets.token_urlsafe(24)
    return {
        "ok": True,
        "status": "awaiting_user_confirmation",
        "requires_confirmation": True,
        "confirmation_token": token,
        "action": {
            "name": "admin_apply_application_service_price",
            "args": {
                "application_id": int(application_id),
                "service_index": int(service_index),
                "price": float(price),
                "confirmation_token": token,
            },
        },
        "summary": (
            f"Փոխել հայտ #{int(application_id)}-ի «{service['name']}» ծառայության "
            f"գինը {service.get('price') or 0} ֏ → {float(price):g} ֏?"
        ),
    }


def apply_application_service_price(*, application_id: int, service_index: int,
                                     price: float, confirmation_token: str,
                                     actor_user_id: int) -> dict[str, Any]:
    """Apply a previously previewed service price change atomically."""
    if not is_admin(int(actor_user_id)):
        raise PermissionError("admin_required")
    if not confirmation_token or len(confirmation_token) < 20:
        raise PermissionError("invalid_confirmation_token")
    if price < 0:
        raise ValueError("invalid_service_price")
    app = get_application_full(int(application_id))
    if not app:
        raise ValueError("application_not_found")
    services = _application_payload_services(app)
    index = int(service_index)
    if index < 0 or index >= len(services):
        raise ValueError("service_not_found")
    updated = [dict(x) for x in services]
    old_price = updated[index].get("price")
    updated[index]["price"] = float(price)
    payload = app.get("payload_json") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload = dict(payload)
    payload["services"] = updated
    row = execute(
        "UPDATE partner_applications SET payload_json=%s, updated_at=NOW() "
        "WHERE id=%s RETURNING *",
        (json_dump(payload), int(application_id)),
        returning=True,
    )
    if not row:
        raise RuntimeError("application_service_price_update_failed")
    return {
        "ok": True,
        "application_id": int(application_id),
        "service_index": index,
        "service_name": str(updated[index].get("name") or updated[index].get("service_name") or ""),
        "old_price": old_price,
        "price": float(price),
        "status": row.get("status"),
        "message": "application_service_price_updated",
    }


def prepare_bulk_catalog_resolution(*, application_id: int, resolve_all: bool = True,
                                      actor_user_id: int) -> dict[str, Any]:
    """Compatibility entry point; canonical state is pending_action only."""
    if not resolve_all:
        raise ValueError("bulk_resolution_requires_resolve_all")
    return init_bulk_catalog_resolution(
        application_id=int(application_id),
        actor_user_id=int(actor_user_id),
    )


def prepare_catalog_resolution(*, application_id: int, mappings: list[dict[str, Any]],
                               actor_user_id: int) -> dict[str, Any]:
    """Validate proposed mappings and prepare an explicit confirmation action."""
    if not is_admin(int(actor_user_id)):
        raise PermissionError("admin_required")
    app = get_application_full(int(application_id))
    if not app:
        raise ValueError("application_not_found")

    services = {int(x["service_index"]): x for x in application_service_items(int(application_id))}
    catalog = search_catalog(limit=500)
    normalized_catalog = {}
    for cat in catalog:
        for key in ("name_am", "name_ru", "name_en", "slug"):
            value = cat.get(key)
            if value:
                normalized_catalog[_catalog_text(value)] = cat

    validated = []
    seen = set()
    for raw in mappings:
        if not isinstance(raw, dict):
            raise ValueError("invalid_catalog_mapping")
        try:
            index = int(raw.get("service_index"))
        except (TypeError, ValueError):
            raise ValueError("invalid_catalog_mapping")
        catalog_name = str(raw.get("catalog_name") or "").strip()
        if index in seen or index not in services or not catalog_name:
            raise ValueError("invalid_catalog_mapping")
        seen.add(index)
        cat = normalized_catalog.get(_catalog_text(catalog_name))
        if not cat or not cat.get("id") or not cat.get("master_category_id"):
            raise ValueError("catalog_name_not_in_live_catalog")
        validated.append({
            "service_index": index,
            "service_name": services[index]["name"],
            "category_id": int(cat["id"]),
            "master_category_id": int(cat["master_category_id"]),
            "category_name_am": cat.get("name_am"),
            "category_name_ru": cat.get("name_ru"),
            "category_name_en": cat.get("name_en"),
        })

    if not validated:
        raise ValueError("catalog_mapping_required")

    token = secrets.token_urlsafe(24)
    summary_lines = [f"Изменить каталог заявки #{int(application_id)} ({app.get('business_name') or '—'}):"]
    for row in validated:
        summary_lines.append(
            f"• {row['service_name']} → {row.get('category_name_am') or row.get('category_name_ru')} (ID {row['category_id']})"
        )
    return {
        "ok": True,
        "status": "awaiting_user_confirmation",
        "requires_confirmation": True,
        "confirmation_token": token,
        "action": {
            "name": "admin_apply_catalog_resolution",
            "args": {
                "application_id": int(application_id),
                "confirmation_token": token,
                "mappings": [{
                    "service_index": row["service_index"],
                    "category_id": row["category_id"],
                    "master_category_id": row["master_category_id"],
                } for row in validated],
            },
        },
        "summary": "\\n".join(summary_lines),
        "changes": validated,
    }


def apply_catalog_resolution(*, application_id: int, mappings: list[dict[str, Any]],
                             confirmation_token: str, actor_user_id: int) -> dict[str, Any]:
    """Apply a previously previewed mapping in one atomic application update."""
    if not is_admin(int(actor_user_id)):
        raise PermissionError("admin_required")
    if not confirmation_token or len(confirmation_token) < 20:
        raise PermissionError("invalid_confirmation_token")
    app = get_application_full(int(application_id))
    if not app:
        raise ValueError("application_not_found")
    current_services = _application_payload_services(app)
    if not current_services:
        raise ValueError("application_services_missing")

    updated = [dict(x) for x in current_services]
    applied = []
    for raw in mappings:
        index = int(raw.get("service_index"))
        if index < 0 or index >= len(updated):
            raise ValueError("invalid_catalog_mapping")
        category_id = int(raw.get("category_id"))
        master_id = int(raw.get("master_category_id"))
        cat = get_catalog_category(category_id)
        if not cat or not cat.get("is_active") or int(cat.get("master_category_id") or 0) != master_id:
            raise ValueError("catalog_category_invalid")
        updated[index].update({
            "category_id": category_id,
            "master_category_id": master_id,
            "catalog_match_status": "admin_confirmed",
            "category_name_am": cat.get("name_am"),
            "category_name_ru": cat.get("name_ru"),
            "category_name_en": cat.get("name_en"),
            "master_name_am": cat.get("master_name_am"),
            "master_name_ru": cat.get("master_name_ru"),
            "master_name_en": cat.get("master_name_en"),
        })
        applied.append({
            "service_index": index,
            "service_name": str(updated[index].get("name") or updated[index].get("service_name") or ""),
            "category_id": category_id,
            "master_category_id": master_id,
            "category_name_am": cat.get("name_am"),
            "category_name_ru": cat.get("name_ru"),
        })

    payload = app.get("payload_json") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload = dict(payload)
    payload["services"] = updated

    first = updated[0]
    first_cat = get_catalog_category(int(first["category_id"])) if first.get("category_id") else None
    fields = ["payload_json=%s", "updated_at=NOW()"]
    params: list[Any] = [json_dump(payload)]
    if first_cat:
        fields.extend(["direction_name=%s", "master_category_id=%s", "subcategory_name=%s", "category_id=%s"])
        params.extend([
            first_cat.get("master_name_am") or first_cat.get("master_name_ru"),
            int(first_cat["master_category_id"]),
            first_cat.get("name_am") or first_cat.get("name_ru"),
            int(first_cat["id"]),
        ])
    params.append(int(application_id))
    row = execute(
        "UPDATE partner_applications SET " + ",".join(fields) + " WHERE id=%s RETURNING *",
        tuple(params), returning=True,
    )
    if not row:
        raise RuntimeError("catalog_resolution_update_failed")
    return {"ok": True, "application_id": int(application_id), "changes": applied,
            "status": row.get("status"), "message": "catalog_resolution_saved"}


def active_directions():
    return rows(
        """SELECT id,name_am,name_ru,name_en,slug,is_active
           FROM master_categories WHERE is_active=TRUE ORDER BY id"""
    )


def get_catalog_category(category_id: int):
    return one(
        """SELECT c.id,c.master_category_id,c.name_am,c.name_ru,c.name_en,c.slug,c.is_active,
                  m.name_am AS master_name_am,m.name_ru AS master_name_ru,m.name_en AS master_name_en
           FROM categories c
           LEFT JOIN master_categories m ON m.id=c.master_category_id
           WHERE c.id=%s""",
        (int(category_id),),
    )


def get_user(user_id: int):
    return one(
        """SELECT telegram_id,username,full_name,role,lang,phone,is_verified,is_frozen,balance,rating_avg,rating_count
           FROM users WHERE telegram_id=%s""",
        (int(user_id),),
    )


def is_admin(telegram_id: int) -> bool:
    """Canonical backend authorization check for administrative operations."""
    try:
        actor_id = int(telegram_id)
    except (TypeError, ValueError):
        return False

    user = get_user(actor_id) or {}
    role = str(user.get("role") or "").strip().lower()
    if role == "admin":
        return True

    configured_raw = os.getenv("ADMIN_TELEGRAM_ID", "").strip()
    if not configured_raw:
        return False
    try:
        return actor_id == int(configured_raw)
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Partners / companies / services
# ---------------------------------------------------------------------------

def get_partner(partner_id: int):
    return platform_db.get_partner(int(partner_id))


def get_partner_by_user(user_id: int):
    return platform_db.get_partner_by_user(int(user_id))


def search_partners(query: str = "", city: str = "", limit: int = 20):
    q = str(query or "").strip()
    city = str(city or "").strip()
    limit = max(1, min(int(limit or 20), 100))
    where = ["p.status <> 'archived'"]
    params: list[Any] = []
    if q:
        where.append("(p.business_name ILIKE %s OR p.business_description ILIKE %s)")
        params.extend([f"%{q}%", f"%{q}%"])
    if city:
        where.append("""EXISTS (
            SELECT 1 FROM partner_applications pa
            WHERE pa.partner_id=p.id AND pa.location_city ILIKE %s
        )""")
        params.append(f"%{city}%")
    params.append(limit)
    return rows(
        """SELECT p.id,p.user_id,p.business_name,p.status,p.verification_status,
                  p.business_description
           FROM partners p
           WHERE """ + " AND ".join(where) +
        " ORDER BY p.id DESC LIMIT %s",
        tuple(params),
    )


def get_company(company_id: int):
    return one(
        """SELECT b.id,b.partner_id,b.name,b.description,b.phone,b.status,
                  p.business_name AS partner_name
           FROM partner_businesses b
           LEFT JOIN partners p ON p.id=b.partner_id
           WHERE b.id=%s""",
        (int(company_id),),
    )


def list_companies(partner_id: int, include_archived: bool = False):
    where = "partner_id=%s"
    params: list[Any] = [int(partner_id)]
    if not include_archived:
        where += " AND status<>'archived'"
    return rows(
        f"""SELECT id,partner_id,name,description,phone,status
            FROM partner_businesses WHERE {where} ORDER BY id DESC""",
        tuple(params),
    )


def get_service(service_id: int):
    return one(
        """SELECT s.id,s.partner_id,s.business_id,s.name,s.category_id,s.price,s.status,
                  p.business_name AS partner_name,b.name AS company_name,
                  c.master_category_id,c.name_am AS category_name_am,
                  c.name_ru AS category_name_ru,c.name_en AS category_name_en
           FROM services s
           LEFT JOIN partners p ON p.id=s.partner_id
           LEFT JOIN partner_businesses b ON b.id=s.business_id
           LEFT JOIN categories c ON c.id=s.category_id
           WHERE s.id=%s""",
        (int(service_id),),
    )


def search_services(
    partner_id: int | None = None,
    category_id: int | None = None,
    city: str = "",
    max_price: float | None = None,
    limit: int = 100,
):
    where = ["s.status='approved'", "p.status='approved'", "EXISTS (SELECT 1 FROM partner_direction_categories pdc JOIN partner_directions pd ON pd.id=pdc.partner_direction_id WHERE pdc.category_id=s.category_id AND pd.partner_id=s.partner_id AND pd.status='approved')"]
    params: list[Any] = []
    if partner_id is not None:
        where.append("s.partner_id=%s")
        params.append(int(partner_id))
    if category_id is not None:
        where.append("s.category_id=%s")
        params.append(int(category_id))
    if max_price is not None:
        where.append("(s.price IS NULL OR s.price<=%s)")
        params.append(float(max_price))
    if city:
        where.append("""EXISTS (
            SELECT 1 FROM partner_objects pl
            WHERE pl.partner_id=p.id
              AND (LOWER(COALESCE(pl.city,''))=LOWER(%s)
                OR LOWER(COALESCE(pl.village,''))=LOWER(%s)
                OR LOWER(COALESCE(pl.marz,''))=LOWER(%s)
                OR LOWER(COALESCE(pl.data_json->>'coverage',''))='all_armenia'
                OR LOWER(COALESCE(pl.data_json->>'service_area',''))='all_armenia')
        )""")
        params.extend([city, city, city])
    params.append(max(1, min(int(limit or 100), 200)))
    return rows(
        """SELECT DISTINCT s.id,s.partner_id,s.business_id,s.name,s.category_id,
                  s.price,s.status,p.business_name AS partner_name,
                  b.name AS company_name,c.name_am AS category_name_am,
                  c.name_ru AS category_name_ru,c.name_en AS category_name_en,
                  c.master_category_id
           FROM services s
           JOIN categories c ON c.id=s.category_id
           JOIN partners p ON p.id=s.partner_id
           LEFT JOIN partner_businesses b ON b.id=s.business_id
           WHERE """ + " AND ".join(where) +
        " ORDER BY CASE WHEN s.price IS NULL THEN 1 ELSE 0 END,s.created_at DESC LIMIT %s",
        tuple(params),
    )



# ---------------------------------------------------------------------------
# Named AI tool reads
# ---------------------------------------------------------------------------

def list_services(partner_id: int | None = None, category_id: int | None = None,
                  actor_user_id: int | None = None, approved_only: bool = False,
                  limit: int = 100):
    if actor_user_id is not None:
        assert_partner_owns_partner(int(partner_id), int(actor_user_id))
    where = ["s.status <> 'deleted'"]
    params: list[Any] = []
    if approved_only:
        where += ["s.status='approved'", "p.status='approved'"]
    if partner_id is not None:
        where.append("s.partner_id=%s"); params.append(int(partner_id))
    if category_id is not None:
        where.append("s.category_id=%s"); params.append(int(category_id))
    params.append(max(1, min(int(limit or 100), 200)))
    return rows(
        """SELECT s.id,s.partner_id,s.business_id,s.name,s.category_id,s.price,s.status,
                  p.business_name AS partner_name,c.name_am AS category_name_am,
                  c.name_ru AS category_name_ru,c.name_en AS category_name_en,
                  c.master_category_id
           FROM services s
           LEFT JOIN categories c ON c.id=s.category_id
           LEFT JOIN partners p ON p.id=s.partner_id
           WHERE """ + " AND ".join(where) +
        " ORDER BY s.id DESC LIMIT %s", tuple(params)
    )


def list_partner_companies(*, partner_id: int, actor_user_id: int) -> list[dict]:
    assert_partner_owns_partner(int(partner_id), int(actor_user_id))
    return rows(
        """SELECT id,partner_id,name,description,phone,status
           FROM partner_businesses
           WHERE partner_id=%s AND status<>'archived'
           ORDER BY id""",
        (int(partner_id),),
    )


def create_partner_company(*, partner_id: int, actor_user_id: int, name: str,
                          description: str | None = None, phone: str | None = None) -> dict:
    assert_partner_owns_partner(int(partner_id), int(actor_user_id))
    name=str(name or "").strip()
    if not name: raise ValueError("company_name_required")
    row=one(
        """INSERT INTO partner_businesses (partner_id,name,description,phone,status)
           VALUES (%s,%s,%s,%s,'active')
           RETURNING id,partner_id,name,description,phone,status""",
        (int(partner_id),name,str(description or "").strip() or None,str(phone or "").strip() or None),
    )
    if not row: raise ValueError("company_create_failed")
    return row


def update_partner_company(*, company_id: int, actor_user_id: int,
                           name: str | None = None, description: str | None = None,
                           phone: str | None = None) -> dict:
    company=get_company(int(company_id))
    if not company: raise ValueError("company_not_found")
    assert_partner_owns_partner(int(company["partner_id"]), int(actor_user_id))
    if company.get("status")=="archived": raise ValueError("company_archived")
    fields=[]; vals=[]
    for key,value in {"name":name,"description":description,"phone":phone}.items():
        if value is not None:
            value=str(value).strip()
            if key=="name" and not value: raise ValueError("company_name_required")
            fields.append(f"{key}=%s"); vals.append(value or None)
    if not fields: raise ValueError("no_changes")
    vals.extend([int(company_id),int(company["partner_id"])])
    row=one("UPDATE partner_businesses SET "+",".join(fields)+" WHERE id=%s AND partner_id=%s AND status<>'archived' RETURNING id,partner_id,name,description,phone,status",tuple(vals))
    if not row: raise ValueError("company_update_failed")
    return row


def archive_partner_company(*, company_id: int, actor_user_id: int) -> dict:
    company=get_company(int(company_id))
    if not company: raise ValueError("company_not_found")
    assert_partner_owns_partner(int(company["partner_id"]), int(actor_user_id))
    if company.get("status")=="archived": return company
    row=one("UPDATE partner_businesses SET status='archived' WHERE id=%s AND partner_id=%s RETURNING id,partner_id,name,description,phone,status",(int(company_id),int(company["partner_id"])))
    if not row: raise ValueError("company_archive_failed")
    return row


def create_partner_address(*, partner_id: int, actor_user_id: int, company_id: int | None,
                         address: str, city: str | None = None, marz: str | None = None,
                         phone: str | None = None, object_name: str | None = None) -> dict:
    assert_partner_owns_partner(int(partner_id), int(actor_user_id))
    address = str(address or "").strip()
    if not address:
        raise ValueError("address_required")
    if company_id is not None:
        company = get_company(int(company_id))
        if not company or int(company.get("partner_id") or 0) != int(partner_id):
            raise PermissionError("company_not_owned")
    cols=["partner_id","business_id","object_name","address","city","marz","phone"]
    vals=[int(partner_id), int(company_id) if company_id is not None else None,
          str(object_name or "").strip() or None, address,
          str(city or "").strip() or None, str(marz or "").strip() or None,
          str(phone or "").strip() or None]
    row=one(
        f"INSERT INTO partner_objects ({','.join(cols)}) VALUES ({','.join(['%s']*len(vals))}) RETURNING id,partner_id,business_id,object_name,address,city,marz,phone",
        tuple(vals),
    )
    if not row: raise ValueError("address_create_failed")
    return row


def update_partner_address(*, address_id: int, actor_user_id: int, address: str | None = None,
                           city: str | None = None, marz: str | None = None,
                           phone: str | None = None, object_name: str | None = None) -> dict:
    obj=one("SELECT id,partner_id,business_id FROM partner_objects WHERE id=%s",(int(address_id),))
    if not obj: raise ValueError("address_not_found")
    assert_partner_owns_partner(int(obj["partner_id"]), int(actor_user_id))
    fields=[]; vals=[]
    for key,value in {"address":address,"city":city,"marz":marz,"phone":phone,"object_name":object_name}.items():
        if value is not None:
            value=str(value).strip()
            if key=="address" and not value: raise ValueError("address_required")
            fields.append(f"{key}=%s"); vals.append(value or None)
    if not fields: raise ValueError("no_changes")
    vals.extend([int(address_id),int(obj["partner_id"])])
    row=one("UPDATE partner_objects SET "+",".join(fields)+" WHERE id=%s AND partner_id=%s RETURNING id,partner_id,business_id,object_name,address,city,marz,phone",tuple(vals))
    if not row: raise ValueError("address_update_failed")
    return row


def get_partner_addresses(partner_id: int, actor_user_id: int | None = None,
                          public_only: bool = False, limit: int = 100):
    if actor_user_id is not None:
        assert_partner_owns_partner(int(partner_id), int(actor_user_id))
    if not _table_exists("partner_objects"):
        return []
    if public_only:
        return rows(
            """SELECT po.id,po.partner_id,po.business_id,po.object_name,
                      po.address,po.city,po.marz
               FROM partner_objects po
               JOIN partners p ON p.id=po.partner_id
               WHERE po.partner_id=%s AND p.status='approved'
               ORDER BY po.business_id,po.id LIMIT %s""",
            (int(partner_id), max(1, min(int(limit or 100), 200))),
        )
    return rows(
        """SELECT id,partner_id,business_id,object_name,address,city,marz,phone
           FROM partner_objects
           WHERE partner_id=%s
           ORDER BY business_id,id LIMIT %s""",
        (int(partner_id), max(1, min(int(limit or 100), 200))),
    )


def check_application(application_id: int) -> dict[str, Any]:
    app = get_application(int(application_id))
    if not app:
        return {"application_id": int(application_id), "found": False, "checks": []}
    checks = [
        {"field": "status", "value": app.get("status"), "severity": "info"},
        {"field": "service", "value": bool(str(app.get("service_name") or "").strip()),
         "severity": "ok" if app.get("service_name") else "warning"},
        {"field": "price", "value": app.get("price"),
         "severity": "ok" if app.get("price") not in (None, "") else "warning"},
    ]
    docs = get_documents(application_id=int(application_id))
    checks.append({"field": "documents", "value": len(docs),
                    "severity": "ok" if docs else "warning"})
    category_id = app.get("category_id")
    master_id = app.get("master_category_id")
    if category_id is not None:
        category = get_catalog_category(int(category_id))
        if category:
            checks.append({"field": "category", "value": category,
                           "severity": "ok" if category.get("is_active") else "error"})
            if master_id is not None and category.get("master_category_id") is not None:
                try:
                    same = int(master_id) == int(category["master_category_id"])
                    checks.append({"field": "direction_category_match", "value": same,
                                   "severity": "ok" if same else "error"})
                except (TypeError, ValueError):
                    pass
        else:
            checks.append({"field": "category", "value": category_id, "severity": "error",
                           "message": "Stored category does not exist in the catalog."})
    return {"application_id": int(application_id), "found": True, "checks": checks,
            "direction_name": app.get("direction_name"),
            "master_category_id": master_id,
            "subcategory_name": app.get("subcategory_name")}

def normalize_phone_number(phone: Any) -> str | None:
    """Normalize an Armenian phone deterministically; return None if invalid."""
    import re
    digits = re.sub(r"\D", "", str(phone or ""))
    if not digits:
        return None
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("0"):
        digits = "374" + digits[1:]
    if digits.startswith("374"):
        return digits if len(digits) == 11 else None
    # Accept already-normalized local mobile length only when unambiguous.
    if len(digits) == 8:
        return "374" + digits
    return None


def validate_service_payload(*, partner_id: int, actor_user_id: int, company_id: int | None,
                            name: str, price: Any = None, category_id: int | None = None) -> dict:
    """Validate a proposed service without writing anything."""
    assert_partner_owns_partner(int(partner_id), int(actor_user_id))
    if company_id is not None:
        company = get_company(int(company_id))
        if not company or int(company.get("partner_id")) != int(partner_id) or company.get("status") == "archived":
            raise PermissionError("company_not_owned_or_archived")
    name = str(name or "").strip()
    if not name:
        raise ValueError("service_name_required")
    category = get_catalog_category(int(category_id)) if category_id is not None else None
    if category_id is not None and not category:
        raise ValueError("catalog_category_not_found")
    if category and not category.get("is_active"):
        raise ValueError("catalog_category_inactive")
    numeric_price = None
    if price not in (None, ""):
        try:
            numeric_price = float(price)
        except (TypeError, ValueError):
            raise ValueError("invalid_service_price")
        if numeric_price < 0:
            raise ValueError("invalid_service_price")
    return {"ok": True, "partner_id": int(partner_id), "company_id": int(company_id) if company_id is not None else None,
            "name": name, "price": numeric_price, "category": category}


def update_service_safe(*, service_id: int, actor_user_id: int, name: str | None = None,
                        price: Any = None, category_id: int | None = None,
                        description: str | None = None) -> dict:
    """Validated domain write. Ownership and catalog are checked before mutation."""
    service = get_service(int(service_id))
    if not service:
        raise ValueError("service_not_found")
    assert_partner_owns_partner(int(service["partner_id"]), int(actor_user_id))
    fields=[]; params=[]
    if name is not None:
        name=str(name).strip()
        if not name: raise ValueError("service_name_required")
        fields.append("name=%s"); params.append(name)
    if price not in (None, ""):
        try: price=float(price)
        except (TypeError,ValueError): raise ValueError("invalid_service_price")
        if price < 0: raise ValueError("invalid_service_price")
        fields.append("price=%s"); params.append(price)
    if description is not None:
        fields.append("description=%s"); params.append(str(description).strip()[:5000] or None)
    if category_id is not None:
        category=get_catalog_category(int(category_id))
        if not category or not category.get("is_active"): raise ValueError("catalog_category_invalid")
        fields.append("category_id=%s"); params.append(int(category_id))
    if not fields: raise ValueError("no_changes")
    params.extend([int(service_id),int(service["partner_id"])])
    row=one("UPDATE services SET "+",".join(fields)+" WHERE id=%s AND partner_id=%s AND status<>'deleted' RETURNING id,partner_id,business_id,name,category_id,price,status",tuple(params))
    if not row: raise ValueError("service_update_failed")
    return row


# ---------------------------------------------------------------------------
# Applications / documents
# ---------------------------------------------------------------------------

def get_application(application_id: int):
    return one(
        """SELECT a.*,p.user_id,p.business_name AS partner_business_name
           FROM partner_applications a
           LEFT JOIN partners p ON p.id=a.partner_id
           WHERE a.id=%s""",
        (int(application_id),),
    )


def count_entities(*, entity: str, status: str | None = None, marz: str | None = None,
                   city: str | None = None) -> int:
    """Backend-only exact counts for the conversational AI tools."""
    entity = str(entity or "").strip().lower()
    if entity == "applications":
        where = ["1=1"]; params = []
        if status:
            where.append("a.status=%s"); params.append(status)
        if marz:
            where.append("a.location_marz ILIKE %s"); params.append(f"%{marz}%")
        if city:
            where.append("a.location_city ILIKE %s"); params.append(f"%{city}%")
        row = one("SELECT COUNT(*) AS n FROM partner_applications a WHERE " + " AND ".join(where), tuple(params))
        return int(row.get("n") or 0) if row else 0
    if entity == "partners":
        row = one("SELECT COUNT(*) AS n FROM partners p WHERE p.status <> 'archived'", ())
        return int(row.get("n") or 0) if row else 0
    if entity == "services":
        where = ["s.status <> 'deleted'"]; params = []
        if status:
            where.append("s.status=%s"); params.append(status)
        row = one("SELECT COUNT(*) AS n FROM services s WHERE " + " AND ".join(where), tuple(params))
        return int(row.get("n") or 0) if row else 0
    if entity == "companies":
        row = one("SELECT COUNT(*) AS n FROM partner_businesses b WHERE b.status <> 'archived'", ())
        return int(row.get("n") or 0) if row else 0
    raise ValueError("unsupported_entity")


def search_applications(status: str | None = None, marz: str | None = None,
                        city: str | None = None, limit: int = 50):
    where = ["1=1"]
    params: list[Any] = []
    if status:
        where.append("a.status=%s")
        params.append(status)
    if marz:
        where.append("a.location_marz ILIKE %s")
        params.append(f"%{marz}%")
    if city:
        where.append("a.location_city ILIKE %s")
        params.append(f"%{city}%")
    params.append(max(1, min(int(limit or 50), 200)))
    return rows(
        """SELECT a.*,p.business_name AS partner_business_name
           FROM partner_applications a
           LEFT JOIN partners p ON p.id=a.partner_id
           WHERE """ + " AND ".join(where) +
        " ORDER BY a.id DESC LIMIT %s",
        tuple(params),
    )



def admin_get_pending_applications(limit: int = 50):
    return rows(
        """SELECT a.*,p.business_name AS partner_business_name
           FROM partner_applications a
           LEFT JOIN partners p ON p.id=a.partner_id
           WHERE a.status NOT IN ('approved','rejected','pending_partner','deleted')
           ORDER BY a.id DESC LIMIT %s""",
        (max(1, min(int(limit or 50), 200)),),
    )


def admin_get_partner_profile(partner_id: int):
    return one(
        """SELECT p.id,p.user_id,p.business_name,p.business_description,p.status,
                  p.verification_status,p.created_at,
                  u.telegram_id,u.username,u.full_name,u.phone,u.is_verified,u.is_frozen,
                  u.lang,u.rating_avg,u.rating_count
           FROM partners p
           LEFT JOIN users u ON u.telegram_id=p.user_id
           WHERE p.id=%s""",
        (int(partner_id),),
    )


def prepare_application_approval(*, application_id: int, actor_user_id: int) -> dict[str, Any]:
    """Read-only approval gate. Never changes application state."""
    if not is_admin(int(actor_user_id)):
        raise PermissionError("admin_required")
    app = get_application_full(int(application_id))
    if not app:
        raise ValueError("application_not_found")
    current_status = str(app.get("status") or "").lower()
    if current_status == "approved":
        # Approval is idempotent. An already approved application is already
        # materialized; repeated admin commands must never create duplicates.
        return {
            "ok": True,
            "can_approve": False,
            "already_active": True,
            "reason_code": "application_already_active",
            "message": f"Հայտ #{int(application_id)}-ն արդեն հաստատված և ակտիվ է։ Կրկին ակտիվացում պետք չէ։",
        }
    if current_status == "rejected":
        raise ValueError("application_already_final")

    payload = app.get("payload_json") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {}
    if not isinstance(payload, dict):
        payload = {}

    source = str(payload.get("source") or "").strip().lower()
    business_id = app.get("business_id")
    master_id = app.get("master_category_id") or payload.get("master_category_id") or payload.get("ai_master_category_id")
    try:
        master_id = int(master_id) if master_id is not None else None
    except (TypeError, ValueError):
        master_id = None

    document_required = source != "partner_service"
    approved_direction = None
    if source == "partner_service" and business_id and master_id:
        approved_direction = one(
            """SELECT id FROM partner_directions
               WHERE partner_id=%s AND business_id=%s AND master_category_id=%s
                 AND status='approved' LIMIT 1""",
            (int(app["partner_id"]), int(business_id), int(master_id)),
        )
        document_required = not bool(approved_direction)

    document = None
    if document_required:
        if not app.get("document_id"):
            return {
                "ok": False, "can_approve": False,
                "reason_code": "document_required",
                "message": "Հայտը չի կարելի հաստատել․ պարտադիր փաստաթուղթը կցված չէ։",
            }
        document = one(
            """SELECT id,original_filename,status,rejection_reason
               FROM partner_verification_documents
               WHERE id=%s AND partner_id=%s LIMIT 1""",
            (int(app["document_id"]), int(app["partner_id"])),
        )
        if not document:
            return {
                "ok": False, "can_approve": False,
                "reason_code": "document_not_found",
                "message": "Հայտի փաստաթուղթը չի գտնվել։",
            }
        if str(document.get("status") or "").lower() != "approved":
            return {
                "ok": False, "can_approve": False,
                "reason_code": "document_not_approved",
                "document_status": document.get("status"),
                "message": "Հայտը չի կարելի հաստատել․ փաստաթուղթը դեռ հաստատված չէ։",
            }

    services = application_service_items(int(application_id))
    if not services:
        return {"ok": False, "can_approve": False, "reason_code": "services_missing",
                "message": "Հայտում հաստատվող ծառայություններ չկան։"}

    unresolved = [str(x.get("name") or "") for x in services if not (
        x.get("category_id") or x.get("matched_subcategory_id") or x.get("subcategory_id")
    )]
    if unresolved:
        return {"ok": False, "can_approve": False, "reason_code": "services_need_classification",
                "services": unresolved,
                "message": "Որոշ ծառայություններ դեռ դասակարգված չեն։"}

    return {
        "ok": True,
        "can_approve": True,
        "document_required": document_required,
        "document": document,
        "approved_direction_id": int(approved_direction["id"]) if approved_direction else None,
        "action": {
            "name": "admin_approve_application",
            "args": {"application_id": int(application_id)},
        },
        "summary": f"Հաստատել հայտ #{int(application_id)}{'՝ փաստաթուղթը ստուգված է' if document_required else '՝ գործող հաստատված ուղղության ներքո, նոր փաստաթուղթ պետք չէ'}?",
    }


def request_application_document_correction(*, application_id: int, reason: str, actor_user_id: int) -> dict[str, Any]:
    """Request a replacement document without rejecting the application."""
    if not is_admin(int(actor_user_id)):
        raise PermissionError("admin_required")
    reason = str(reason or "").strip()
    if not reason:
        raise ValueError("reason_required")
    app = get_application_full(int(application_id))
    if not app:
        raise ValueError("application_not_found")
    if str(app.get("status") or "").lower() in {"approved", "rejected"}:
        raise ValueError("application_already_final")
    document_id = app.get("document_id")
    if document_id:
        execute(
            """UPDATE partner_verification_documents
               SET status='replaced', is_current=FALSE, rejection_reason=%s,
                   reviewed_by=%s, reviewed_at=NOW()
               WHERE id=%s AND partner_id=%s AND is_current=TRUE""",
            (reason[:3000], int(actor_user_id), int(document_id), int(app["partner_id"])),
        )
    row = execute(
        """UPDATE partner_applications
           SET status='pending_partner',
               admin_note=%s, reviewed_by=%s, reviewed_at=NOW(), updated_at=NOW()
           WHERE id=%s AND status NOT IN ('approved','rejected')
           RETURNING *""",
        (reason[:3000], int(actor_user_id), int(application_id)),
        returning=True,
    )
    if not row:
        raise ValueError("application_correction_request_failed")
    return {"ok": True, "application": row, "application_id": int(application_id),
            "reason": reason[:3000], "correction_required": True}


def admin_approve_application(application_id: int, admin_telegram_id: int):
    """Approve a partner application only after backend-owned verification gates.

    Registration applications always require an approved verification document.
    New-service proposals under an already approved partner direction do not
    require a new document. New directions still require an approved document.
    """
    if not is_admin(int(admin_telegram_id)):
        raise PermissionError("admin_required")
    row = get_application_full(int(application_id))
    if not row:
        raise ValueError("application_not_found")
    status = str(row.get("status") or "").lower()
    if status == "approved":
        # Idempotent final write: a stale confirmation must not fail or
        # duplicate services when the application was already activated.
        return {
            "ok": True,
            "already_active": True,
            "application_id": int(application_id),
            "status": "approved",
            "message": f"Հայտ #{int(application_id)}-ն արդեն հաստատված և ակտիվ է։",
        }
    if status == "rejected":
        raise ValueError("application_already_final")

    payload = row.get("payload_json") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {}
    if not isinstance(payload, dict):
        payload = {}

    source = str(payload.get("source") or "").strip().lower()
    business_id = row.get("business_id")
    master_id = row.get("master_category_id") or payload.get("master_category_id") or payload.get("ai_master_category_id")
    try:
        master_id = int(master_id) if master_id is not None else None
    except (TypeError, ValueError):
        master_id = None

    # A service proposal is approvable without a NEW document only when
    # the company already has an approved verification document.
    document_required = source != "partner_service"
    approved_direction = None
    if source == "partner_service" and business_id and master_id:
        approved_direction = one(
            """SELECT id FROM partner_directions
               WHERE partner_id=%s AND business_id=%s AND master_category_id=%s
                 AND status='approved' LIMIT 1""",
            (int(row["partner_id"]), int(business_id), int(master_id)),
        )
        verified_document = one(
            """SELECT id,status FROM partner_verification_documents
               WHERE partner_id=%s AND business_id=%s
                 AND COALESCE(is_current,TRUE)=TRUE AND status='approved'
               ORDER BY created_at DESC,id DESC LIMIT 1""",
            (int(row["partner_id"]), int(business_id)),
        )
        document_required = not bool(verified_document)

    doc = None
    if document_required:
        doc_id = row.get("document_id")
        if not doc_id:
            raise ValueError("document_required")
        doc = one(
            """SELECT id,status FROM partner_verification_documents
               WHERE id=%s AND partner_id=%s
               LIMIT 1""",
            (int(doc_id), int(row["partner_id"])),
        )
        if not doc:
            raise ValueError("document_not_found")
        if str(doc.get("status") or "").lower() != "approved":
            raise ValueError("document_not_approved")

    services = _application_payload_services(row)
    if not services:
        raise ValueError("application_services_missing")

    category_ids = []
    for svc in services:
        cid = svc.get("matched_subcategory_id") or svc.get("subcategory_id") or svc.get("category_id")
        try:
            cid = int(cid) if cid is not None else None
        except (TypeError, ValueError):
            cid = None
        if cid is None:
            raise ValueError("services_need_classification")
        category_ids.append(cid)

    if master_id is None:
        first_cat = get_catalog_category(category_ids[0]) if category_ids else None
        master_id = int(first_cat["master_category_id"]) if first_cat and first_cat.get("master_category_id") else None
    if master_id is None:
        raise ValueError("direction_required")

    valid = rows(
        """SELECT id FROM categories
           WHERE master_category_id=%s AND id=ANY(%s::int[]) AND is_active=TRUE""",
        (int(master_id), list(set(category_ids))),
    )
    valid_ids = {int(x["id"]) for x in valid}
    invalid = [cid for cid in category_ids if cid not in valid_ids]
    if invalid:
        raise ValueError("invalid_subcategory_for_direction")

    partner_id = int(row["partner_id"])
    bid = int(business_id) if business_id else None
    if bid:
        business = one(
            "SELECT id,status FROM partner_businesses WHERE id=%s AND partner_id=%s",
            (bid, partner_id),
        )
        if not business:
            raise ValueError("business_not_found")
    else:
        business = one(
            "SELECT id,status FROM partner_businesses WHERE partner_id=%s "
            "ORDER BY is_default DESC,id LIMIT 1",
            (partner_id,),
        )
        if business:
            bid = int(business["id"])
        else:
            business = one(
                """INSERT INTO partner_businesses(partner_id,name,description,is_default)
                   VALUES(%s,%s,%s,TRUE) RETURNING id,status""",
                (partner_id, row.get("business_name") or "Իմ բիզնեսը", row.get("description")),
            )
            bid = int(business["id"])

    # Materialize the approved application into the partner's canonical
    # company/address structure. The application remains the audit source;
    # partner cabinet reads the normalized entities.
    execute(
        """UPDATE partner_businesses
           SET name=%s,description=%s,phone=%s,status='active',updated_at=NOW()
           WHERE id=%s AND partner_id=%s""",
        (
            str(row.get("business_name") or "").strip() or (business.get("name") or "Իմ բիզնեսը"),
            str(row.get("description") or "").strip() or None,
            str(row.get("phone") or "").strip() or None,
            int(bid), partner_id,
        ),
    )

    app_address = str(row.get("address") or "").strip()
    app_city = str(row.get("location_city") or "").strip() or None
    app_marz = str(row.get("location_marz") or "").strip() or None
    app_phone = str(row.get("phone") or "").strip() or None
    if app_address:
        existing_object = one(
            """SELECT id FROM partner_objects
               WHERE partner_id=%s AND business_id=%s AND address=%s
                 AND COALESCE(city,'')=COALESCE(%s,'')
               ORDER BY id LIMIT 1""",
            (partner_id, int(bid), app_address, app_city),
        )
        if existing_object:
            execute(
                """UPDATE partner_objects
                   SET city=%s,marz=%s,phone=%s,
                       object_name=%s
                   WHERE id=%s AND partner_id=%s""",
                (
                    app_city, app_marz, app_phone,
                    str(row.get("business_name") or "").strip() or None,
                    int(existing_object["id"]), partner_id,
                ),
            )
        else:
            execute(
                """INSERT INTO partner_objects
                   (partner_id,business_id,object_name,address,city,marz,phone)
                   VALUES(%s,%s,%s,%s,%s,%s,%s)""",
                (
                    partner_id, int(bid),
                    str(row.get("business_name") or "").strip() or None,
                    app_address, app_city, app_marz, app_phone,
                ),
            )

    if not approved_direction:
        approved_direction = one(
            """SELECT id FROM partner_directions
               WHERE partner_id=%s AND business_id=%s AND master_category_id=%s
               ORDER BY id DESC LIMIT 1""",
            (partner_id, bid, int(master_id)),
        )
    if approved_direction:
        direction_id = int(approved_direction["id"])
        execute(
            "UPDATE partner_directions SET status='approved',rejection_reason=NULL,updated_at=NOW() WHERE id=%s",
            (direction_id,),
        )
    else:
        direction = one(
            """INSERT INTO partner_directions(partner_id,business_id,master_category_id,status)
               VALUES(%s,%s,%s,'approved') RETURNING id""",
            (partner_id, bid, int(master_id)),
        )
        direction_id = int(direction["id"])

    for cid in sorted(set(category_ids)):
        execute(
            """INSERT INTO partner_direction_categories(partner_direction_id,category_id)
               VALUES(%s,%s) ON CONFLICT DO NOTHING""",
            (direction_id, int(cid)),
        )

    if doc:
        execute(
            """UPDATE partner_verification_documents
               SET business_id=%s,partner_direction_id=%s,status='approved',
                   rejection_reason=NULL,reviewed_by=%s,reviewed_at=NOW()
               WHERE id=%s""",
            (bid, direction_id, int(admin_telegram_id), int(doc["id"])),
        )

    for svc in services:
        name = str(svc.get("name") or svc.get("service_name") or "").strip()[:300]
        cid = int(svc.get("matched_subcategory_id") or svc.get("subcategory_id") or svc.get("category_id"))
        price = svc.get("price")
        try:
            price = float(price) if price not in (None, "") else None
        except (TypeError, ValueError):
            price = None
        existing = one(
            """SELECT id FROM services
               WHERE partner_id=%s AND business_id=%s AND name=%s AND status<>'deleted'
               ORDER BY id DESC LIMIT 1""",
            (partner_id, bid, name),
        )
        data_json = json_dump({
            "application_id": int(application_id),
            "ai_source": True,
            "price_type": svc.get("price_type") or "fixed",
            "matched_subcategory_id": cid,
            "direction_id": direction_id,
        })
        if existing:
            execute(
                """UPDATE services
                   SET category_id=%s,name=%s,price=%s,status='approved',
                       data_json=%s,updated_at=NOW()
                   WHERE id=%s""",
                (cid, name, price, data_json, int(existing["id"])),
            )
        else:
            execute(
                """INSERT INTO services
                   (partner_id,business_id,category_id,subcategory_id,name,description,
                    price,currency,status,data_json,object_id,contact_phone)
                   VALUES(%s,%s,%s,NULL,%s,%s,%s,'AMD','approved',%s,%s,%s)""",
                (
                    partner_id,
                    bid,
                    cid,
                    name,
                    str(svc.get("description") or "").strip(),
                    price,
                    data_json,
                    int(svc.get("object_id")) if svc.get("object_id") not in (None, "") else None,
                    str(svc.get("contact_phone") or app_phone or "").strip() or None,
                ),
            )

    execute(
        """UPDATE partner_businesses
           SET status='active',updated_at=NOW()
           WHERE id=%s AND partner_id=%s""",
        (bid, partner_id),
    )
    execute(
        """UPDATE partners
           SET status='approved',verification_status='approved',rejection_reason=NULL
           WHERE id=%s""",
        (partner_id,),
    )
    execute(
        """UPDATE users
           SET is_verified=TRUE
           WHERE telegram_id=(SELECT user_id FROM partners WHERE id=%s)""",
        (partner_id,),
    )

    # Final backend truth gate: an approval is not considered successful until
    # every service from the application is actually materialized in the
    # canonical services table for this exact company. This prevents the AI
    # layer from ever reporting "active" while the partner cabinet still sees
    # only the application record.
    materialized = rows(
        """SELECT id,name,price,status,category_id,business_id
           FROM services
           WHERE partner_id=%s AND business_id=%s
             AND status='approved'
             AND data_json->>'application_id'=%s
           ORDER BY id""",
        (partner_id, int(bid), str(int(application_id))),
    )
    if len(materialized) < len(services):
        raise RuntimeError(
            f"application_service_materialization_failed: application={int(application_id)} "
            f"expected={len(services)} actual={len(materialized)}"
        )

    return one(
        """UPDATE partner_applications
           SET status='approved',reviewed_by=%s,reviewed_at=NOW(),
               admin_note='Հայտը հաստատված է և ծառայությունները ակտիվացված են։',
               business_id=%s,master_category_id=%s,updated_at=NOW()
           WHERE id=%s RETURNING *""",
        (int(admin_telegram_id), bid, int(master_id), int(application_id)),
    )

def admin_reject_application(application_id: int, reason: str, admin_telegram_id: int):
    reason = str(reason or "").strip()
    if not reason:
        raise ValueError("reason_required")
    row = get_application_full(int(application_id))
    if not row:
        raise ValueError("application_not_found")
    return one(
        """UPDATE partner_applications
           SET status='rejected',admin_note=%s,reviewed_at=NOW(),updated_at=NOW()
           WHERE id=%s AND status NOT IN ('approved',)
           RETURNING *""",
        (reason[:3000], int(application_id)),
    )


def admin_suspend_partner(partner_id: int, reason: str, admin_telegram_id: int):
    reason = str(reason or "").strip()
    if not reason:
        raise ValueError("reason_required")
    partner = get_partner(int(partner_id))
    if not partner:
        raise ValueError("partner_not_found")
    user_id = int(partner.get("user_id") or 0)
    if not user_id:
        raise ValueError("partner_user_not_found")
    return one(
        """UPDATE users
           SET is_frozen=TRUE
           WHERE telegram_id=%s
           RETURNING telegram_id,is_frozen""",
        (user_id,),
    )


def admin_view_audit_logs(limit: int = 50):
    # The current branch has no canonical audit-log table/API. Do not invent
    # one here: callers receive an explicit backend capability error.
    raise NotImplementedError("audit_log_backend_not_configured")


def get_application_full(application_id: int, partner_id: int | None = None):
    where = "a.id=%s"
    params: list[Any] = [int(application_id)]
    if partner_id is not None:
        where += " AND a.partner_id=%s"
        params.append(int(partner_id))
    return one(
        """SELECT a.*,p.user_id,p.business_name AS partner_business_name
           FROM partner_applications a
           LEFT JOIN partners p ON p.id=a.partner_id
           WHERE """ + where,
        tuple(params),
    )



def save_partner_application_draft(*, user_id: int, profile: dict[str, Any]) -> dict[str, Any]:
    """Create/update the partner-facing registration draft.

    This is deliberately independent from catalogue classification. During
    onboarding the partner only supplies business facts; direction/category
    fields stay empty until the admin-side classification step.
    """
    uid = int(user_id)
    profile = dict(profile or {})
    partner = get_partner_by_user(uid) or ensure_partner(uid)
    if not partner:
        raise ValueError("partner_create_failed")
    partner_id = int(partner["id"])

    services = profile.get("services") if isinstance(profile.get("services"), list) else []
    services = [dict(x) for x in services if isinstance(x, dict) and str(x.get("name") or x.get("service_name") or "").strip()]
    payload = dict(profile)
    payload["services"] = services

    business_name = str(profile.get("business_name") or "").strip() or None
    marz = str(profile.get("marz") or profile.get("location_marz") or "").strip() or None
    city = str(profile.get("city") or profile.get("location_city") or "").strip() or None
    village = str(profile.get("village") or profile.get("location_village") or "").strip() or None
    address = str(profile.get("address") or "").strip() or None
    phone = str(profile.get("phone") or "").strip() or None
    description = str(profile.get("business_description") or profile.get("description") or "").strip() or None
    service_name = str(services[0].get("name") or services[0].get("service_name") or "").strip() or None if services else None
    price = services[0].get("price") if services else None
    try:
        price = float(price) if price not in (None, "") else None
    except (TypeError, ValueError):
        price = None

    existing = one(
        """SELECT id FROM partner_applications
           WHERE partner_id=%s
             AND status IN ('draft','pending_partner','sent_back')
           ORDER BY id DESC LIMIT 1""",
        (partner_id,),
    )
    payload_json = json_dump(payload)
    if existing:
        return execute(
            """UPDATE partner_applications
               SET business_name=%s,location_marz=%s,location_city=%s,
                   location_village=%s,address=%s,phone=%s,
                   direction_name=NULL,master_category_id=NULL,
                   subcategory_name=NULL,category_id=NULL,
                   service_name=%s,price=%s,description=%s,
                   payload_json=%s,updated_at=NOW()
               WHERE id=%s AND partner_id=%s
               RETURNING id AS application_id,*""",
            (business_name,marz,city,village,address,phone,service_name,price,
             description,payload_json,int(existing["id"]),partner_id),
            returning=True,
        )

    return execute(
        """INSERT INTO partner_applications
           (partner_id,status,business_name,location_marz,location_city,
            location_village,address,phone,direction_name,master_category_id,
            subcategory_name,category_id,service_name,price,description,
            payload_json)
           VALUES(%s,'pending_partner',%s,%s,%s,%s,%s,%s,NULL,NULL,NULL,NULL,%s,%s,%s,%s)
           RETURNING id AS application_id,*""",
        (partner_id,business_name,marz,city,village,address,phone,
         service_name,price,description,payload_json),
        returning=True,
    )


def update_partner_application(application_id: int, *, partner_id: int,
                                actor_user_id: int, payload: dict[str, Any]):
    assert_partner_owns_partner(int(partner_id), int(actor_user_id))
    current = get_application_full(int(application_id), partner_id=int(partner_id))
    if not current:
        return None
    allowed = (
        "business_name","location_marz","location_city","location_village","address",
        "phone","direction_name","master_category_id","subcategory_name","category_id",
        "service_name","price","description","object_name","object_id","payload_json",
    )
    values = {k: payload.get(k) for k in allowed if k in payload}
    if "payload" in payload:
        values["payload_json"] = json_dump(payload.get("payload") or {})
    if not values:
        return current
    assignments = []
    params: list[Any] = []
    for key, value in values.items():
        assignments.append(f"{key}=%s")
        params.append(value)
    params.append(int(application_id))
    params.append(int(partner_id))
    return execute(
        "UPDATE partner_applications SET " + ",".join(assignments) +
        ",updated_at=NOW() WHERE id=%s AND partner_id=%s RETURNING *",
        tuple(params), returning=True,
    )


def submit_partner_application(application_id: int, *, partner_id: int, actor_user_id: int):
    assert_partner_owns_partner(int(partner_id), int(actor_user_id))
    app = get_application_full(int(application_id), partner_id=int(partner_id))
    if not app:
        return None
    if str(app.get("status") or "") not in ("pending_partner","sent_back","draft","pending_admin"):
        return None
    services = []
    payload = app.get("payload_json") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {}
    if isinstance(payload, dict):
        services = payload.get("services") or []
    if not str(app.get("business_name") or "").strip():
        raise ValueError("business_name_required")
    if not str(app.get("service_name") or "").strip() and not services:
        raise ValueError("service_required")

    # Initial partner registration is a verification workflow. Never allow a
    # partner application without a document to become visible to Admin as
    # pending_admin. The document upload endpoint attaches document_id first;
    # only then may the application enter document_under_review.
    partner = get_partner_by_user(int(actor_user_id)) or {}
    if str(partner.get("status") or "").lower() != "approved" and not app.get("document_id"):
        raise ValueError("document_required")

    new_status = "document_under_review" if app.get("document_id") else "pending_admin"
    return execute(
        "UPDATE partner_applications SET status=%s,updated_at=NOW() WHERE id=%s AND partner_id=%s RETURNING *",
        (new_status, int(application_id), int(partner_id)), returning=True,
    )


def application_directions(limit: int = 200):
    """Return directions actually represented by partner applications.

    This is intentionally different from active_directions(): the latter is the
    whole catalogue, while this query answers the admin question "which
    directions have applications?" using only application data.
    """
    return rows(
        """SELECT m.id, m.name_am, m.name_ru, m.name_en,
                  COUNT(a.id) AS application_count
           FROM partner_applications a
           JOIN master_categories m ON m.id=a.master_category_id
           WHERE m.is_active=TRUE
           GROUP BY m.id,m.name_am,m.name_ru,m.name_en
           ORDER BY application_count DESC,m.id ASC
           LIMIT %s""",
        (max(1, min(int(limit or 200), 200)),),
    )

def search_companies(query: str = "", marz: str = "", city: str = "", limit: int = 50):
    q = str(query or "").strip()
    marz = str(marz or "").strip()
    city = str(city or "").strip()
    where = ["b.status <> 'archived'"]
    params: list[Any] = []
    if q:
        where.append("(b.name ILIKE %s OR b.description ILIKE %s OR p.business_name ILIKE %s)")
        params.extend([f"%{q}%", f"%{q}%", f"%{q}%"])
    if marz:
        where.append("EXISTS (SELECT 1 FROM partner_objects po WHERE po.partner_id=b.partner_id AND po.marz ILIKE %s)")
        params.append(f"%{marz}%")
    if city:
        where.append("EXISTS (SELECT 1 FROM partner_objects po WHERE po.partner_id=b.partner_id AND po.city ILIKE %s)")
        params.append(f"%{city}%")
    params.append(max(1, min(int(limit or 50), 200)))
    return rows(
        "SELECT b.id,b.partner_id,b.name,b.description,b.phone,b.status,p.business_name AS partner_name "
        "FROM partner_businesses b JOIN partners p ON p.id=b.partner_id WHERE " + " AND ".join(where) +
        " ORDER BY b.id DESC LIMIT %s", tuple(params)
    )


def catalog_overview():
    return one("""SELECT
        (SELECT COUNT(*) FROM master_categories WHERE is_active=TRUE) AS directions_count,
        (SELECT COUNT(*) FROM categories WHERE is_active=TRUE) AS subcategories_count,
        (SELECT COUNT(*) FROM services WHERE status <> 'deleted') AS services_count,
        (SELECT COUNT(*) FROM partners WHERE status <> 'archived') AS partners_count,
        (SELECT COUNT(*) FROM partner_businesses WHERE status <> 'archived') AS companies_count""")


def ai_usage_summary(days: int = 1):
    days = max(1, min(int(days or 1), 365))
    return one("""SELECT COUNT(*) AS operations,
        COALESCE(SUM(input_tokens),0) AS input_tokens,
        COALESCE(SUM(output_tokens),0) AS output_tokens,
        COALESCE(SUM(total_tokens),0) AS total_tokens,
        COALESCE(SUM(total_cost_usd),0) AS total_cost_usd,
        COALESCE(SUM(total_cost_amd),0) AS total_cost_amd
        FROM ai_usage_ledger WHERE created_at >= NOW() - (%s * INTERVAL '1 day')""", (days,))


def count(entity: str) -> int:
    allowed = {
        "partners": "SELECT COUNT(*) AS n FROM partners",
        "applications": "SELECT COUNT(*) AS n FROM partner_applications",
        "services": "SELECT COUNT(*) AS n FROM services WHERE status <> 'deleted'",
        "directions": "SELECT COUNT(*) AS n FROM master_categories WHERE is_active=TRUE",
        "subcategories": "SELECT COUNT(*) AS n FROM categories WHERE is_active=TRUE",
        "companies": "SELECT COUNT(*) AS n FROM partner_businesses WHERE status <> 'archived'",
        "addresses": "SELECT COUNT(*) AS n FROM partner_objects",
        "documents": "SELECT COUNT(*) AS n FROM partner_verification_documents",
    }
    sql = allowed.get(str(entity or "").strip().lower())
    if not sql:
        raise ValueError("unsupported_count_entity")
    row = one(sql) or {}
    return int(row.get("n") or 0)



def get_documents(application_id: int | None = None, partner_id: int | None = None, limit: int = 50):
    if application_id is not None:
        app = get_application(int(application_id))
        partner_id = app.get("partner_id") if app else None
    if not partner_id:
        return []
    # Keep compatibility with deployments where document columns differ.
    cols = rows(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='partner_verification_documents' "
        "ORDER BY ordinal_position"
    )
    names = {str(x.get("column_name")) for x in cols}
    wanted = [
        "id","partner_id","application_id","partner_application_id","document_type",
        "file_name","original_filename","file_url","status","verification_status",
        "admin_note","rejection_reason","created_at","updated_at"
    ]
    select_cols = [x for x in wanted if x in names]
    if not select_cols:
        return []
    return rows(
        "SELECT " + ",".join(select_cols) +
        " FROM partner_verification_documents WHERE partner_id=%s ORDER BY id DESC LIMIT %s",
        (int(partner_id), max(1, min(int(limit or 50), 200))),
    )




def search_orders(actor_role: str = "admin", actor_id: int | None = None,
                 partner_id: int | None = None, company_id: int | None = None,
                 status: str | None = None, city: str = "", limit: int = 100):
    """List visible canonical bookings with role and optional entity filters.

    This is the single public search surface used by both admin AI and reports.
    """
    if not _table_exists("bookings"):
        return []
    where = ["1=1"]
    params: list[Any] = []
    role = str(actor_role or "admin").strip().lower()
    if role == "client":
        if actor_id is None:
            return []
        where.append("b.client_id=%s")
        params.append(int(actor_id))
    elif role == "partner":
        if actor_id is None:
            return []
        partner = get_partner_by_user(int(actor_id))
        if not partner:
            return []
        where.append("b.partner_id=%s")
        params.append(int(partner["id"]))
    elif role != "admin":
        return []
    if partner_id is not None:
        where.append("b.partner_id=%s")
        params.append(int(partner_id))
    if company_id is not None:
        where.append("b.business_id=%s")
        params.append(int(company_id))
    if status:
        where.append("b.status=%s")
        params.append(str(status).strip())
    if city:
        where.append("""EXISTS (
            SELECT 1 FROM service_requests sr
            WHERE sr.id=b.request_id AND sr.city ILIKE %s
        )""")
        params.append("%" + str(city).strip() + "%")
    params.append(max(1, min(int(limit or 100), 200)))
    return rows(
        """SELECT b.*, p.business_name AS partner_business_name,
                  pb.name AS company_name, sr.city
           FROM bookings b
           LEFT JOIN partners p ON p.id=b.partner_id
           LEFT JOIN partner_businesses pb ON pb.id=b.business_id
           LEFT JOIN service_requests sr ON sr.id=b.request_id
           WHERE """ + " AND ".join(where) +
        " ORDER BY b.updated_at DESC, b.id DESC LIMIT %s",
        tuple(params),
    )


def get_order(order_id: int, actor_role: str = "admin", actor_id: int | None = None):
    """Return one booking/order through the role boundary.

    bookings is the current canonical order object. Visibility is enforced here
    so AI tools never need to build partner/client ownership predicates.
    """
    if not _table_exists("bookings"):
        return None
    where = ["b.id=%s"]
    params: list[Any] = [int(order_id)]
    role = str(actor_role or "admin").strip().lower()
    if role == "client":
        where.append("b.client_id=%s")
        params.append(int(actor_id))
    elif role == "partner":
        partner = get_partner_by_user(int(actor_id)) if actor_id is not None else None
        if not partner:
            return None
        where.append("b.partner_id=%s")
        params.append(int(partner["id"]))
    elif role != "admin":
        return None
    return one(
        "SELECT b.*, p.business_name AS partner_business_name "
        "FROM bookings b LEFT JOIN partners p ON p.id=b.partner_id "
        "WHERE " + " AND ".join(where),
        tuple(params),
    )


def get_negotiation(negotiation_id: int, actor_role: str = "admin",
                    actor_id: int | None = None):
    """Return a negotiation visible to the actor."""
    role = str(actor_role or "admin").strip().lower()
    where = ["n.id=%s"]
    params: list[Any] = [int(negotiation_id)]
    if role == "client":
        where.append("n.client_id=%s")
        params.append(int(actor_id)) if actor_id is not None else params.append(-1)
    elif role == "partner":
        partner = get_partner_by_user(int(actor_id)) if actor_id is not None else None
        if not partner:
            return None
        where.append("n.partner_id=%s")
        params.append(int(partner["id"]))
    elif role != "admin":
        return None
    return one(
        "SELECT n.* FROM negotiations n WHERE " + " AND ".join(where),
        tuple(params),
    )


def get_negotiation_messages(negotiation_id: int, actor_role: str = "admin",
                             actor_id: int | None = None, limit: int = 500):
    if not get_negotiation(negotiation_id, actor_role=actor_role, actor_id=actor_id):
        return []
    return rows(
        "SELECT id,sender_role,sender_id,message,data_json,created_at "
        "FROM negotiation_messages WHERE negotiation_id=%s ORDER BY id LIMIT %s",
        (int(negotiation_id), max(1, min(int(limit or 500), 1000))),
    )


def append_negotiation_message(negotiation_id: int, sender_role: str,
                               sender_id: int | None, message: str,
                               data: dict[str, Any] | None = None):
    return execute(
        """INSERT INTO negotiation_messages
           (negotiation_id,sender_role,sender_id,message,data_json)
           VALUES(%s,%s,%s,%s,%s::jsonb) RETURNING *""",
        (int(negotiation_id), str(sender_role), sender_id, str(message),
         json.dumps(data or {}, ensure_ascii=False)),
        True,
    )


def update_negotiation(negotiation_id: int, state: dict[str, Any],
                       status: str | None = None,
                       actor_role: str = "admin", actor_id: int | None = None):
    current = get_negotiation(negotiation_id, actor_role=actor_role, actor_id=actor_id)
    if not current:
        return None

    current_status = str(current.get("status") or "").strip().lower()
    requested_status = current_status if status is None else str(status).strip().lower()

    # Explicit state machine: callers cannot jump between arbitrary states.
    allowed = {
        "active": {"active", "agreed", "cancelled", "rejected"},
        "agreed": {"agreed", "cancelled"},
        "cancelled": {"cancelled"},
        "rejected": {"rejected"},
    }
    if requested_status not in allowed.get(current_status, {current_status}):
        return None

    next_state = dict(state or {})
    if requested_status == "agreed":
        final_price = next_state.get("final_price", next_state.get("agreed_price"))
        if final_price is None:
            return None
        try:
            if float(final_price) <= 0:
                return None
        except (TypeError, ValueError):
            return None
        next_state["final_price"] = float(final_price)

    if status is not None:
        return execute(
            """UPDATE negotiations
               SET state_json=%s::jsonb,status=%s,updated_at=NOW()
               WHERE id=%s AND status=%s RETURNING *""",
            (json.dumps(next_state, ensure_ascii=False), requested_status,
             int(negotiation_id), current_status),
            True,
        )
    return execute(
        """UPDATE negotiations SET state_json=%s::jsonb,updated_at=NOW()
           WHERE id=%s AND status=%s RETURNING *""",
        (json.dumps(next_state, ensure_ascii=False), int(negotiation_id), current_status),
        True,
    )


def update_request_status(request_id: int, status: str,
                         actor_role: str = "admin", actor_id: int | None = None):
    role = str(actor_role or "admin").strip().lower()
    if role == "client":
        allowed = one(
            """SELECT 1 FROM service_requests sr
               WHERE sr.id=%s AND sr.client_id=%s""",
            (int(request_id), int(actor_id)) if actor_id is not None else (int(request_id), -1),
        )
    elif role == "partner":
        partner = get_partner_by_user(int(actor_id)) if actor_id is not None else None
        allowed = one(
            """SELECT 1 FROM service_requests sr
               JOIN negotiations n ON n.request_id=sr.id
               WHERE sr.id=%s AND n.partner_id=%s""",
            (int(request_id), int(partner["id"])) if partner else (int(request_id), -1),
        )
    elif role == "admin":
        allowed = {"ok": 1}
    else:
        return None
    if not allowed:
        return None

    return execute(
        "UPDATE service_requests SET status=%s,updated_at=NOW() WHERE id=%s RETURNING *",
        (str(status), int(request_id)), True,
    )


def get_booking(booking_id: int, actor_role: str = "admin", actor_id: int | None = None):
    role = str(actor_role or "admin").strip().lower()
    where = ["b.id=%s"]; params=[int(booking_id)]
    if role == "client":
        where.append("b.client_id=%s"); params.append(int(actor_id) if actor_id is not None else -1)
    elif role == "partner":
        partner = get_partner_by_user(int(actor_id)) if actor_id is not None else None
        if not partner: return None
        where.append("b.partner_id=%s"); params.append(int(partner["id"]))
    elif role != "admin":
        return None
    return one("SELECT b.* FROM bookings b WHERE " + " AND ".join(where), tuple(params))

def cancel_booking(booking_id: int, actor_role: str, actor_id: int,
                   new_status: str, reason: str = "", refund_amount: float = 0.0):
    booking = get_booking(booking_id, actor_role=actor_role, actor_id=actor_id)
    if not booking: return None
    current = str(booking.get("status") or "").lower()
    if current in {"cancelled", "refunded", "completed"}: return None
    target = str(new_status or "").lower()
    if target not in {"cancelled", "refunded"}: return None
    updated = execute(
        """UPDATE bookings SET status=%s,updated_at=NOW()
           WHERE id=%s AND status=%s RETURNING *""",
        (target, int(booking_id), current), True)
    if not updated: return None
    if booking.get("request_id"):
        execute("UPDATE service_requests SET status='cancelled',updated_at=NOW() WHERE id=%s",
                (int(booking["request_id"]),), False)
    if booking.get("negotiation_id"):
        update_negotiation(int(booking["negotiation_id"]), {}, "cancelled",
                           actor_role=actor_role, actor_id=actor_id)
    execute(
        """INSERT INTO booking_cancellations
           (booking_id,cancelled_by,reason,refund_amount)
           VALUES(%s,%s,%s,%s)""",
        (int(booking_id), str(actor_role), str(reason or "")[:500], float(refund_amount or 0)), False)
    if float(refund_amount or 0) > 0:
        execute(
            """INSERT INTO project_expenses
               (booking_id,partner_id,expense_type,amount,currency,description,source)
               VALUES(%s,%s,'refund',%s,'AMD',%s,'booking_cancellation')""",
            (int(booking_id), booking.get("partner_id"), float(refund_amount),
             str(reason or "Booking refund")[:1000]), False)
    return updated

def update_payment_status_for_booking(booking_id: int, status: str):
    return execute(
        "UPDATE payments SET status=%s,updated_at=NOW() WHERE booking_id=%s AND payment_type='commission' RETURNING *",
        (str(status), int(booking_id)), True,
    )


def checkin_booking(booking_id: int, partner_user_id: int, token: str):
    row = one(
        """SELECT bc.*,b.status booking_status,b.partner_id,b.client_id,b.service_name,
                  b.agreed_price,b.currency,p.business_name
           FROM booking_checkins bc
           JOIN bookings b ON b.id=bc.booking_id
           JOIN partners p ON p.id=b.partner_id
           WHERE bc.id=%s AND bc.token=%s AND p.user_id=%s""",
        (int(booking_id), str(token), int(partner_user_id)),
    )
    if not row: return None
    if row.get("status") == "checked_in": return {"already_checked_in": True, "checkin": row}
    if row.get("booking_status") not in ("paid", "confirmed"):
        return {"error": "booking_not_paid", "booking_status": row.get("booking_status")}
    check = execute(
        """UPDATE booking_checkins
           SET status='checked_in',checked_in_at=NOW(),checked_in_by=%s
           WHERE id=%s AND status='active' RETURNING *""",
        (int(partner_user_id), int(row["id"])), True,
    )
    if not check:
        current = one("SELECT * FROM booking_checkins WHERE id=%s", (int(row["id"]),))
        return {"already_checked_in": True, "checkin": current}
    booking = execute(
        """UPDATE bookings SET status='completed',updated_at=NOW()
           WHERE id=%s AND status IN ('paid','confirmed') RETURNING *""",
        (int(booking_id),), True,
    )
    return {"already_checked_in": False, "checkin": check,
            "booking": booking, "service_name": row["service_name"],
            "agreed_price": row["agreed_price"], "currency": row["currency"],
            "business_name": row["business_name"]}


def record_payment_provider_fee(booking_id: int, amount_amd: float, provider: str = ""):
    if float(amount_amd or 0) <= 0:
        return None
    return execute(
        """INSERT INTO project_expenses
           (booking_id,expense_type,amount,currency,description,source)
           VALUES(%s,'payment_fee',%s,'AMD',%s,'payment_provider') RETURNING *""",
        (int(booking_id),float(amount_amd),
         ("Payment provider fee" + (": "+str(provider) if provider else ""))[:1000]), True)

def record_payment_provider_fee_for_payment(payment: dict):
    """Record the configured provider fee once, after a payment settles.
    The rate is configurable because the provider callback does not expose the
    merchant fee in the current Idram contract.
    """
    import os
    if not payment or str(payment.get("status") or "").lower() != "paid":
        return None
    booking_id=payment.get("booking_id")
    if not booking_id:
        return None
    try:
        pct=float(os.getenv("PAYMENT_PROVIDER_FEE_PCT","0") or 0)
    except ValueError:
        pct=0
    pct=max(0,min(pct,100))
    if pct<=0:
        return None
    existing=one("""SELECT id FROM project_expenses
                    WHERE booking_id=%s AND expense_type='payment_fee'
                      AND source='payment_provider' AND data_json->>'payment_id'=%s
                    LIMIT 1""",(int(booking_id),str(payment.get("id"))))
    if existing:
        return existing
    fee=round(float(payment.get("amount") or 0)*pct/100,4)
    if fee<=0:
        return None
    return execute(
        """INSERT INTO project_expenses
           (booking_id,partner_id,expense_type,amount,currency,description,source,data_json)
           VALUES(%s,%s,'payment_fee',%s,'AMD',%s,'payment_provider',%s::jsonb)
           RETURNING *""",
        (int(booking_id),payment.get("partner_id"),fee,
         f"Payment provider fee ({pct:g}%)",
         json.dumps({"payment_id":payment.get("id"),"provider":payment.get("provider"),"rate_pct":pct},ensure_ascii=False)),True)


def reconcile_paid_payment(payment_id: int, transaction_id: str | None = None):
    payment = one("SELECT * FROM payments WHERE id=%s", (int(payment_id),))
    if not payment:
        return None
    if str(payment.get("status") or "").lower() == "paid":
        return {"payment": payment, "already_paid": True}
    updated = execute(
        """UPDATE payments SET status='paid',provider_payment_id=COALESCE(%s,provider_payment_id),updated_at=NOW()
           WHERE id=%s AND status<>'paid' RETURNING *""",
        (transaction_id, int(payment_id)), True)
    if not updated:
        return {"payment": one("SELECT * FROM payments WHERE id=%s",(int(payment_id),)), "already_paid": True}
    payment = updated
    record_payment_provider_fee_for_payment(payment)
    booking_id = payment.get("booking_id")
    booking = None
    if booking_id:
        booking = execute(
            """UPDATE bookings SET status='paid',updated_at=NOW()
               WHERE id=%s AND status IN ('pending_payment','confirmed')
               RETURNING *""",
            (int(booking_id),), True)
        booking = booking or one("SELECT * FROM bookings WHERE id=%s",(int(booking_id),))
        if booking and booking.get("request_id"):
            execute("""UPDATE service_requests SET status='booked',updated_at=NOW()
                       WHERE id=%s AND status<>'booked'""",(int(booking["request_id"]),),False)
    return {"payment": payment, "booking": booking, "already_paid": False}


def get_payment_by_bill_no(bill_no: str):
    """Return the latest payment associated with an external Idram bill number."""
    value = str(bill_no or "").strip()
    if not value:
        return None
    return one(
        "SELECT * FROM payments WHERE data_json->>'bill_no'=%s ORDER BY id DESC LIMIT 1",
        (value,),
    )


def get_approved_service_for_booking(service_id: int):
    return one(
        """SELECT s.* FROM services s
           JOIN partners p ON p.id=s.partner_id
           JOIN partner_direction_categories pdc ON pdc.category_id=s.category_id
           JOIN partner_directions pd ON pd.id=pdc.partner_direction_id AND pd.partner_id=p.id
           WHERE s.id=%s AND s.status='approved' AND p.status='approved' AND pd.status='approved'""",
        (int(service_id),),
    )

def persist_direct_booking(*, client_id: int, service: dict, request_row: dict,
                           package_id: int | None, status: str, price: float,
                           currency: str, commission: float, partner_amount: float,
                           scheduled_at=None, client_note: str = "",
                           intent=None, metadata: dict | None = None):
    """Atomically persist a direct booking and its payment/ledger/check-in rows.

    The provider invoice may exist before this DB transaction starts. If any DB
    step fails, PostgreSQL rolls back all booking-side rows and the caller can
    safely retry/reconcile using the same service request/external bill number.
    """
    if not request_row or not request_row.get("id"):
        return None
    request_id = int(request_row["id"])
    booking_status = str(status or "pending_payment")
    metadata_json = json.dumps(metadata or {}, ensure_ascii=False)
    token = __import__("secrets").token_urlsafe(24)

    def _tx(cur):
        cur.execute(
            """SELECT id FROM bookings WHERE request_id=%s FOR UPDATE""",
            (request_id,),
        )
        existing = cur.fetchone()
        if existing:
            cur.execute("SELECT * FROM bookings WHERE id=%s", (int(existing["id"]),))
            booking = cur.fetchone()
            cur.execute("SELECT * FROM payments WHERE booking_id=%s ORDER BY id DESC LIMIT 1", (int(existing["id"]),))
            payment = cur.fetchone()
            return {"booking": booking, "payment": payment, "checkin": None, "already_exists": True}

        cur.execute(
            """INSERT INTO bookings(
                 request_id,negotiation_id,client_id,partner_id,service_id,package_id,business_id,
                 status,service_name,agreed_price,currency,commission_amount,partner_amount,
                 scheduled_at,client_note,data_json)
               VALUES(%s,NULL,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
               RETURNING *""",
            (request_id, int(client_id), int(service["partner_id"]), int(service["id"]),
             package_id, service.get("business_id"), booking_status, service["name"], float(price), currency,
             float(commission), float(partner_amount), scheduled_at, client_note, metadata_json),
        )
        booking = cur.fetchone()
        cur.execute(
            """INSERT INTO payments(
                 booking_id,client_id,partner_id,payment_type,status,amount,currency,
                 provider,provider_payment_id,data_json)
               VALUES(%s,%s,%s,'commission',%s,%s,%s,%s,%s,%s::jsonb)
               RETURNING *""",
            (int(booking["id"]), int(client_id), int(service["partner_id"]),
             getattr(intent, "status", booking_status), float(commission), currency,
             getattr(intent, "provider", None), getattr(intent, "transaction_id", None),
             json.dumps({"mode": getattr(intent, "mode", None),
                         "bill_no": getattr(intent, "bill_no", None),
                         "payment_url": getattr(intent, "payment_url", None)}, ensure_ascii=False)),
        )
        payment = cur.fetchone()
        cur.execute(
            """INSERT INTO partner_financial_ledger
               (partner_id,booking_id,entry_type,amount,currency,description)
               VALUES(%s,%s,'commission',%s,%s,%s),
                     (%s,%s,'partner_due',%s,%s,%s)""",
            (int(service["partner_id"]), int(booking["id"]), float(commission), currency,
             "Direct booking platform commission",
             int(service["partner_id"]), int(booking["id"]), float(partner_amount), currency,
             "Partner amount after platform commission"),
        )
        cur.execute(
            "INSERT INTO booking_checkins(booking_id,token) VALUES(%s,%s) RETURNING *",
            (int(booking["id"]), token),
        )
        checkin = cur.fetchone()
        cur.execute(
            "UPDATE service_requests SET status=%s,updated_at=NOW() WHERE id=%s",
            ("booked" if getattr(intent, "status", booking_status) == "paid" else "pending_payment", request_id),
        )
        return {"booking": booking, "payment": payment, "checkin": checkin, "already_exists": False}

    try:
        return platform_db.transaction(_tx)
    except Exception:
        return None

def create_direct_booking_request(client_id: int, summary: str, preferences: dict):
    return execute(
        """INSERT INTO service_requests(client_id,status,language,summary,preferences_json)
           SELECT %s,'booked','hy',%s,%s::jsonb
           WHERE NOT EXISTS (
               SELECT 1 FROM service_requests
               WHERE client_id=%s
                 AND status IN ('booked','pending_payment')
                 AND preferences_json->>'direct'='true'
                 AND preferences_json->>'service_id'=%s
                 AND created_at > NOW() - INTERVAL '2 minutes'
           )
           RETURNING *""",
        (int(client_id),str(summary)[:500],json.dumps(preferences or {},ensure_ascii=False),
         int(client_id),str(preferences.get('service_id') or '')),True)

def add_booking_financial_entries(partner_id: int, booking_id: int,
                                  commission: float, partner_amount: float, currency: str):
    execute("""INSERT INTO partner_financial_ledger
               (partner_id,booking_id,entry_type,amount,currency,description)
               VALUES(%s,%s,'commission',%s,%s,%s)""",
            (int(partner_id),int(booking_id),float(commission),currency,
             'Direct booking platform commission'),False)
    execute("""INSERT INTO partner_financial_ledger
               (partner_id,booking_id,entry_type,amount,currency,description)
               VALUES(%s,%s,'partner_due',%s,%s,%s)""",
            (int(partner_id),int(booking_id),float(partner_amount),currency,
             'Partner amount after platform commission'),False)

def create_booking_checkin(booking_id: int, token: str):
    return execute(
        "INSERT INTO booking_checkins(booking_id,token) VALUES(%s,%s) RETURNING *",
        (int(booking_id),str(token)),True)


def get_partner_booking_display(partner_id: int):
    partner = one("""SELECT id,business_name,business_description,contact_share_policy,
                            contact_sharing_enabled,profile_json
                     FROM partners WHERE id=%s""",(int(partner_id),))
    if not partner: return None
    locations = rows("""SELECT marz,city,village,address,location_type
                        FROM partner_objects WHERE partner_id=%s ORDER BY id LIMIT 5""",
                     (int(partner_id),))
    return {"partner":partner,"locations":locations}

def get_approved_service_options(service_id: int, option_ids: list[int]):
    if not option_ids: return []
    return rows("SELECT * FROM service_options WHERE id=ANY(%s) AND service_id=%s AND is_active=TRUE",
                (option_ids,int(service_id)))

def get_active_package(service_id: int, package_id: int):
    return one("SELECT * FROM service_packages WHERE id=%s AND service_id=%s AND is_active=TRUE",
               (int(package_id),int(service_id)))


# ---------------------------------------------------------------------------
# Client request / candidate persistence
# ---------------------------------------------------------------------------

def create_service_request(client_id: int, category_id: int | None, status: str,
                           language: str, city: str | None, summary: str,
                           preferences: dict[str, Any]):
    return execute(
        """INSERT INTO service_requests
           (client_id,category_id,status,language,city,summary,preferences_json)
           VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb) RETURNING *""",
        (int(client_id),category_id,status,language,city,summary,json.dumps(preferences,ensure_ascii=False)),
        True,
    )


def update_service_request(request_id: int, category_id: int | None, status: str,
                           summary: str, city: str | None, preferences: dict[str, Any]):
    return execute(
        """UPDATE service_requests
           SET category_id=%s,status=%s,summary=%s,city=%s,
               preferences_json=%s::jsonb,updated_at=NOW()
           WHERE id=%s RETURNING *""",
        (category_id,status,summary,city,json.dumps(preferences,ensure_ascii=False),int(request_id)),
        True,
    )


def replace_request_candidates(request_id: int, candidates: list[dict[str, Any]], reason: str):
    execute("DELETE FROM request_candidates WHERE request_id=%s",(int(request_id),))
    for rank, candidate in enumerate(candidates,1):
        execute(
            """INSERT INTO request_candidates
               (request_id,partner_id,service_id,rank_score,match_reason)
               VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
            (int(request_id),candidate.get("partner_id"),candidate.get("service_id"),
             100-rank*5,reason),
        )



def create_partner_service(*, partner_id: int, actor_user_id: int, company_id: int | None,
                         name: str, price: Any = None, category_id: int | None = None,
                         address_id: int | None = None, phone: str | None = None) -> dict:
    """Canonical validated service creation entry point for partner tools/API."""
    checked = validate_service_payload(
        partner_id=int(partner_id), actor_user_id=int(actor_user_id),
        company_id=int(company_id) if company_id is not None else None,
        name=name, price=price, category_id=category_id,
    )
    if address_id is not None:
        obj = one("SELECT id,partner_id,business_id FROM partner_objects WHERE id=%s", (int(address_id),))
        if not obj or int(obj.get("partner_id") or 0) != int(partner_id):
            raise PermissionError("address_not_owned")
        if company_id is not None and int(obj.get("business_id") or 0) != int(company_id):
            raise PermissionError("address_not_in_company")
    cols=["partner_id","business_id","name","price"]
    vals=[int(partner_id), checked["company_id"], checked["name"], checked["price"]]
    if category_id is not None: cols.append("category_id"); vals.append(int(category_id))
    if address_id is not None: cols.append("object_id"); vals.append(int(address_id))
    if phone is not None: cols.append("phone"); vals.append(str(phone).strip())
    placeholders=",".join(["%s"]*len(vals))
    row=one(f"INSERT INTO services ({','.join(cols)}) VALUES ({placeholders}) RETURNING id,partner_id,business_id,name,category_id,price,status",tuple(vals))
    if not row: raise ValueError("service_create_failed")
    return row


def get_current_partner_document(*, partner_id: int, company_id: int) -> dict[str, Any] | None:
    """Return the current verification document for an owned company."""
    row = one(
        """SELECT id,status,document_type,original_filename
           FROM partner_verification_documents
           WHERE partner_id=%s AND business_id=%s
             AND COALESCE(is_current,TRUE)=TRUE
           ORDER BY CASE WHEN status='approved' THEN 0
                         WHEN status='under_review' THEN 1
                         WHEN status='pending' THEN 2 ELSE 3 END,
                    created_at DESC,id DESC
           LIMIT 1""",
        (int(partner_id), int(company_id)),
    )
    return row


def create_partner_service_proposal(*, partner_id: int, actor_user_id: int, company_id: int, name: str, price: Any = None,
                                      address_id: int | None = None,
                                      phone: str | None = None,
                                      category_id: int | None = None,
                                      description: str | None = None,
                                      price_type: str | None = None,
                                      service_mode: str | None = None,
                                      service_location: dict[str, Any] | None = None,
                                      submission_token: str | None = None):
    """Create one admin-review application for a single AI-added service."""
    return create_partner_services_proposal(
        partner_id=partner_id, actor_user_id=actor_user_id, company_id=company_id,
        services=[{"name": name, "price": price, "address_id": address_id,
                   "phone": phone, "category_id": category_id, "description": description,
                   "price_type": price_type}],
        service_mode=service_mode,
        service_location=service_location,
        submission_token=submission_token,
    )


def create_partner_services_proposal(*, partner_id: int, actor_user_id: int,
                                     company_id: int, services: list[dict[str, Any]],
                                     service_mode: str | None = None,
                                     service_location: dict[str, Any] | None = None,
                                     submission_token: str | None = None) -> dict:
    """Create ONE admin-review application containing the whole service batch."""
    pid = int(partner_id)
    cid = int(company_id)
    assert_partner_owns_partner(pid, int(actor_user_id))
    company = get_company(cid)
    if not company or int(company.get("partner_id") or 0) != pid:
        raise PermissionError("company_not_owned")
    raw_services = services if isinstance(services, list) else []
    if not raw_services:
        raise ValueError("services_required")

    token = str(submission_token or "").strip()
    if token:
        existing_submission = one(
            """SELECT id AS application_id,id,status,business_id,document_id,
                      service_name,price,category_id,master_category_id,created_at
               FROM partner_applications
               WHERE partner_id=%s
                 AND business_id=%s
                 AND COALESCE(payload_json->>'submission_token','')=%s
               ORDER BY id DESC
               LIMIT 1""",
            (pid, cid, token),
        )
        if existing_submission:
            return {
                **existing_submission,
                "workflow": "admin_verification",
                "idempotent": True,
            }

    company_object = one(
        """SELECT id,partner_id,business_id,object_name,address,city,marz,phone
           FROM partner_objects
           WHERE partner_id=%s AND business_id=%s
             AND COALESCE(is_active,TRUE)=TRUE
           ORDER BY id LIMIT 1""",
        (pid, cid),
    )
    # If the confirmed draft selected a specific/new service address,
    # use that object as the application header as well.
    requested_header_object_id = None
    if raw_services and isinstance(raw_services[0], dict):
        requested_header_object_id = raw_services[0].get("address_id")
    if requested_header_object_id not in (None, ""):
        header_object = one(
            """SELECT id,partner_id,business_id,object_name,address,city,marz,phone
               FROM partner_objects
               WHERE id=%s AND partner_id=%s AND business_id=%s
                 AND COALESCE(is_active,TRUE)=TRUE""",
            (int(requested_header_object_id), pid, cid),
        )
        if header_object:
            company_object = header_object

    document = one(
        """SELECT id,status,document_type,original_filename
           FROM partner_verification_documents
           WHERE partner_id=%s AND business_id=%s
             AND COALESCE(is_current,TRUE)=TRUE
           ORDER BY CASE WHEN status='approved' THEN 0
                         WHEN status='under_review' THEN 1
                         WHEN status='pending' THEN 2 ELSE 3 END,
                    created_at DESC,id DESC
           LIMIT 1""",
        (pid, cid),
    )

    company_phone = str(
        company.get("phone") or (company_object or {}).get("phone") or ""
    ).strip() or None

    prepared = []
    for raw in raw_services[:30]:
        if not isinstance(raw, dict):
            raise ValueError("invalid_service_payload")
        service_name = str(raw.get("name") or raw.get("service_name") or "").strip()
        if not service_name:
            raise ValueError("service_name_required")

        checked = validate_service_payload(
            partner_id=pid,
            actor_user_id=int(actor_user_id),
            company_id=cid,
            name=service_name,
            price=raw.get("price"),
            category_id=raw.get("category_id"),
        )

        service_object = company_object
        requested_object_id = raw.get("address_id")
        if requested_object_id not in (None, ""):
            service_object = one(
                """SELECT id,partner_id,business_id,object_name,address,city,marz,phone
                   FROM partner_objects
                   WHERE id=%s AND partner_id=%s AND business_id=%s
                     AND COALESCE(is_active,TRUE)=TRUE""",
                (int(requested_object_id), pid, cid),
            )
            if not service_object:
                raise PermissionError("address_not_in_company")

        if not service_object or not str(service_object.get("address") or "").strip():
            raise ValueError("service_address_required")

        service_phone = str(
            raw.get("phone")
            or (service_object or {}).get("phone")
            or company_phone
            or ""
        ).strip() or None
        if not service_phone:
            raise ValueError("service_phone_required")

        prepared.append({
            "name": checked["name"],
            "price": checked["price"],
            "price_type": raw.get("price_type") or "fixed",
            "description": str(raw.get("description") or "").strip(),
            "object_id": int(service_object["id"]) if service_object else None,
            "object_name": (service_object or {}).get("object_name"),
            "location": {
                "marz": (service_object or {}).get("marz"),
                "city": (service_object or {}).get("city"),
                "address": (service_object or {}).get("address"),
            },
            "contact_phone": service_phone,
            "category_id": raw.get("category_id"),
            "master_category_id": raw.get("master_category_id"),
            "matched_subcategory_id": raw.get("matched_subcategory_id"),
            "subcategory_id": raw.get("subcategory_id"),
        })

    # Classification is a backend responsibility and must happen before the
    # application is written. The AI may provide service names, but it cannot
    # invent catalogue IDs. Preserve unresolved services for admin review.
    prepared = resolve_catalog_services(prepared, limit=500)

    # A service proposal must never reach Admin without an attached document.
    # Existing approved partners reuse their current company verification
    # document; a new document is not required when an approved one exists.
    if not document:
        raise ValueError("document_required")
    document_status = str(document.get("status") or "").strip().lower()
    if document_status in {"rejected", "expired", "cancelled", "canceled"}:
        raise ValueError("document_replacement_required")

    first = prepared[0]
    payload = {
        "source": "partner_service",
        "company_id": cid,
        "company": {
            "name": company.get("name"),
            "address": (company_object or {}).get("address"),
            "city": (company_object or {}).get("city"),
            "marz": (company_object or {}).get("marz"),
            "phone": company_phone,
            "document_id": int(document["id"]) if document else None,
            "document_status": document.get("status") if document else None,
            "document_filename": document.get("original_filename") if document else None,
            "required_checks": {
                "document_present": True,
                "catalog_classification": "backend_live_catalog",
            },
        },
        "service_mode": service_mode if service_mode in {"at_address", "mobile"} else None,
        "service_location": service_location if isinstance(service_location, dict) else None,
        "services": prepared,
        "submission_token": token or None,
    }

    # Keep the application header synchronized with the first service, while
    # the complete per-service classification remains authoritative in payload.
    first_category_id = first.get("category_id")
    first_master_id = first.get("master_category_id")
    first_category = (
        get_catalog_category(int(first_category_id))
        if first_category_id not in (None, "")
        else None
    )

    row = one(
        """INSERT INTO partner_applications(
               partner_id,business_id,status,business_name,location_marz,location_city,
               address,object_name,object_id,phone,direction_name,master_category_id,
               subcategory_name,category_id,service_name,price,description,document_id,
               payload_json)
           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
           RETURNING id AS application_id,id,status,business_id,document_id,
                     service_name,price,category_id,master_category_id,created_at""",
        (
            pid,
            cid,
            "pending_admin" if document_status == "approved" else "document_under_review",
            company.get("name") or "",
            (company_object or {}).get("marz"),
            (company_object or {}).get("city"),
            (company_object or {}).get("address"),
            (company_object or {}).get("object_name"),
            int(company_object["id"]) if company_object else None,
            company_phone,
            first_category.get("master_name_am") if first_category else None,
            int(first_master_id) if first_master_id not in (None, "") else (
                int(first_category["master_category_id"]) if first_category and first_category.get("master_category_id") else None
            ),
            first_category.get("name_am") if first_category else None,
            first.get("category_id"),
            first["name"],
            first.get("price"),
            first.get("description") or "",
            int(document["id"]),
            json_dump(payload),
        ),
    )
    if not row:
        raise ValueError("service_application_create_failed")

    return {
        **row,
        "workflow": "admin_verification",
        "document_present": True,
        "document_status": document.get("status"),
        "catalog_classified": sum(1 for x in prepared if x.get("catalog_match_status") == "matched"),
        "catalog_unclassified": sum(1 for x in prepared if x.get("catalog_match_status") != "matched"),
        "company": payload["company"],
        "services": prepared,
    }


# ---------------------------------------------------------------------------
# Operational AI context reads
# ---------------------------------------------------------------------------
# These methods keep schema knowledge inside Data Core. AI Context should call
# these named business reads instead of composing SQL itself.

def _table_exists(table_name: str) -> bool:
    row = one(
        "SELECT 1 AS ok FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name=%s LIMIT 1",
        (str(table_name),),
    )
    return bool(row)


def _count_table(table_name: str, where: str = "", params: Iterable[Any] = ()) -> int | None:
    if not _table_exists(table_name):
        return None
    # table_name is selected only from application-owned call sites below.
    sql = f'SELECT COUNT(*) AS n FROM "public"."{table_name}"'
    if where:
        sql += " WHERE " + where
    row = one(sql, tuple(params))
    return int((row or {}).get("n") or 0)


def operational_stats() -> dict[str, Any]:
    """Return verified platform statistics for AI Context/Admin AI."""
    stats: dict[str, Any] = {
        "platform": {}, "catalog": {}, "geography": {},
        "applications": {}, "documents": {}, "orders": {}, "work_queue": {},
    }
    for key, table, where in (
        ("partners", "partners", ""),
        ("companies", "partner_businesses", "status <> 'archived'"),
        ("services", "services", "status IS NULL OR status <> 'deleted'"),
        ("clients", "users", "role = 'client'"),
        ("applications", "partner_applications", ""),
        ("directions", "master_categories", "is_active = TRUE"),
        ("subcategories", "categories", "is_active = TRUE"),
    ):
        value = _count_table(table, where)
        if value is not None:
            if key in {"directions", "subcategories"}:
                stats["catalog"][key] = value
            elif key == "clients":
                stats["platform"][key] = value
            elif key == "applications":
                stats["applications"]["total"] = value
            else:
                stats["platform"][key] = value

    for status in ("pending_admin", "approved", "rejected"):
        value = _count_table("partner_applications", "status=%s", (status,))
        if value is not None:
            stats["applications"][status] = value

    if _table_exists("partner_verification_documents"):
        stats["documents"]["total"] = _count_table("partner_verification_documents") or 0
        doc_cols = {
            str(x.get("column_name"))
            for x in rows(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='partner_verification_documents'"
            )
        }
        for status in ("pending", "under_review", "approved", "rejected"):
            clauses = []
            params = []
            if "status" in doc_cols:
                clauses.append("status=%s"); params.append(status)
            if "verification_status" in doc_cols:
                clauses.append("verification_status=%s"); params.append(status)
            if clauses:
                value = _count_table(
                    "partner_verification_documents",
                    "(" + " OR ".join(clauses) + ")",
                    tuple(params),
                )
                if value is not None:
                    stats["documents"][status] = value

    if _table_exists("bookings"):
        stats["orders"]["total"] = _count_table("bookings") or 0
        for status in ("new", "pending", "confirmed", "active", "completed", "cancelled", "canceled"):
            value = _count_table("bookings", "status=%s", (status,))
            if value:
                stats["orders"][status] = value

    stats["work_queue"]["applications_to_review"] = stats["applications"].get("pending_admin", 0)
    stats["work_queue"]["documents_to_review"] = (
        stats["documents"].get("pending", 0) + stats["documents"].get("under_review", 0)
    )


    if _table_exists("partner_objects"):
        row = one("SELECT COUNT(DISTINCT city) AS n FROM partner_objects WHERE city IS NOT NULL AND TRIM(city)<>''")
        if row:
            stats["geography"]["cities"] = int(row.get("n") or 0)
        row = one("SELECT COUNT(DISTINCT marz) AS n FROM partner_objects WHERE marz IS NOT NULL AND TRIM(marz)<>''")
        if row:
            stats["geography"]["marzes"] = int(row.get("n") or 0)

    for table in ("payments", "financial_transactions", "transactions", "partner_payouts", "commissions"):
        if _table_exists(table):
            stats.setdefault("finance", {})["records"] = _count_table(table) or 0
            break
    return stats


def get_ai_entity(entity_type: str, entity_id: int, full: bool = False,
                 role: str = "admin", actor_id: int | None = None) -> dict[str, Any] | None:
    """Return a role-neutral verified entity graph for AI Context."""
    eid = int(entity_id)
    kind = str(entity_type or "").strip().lower()
    role = str(role or "admin").strip().lower()

    # AI Context is role-scoped: entity graphs must not become a side door
    # around the normal Data Tools permissions.
    if role == "partner" and actor_id is not None:
        if kind == "partner":
            assert_partner_owns_partner(eid, int(actor_id))
        elif kind in {"company", "business"}:
            assert_partner_owns_company(eid, int(actor_id))
        elif kind == "service":
            assert_partner_owns_service(eid, int(actor_id))
    elif role == "client" and kind in {"application", "partner", "company", "business", "service", "order"}:
        # Client Context may contain only marketplace-visible approved data.
        if kind == "application":
            return None

    if kind == "application":
        app = one(
            """SELECT a.id,a.partner_id,a.business_id,a.business_name,a.status,a.service_name,a.price,
                      a.direction_name,a.master_category_id,a.subcategory_name,a.category_id,
                      a.location_marz,a.location_city,a.location_village,a.address,a.phone,
                      a.description,a.object_name,a.object_id,a.created_at,a.updated_at,a.payload_json,
                      p.business_name AS partner_name
               FROM partner_applications a LEFT JOIN partners p ON p.id=a.partner_id
               WHERE a.id=%s""", (eid,))
        if not app:
            return None
        out={"type":"application","id":eid,"profile":app}
        out["documents"]=get_documents(application_id=eid,limit=30)
        if app.get("business_id"):
            out["company"]=get_ai_entity("company",int(app["business_id"]),full=full,role=role,actor_id=actor_id)
        if app.get("partner_id"):
            out["partner"]=get_ai_entity("partner",int(app["partner_id"]),full=False,role=role,actor_id=actor_id)
        return out

    if kind == "partner":
        partner=one("""SELECT id,user_id,business_name,business_description,status,
                              verification_status,contact_share_policy,created_at,updated_at
                       FROM partners WHERE id=%s""",(eid,))
        if not partner: return None
        if role == "client" and partner.get("status") != "approved": return None
        if role == "client": partner = {k: partner.get(k) for k in ("id","business_name","business_description","status")}
        out={"type":"partner","id":eid,"profile":partner}
        out["companies"]=([dict(x, phone=None) for x in list_companies(eid)]
                          if role == "client" else list_companies(eid))
        if full:
            out["services"]=rows(
                """SELECT id,business_id,name,description,price,status,category_id,created_at,updated_at
                   FROM services WHERE partner_id=%s AND status='approved'
                   ORDER BY id DESC LIMIT 100""",(eid,)) if role == "client" else rows(
                """SELECT id,business_id,name,description,price,status,category_id,created_at,updated_at
                   FROM services WHERE partner_id=%s AND (status IS NULL OR status<>'deleted')
                   ORDER BY id DESC LIMIT 100""",(eid,))
            if _table_exists("partner_objects"):
                if role == "client":
                    out["addresses"]=rows(
                        "SELECT id,business_id,object_name,address,city,marz FROM partner_objects "
                        "WHERE partner_id=%s ORDER BY business_id,id LIMIT 100",(eid,))
                else:
                    out["addresses"]=rows(
                        "SELECT id,business_id,object_name,address,city,marz,phone FROM partner_objects "
                        "WHERE partner_id=%s ORDER BY business_id,id LIMIT 100",(eid,))
        return out

    if kind in {"company","business"}:
        company=get_company(eid)
        if not company: return None
        if role == "client" and company.get("partner_id") is not None:
            partner = get_partner(int(company["partner_id"]))
            if not partner or partner.get("status") != "approved":
                return None
            company = {k: company.get(k) for k in ("id","partner_id","name","description","status","partner_name")}
        out={"type":"company","id":eid,"profile":company}
        if _table_exists("partner_objects"):
            out["addresses"]=rows(
                """SELECT id,business_id,object_name,address,city,marz,phone
                   FROM partner_objects WHERE business_id=%s
                   ORDER BY id LIMIT 50""",(eid,))
        out["services"]=rows(
            """SELECT id,business_id,name,description,price,status,category_id,created_at,updated_at
               FROM services WHERE business_id=%s AND status='approved'
               ORDER BY id DESC LIMIT 100""",(eid,)) if role == "client" else rows(
            """SELECT id,business_id,name,description,price,status,category_id,created_at,updated_at
               FROM services WHERE business_id=%s AND (status IS NULL OR status<>'deleted')
               ORDER BY id DESC LIMIT 100""",(eid,))
        return out

    if kind == "service":
        service = get_service(eid)
        if role == "client" and (not service or service.get("status") != "approved"):
            return None
        return service

    if kind in {"category","subcategory"}:
        return get_catalog_category(eid)

    if kind == "order":
        return get_order(eid, actor_role=role, actor_id=actor_id)
    return None


def active_negotiation_id_for_user(user_id: int) -> int | None:
    """Return the latest active negotiation visible to this Telegram user."""
    row = one(
        """SELECT id FROM negotiations
           WHERE (client_id=%s OR partner_id=(SELECT id FROM partners WHERE user_id=%s))
             AND status IN ('active')
           ORDER BY updated_at DESC LIMIT 1""",
        (int(user_id), int(user_id)),
    )
    return int(row["id"]) if row and row.get("id") is not None else None


# ---------------------------------------------------------------------------
# AI session / history
# ---------------------------------------------------------------------------

def active_session(user_id: int, role: str, session_type: str):
    return platform_db.active_session(int(user_id),role,session_type)


def create_session(user_id: int, role: str, session_type: str, context: dict[str, Any] | None = None):
    return platform_db.create_session(int(user_id),role,session_type,context or {})


def update_session(session_id: int, context: dict[str, Any]):
    return platform_db.update_session(int(session_id),context)


def add_ai_message(
    session_id: int,
    sender_role: str,
    text: str,
    data: dict[str, Any] | None = None,
    tool_call_id: str | None = None,
):
    return platform_db.add_ai_message(
        int(session_id), sender_role, text, data or {}, tool_call_id
    )


def recent_ai_messages(session_id: int, limit: int = 16):
    return platform_db.recent_ai_messages(int(session_id),limit)


# ---------------------------------------------------------------------------
# Potential partners
# ---------------------------------------------------------------------------

def potential_partners(status: str | None = None, query: str | None = None):
    return platform_db.potential_partners(status,query)


def create_potential(data: dict[str, Any]):
    return platform_db.create_potential(data)


def update_potential(potential_id: int, **fields):
    return platform_db.update_potential(int(potential_id),**fields)


# ---------------------------------------------------------------------------
# Safe ownership / permissions
# ---------------------------------------------------------------------------

def assert_partner_owns_partner(partner_id: int, actor_user_id: int):
    partner = get_partner(int(partner_id))
    if not partner or int(partner.get("user_id") or 0) != int(actor_user_id):
        raise PermissionError("partner_access_denied")
    return partner


def assert_partner_owns_company(company_id: int, actor_user_id: int):
    company = get_company(int(company_id))
    if not company:
        raise LookupError("company_not_found")
    assert_partner_owns_partner(int(company["partner_id"]), int(actor_user_id))
    return company


def assert_partner_owns_service(service_id: int, actor_user_id: int):
    service = get_service(int(service_id))
    if not service:
        raise LookupError("service_not_found")
    assert_partner_owns_partner(int(service["partner_id"]), int(actor_user_id))
    return service


# ---------------------------------------------------------------------------
# Marketplace API gateways
# ---------------------------------------------------------------------------

def resolve_service_commission(service: dict[str, Any]):
    stype = service.get("commission_type")
    if stype:
        try:
            value = float(service.get("commission_value") or 0)
        except Exception:
            value = 0.0
        return str(stype), max(0.0, value)
    cat_id = service.get("category_id")
    if cat_id:
        row = one("""SELECT COALESCE(c.commission_type,m.commission_type,'on_top') AS ctype,
                            COALESCE(c.commission_value,m.commission_value,10) AS cvalue
                     FROM categories c
                     LEFT JOIN master_categories m ON m.id=c.master_category_id
                     WHERE c.id=%s""",(int(cat_id),))
        if row:
            try:
                value=float(row.get("cvalue") or 0)
            except Exception:
                value=0.0
            return str(row.get("ctype") or "on_top"), max(0.0,value)
    data=service.get("data_json") or {}
    if isinstance(data,str):
        try: data=json.loads(data)
        except Exception: data={}
    try: rate=float(data.get("commission_percent",10))
    except Exception: rate=10.0
    return "on_top", max(0.0,min(100.0,rate))


def marketplace_client_search(query="", city="", category_id=0, limit=20):
    q=str(query or "").strip(); city=str(city or "").strip(); category_id=int(category_id or 0)
    params=[]; where=["s.status='approved'","p.status='approved'","pd.status='approved'"]
    if q:
        like=f"%{q}%"; params += [like]*5
        where.append("(LOWER(s.name) LIKE LOWER(%s) OR LOWER(s.description) LIKE LOWER(%s) OR LOWER(p.business_name) LIKE LOWER(%s) OR LOWER(c.name_am) LIKE LOWER(%s) OR LOWER(c.name_ru) LIKE LOWER(%s))")
    if city:
        params.append(city)
        where.append("""EXISTS(SELECT 1 FROM partner_objects po WHERE po.partner_id=p.id
                         AND (LOWER(COALESCE(po.city,''))=LOWER(%s)
                           OR LOWER(COALESCE(po.village,''))=LOWER(%s)
                           OR LOWER(COALESCE(po.marz,''))=LOWER(%s)
                           OR LOWER(COALESCE(po.data_json->>'coverage',''))='all_armenia'
                           OR LOWER(COALESCE(po.data_json->>'service_area',''))='all_armenia'))""")
        params.extend([city,city])
    if category_id:
        params.append(category_id); where.append("s.category_id=%s")
    sql="""SELECT s.id service_id,s.partner_id,s.category_id,s.name service_name,s.description,s.price,
                  s.currency,s.data_json,p.business_name,p.business_description,p.contact_share_policy,
                  c.name_am category_name_am,c.name_ru category_name_ru,
                  COALESCE((SELECT po.city FROM partner_objects po WHERE po.partner_id=p.id ORDER BY po.id LIMIT 1),'') city,
                  COALESCE((SELECT po.marz FROM partner_objects po WHERE po.partner_id=p.id ORDER BY po.id LIMIT 1),'') marz
           FROM services s JOIN partners p ON p.id=s.partner_id
           JOIN partner_direction_categories pdc ON pdc.category_id=s.category_id
           JOIN partner_directions pd ON pd.id=pdc.partner_direction_id AND pd.partner_id=p.id
           LEFT JOIN categories c ON c.id=s.category_id
           WHERE """+" AND ".join(where)+""" GROUP BY s.id,p.id,c.id,p.business_name,p.business_description,
                  p.contact_share_policy ORDER BY s.created_at DESC LIMIT %s"""
    params.append(max(1,min(int(limit or 20),50)))
    result=rows(sql,tuple(params))
    for item in result:
        item.pop("data_json",None); item.pop("business_description",None)
    return result


def marketplace_partner_negotiations(user_id:int):
    partner=get_partner_by_user(int(user_id))
    if not partner: return None
    return rows("""SELECT n.id,n.request_id,n.status,n.state_json,n.updated_at,sr.summary,sr.city,
                          s.name service_name,p.business_name
                   FROM negotiations n JOIN service_requests sr ON sr.id=n.request_id
                   LEFT JOIN services s ON s.id=(n.state_json->>'service_id')::bigint
                   JOIN partners p ON p.id=n.partner_id
                   WHERE n.partner_id=%s AND n.status='active'
                   ORDER BY n.updated_at DESC""",(int(partner["id"]),))


def marketplace_booking_bundle(booking_id:int, partner_id:int|None=None):
    booking=one("SELECT * FROM bookings WHERE id=%s"+(" AND partner_id=%s" if partner_id else ""),
                (int(booking_id),int(partner_id)) if partner_id else (int(booking_id),))
    if not booking: return None
    payment=one("SELECT * FROM payments WHERE booking_id=%s ORDER BY id DESC LIMIT 1",(int(booking["id"]),))
    check=one("SELECT * FROM booking_checkins WHERE booking_id=%s",(int(booking["id"]),))
    return {"booking":booking,"payment":payment,"checkin":check}


def marketplace_booking_by_negotiation(negotiation_id:int):
    return one("SELECT * FROM bookings WHERE negotiation_id=%s ORDER BY id DESC LIMIT 1",(int(negotiation_id),))


def marketplace_partner_profile(partner_id:int):
    return one("""SELECT id,business_name,business_description,contact_share_policy,
                         contact_sharing_enabled,profile_json,user_id
                  FROM partners WHERE id=%s""",(int(partner_id),))


def marketplace_partner_locations(partner_id:int):
    return rows("""SELECT marz,city,village,address,location_type
                   FROM partner_objects WHERE partner_id=%s ORDER BY id LIMIT 5""",(int(partner_id),))


def marketplace_partner_owner(partner_id:int):
    return one("SELECT user_id FROM partners WHERE id=%s",(int(partner_id),))


def marketplace_service_for_partner(service_id:int,partner_id:int):
    return one("SELECT * FROM services WHERE id=%s AND partner_id=%s AND status='approved'",
               (int(service_id),int(partner_id)))


def marketplace_payment_bundle_for_booking(booking_id:int):
    return marketplace_booking_bundle(int(booking_id))


def marketplace_cancel_side_effects(partner_id:int,booking_id:int,actor:str,reason:str,refund_amount:float,currency:str):
    execute("""INSERT INTO partner_financial_ledger
               (partner_id,booking_id,entry_type,amount,currency,description)
               VALUES(%s,%s,'refund',%s,%s,%s)""",
            (int(partner_id),int(booking_id),-float(refund_amount),currency,
             f"Refund on cancellation ({actor})"),False)
    execute("""INSERT INTO booking_cancellations
               (booking_id,cancelled_by,reason,refund_amount)
               VALUES(%s,%s,%s,%s)""",
            (int(booking_id),actor,reason,float(refund_amount)),False)
    return True


def marketplace_existing_payment(negotiation_id:int):
    booking=marketplace_booking_by_negotiation(int(negotiation_id))
    if not booking: return None
    return marketplace_booking_bundle(int(booking["id"]))


def marketplace_partner_search_context(partner_id:int):
    return {"partner":marketplace_partner_profile(int(partner_id)),
            "locations":marketplace_partner_locations(int(partner_id))}


def marketplace_create_negotiation_selection(request_id:int,client_id:int,service_id:int):
    item=one("""SELECT sr.id request_id,sr.status,s.id service_id,s.partner_id,s.name service_name,
                       s.price,s.currency,p.business_name,p.contact_share_policy
                FROM service_requests sr JOIN services s ON s.id=%s
                JOIN partners p ON p.id=s.partner_id
                WHERE sr.id=%s AND sr.client_id=%s AND s.status='approved' AND p.status='approved'""",
             (int(service_id),int(request_id),int(client_id)))
    if not item: return None
    execute("""INSERT INTO request_candidates(request_id,partner_id,service_id,rank_score,status)
              VALUES(%s,%s,%s,100,'selected')
              ON CONFLICT(request_id,partner_id,service_id) DO UPDATE SET status='selected'""",
            (int(request_id),int(item["partner_id"]),int(service_id)),False)
    negotiation=execute("""INSERT INTO negotiations(request_id,client_id,partner_id,state_json)
                           VALUES(%s,%s,%s,%s::jsonb) RETURNING *""",
        (int(request_id),int(client_id),int(item["partner_id"]),
         json.dumps({"service_id":int(service_id),"service_name":item["service_name"],
                     "price":float(item["price"] or 0),"currency":item["currency"],
                     "client_agreed":False,"partner_agreed":False},ensure_ascii=False)),True)
    execute("UPDATE service_requests SET status='negotiating',updated_at=NOW() WHERE id=%s",(int(request_id),),False)
    execute("""INSERT INTO negotiation_messages(negotiation_id,sender_role,message,data_json)
               VALUES(%s,'ai',%s,%s::jsonb)""",
            (int(negotiation["id"]),
             f"Ընտրված ծառայությունն է՝ {item['service_name']}։ Գինը՝ {item['price']} {item['currency']}։ Կարող եք գրել ձեր ցանկությունները։",
             json.dumps({"type":"selection"},ensure_ascii=False)),False)
    return {"negotiation":negotiation,"candidate":item}


def marketplace_persist_negotiation_booking(*,request_id:int,negotiation_id:int,client_id:int,
                                            partner_id:int,service:dict,status:str,price:float,
                                            currency:str,commission:float,partner_amount:float,
                                            intent=None):
    token=__import__("secrets").token_urlsafe(24)
    def _tx(cur):
        cur.execute("SELECT id FROM bookings WHERE negotiation_id=%s FOR UPDATE",(int(negotiation_id),))
        existing=cur.fetchone()
        if existing:
            bid=int(existing["id"])
            cur.execute("SELECT * FROM bookings WHERE id=%s",(bid,)); booking=cur.fetchone()
            cur.execute("SELECT * FROM payments WHERE booking_id=%s ORDER BY id DESC LIMIT 1",(bid,)); payment=cur.fetchone()
            cur.execute("SELECT * FROM booking_checkins WHERE booking_id=%s",(bid,)); check=cur.fetchone()
            return {"booking":booking,"payment":payment,"checkin":check,"already_exists":True}
        cur.execute("""INSERT INTO bookings(request_id,negotiation_id,client_id,partner_id,service_id,business_id,status,
                         service_name,agreed_price,currency,commission_amount,partner_amount,data_json)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb) RETURNING *""",
                    (int(request_id),int(negotiation_id),int(client_id),int(partner_id),int(service["id"]),
                     service.get("business_id"),status,service["name"],float(price),currency,float(commission),float(partner_amount),
                     json.dumps({"payment_mode":getattr(intent,"provider",None),
                                 "payment_status":getattr(intent,"status",status),
                                 "test_transaction":getattr(intent,"transaction_id",None),
                                 "service_price":float(price)},ensure_ascii=False)))
        booking=cur.fetchone()
        # Link all AI usage from this negotiation to the concrete booking.
        cur.execute("""UPDATE ai_usage_ledger SET order_id=%s
                       WHERE order_id IS NULL AND negotiation_id=%s""",
                    (int(booking["id"]), int(negotiation_id)))
        cur.execute("""INSERT INTO payments(booking_id,client_id,partner_id,payment_type,status,amount,currency,
                         provider,provider_payment_id,data_json)
                       VALUES(%s,%s,%s,'commission',%s,%s,%s,%s,%s,%s::jsonb) RETURNING *""",
                    (int(booking["id"]),int(client_id),int(partner_id),getattr(intent,"status",status),
                     float(commission),currency,getattr(intent,"provider",None),
                     getattr(intent,"transaction_id",None),json.dumps({"mode":getattr(intent,"mode",None),
                     "bill_no":getattr(intent,"bill_no",None),"payment_url":getattr(intent,"payment_url",None)},ensure_ascii=False)))
        payment=cur.fetchone()
        cur.execute("""INSERT INTO partner_financial_ledger(partner_id,booking_id,entry_type,amount,currency,description)
                       VALUES(%s,%s,'commission',%s,%s,%s),(%s,%s,'partner_due',%s,%s,%s)""",
                    (int(partner_id),int(booking["id"]),float(commission),currency,"Test Idram platform commission",
                     int(partner_id),int(booking["id"]),float(partner_amount),currency,"Partner amount after platform commission"))
        cur.execute("INSERT INTO booking_checkins(booking_id,token) VALUES(%s,%s) RETURNING *",(int(booking["id"]),token))
        check=cur.fetchone()
        cur.execute("UPDATE service_requests SET status='booked',updated_at=NOW() WHERE id=%s",(int(request_id),))
        return {"booking":booking,"payment":payment,"checkin":check,"already_exists":False}
    try: return platform_db.transaction(_tx)
    except Exception: return None


def get_admin_setting(key: str, default: str = "") -> str:
    row = one("SELECT value_json FROM admin_settings WHERE key=%s", (str(key),))
    if not row or row.get("value_json") is None:
        return str(default)
    value = row.get("value_json")
    if isinstance(value, dict):
        value = value.get("value") or value.get("model")
    return str(value) if value is not None else str(default)


def ensure_partner(user_id: int):
    return platform_db.ensure_partner(int(user_id))


def update_partner(partner_id: int, **fields):
    return platform_db.update_partner(int(partner_id), **fields)


def create_or_update_proposal(partner_id: int, data: dict, proposal_id: int | None = None):
    return platform_db.create_or_update_proposal(int(partner_id), data, proposal_id)


def latest_clarification(partner_id: int):
    return platform_db.latest_clarification(int(partner_id))


def mark_clarification_answered(clarification_id: int):
    return platform_db.execute(
        "UPDATE admin_clarifications SET status='answered',answered_at=NOW() WHERE id=%s RETURNING *",
        (int(clarification_id),), True
)







