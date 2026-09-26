"""Validated AI action plans.

The model may suggest what should happen next, but this module decides which
fields/actions are structurally acceptable. It never writes to the database.
"""
from __future__ import annotations
from typing import Any
import data_core

ALLOWED_ACTIONS = {
    "update_profile",
    "add_service",
    "update_service",
    "propose_catalog",
    "request_document",
    "ask_clarification",
    "wait_admin",
    "complete",
}

PROFILE_FIELDS = {
    "business_name", "business_description", "location", "marz", "city",
    "village", "address", "phone", "working_hours", "services", "prices",
    "staff", "packages",
}


def _clean(v: Any) -> Any:
    if isinstance(v, str):
        return v.strip()
    return v


def validate_profile_patch(patch: Any) -> dict:
    if not isinstance(patch, dict):
        return {}
    out = {}
    for key, value in patch.items():
        if key not in PROFILE_FIELDS:
            continue
        value = _clean(value)
        if value in ("", None, []):
            continue
        if key in {"services", "prices", "staff", "packages"} and not isinstance(value, list):
            continue
        out[key] = value
    return out


def validate_catalog_match(match: Any) -> dict:
    """Accept only IDs that exist in the current active catalog."""
    if not isinstance(match, dict):
        return {}
    out = {"category_ids": []}
    master = match.get("master_category_id")
    master_id = int(master) if str(master).isdigit() else None
    try:
        active = data_core.search_catalog(
            query="",
            master_category_id=master_id,
            limit=500,
        )
    except Exception:
        active = []
    active_master_ids = {
        int(x.get("master_category_id")) for x in active
        if str(x.get("master_category_id")).isdigit()
    }
    active_category_ids = {
        int(x.get("id")) for x in active
        if str(x.get("id")).isdigit()
    }
    if master_id is not None and master_id in active_master_ids:
        out["master_category_id"] = master_id
    ids = match.get("category_ids")
    if isinstance(ids, list):
        out["category_ids"] = [
            int(x) for x in ids
            if str(x).isdigit() and int(x) in active_category_ids
        ][:50]
    return out


def validate_plan(plan: Any, *, allowed_actions: set[str] | None = None) -> dict:
    plan = plan if isinstance(plan, dict) else {}
    allowed = allowed_actions or ALLOWED_ACTIONS
    action = str(plan.get("action") or "ask_clarification").strip()
    if action not in allowed:
        action = "ask_clarification"
    requires_confirmation = bool(plan.get("requires_confirmation", False))
    return {
        "action": action,
        "requires_confirmation": requires_confirmation,
        "reason": str(plan.get("reason") or "").strip()[:1000],
        "profile_patch": validate_profile_patch(plan.get("profile_patch")),
        "catalog_match": validate_catalog_match(plan.get("catalog_match")),
        "catalog_proposal": plan.get("catalog_proposal") if isinstance(plan.get("catalog_proposal"), dict) else {},
    }


def service_action_plan(data: dict, *, partner_id: int, actor_user_id: int) -> dict:
    """Validate an AI-proposed service mutation before confirmation/execution."""
    data = data if isinstance(data, dict) else {}
    action = str(data.get("action") or "ask_clarification").strip()
    if action not in {"add_service", "update_service"}:
        return validate_plan({"action": "ask_clarification", "reason": "Unsupported service action."})
    payload = data.get("service") if isinstance(data.get("service"), dict) else data
    name = payload.get("name") or payload.get("service_name")
    price = payload.get("price")
    category_id = payload.get("category_id")
    company_id = payload.get("company_id")
    if action == "add_service":
        checked = data_core.validate_service_payload(
            partner_id=int(partner_id), actor_user_id=int(actor_user_id),
            company_id=int(company_id) if str(company_id).isdigit() else None,
            name=name, price=price,
            category_id=int(category_id) if str(category_id).isdigit() else None,
        )
    else:
        service_id = payload.get("service_id")
        if not str(service_id).isdigit():
            return validate_plan({"action": "ask_clarification", "reason": "service_id is required."})
        checked = {"ok": True, "service_id": int(service_id), "name": name, "price": price, "category_id": category_id}
    return {
        "action": action,
        "requires_confirmation": True,
        "reason": str(data.get("reason") or "Service change requires confirmation."),
        "service": checked,
        "execution": "confirmation_required",
    }


def company_action_plan(data: dict, *, partner_id: int, actor_user_id: int) -> dict:
    data = data if isinstance(data, dict) else {}
    action = str(data.get("action") or "").strip()
    if action not in {"add_company", "update_company", "archive_company"}:
        return validate_plan({"action": "ask_clarification", "reason": "Unsupported company action."})
    payload = data.get("company") if isinstance(data.get("company"), dict) else data
    result = {"action": action, "requires_confirmation": True, "execution": "confirmation_required",
              "reason": str(data.get("reason") or "Company change requires confirmation.")}
    if action in {"update_company", "archive_company"} and not str(payload.get("company_id")).isdigit():
        return validate_plan({"action": "ask_clarification", "reason": "company_id is required."})
    if action == "add_company" and not str(payload.get("name") or "").strip():
        return validate_plan({"action": "ask_clarification", "reason": "company name is required."})
    result["company"] = {
        k: _clean(payload.get(k)) for k in ("company_id", "name", "description", "phone")
        if payload.get(k) not in (None, "")
    }
    return result


def address_action_plan(data: dict, *, partner_id: int, actor_user_id: int) -> dict:
    data = data if isinstance(data, dict) else {}
    action = str(data.get("action") or "").strip()
    if action not in {"add_address", "update_address"}:
        return validate_plan({"action": "ask_clarification", "reason": "Unsupported address action."})
    payload = data.get("address_data") if isinstance(data.get("address_data"), dict) else data
    if action == "update_address" and not str(payload.get("address_id")).isdigit():
        return validate_plan({"action": "ask_clarification", "reason": "address_id is required."})
    if action == "add_address" and not str(payload.get("address") or "").strip():
        return validate_plan({"action": "ask_clarification", "reason": "address is required."})
    return {
        "action": action, "requires_confirmation": True, "execution": "confirmation_required",
        "reason": str(data.get("reason") or "Address change requires confirmation."),
        "address": {k: _clean(payload.get(k)) for k in
                    ("address_id", "company_id", "address", "city", "marz", "phone", "object_name")
                    if payload.get(k) not in (None, "")},
    }


def registration_plan(data: dict) -> dict:
    """Turn model output into a safe, deterministic registration plan."""
    data = data if isinstance(data, dict) else {}
    action = "ask_clarification"
    if data.get("catalog_proposal", {}).get("needed"):
        action = "propose_catalog"
    elif data.get("needs_document"):
        action = "request_document"
    elif data.get("confirmed"):
        action = "complete"
    elif validate_profile_patch(data.get("profile_patch")):
        action = "update_profile"
    return validate_plan({
        "action": action,
        "requires_confirmation": action in {"complete", "propose_catalog"},
        "reason": str(data.get("next_step") or ""),
        "profile_patch": data.get("profile_patch"),
        "catalog_match": data.get("catalog_match"),
        "catalog_proposal": data.get("catalog_proposal"),
    })
