import json
import re

from .core import chat
from .prompts import prompt
from .session import save
from .tools import partner_context
from catalog.classifier import classify
from db import run, exec


def parse_json(value):
    text = (value or "").strip()
    marker = chr(96) * 3
    if text.startswith(marker):
        text = text.replace(marker + "json", "", 1).replace(marker, "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


async def turn(uid, context, text):
    context_data = partner_context(uid) if context == "PARTNER" else None
    result = await chat(prompt(context, text, context_data))
    try:
        exec(
            "INSERT INTO aig_ai_costs(telegram_id,provider,model,operation,input_tokens,output_tokens) VALUES(%s,%s,%s,%s,%s,%s)",
            (uid, result["provider"], result["model"], context, result["input_tokens"], result["output_tokens"]),
        )
    except Exception:
        pass
    return result


def _price_type(raw, source):
    value = str(raw or "").strip().lower()
    if value in ("from", "starting", "starting_from", "от", "от_цены", "սկսած", "դրամից"):
        return "from"
    if value in ("fixed", "exact", "фиксированная", "фиксированный", "ֆիքսված"):
        return "fixed"
    lower = str(source).lower()
    return "from" if re.search(r"(^|\s)(от|սկսած|դրամից|from)(\s|$)", lower) else ""


def _number(value):
    if isinstance(value, (int, float)):
        return float(value)
    m = re.search(r"\d+(?:[.,]\d+)?", str(value or "").replace(" ", ""))
    return float(m.group(0).replace(",", ".")) if m else None


def _list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _direction(category):
    if not category:
        return None
    if category.get("parent_id"):
        return run(
            "SELECT id,name_am,name_ru,name_en,slug,parent_id FROM aig_catalog_categories WHERE id=%s",
            (category["parent_id"],),
        )
    return category


def _has_direction_document(company_id, direction_id):
    # Uploading a document is enough to let the service application reach Admin.
    # Admin later decides whether the direction document becomes ACTIVE.
    return bool(run(
        "SELECT id FROM aig_direction_documents WHERE company_id=%s AND catalog_category_id=%s "
        "AND status IN ('PENDING_ADMIN','ACTIVE') ORDER BY id DESC LIMIT 1",
        (company_id, direction_id),
    ))


def _normalise_service(company_id, item, source, default_location):
    name = str(item.get("name", "")).strip()
    price_type = _price_type(item.get("price_type"), source)
    price_amd = _number(item.get("price_amd"))
    loc = item.get("location") if isinstance(item.get("location"), dict) else {}
    loc = {**default_location, **loc}
    return {
        "company_id": company_id,
        "name": name,
        "description": str(item.get("description") or "").strip() or None,
        "price_type": price_type,
        "price_amd": price_amd,
        "hours": str(item.get("hours") or "").strip() or None,
        "at_client": bool(item.get("at_client", False)),
        "territory": str(item.get("territory") or "").strip() or None,
        "marzes": _list(item.get("marzes") or loc.get("marzes") or loc.get("marz")),
        "cities": _list(item.get("cities") or loc.get("cities") or loc.get("city")),
        "districts": _list(item.get("districts") or loc.get("districts") or loc.get("district")),
        "address": str(item.get("address") or loc.get("address") or "").strip() or None,
        "internal_phone": str(item.get("internal_phone") or "").strip() or None,
    }


def _recover_explicit_services(source):
    """Parse explicit service + price pairs from the partner's original message."""
    text = str(source or "").strip()
    pattern = re.compile(
        r"(?:^|[,;])\s*(?:создай(?:те)?\s+)?(?:добавь(?:те)?\s+)?"
        r"(?:услугу\s+|услуги\s+)?"
        r"(.+?)\s+(?:от|սկսած|from)\s*([0-9][0-9\s.,]*)"
        r"\s*(?:драм(?:ов)?|amd|֏)?(?=\s*(?:[,;.]|$))",
        re.I,
    )
    found = []
    for m in pattern.finditer(text):
        name = m.group(1).strip(" .,-")
        name = re.sub(r"^(?:создай|создайте|добавь|добавьте)\s+(?:услугу|услуги)\s+", "", name, flags=re.I)
        name = re.sub(r"^(?:услуга|услуги)\s+", "", name, flags=re.I).strip()
        if not name:
            continue
        price = _number(m.group(2))
        if price is not None:
            found.append({"name": name, "price_type": "from", "price_amd": price})
    return found


def _extract_from_text(text, data):
    lower = str(text).lower()
    prices = re.findall(r"(?:от|սկսած|from)\s*([0-9][0-9\s.,]*)", lower)
    nums = [_number(x) for x in prices if _number(x) is not None]
    names = data.get("services") if isinstance(data.get("services"), list) else []
    if names and len(nums) >= len(names):
        for i, item in enumerate(names):
            if not item.get("price_amd"):
                item["price_amd"] = nums[i]
            if not item.get("price_type"):
                item["price_type"] = "from"
    return data


async def partner_service_preview(uid, text):
    state = partner_context(uid)
    if not state["partner"]:
        raise ValueError("partner_registration_required")
    if len(state["companies"]) != 1:
        return {"kind": "select_company", "companies": state["companies"]}

    explicit_services = _recover_explicit_services(text)

    # A partner command that explicitly contains service names and prices is
    # already structured enough to enter the service lifecycle. Do NOT call
    # Groq first: a provider timeout/429/parser failure must never turn a valid
    # partner request into ai_request_failed.
    if explicit_services:
        data = {
            "action": "create_service",
            "services": explicit_services,
            "location": {},
        }
    else:
        result = await turn(uid, "PARTNER", text)
        data = _extract_from_text(text, parse_json(result["text"]))
        if data.get("action") != "create_service":
            return {"kind": "message", "answer": data.get("answer", "")}

    raw_services = data.get("services")
    if not isinstance(raw_services, list):
        raw_services = [{
            "name": data.get("name"),
            "price_type": data.get("price_type"),
            "price_amd": data.get("price_amd"),
            "hours": data.get("hours"),
            "at_client": data.get("at_client"),
            "territory": data.get("territory"),
            "internal_phone": data.get("internal_phone"),
            "description": data.get("description"),
            "location": data.get("location", {}),
        }]

    default_location = data.get("location") if isinstance(data.get("location"), dict) else {}
    services = [_normalise_service(state["companies"][0]["id"], item, text, default_location) for item in raw_services]
    partner_phone = str((state.get("partner") or {}).get("phone") or "").strip() or None
    for service in services:
        service["internal_phone"] = service["internal_phone"] or partner_phone

    # If the model collapsed multiple services into one object, recover the two explicit services from the user's text.
    if len(services) == 1:
        chunks = re.split(r",\s*(?=(?:ремонт|услуга|установка|замена|чистка|диагностика)\b)", str(text), flags=re.I)
        if len(chunks) > 1:
            rebuilt = []
            for chunk in chunks:
                m = re.search(r"(.+?)\s+(?:от|սկսած|from)\s*([0-9][0-9\s.,]*)\s*(?:драм|amd|֏)?", chunk, flags=re.I)
                if m:
                    rebuilt.append({
                        "name": m.group(1).strip(" .,-"),
                        "price_type": "from",
                        "price_amd": _number(m.group(2)),
                    })
            if len(rebuilt) > 1:
                services = [_normalise_service(state["companies"][0]["id"], x, text, default_location) for x in rebuilt]
                for service in services:
                    service["internal_phone"] = partner_phone

    # Deterministic recovery is the final guard: explicit service names/prices written
    # by the partner must not be lost because the model returned an incomplete JSON shape.
    def _recover_explicit_services(source):
        # Recover explicit service/price pairs from the partner's original text.
        # This is deliberately deterministic and runs after the AI response.
        chunks = re.split(
            r",\s*(?=(?:ремонт|услуга|установка|замена|чистка|диагностика|մաքրում|վերանորոգում|տեղադրում|փոխարինում)\b)",
            str(source),
            flags=re.I,
        )
        recovered = []
        for chunk in chunks:
            m = re.search(
                r"(?:создай(?:те)?\s+(?:услугу|услуги)|добавь(?:те)?\s+(?:услугу|услуги))?\s*(.+?)\s+(?:от|սկսած|from)\s*([0-9][0-9\s.,]*)\s*(?:драм(?:ов)?|amd|֏)?",
                chunk,
                flags=re.I,
            )
            if not m:
                continue
            name = m.group(1).strip(" .,-")
            if not name:
                continue
            recovered.append({
                "name": name,
                "price_type": "from",
                "price_amd": _number(m.group(2)),
            })
        return recovered

    # The partner's explicit service/price pairs are authoritative.
    explicit_services = _recover_explicit_services(text)
    if explicit_services:
        services = [_normalise_service(state["companies"][0]["id"], x, text, default_location) for x in explicit_services]
        for service in services:
            service["internal_phone"] = partner_phone

    invalid = (
        not services
        or any(
            not s["name"]
            or s["price_type"] not in ("fixed", "from")
            or s["price_amd"] is None
            for s in services
        )
    )
    if invalid:
        recovered = _recover_explicit_services(text)
        if recovered:
            services = [_normalise_service(state["companies"][0]["id"], x, text, default_location) for x in recovered]
            for service in services:
                service["internal_phone"] = partner_phone

    if not services or any(not s["name"] or s["price_type"] not in ("fixed", "from") or s["price_amd"] is None for s in services):
        raise ValueError("service_data_incomplete")

    missing = {}
    for service in services:
        category = classify(service["name"])
        direction = _direction(category)
        service["catalog_category_id"] = category["id"] if category else None
        service["direction_category_id"] = direction["id"] if direction else None
        service["classification_confidence"] = category.get("classification_confidence") if category else None
        service["classification_margin"] = category.get("classification_margin") if category else None
        service["classification"] = category
        service["direction"] = direction
        if direction and not _has_direction_document(service["company_id"], direction["id"]):
            missing[str(direction["id"])] = {
                "id": direction["id"],
                "name_am": direction["name_am"],
                "name_ru": direction["name_ru"],
                "name_en": direction["name_en"],
            }

    save(uid, "PARTNER", {
        "action": "create_services",
        "services": services,
        "missing_documents": list(missing.values()),
    })
    return {
        "kind": "preview",
        "services": services,
        "missing_documents": list(missing.values()),
        "can_submit_to_admin": not missing and all(s["classification"] for s in services),
    }
