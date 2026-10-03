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


async def turn(uid, context, text, extra_context=None):
    context_data = partner_context(uid) if context in ("PARTNER", "REGISTRATION") else None
    if extra_context:
        context_data = {**(context_data or {}), **extra_context}
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
    """Recover explicit service/price pairs without relying on the AI provider.

    The parser is deliberately based on every price marker (от/սկսած/from),
    then takes the service text immediately before that marker. This is more
    reliable than a single greedy regex when the same message also contains
    address/location text after the last price.
    """
    text = str(source or "").strip()
    if not text:
        return []

    marker = re.compile(r"\b(?:от|սկсած|սկսած|from)\b\s*([0-9][0-9\s.,]*)", re.I)
    matches = list(marker.finditer(text))
    if not matches:
        return []

    found = []
    start = 0
    for match in matches:
        segment = text[start:match.start()]
        # A new service normally starts after a comma/semicolon. Keep only the
        # final segment so location or previous service text cannot leak into
        # the next service name.
        if "," in segment or ";" in segment:
            segment = re.split(r"[,;]", segment)[-1]
        name = re.sub(
            r"^(?:создай(?:те)?|добавь(?:те)?)\s+",
            "",
            segment.strip(" .,-"),
            flags=re.I,
        )
        name = re.sub(r"^(?:услугу|услуги)\s+", "", name, flags=re.I).strip(" .,-")
        price = _number(match.group(1))
        if name and price is not None:
            found.append({"name": name, "price_type": "from", "price_amd": price})
        start = match.end()

    # Ignore a trailing location sentence after the final price. For a fixed
    # price the normal AI path handles it; explicit "от" pairs stay deterministic.
    return found

def _extract_from_text(text, data):
    """Merge deterministic prices into an AI-extracted structure."""
    source = str(text or "")
    prices = []
    marker = re.compile(r"\b(?:от|սկսած|from)\b\s*([0-9][0-9\s.,]*)", re.I)
    for match in marker.finditer(source):
        value = _number(match.group(1))
        if value is not None:
            prices.append(value)

    names = data.get("services") if isinstance(data.get("services"), list) else []
    if names and prices:
        for i, item in enumerate(names):
            if i < len(prices):
                item["price_amd"] = prices[i]
                item["price_type"] = "from"
    return data

async def partner_registration_preview(uid, text):
    """Maintain a conversational partner-registration draft until confirmation."""
    state = partner_context(uid)
    existing = state.get("partner")
    if existing:
        companies = state.get("companies") or []
        if companies:
            return {"kind": "message", "answer": "Ваш бизнес уже зарегистрирован. Откройте кабинет партнёра.", "destination": "/partner_cabinet.html"}

    session = __import__("ai.session", fromlist=["get"]).get(uid)
    pending = (session or {}).get("pending") if session else None
    draft = dict(pending) if isinstance(pending, dict) and pending.get("action") == "register_partner" else {}

    # Deterministic fields are always authoritative when explicitly present.
    source = str(text or "").strip()
    phone_match = re.search(r"(?:\+?\d[\d\s().-]{6,}\d)", source)
    if phone_match:
        draft["phone"] = phone_match.group(0).strip()
    name_match = re.search(r"[«“\"]([^»”\"]+)[»”\"]", source)
    if name_match:
        draft["name"] = name_match.group(1).strip()

    try:
        result = await turn(uid, "REGISTRATION", source)
        extracted = parse_json(result["text"])
    except Exception:
        extracted = {}

    if isinstance(extracted, dict):
        for key in ("name", "phone", "marz", "city", "village", "address", "hours", "description"):
            value = extracted.get(key)
            if value not in (None, "", [], {}):
                draft[key] = value
        services = extracted.get("services")
        if isinstance(services, list) and services:
            draft["services"] = services

    # Normalize common aliases returned by AI.
    draft["name"] = str(draft.get("name") or "").strip()
    draft["phone"] = str(draft.get("phone") or "").strip()
    draft["city"] = str(draft.get("city") or "").strip()
    draft["marz"] = str(draft.get("marz") or "").strip()
    draft["village"] = str(draft.get("village") or "").strip()
    draft["address"] = str(draft.get("address") or "").strip()
    draft["hours"] = str(draft.get("hours") or "").strip()
    draft["description"] = str(draft.get("description") or "").strip()
    draft["services"] = draft.get("services") if isinstance(draft.get("services"), list) else []

    required = [
        ("name", "անվանումը"),
        ("phone", "հեռախոսահամարը"),
        ("city", "քաղաքը/գյուղը"),
        ("address", "ճշգրիտ հասցեն"),
        ("hours", "աշխատանքային օրերն ու ժամերը"),
    ]
    missing = [label for key, label in required if not draft.get(key)]
    __import__("ai.session", fromlist=["save"]).save(uid, "REGISTRATION", {"action": "register_partner", "draft": draft, "missing": missing})
    return {
        "kind": "registration_preview" if not missing else "registration_missing",
        "draft": draft,
        "missing": missing,
        "can_confirm": not missing,
        "answer": (
            "Պարզ է։ Արդեն ունեմ՝ անվանումը, հեռախոսը և տրամադրված տվյալները։ "
            "Մնացածը կարող եք ուղարկել մեկ հաղորդագրությամբ։"
            if missing else
            "Տվյալները լրացված են։ Ստորև վերջնական նախադիտումն է։ Հաստատելուց հետո կստեղծվի բիզնեսի գրանցումը."
        ),
    }


async def partner_service_preview(uid, text):
    state = partner_context(uid)
    if not state["partner"]:
        raise ValueError("partner_registration_required")
    if len(state["companies"]) != 1:
        return {"kind": "select_company", "companies": state["companies"]}

    session = __import__("ai.session", fromlist=["get"]).get(uid)
    pending = (session or {}).get("pending") if session else None
    pending_services = []
    if isinstance(pending, dict) and pending.get("action") in ("create_service", "create_services"):
        pending_services = pending.get("services") or []
        if not isinstance(pending_services, list):
            pending_services = []

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
        result = await turn(uid, "PARTNER", text, {"service_draft": pending_services})
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
    if pending_services and not explicit_services and raw_services:
        base = [dict(x) for x in pending_services]
        for idx, item in enumerate(raw_services):
            if idx < len(base):
                base[idx] = {**base[idx], **{k:v for k,v in item.items() if v not in (None, "", [], {})}}
            else:
                base.append(item)
        raw_services = base
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

    # Deterministic recovery is the final guard: explicit service names/prices
    # from the partner's original message remain authoritative.

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

    missing_fields = []
    for index, service in enumerate(services, 1):
        if not service["name"]:
            missing_fields.append({"service": index, "field": "name", "label": "անվանումը"})
        if service["price_amd"] is None:
            missing_fields.append({"service": index, "field": "price_amd", "label": "գինը"})
        elif service["price_type"] not in ("fixed", "from"):
            missing_fields.append({"service": index, "field": "price_type", "label": "գնի տեսակը՝ ֆիքսված կամ «սկսած»"})

    if not services:
        missing_fields.extend([
            {"service": 1, "field": "name", "label": "ծառայության անվանումը"},
            {"service": 1, "field": "price_amd", "label": "գինը"},
        ])

    if missing_fields:
        save(uid, "PARTNER", {
            "action": "create_services",
            "services": services,
            "missing_fields": missing_fields,
            "missing_documents": [],
        })
        labels = []
        for item in missing_fields:
            if item["label"] not in labels:
                labels.append(item["label"])
        return {
            "kind": "service_data_incomplete",
            "services": services,
            "missing_fields": missing_fields,
            "can_confirm": False,
            "answer": "Շատ լավ, տվյալների մեծ մասը արդեն ունեմ։ Խնդրում եմ նշեք միայն բացակայող տվյալները՝ " + ", ".join(labels) + "։",
        }

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
        "missing_fields": [],
    })
    return {
        "kind": "preview",
        "services": services,
        "missing_documents": list(missing.values()),
        "can_submit_to_admin": not missing and all(s["classification"] for s in services),
    }
