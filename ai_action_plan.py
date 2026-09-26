"""Validated AI action plans.

The model may suggest what should happen next, but this module decides which
fields/actions are structurally acceptable. It never writes to the database.
"""
from __future__ import annotations
from typing import Any

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
    if not isinstance(match, dict):
        return {}
    out = {}
    master = match.get("master_category_id")
    if str(master).isdigit():
        out["master_category_id"] = int(master)
    ids = match.get("category_ids")
    if isinstance(ids, list):
        out["category_ids"] = [int(x) for x in ids if str(x).isdigit()][:50]
    else:
        out["category_ids"] = []
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
