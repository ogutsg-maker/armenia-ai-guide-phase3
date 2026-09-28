"""AI-first partner onboarding for Armenia AI Guide."""
from __future__ import annotations

import json
import os
import re
from typing import Any


import data_core

try:
    from groq import AsyncGroq
except Exception:
    AsyncGroq = None


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _safe_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        raw = str(value).strip()
        if not raw.isdigit():
            return None
        return int(raw)
    except (TypeError, ValueError):
        return None


# Armenian place-name normalization used only for extracting facts from free text.
# The partner never has to choose a marz or city manually.
def _normalize_place_name(value: Any) -> str:
    text = _norm(value)
    if not text:
        return ""
    text = (
        text.replace("օ", "ո")
            .replace("Օ", "Ո")
            .replace("ւ", "ու")
    )
    # Common Armenian locative endings.
    m = re.fullmatch(r"([\u0531-\u058F]{3,})(?:անում|ենում|ում)", text, flags=re.I)
    if m:
        text = m.group(1)
    return text.strip(" .,;:()«»\"'")

# Administrative mapping for common Armenian cities/towns. This is a
# deterministic safety net; explicit AI extraction still has priority.
_ARMENIA_CITY_TO_MARZ = {
    # Kotayk
    "հրազդան":"Կոտայք","աբովյան":"Կոտայք","չարենցավան":"Կոտայք",
    "բյուրեղավան":"Կոտայք","նոր հաճն":"Կոտայք","նոր հաճըն":"Կոտայք",
    "բալահովիտ":"Կոտայք","ծաղկաձոր":"Կոտայք","գառնի":"Կոտայք",
    "բյուրական":"Կոտայք","մարմարիկ":"Կոտայք",
    # Shirak
    "գյումրի":"Շիրակ","մարալիկ":"Շիրակ","արթիկ":"Շիրակ",
    "ամասիա":"Շիրակ","մեծ մանթաշ":"Շիրակ",
    # Lori
    "վանաձոր":"Լոռի","սպիտակ":"Լոռի","ստեփանավան":"Լոռի",
    "տաշիր":"Լոռի","ալավերդի":"Լոռի","թումանյան":"Լոռի",
    # Tavush
    "իջևան":"Տավուշ","դիլիջան":"Տավուշ","բերդ":"Տավուշ",
    "նոյեմբերյան":"Տավուշ","թումանյան":"Լոռի",
    # Gegharkunik
    "գավառ":"Գեղարքունիք","սևան":"Գեղարքունիք","մարտունի":"Գեղարքունիք",
    "վարդենիս":"Գեղարքունիք","ճամբարակ":"Գեղարքունիք",
    # Aragatsotn
    "աշտարակ":"Արագածոտն","ապարան":"Արագածոտն","թալին":"Արագածոտն",
    "արարատ":"Արարատ","արտաշատ":"Արարատ","վեդի":"Արարատ","մասիս":"Արարատ",
    # Armavir
    "արմավիր":"Արմավիր","վաղարշապատ":"Արմավիր","մեծամոր":"Արմավիր",
    # Syunik
    "կապան":"Սյունիք","գորիս":"Սյունիք","քաջարան":"Սյունիք",
    "սիսիան":"Սյունիք","մեղրի":"Սյունիք","ագարակ":"Սյունիք",
    # Vayots Dzor
    "եղեգնաձոր":"Վայոց ձոր","ջերմուկ":"Վայոց ձոր","վայք":"Վայոց ձոր",
    # Yerevan
    "երևան":"Երևան","yerevan":"Երևան","erevan":"Երևան",
    # common Latin/Russian spellings
    "hrazdan":"Կոտայք","razdan":"Կոտայք","abovyan":"Կոտայք",
    "charentsavan":"Կոտայք","gyumri":"Շիրակ","vanadzor":"Լոռի",
    "dilijan":"Տավուշ","ijevan":"Տավուշ","gavar":"Գեղարքունիք",
    "sevan":"Գեղարքունիք","ashtarak":"Արագածոտն","aparan":"Արագածոտն",
    "artashat":"Արարատ","armavir":"Արմավիր","kapan":"Սյունիք",
    "goris":"Սյունիք","sisian":"Սյունիք","jermuk":"Վայոց ձոր",
}
 
def get_catalog(db=None) -> list[dict]:
    """Read the active catalogue through Data Core only."""
    rows = []
    try:
        for row in data_core.rows(
            """SELECT m.id AS master_id,m.name_am AS master_am,m.name_ru AS master_ru,
                      m.name_en AS master_en,m.slug AS master_slug,
                      c.id AS category_id,c.name_am AS category_am,c.name_ru AS category_ru,
                      c.name_en AS category_en,c.slug AS category_slug
               FROM master_categories m
               JOIN categories c ON c.master_category_id=m.id
               WHERE m.is_active=TRUE AND c.is_active=TRUE
               ORDER BY m.id,c.id"""
        ):
            rows.append(dict(row))
    except Exception:
        return []
    return rows


def get_master_catalog(db=None) -> list[dict]:
    return [
        {
            "master_id": row["id"],
            "master_am": row.get("name_am") or "",
            "master_ru": row.get("name_ru") or "",
            "master_en": row.get("name_en") or "",
            "master_slug": row.get("slug") or "",
        }
        for row in data_core.active_directions()
    ]


def get_catalog_for_master(db=None, master_id: int | None = None) -> list[dict]:
    mid = _safe_int(master_id)
    if mid is None:
        return []
    return [row for row in get_catalog() if _safe_int(row.get("master_id")) == mid]


def _heuristic(text: str) -> dict:
    """Minimal non-classifying fallback when Groq is unavailable."""
    raw = _norm(text)
    return {
        "business_name": None,
        "city": None,
        "district": None,
        "marz": None,
        "address": None,
        "phone": None,
        "working_hours": None,
        "direction": None,
        "master_category_id": None,
        "subcategory_names": [],
        "description": raw,
        "services": [],
        "missing": [
            "business_name",
            "marz",
            "city",
            "address",
            "phone",
            "services",
        ],
        "ready": False,
    }


def _recover_obvious_facts(text: str, data: dict) -> dict:
    """Recover simple facts the LLM may omit while answering a pending field."""
    out = dict(data or {})
    raw = _norm(text)
    low = raw.lower()

    # Common natural-language location forms in Russian/English/Armenian.
    # Recover a city from the partner's original sentence even when the
    # current LLM turn is answering another pending field. This must work
    # generically; do not maintain a city-by-city dictionary.
    city_patterns = [
        # Russian: "в Раздане", "из Раздана", "город Раздан"
        r"\b(?:в|из|город(?:е)?|город)\s+([А-ЯЁA-Z][А-ЯЁA-Zа-яёa-z-]{2,})",
        # English: "in Hrazdan", "from Hrazdan"
        r"\b(?:in|from)\s+([A-Z][A-Za-z-]{2,})",
        # Armenian locative forms: "Հրազդանում", "Երևանում", "Գյումրիում".
        # Use the Armenian Unicode block instead of a hand-written character
        # range; this avoids regex parser errors such as "bad character range".
        r"(?:Ես\s+)?([\u0531-\u058F]+?)(?:անում|ենում|ում)(?=\s+(?:գեղեցկության|սրահ|աշխատ|գործ|ունեմ|ենք|է|եմ|առաջարկ|կազմակերպ|զբաղ|ծառայ|ուն|աշխատանք))",
        r"\b([\u0531-\u058F]{3,})(?:անում|ենում|ում)\b",
        r"քաղաք\s+([\u0531-\u058F]+?)(?:անում|ենում|ում)\b",
    ]
    if not out.get("city"):
        # Explicit Armenian city wording: «Հրազդան քաղաքում», «Երևան քաղաքում».
        m_city = re.search(r"([\u0531-\u058F]{3,})\s+քաղաք(?:ում|ում\b)", raw, flags=re.I)
        if m_city:
            out["city"] = m_city.group(1).strip()
    if not out.get("city"):
        # "Հրազդանի Կենտրոնում" means city=Հրազդան, district=Կենտրոն.
        m_city_district = re.search(
            r"([\u0531-\u058F]{3,})ի\s+([\u0531-\u058F]{3,})(?:ում|ենում|անում)\b",
            raw, flags=re.I
        )
        if m_city_district:
            out["city"] = _normalize_place_name(m_city_district.group(1))
            out["district"] = _norm(m_city_district.group(2))
    if not out.get("city"):
        for pattern in city_patterns:
            m = re.search(pattern, raw, flags=re.I)
            if not m:
                continue
            candidate = m.group(1).strip(" .,;:()")
            if candidate.lower() not in {"the", "city", "ես", "կենտրոն"} and len(candidate) >= 3:
                out["city"] = candidate
                break

    if not out.get("business_name"):
        # Compact real-world form: "BIT servis կազմակերպությունը ..."
        m = re.search(
            r"^\s*([A-Za-zА-Яа-яЁёԱ-Ֆա-ֆ0-9][A-Za-zА-Яа-яЁёԱ-Ֆա-ֆ0-9._&'\- ]{1,80}?)\s+"
            r"(?:կազմակերպ(?:ությունը|ությունն)|կազմակերպություն|բիզնես(?:ը)?|ընկեր(?:ությունը|ություն)|սրահ(?:ը)?|"
            r"service|servis|սերվիս)\b",
            raw, flags=re.I
        )
        if m:
            out["business_name"] = _norm(m.group(1)).strip(" .,;:()«»\"'")

    if not out.get("business_name"):
        name_patterns = [
            r"[«\"']([^«»\"']{1,100})[»\"']\s+(?:անունով\s+)?(?:սրահ|բիզնես|կազմակերպություն|(?:տուրիստական\s+)?ընկերություն)",
            r"([A-Za-zА-Яа-яЁёԱ-Ֆա-ֆ0-9][A-Za-zА-Яа-яЁёԱ-Ֆա-ֆ0-9._&'_-]{0,60})\s+անունով\s+(?:սրահ|բիզնես|կազմակերպություն)",
            r"(?:salon|салон|studio|студия)\s+([A-Za-zА-Яа-яЁёԱ-Ֆա-ֆ0-9][A-Za-zА-Яа-яЁёԱ-Ֆա-ֆ0-9 .&'_-]{1,80})",
        ]
        name_patterns.extend([
            r"(?:ունեմ|ունենք)\s+\*{0,2}([A-Za-zА-Яа-яЁёԱ-Ֆա-ֆ0-9][A-Za-zА-Яа-яЁёԱ-Ֆա-ֆ0-9._&'\- ]{1,80})\*{0,2}\s*\*{0,2}\s+(?=(?:ավտոսպասարկման|ավտոսպասարկման կենտրոն|սրահ|բիզնես|կազմակերպություն|կենտրոն|ծառայություն))",
            r"(?:բիզնես(?:ի)?\s+անուն(?:ը)?|անվանում(?:ը)?)\s*[:՝-]\s*\*{0,2}([^\n,;.!?]{2,100})\*{0,2}"
        ])
        for pattern in name_patterns:
            m = re.search(pattern, raw, flags=re.I)
            if m:
                candidate = _norm(m.group(1)).strip(" .,;:()«»\"'")
                if candidate:
                    out["business_name"] = candidate
                    break
        if not out.get("business_name"):
            # Compact forms such as "BYUTI սրահ" / "BYUTI salon".
            m = re.search(
                r"\b([A-Za-zА-Яа-яЁёԱ-Ֆա-ֆ0-9][A-Za-zА-Яа-яЁёԱ-Ֆա-ֆ0-9._&'\\-]{1,60})\\s+(?:սրահ|salon|studio|студия)\\b",
                raw, flags=re.I
            )
            if m:
                out["business_name"] = _norm(m.group(1)).strip(" .,;:()«»\"'")

    # Deterministic recovery of contact/location facts if Groq fails.
    # Generic Armenian compact location: "ք Երևան, Օրբելու 22".
    if not out.get("city"):
        m = re.search(r"\bք(?:աղաք)?\s+([\u0531-\u058FԱ-Ֆա-ֆ-]{3,})\b", raw, flags=re.I)
        if m:
            out["city"] = _normalize_place_name(m.group(1))
    if not out.get("address") and out.get("city"):
        m = re.search(
            r"\bք(?:աղաք)?\s+[\u0531-\u058FԱ-Ֆա-ֆ-]{3,}\s*[,՝:]\s*([^.!?։\n]+?)(?=\s*,\s*(?:ժամը|ամեն|աշխատ)|\s*\.\s*|\s+ժամը\b|$)",
            raw, flags=re.I
        )
        if m:
            out["address"] = _norm(m.group(1)).strip(" .,;:")
    if not out.get("phone"):
        m = re.search(r"(?:հեռախոս(?:ահամար)?|հեռ\.?|телефон|phone)\s*[:՝-]?\s*(\+?\d[\d\s().-]{7,})", raw, flags=re.I)
        if m:
            out["phone"] = _norm(m.group(1)).strip(" .,-")
        else:
            m = re.search(r"\b(0\d{2}[\s-]?\d{6})\b", raw)
            if m:
                out["phone"] = _norm(m.group(1))
    if not out.get("address"):
        m = re.search(r"(?:հասցեն|հասցե|адрес|address)\s*[:՝-]?\s*([^.!?։\n]+)", raw, flags=re.I)
        if m:
            out["address"] = _norm(m.group(1)).strip(" .,;")
    if not out.get("working_hours"):
        hour_patterns = [
            r"(?:ամեն\s+օր\s*)?(?:՝|:|-)?\s*(?:ժամը\s*)?(\d{1,2}[\.:]\d{2})\s*(?:-ից|ից)?\s*(?:մինչև|[-–—])\s*(\d{1,2}[\.:]\d{2})\s*(?:-ը|ը)?",
            r"(?:daily|every\s+day|ежедневно)\s*[:\-]?\s*(\d{1,2}:\d{2})\s*(?:-|до|to)\s*(\d{1,2}:\d{2})",
            r"(?:աշխատում\s+ենք|աշխատանքային\s+ժամ(?:երը|եր)?|ժամերը)\s*(?:՝|:|-)?\s*([^.!?։\n]+)"
        ]
        for pattern in hour_patterns:
            hm = re.search(pattern, raw, flags=re.I)
            if hm:
                if len(hm.groups()) >= 2 and hm.group(2):
                    out["working_hours"] = f"Ամեն օր՝ {hm.group(1)}–{hm.group(2)}"
                else:
                    out["working_hours"] = _norm(hm.group(1))
                break

    if out.get("city"):
        out["city"] = _normalize_place_name(out.get("city"))

    if out.get("marz"):
        out["marz"] = _norm(out.get("marz"))

    # Keep a useful human-readable description when the model leaves it empty.
    if not out.get("description") and raw:
        out["description"] = raw

    if not out.get("marz") and out.get("city"):
        city_key = _normalize_place_name(out["city"]).lower()
        if city_key in _ARMENIA_CITY_TO_MARZ:
            out["marz"] = _ARMENIA_CITY_TO_MARZ[city_key]

    # Explicit region wording always wins over the city safety map.
    if not out.get("marz"):
        m_marz = re.search(
            r"(?:մարզ(?:ում|ը)?|марз|область|region)\s*[:՝-]?\s*([\u0531-\u058FА-Яа-яЁёA-Za-z -]{2,60})",
            raw, flags=re.I
        )
        if m_marz:
            out["marz"] = _norm(m_marz.group(1)).strip(" .,;:()")


    return out




def _recover_master_category(db, text: str, data: dict) -> dict:
    """Resolve a high-signal top-level direction when Groq omits its ID."""
    out = dict(data or {})
    try:
        masters = db.get_all_master_categories() or []
    except Exception:
        return out
    valid = {}
    for row in masters:
        mid = _safe_int(row.get("id"))
        if mid is None:
            continue
        valid[mid] = [_norm(row.get(k)) for k in ("name_am", "name_ru", "name_en") if _norm(row.get(k))]

    current = _norm(out.get("direction"))
    if current and _safe_int(out.get("master_category_id")) is None:
        low = current.lower()
        for mid, labels in valid.items():
            if any(low == label.lower() or low in label.lower() or label.lower() in low for label in labels):
                out["master_category_id"] = mid
                return out

    low_text = _norm(text).lower()
    if any(w in low_text for w in ("ֆոտոստուդ", "լուսանկար", "ֆոտոսեսիա", "տեսանկարահանում", "տեսանյութ", "photograph", "photo studio", "video")):
        for mid, labels in valid.items():
            joined = " ".join(labels).lower()
            if "ֆոտո" in joined and ("տեսանյութ" in joined or "video" in joined or "լուսանկար" in joined):
                out["master_category_id"] = mid
                out["direction"] = next((x for x in labels if x), out.get("direction") or "")
                return out
    return out


def _extract_price_mentions(text: str) -> list[int]:
    """Extract explicit monetary amounts; phone/address numbers are ignored."""
    raw = _norm(text)
    if not raw:
        return []
    pattern = r"(?<!\d)(\d{3,6})(?:[.,]\d{1,2})?\s*(?:դրամ(?:ից|ով|ի)?|դր\.?|֏|amd|dram|драм(?:ов|а)?|амд)(?!\w)"
    return [
        int(re.sub(r"[^0-9]", "", match.group(1)))
        for match in re.finditer(pattern, raw, flags=re.I)
    ]


async def _recover_missing_services(
    client,
    model: str,
    partner_text: str,
    services: list[dict],
    expected_prices: list[int],
) -> list[dict]:
    """Recover only price-bearing services that the first extraction missed.

    Existing services are treated as authoritative. Groq is asked only to map
    missing monetary amounts to the service phrase immediately associated with
    each amount; Python validates the returned prices before accepting them.
    """
    if not expected_prices:
        return services

    existing = [dict(x) for x in services if isinstance(x, dict)]
    existing_prices = []
    for item in existing:
        try:
            if item.get("price") is not None:
                existing_prices.append(int(float(item["price"])))
        except (TypeError, ValueError):
            pass

    missing_prices = list(expected_prices)
    for price in existing_prices:
        if price in missing_prices:
            missing_prices.remove(price)
    if not missing_prices:
        return existing

    # Give the model compact source snippets around every monetary amount.
    snippets = []
    for m in re.finditer(
        r"(?<!\d)(\d{3,6})(?:[.,]\d{1,2})?\s*(?:դրամ(?:ից|ով|ի)?|դր\.?|֏|amd|dram|драм(?:ов|а)?|амд)(?!\w)",
        partner_text,
        flags=re.I,
    ):
        start = max(0, m.start() - 140)
        end = min(len(partner_text), m.end() + 80)
        snippets.append({
            "price": int(re.sub(r"[^0-9]", "", m.group(1))),
            "context": partner_text[start:end].strip(),
        })

    schema = {
        "type": "object",
        "properties": {
            "services": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "service_name": {"type": "string"},
                        "price": {"type": "integer"},
                        "price_type": {"type": "string"},
                    },
                    "required": ["service_name", "price", "price_type"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["services"],
        "additionalProperties": False,
    }
    system = """You are a recovery extractor for a business registration form.
Recover ONLY services that are explicitly connected to a monetary amount in the
supplied source snippets.

Rules:
- Every missing price must correspond to exactly one service.
- Never invent a service or a price.
- Do not return an existing service again if it is already represented in the
  EXISTING SERVICES list.
- Keep the complete service phrase, including meaningful modifiers.
- Understand Armenian, Russian and English.
- Normalize the recovered service name to clean Armenian when possible.
- "և", "ու", "նաև", "and", "also", "и", "а" are connectors only; never delete
  the service itself.
- "3500 դրամ քմ-ից" means price=3500 and price_type="from_per_unit".
- "5000 դրամ մեկ պարապմունքից" means price=5000 and price_type="from_per_unit".
- "5000 դրամից" means price_type="from".
- Exact "5000 դրամ" means price_type="fixed".
Return ONLY genuinely missing services."""

    user = (
        "EXISTING SERVICES:\n" + json.dumps(existing, ensure_ascii=False)
        + "\n\nMISSING PRICES:\n" + json.dumps(missing_prices, ensure_ascii=False)
        + "\n\nSOURCE SNIPPETS:\n" + json.dumps(snippets, ensure_ascii=False)
        + "\n\nORIGINAL TEXT:\n" + _norm(partner_text)[:5000]
    )
    try:
        result = await _groq_json(
            client, model, system, user,
            "partner_missing_service_recovery", schema, 700
        )
    except Exception:
        return existing

    recovered = []
    for item in result.get("services") or []:
        if not isinstance(item, dict):
            continue
        name = _norm(item.get("service_name"))
        try:
            price = int(float(item.get("price")))
        except (TypeError, ValueError):
            continue
        if not name or price not in missing_prices:
            continue
        # Never accept a duplicate of an already extracted service+price.
        duplicate = any(
            _norm(x.get("name") or x.get("service_name")).lower() == name.lower()
            and int(float(x.get("price"))) == price
            for x in existing + recovered
            if x.get("price") not in (None, "")
        )
        if duplicate:
            continue
        ptype = _norm(item.get("price_type") or "fixed")
        if "per_unit" not in ptype and ptype not in {"from", "fixed"}:
            ptype = "fixed"
        recovered.append({
            "name": name,
            "raw_sub_direction": name,
            "price": price,
            "price_type": ptype,
            "matched_subcategory_id": None,
        })
        missing_prices.remove(price)
    return existing + recovered


def _recover_services_from_history(history: list[dict]) -> list[dict]:
    """Deterministically recover every explicit price-bearing service from history."""
    text = " ".join(
        _norm(x.get("content") or "")
        for x in history
        if str(x.get("role") or "").lower() == "user"
    )
    if not text:
        return []

    # Handle common price-list separators. This keeps every price-bearing item
    # atomic even when Groq returns only a partial service array.
    clauses = re.split(r"[,;\n/։.!?]+", text)
    found = []
    currency_re = r"(?:դրամ(?:ից|ով|ի)?|դր\.?|֏|amd|dram|драм(?:ов|а)?|амд)"
    price_re = re.compile(
        rf"(?P<name>.+?)\s*(?:՝|:|—|–|-|\b(?:սկսվում\s+են|սկսվում\s+է|սկսած|արժե|գինն\s+է|от|from|starting\s+at)\b)?\s*"
        rf"(?P<price>\d[\d\s.,]*)\s*(?P<currency>{currency_re})\b", flags=re.I
    )
    for clause in clauses:
        clause = _norm(clause).strip(" —–-:;")
        if not clause:
            continue
        m = price_re.search(clause)
        if not m:
            # Some partners omit the currency for a clearly service-priced clause,
            # e.g. "Մեկօրյա տուրը սկսած 8000". Accept only when the clause
            # contains a service/tour signal; never treat arbitrary numbers as prices.
            m_plain = re.search(
                r"(?P<name>.+?)\s*(?:՝|:|—|–|-|\b(?:սկսած|արժե|գինն\s+է|from|starting\s+at)\b)\s*(?P<price>\d[\d\s.,]*)\s*$",
                clause, flags=re.I
            )
            if not m_plain or not re.search(
                r"տուր|տուրեր|շրջագայ|էքսկուրս|ծառայ|tour|trip|travel|excursion|услуг|тур",
                m_plain.group("name"), flags=re.I
            ):
                continue
            class _PlainPrice:
                def __init__(self, name, price):
                    self._name=name; self._price=price
                def group(self, key):
                    return self._name if key=="name" else self._price
            m = _PlainPrice(m_plain.group("name"), m_plain.group("price"))
            currency_re_match = False
        else:
            currency_re_match = True
        if not m:
            continue
        name = _norm(m.group("name")).strip(" —–-:;")
        low_name = name.lower()
        intro_markers = (
            "հիմնական ծառայություններն են", "ծառայություններն են",
            "հիմնական ծառայություններ", "մեր ծառայություններն են",
            "основные услуги", "услуги:", "main services", "our services"
        )
        if any(marker in low_name for marker in intro_markers):
            parts = re.split(r"[՝:]", name, maxsplit=1)
            if len(parts) == 2:
                name = _norm(parts[1]).strip(" —–-:;")
        name = re.sub(
            r"^(?:Ես\s+[^,;.!?։]*?\s+)?(?:ունեմ|ունենք|կատարում\s+ենք|անում\s+ենք|մատուցում\s+ենք|"
            r"առաջարկում\s+ենք|мы\s+делаем|оказываем|предлагаем|we\s+(?:do|offer|provide))\s+",
            "", name, flags=re.I
        ).strip()
        name = re.sub(r"^(?:սրահում|մեզ\s+մոտ)\s+", "", name, flags=re.I).strip()
        name = re.sub(r"^(?:ինչպես\s+նաև|նաև|և|ու)\s+", "", name, flags=re.I).strip()
        name = re.sub(r"^.*(?:հիմնական ծառայություններն են|ծառայություններն են)\s*[՝:]\s*", "", name, flags=re.I)
        if not name:
            continue
        try:
            price = float(m.group("price").replace(" ", "").replace(",", "."))
        except ValueError:
            continue
        full = clause.lower()
        is_from = bool("ից" in full or re.search(r"\b(?:от|from|starting\s+at|սկսվում\s+են|սկսվում\s+է)\b", full, re.I))
        is_per_unit = bool(re.search(r"\b(?:քմ|քառակուսի\s*մետր|кв\.?\s*м|за\s+кв\.?\s*м|պարապմունք|занят(?:ие|ия)|за\s+занятие)\b", full, re.I))
        price_type = ("from_per_unit" if is_from else "fixed_per_unit") if is_per_unit else ("from" if is_from else "fixed")
        found.append({"name": name, "raw_sub_direction": name, "price": price, "price_type": price_type, "matched_subcategory_id": None})

    result=[]; seen=set()
    for item in found:
        key=(item["name"].lower(),item["price"],item["price_type"])
        if key not in seen:
            seen.add(key); result.append(item)
    return result

async def classify_profile_catalog(db, profile: dict) -> dict:
    """Reclassify the services of an already extracted partner profile.

    This is intentionally a compatibility entry point for the partner
    application editor.  Catalogue IDs always come from the active DB
    catalogue; the model is never allowed to invent them.
    """
    profile = dict(profile or {})
    services = profile.get("services") if isinstance(profile.get("services"), list) else []
    services = [dict(x) for x in services if isinstance(x, dict) and str(x.get("name") or x.get("service_name") or "").strip()]
    if not services:
        return {"services": [], "master_category_id": _safe_int(profile.get("master_category_id")), "confidence": 0.0, "needs_review": False, "ambiguities": []}

    master_id = _safe_int(
        profile.get("master_category_id")
        or profile.get("ai_master_category_id")
    )
    if master_id is None:
        # Use the same deterministic master-category recovery already used
        # by onboarding when a profile was edited without an internal ID.
        recovered = _recover_master_category(
            db,
            " ".join([
                str(profile.get("business_name") or ""),
                str(profile.get("description") or ""),
                str(profile.get("direction") or ""),
                str(profile.get("master_category_name") or ""),
                " ".join(str(s.get("name") or "") for s in services),
            ]),
            profile,
        )
        master_id = _safe_int(recovered.get("master_category_id") or recovered.get("master_category_id"))
    if master_id is None:
        return {"services": services, "master_category_id": None, "confidence": 0.0, "needs_review": True, "ambiguities": ["master_category_missing"]}

    catalog = []
    try:
        catalog = get_catalog_for_master(db, master_id)
    except Exception:
        catalog = []
    # get_catalog_for_master may be empty on legacy adapters; use the
    # adapter's explicit method as a compatibility fallback.
    if not catalog and hasattr(db, "get_subcategories_by_master"):
        for row in db.get_subcategories_by_master(master_id) or []:
            catalog.append({
                "category_id": row.get("id"),
                "master_category_id": row.get("master_category_id"),
                "category_am": row.get("name_am"),
                "category_ru": row.get("name_ru"),
                "category_en": row.get("name_en"),
            })

    matched = _dynamic_catalog_match(
        services,
        catalog,
        threshold=0.70,
    )


    unresolved = [
        str(x.get("name") or x.get("service_name") or "").strip()
        for x in matched
        if _safe_int(x.get("matched_subcategory_id")) is None
    ]
    confidence_values = [
        float(x.get("match_confidence") or 0)
        for x in matched
        if _safe_int(x.get("matched_subcategory_id")) is not None
    ]
    return {
        "services": matched,
        "master_category_id": master_id,
        "confidence": min(confidence_values) if confidence_values else 0.0,
        "needs_review": bool(unresolved),
        "ambiguities": unresolved,
    }


def _parse_json(text: str) -> dict:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
        raw = re.sub(r"\s*```$", "", raw)
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        raw = raw[start:end + 1]
    return json.loads(raw)



async def _groq_json(client, model, system_prompt, user_content, schema_name, schema, max_tokens):
    """Compatibility wrapper now routed through the unified AI gateway.

    The historical function name is kept so existing onboarding/classification
    code remains stable while provider/model selection becomes centralized.
    """
    from ai_service import AIService
    service = AIService()
    return await service.structured_json(
        system_prompt,
        user_content,
        schema_name=schema_name,
        schema=schema,
        max_tokens=max_tokens,
        chain="partner_ai",
        stage=schema_name,
        operation=schema_name,
        purpose="structured partner extraction/classification",
    )

async def _groq_catalog_rerank(
    services: list[dict],
    catalog_rows: list[dict],
) -> list[dict]:
    """Semantic catalogue reranking without exposing database IDs to Groq.

    Candidates are selected only from the live DB. Groq returns a candidate
    position; Python maps that position back to the real DB category ID.
    """
    if not services or not catalog_rows or AsyncGroq is None:
        return [dict(x) for x in services]

    key = os.getenv("GROQ_API_KEY", "").strip()
    if not key:
        return [dict(x) for x in services]

    # Build a small candidate set per service from the live catalogue using
    # generic string similarity. This is retrieval only, never classification.
    retrieved = []
    for item in services:
        name = _norm(item.get("name") or item.get("service_name"))
        if not name:
            retrieved.append([])
            continue

        scored = []
        for row in catalog_rows:
            labels = [
                _norm(row.get("category_am")),
                _norm(row.get("category_ru")),
                _norm(row.get("category_en")),
            ]
            labels = [x for x in labels if x]
            if not labels:
                continue
            score = max(_dynamic_catalog_match_score(name, label) for label in labels)
            scored.append((score, row))
        scored.sort(key=lambda x: x[0], reverse=True)

        candidates = []
        seen = set()
        for score, row in scored[:12]:
            cid = _safe_int(row.get("category_id"))
            if cid is None or cid in seen:
                continue
            seen.add(cid)
            candidates.append({
                "candidate_index": len(candidates),
                "name_am": _norm(row.get("category_am")),
                "name_ru": _norm(row.get("category_ru")),
                "name_en": _norm(row.get("category_en")),
                "retrieval_score": round(float(score), 3),
            })
        retrieved.append(candidates)

    payload = [
        {
            "service_index": i,
            "service": _norm(item.get("name") or item.get("service_name")),
            "candidates": candidates,
        }
        for i, (item, candidates) in enumerate(zip(services, retrieved))
        if candidates
    ]
    if not payload:
        return [dict(x) for x in services]

    system = """You are the semantic matching layer for Armenia AI Guide.
For each extracted service, choose the candidate subcategory that means the
same service. Understand Armenian, Russian and English, including inflected
forms, synonyms and natural wording.

Return ONLY JSON:
{"matches":[{"service_index":0,"candidate_index":0,"confidence":0.0}]}

Rules:
- Choose only from the supplied candidates.
- candidate_index is the position inside that service's candidates array.
- Never invent candidates or IDs.
- If none is a reliable semantic match, omit that service from matches.
- confidence must be between 0 and 1.
"""
    user = json.dumps(payload, ensure_ascii=False)

    try:
        client = AsyncGroq(api_key=key)
        response = await client.chat.completions.create(
            model=os.getenv("PARTNER_ONBOARDING_MODEL", os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")),
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=500,
        )
        parsed = _parse_json(response.choices[0].message.content or "{}")
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("Groq semantic catalogue rerank failed: %s", exc)
        return [dict(x) for x in services]

    out = [dict(x) for x in services]
    for match in parsed.get("matches") or []:
        if not isinstance(match, dict):
            continue
        si = _safe_int(match.get("service_index"))
        ci = _safe_int(match.get("candidate_index"))
        if si is None or ci is None or si < 0 or si >= len(out):
            continue
        candidates = retrieved[si] if si < len(retrieved) else []
        if ci < 0 or ci >= len(candidates):
            continue
        try:
            confidence = float(match.get("confidence") or 0)
        except Exception:
            confidence = 0
        if confidence < 0.60:
            continue

        candidate = candidates[ci]
        service = out[si]
        row = next(
            (
                r for r in catalog_rows
                if _safe_int(r.get("category_id")) is not None
                and _safe_int(r.get("category_id")) == next(
                    (
                        _safe_int(x.get("category_id"))
                        for x in catalog_rows
                        if _safe_int(x.get("category_id")) is not None
                        and _norm(x.get("category_am")) == candidate["name_am"]
                        and _norm(x.get("category_ru")) == candidate["name_ru"]
                        and _norm(x.get("category_en")) == candidate["name_en"]
                    ),
                    None,
                )
            ),
            None,
        )
        if not row:
            continue

        service["matched_subcategory_id"] = _safe_int(row.get("category_id"))
        service["direction_id"] = _safe_int(row.get("master_id"))
        service["match_confidence"] = confidence
        service["match_method"] = "groq_semantic_rerank"
        service["subcategory_name"] = (
            _norm(row.get("category_am"))
            or _norm(row.get("category_ru"))
            or _norm(row.get("category_en"))
            or None
        )
        service["direction_name"] = (
            _norm(row.get("master_am"))
            or _norm(row.get("master_ru"))
            or _norm(row.get("master_en"))
            or None
        )

    return out


async def _semantic_catalog_match(
    services: list[dict],
    catalog_rows: list[dict],
    threshold: float = 0.72,
) -> list[dict]:
    """Prefer vector matching; fall back to semantic Groq reranking.

    Vector matching is used when OpenAI embeddings and indexed catalogue
    vectors are available. If they are unavailable (including exhausted
    OpenAI quota), registration still performs semantic matching using live
    DB candidates and Groq. No category mapping is hardcoded.
    """
    out = [dict(x) for x in services]
    try:
        from embeddings_matcher import match_service_to_catalog
        for item in out:
            if _safe_int(item.get("matched_subcategory_id")) is not None:
                continue
            name = _norm(item.get("name") or item.get("service_name"))
            if not name:
                continue
            match = await match_service_to_catalog(name, threshold=threshold)
            if not match:
                continue
            item["matched_subcategory_id"] = _safe_int(match["category_id"])
            item["direction_id"] = _safe_int(match["master_id"])
            item["match_confidence"] = float(match["similarity"])
            item["match_method"] = "embedding_cosine"
            item["subcategory_name"] = (
                _norm(match.get("category_am"))
                or _norm(match.get("category_ru"))
                or _norm(match.get("category_en"))
                or None
            )
            item["direction_name"] = (
                _norm(match.get("master_am"))
                or _norm(match.get("master_ru"))
                or _norm(match.get("master_en"))
                or None
            )
    except Exception as exc:
        import logging
        logging.getLogger(__name__).info(
            "Embedding catalogue matching unavailable; using Groq semantic rerank: %s",
            exc,
        )

    unresolved = [
        x for x in out
        if _safe_int(x.get("matched_subcategory_id")) is None
    ]
    if unresolved:
        reranked = await _groq_catalog_rerank(unresolved, catalog_rows)
        by_name = {
            _norm(x.get("name") or x.get("service_name")).lower(): x
            for x in reranked
        }
        for item in out:
            key = _norm(item.get("name") or item.get("service_name")).lower()
            if key in by_name:
                item.update(by_name[key])

    return out


def _dynamic_catalog_match(
    services: list[dict],
    catalog_rows: list[dict],
    threshold: float = 0.70,
) -> list[dict]:
    """Assign real DB subcategory IDs using only the live catalogue.

    No AI-generated IDs, keyword dictionaries, category aliases or hardcoded
    service/category mappings are used.  Every assigned ID and parent direction
    comes directly from the current database rows.
    """
    out = [dict(x) for x in services]

    candidates = []
    for row in catalog_rows or []:
        category_id = _safe_int(row.get("category_id") or row.get("id"))
        master_id = _safe_int(row.get("master_id") or row.get("master_category_id"))
        if category_id is None:
            continue

        labels = [
            _norm(row.get("category_am") or row.get("name_am")),
            _norm(row.get("category_ru") or row.get("name_ru")),
            _norm(row.get("category_en") or row.get("name_en")),
        ]
        labels = [x for x in labels if x]
        if labels:
            candidates.append((category_id, master_id, labels))

    for item in out:
        if _safe_int(item.get("matched_subcategory_id")) is not None:
            continue

        service_name = _norm(item.get("name") or item.get("service_name"))
        if not service_name:
            continue

        best = None
        for category_id, master_id, labels in candidates:
            score = max(
                _dynamic_catalog_match_score(service_name, label)
                for label in labels
            )
            if best is None or score > best["score"]:
                best = {
                    "category_id": category_id,
                    "master_id": master_id,
                    "score": score,
                }

        if best and best["score"] >= threshold:
            item["matched_subcategory_id"] = best["category_id"]
            item["direction_id"] = best["master_id"]
            item["match_confidence"] = round(best["score"], 3)
            item["match_reason"] = "Dynamic live-database fuzzy catalogue match."
        else:
            item["matched_subcategory_id"] = None
            item["direction_id"] = None
            item["match_confidence"] = round(best["score"], 3) if best else 0.0
            item["match_reason"] = "No live catalogue match reached the 70% threshold."

    return out

def _build_categories_tree(categories_list: list[dict], source_text: str = "") -> list[dict]:
    """Build the smallest useful DB-backed semantic classification tree.

    Groq's current 8K TPM limit makes a fully bilingual 320-row tree too large
    because Armenian/Russian text tokenizes expensively. Prefer the language
    actually used by the partner; keep the other language only as fallback for
    mixed/Latin input. IDs remain the real database subcategory IDs.
    """
    has_arm = bool(re.search(r"[\u0531-\u058F]", source_text or ""))
    has_cyr = bool(re.search(r"[А-Яа-яЁё]", source_text or ""))
    tree = []
    seen = set()
    for row in categories_list or []:
        if not isinstance(row, dict):
            continue
        category_id = _safe_int(row.get("category_id") or row.get("id"))
        if category_id is None or category_id in seen:
            continue
        seen.add(category_id)
        am = _norm(row.get("category_am") or row.get("name_am"))
        ru = _norm(row.get("category_ru") or row.get("name_ru"))
        if has_arm and not has_cyr:
            name = am or ru
        elif has_cyr and not has_arm:
            name = ru or am
        else:
            name = am or ru
        if name:
            tree.append({"id": category_id, "parent_id": _safe_int(row.get("master_id")), "name": name})
    return tree


async def extract_partner_registration_json(
    raw_text: str,
    categories_list: list[dict] | None = None,
    *,
    model: str = "openai/gpt-oss-20b",
    max_tokens: int = 900,
) -> dict:
    """Extract partner facts only.

    Groq never sees catalogue IDs and never performs category classification.
    Classification is performed afterwards by Python against the live DB.
    """
    key = os.getenv("GROQ_API_KEY", "").strip()
    if not key:
        raise RuntimeError("GROQ_API_KEY is not configured")

    source = _norm(raw_text)
    if len(source) > 9000:
        source = source[-9000:]

    instruction = """You are the Armenia AI Guide partner-registration extraction AI.
Understand Armenian, Russian and English.

Your ONLY job is to extract factual information from the partner's text.
Do NOT classify services and do NOT return category IDs.

Return ONLY this JSON object:
{
  "company_or_name": string|null,
  "marz": string|null,
  "city": string|null,
  "address": string|null,
  "phone": string|null,
  "working_hours": string|null,
  "document_type": string|null,
  "extracted_services": [
    {
      "name": string,
      "price": number|null,
      "price_type": "fixed"|"from"
    }
  ]
}

FACT EXTRACTION:
- Extract only facts explicitly present in the partner text.
- Never invent a business name, location, phone, address, schedule, service or price.
- company_or_name: exact business/company/organization name if explicitly stated.
- city: normalize an explicitly stated city/locality, including ordinary inflected forms.
- marz: use an explicit marz/region or a reliable city-to-marz relationship only.
- address: return only the actual address.
- phone: preserve the stated phone number.
- working_hours: preserve an explicit schedule.
- document_type: only when explicitly mentioned; otherwise null.

SERVICE EXTRACTION:
- Extract only actual services offered by the partner.
- Return every independent service as a separate object.
- Keep the user's wording or only lightly normalize grammar/spacing.
- Preserve meaningful modifiers that distinguish one service from another.
- Do not replace a service with a catalogue/category name.
- Do not invent, merge, split, classify, translate or reinterpret services beyond
  what is needed to make each service name readable.
- Never put a business name, address, phone number, schedule or explanation into
  a service name.
- Never invent a price.
- Keep every explicit price attached to the correct service.
- A shared price may be attached to multiple enumerated services only when the
  source grammar clearly applies that price to all of them.
- price is numeric only.
- "3000 դրամից", "от 3000", "from 3000" => price_type="from".
- Exact stated price => price_type="fixed".

Do not output markdown or explanatory text.
Do not output category_id.
Do not output subcategory_id.
Do not output direction_id.
Do not output matched_subcategory_id."""

    user = "PARTNER TEXT:\n" + source

    client = AsyncGroq(api_key=key)
    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": instruction},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
        reasoning_effort="low",
        temperature=0,
        max_tokens=max_tokens,
    )

    parsed = _parse_json(response.choices[0].message.content or "{}")
    if not isinstance(parsed, dict):
        raise RuntimeError("Invalid partner registration JSON")

    services = []
    seen = set()
    for item in parsed.get("extracted_services") or []:
        if not isinstance(item, dict):
            continue

        name = _norm(item.get("name") or item.get("user_service_name"))
        if not name:
            continue

        raw_price = item.get("price")
        try:
            price = float(raw_price) if raw_price is not None else None
        except (TypeError, ValueError):
            price = None
        if price is not None and price.is_integer():
            price = int(price)

        price_type = _norm(item.get("price_type") or "fixed").lower()
        if price_type not in {"fixed", "from"}:
            price_type = "fixed"

        key_tuple = (name.lower(), price, price_type)
        if key_tuple in seen:
            continue
        seen.add(key_tuple)

        services.append({
            "name": name,
            "raw_sub_direction": name,
            "price": price,
            "price_type": price_type,
            "matched_subcategory_id": None,
        })

    parsed["company_or_name"] = _norm(parsed.get("company_or_name")) or None
    parsed["marz"] = _norm(parsed.get("marz")) or None
    parsed["city"] = _norm(parsed.get("city")) or None
    parsed["address"] = _norm(parsed.get("address")) or None
    parsed["phone"] = _norm(parsed.get("phone")) or None
    parsed["working_hours"] = _norm(parsed.get("working_hours")) or None
    parsed["document_type"] = _norm(parsed.get("document_type")) or None
    parsed["extracted_services"] = services
    return parsed


async def extract(
    text: str,
    history: list[dict],
    db,
    previous_profile: dict | None = None,
    pending_field: str | None = None,
) -> dict:
    """Extract partner facts, then classify services against the live DB catalogue.

    Groq is used only for factual extraction. Category/subcategory IDs are
    always selected by Python from current database rows.
    """
    previous_profile = dict(previous_profile or {})
    combined_text = _norm(
        " ".join(
            [str(x.get("content") or "") for x in history if isinstance(x, dict)]
            + [text]
        )
    )

    try:
        extracted = await extract_partner_registration_json(
            combined_text,
            model=os.getenv(
                "PARTNER_ONBOARDING_MODEL",
                os.getenv("GROQ_MODEL", "openai/gpt-oss-20b"),
            ).strip() or "openai/gpt-oss-20b",
            max_tokens=900,
        )

        data = dict(previous_profile)
        data.update({
            "business_name": _norm(
                extracted.get("company_or_name")
                or previous_profile.get("business_name")
            ) or None,
            "marz": _norm(
                extracted.get("marz")
                or previous_profile.get("marz")
            ) or None,
            "city": _norm(
                extracted.get("city")
                or previous_profile.get("city")
            ) or None,
            "address": _norm(
                extracted.get("address")
                or previous_profile.get("address")
            ) or None,
            "phone": _norm(
                extracted.get("phone")
                or previous_profile.get("phone")
            ) or None,
            "working_hours": _norm(
                extracted.get("working_hours")
                or previous_profile.get("working_hours")
            ) or None,
            "document_type": _norm(
                extracted.get("document_type")
                or previous_profile.get("document_type")
            ) or None,
            "description": _norm(
                extracted.get("description")
                or previous_profile.get("description")
                or combined_text
            ),
        })

        services = []
        for item in extracted.get("extracted_services") or []:
            if not isinstance(item, dict):
                continue

            name = _norm(item.get("name"))
            if not name:
                continue

            raw_price = item.get("price")
            try:
                price = float(raw_price) if raw_price not in (None, "") else None
            except (TypeError, ValueError):
                price = None

            if price is not None and price.is_integer():
                price = int(price)

            price_type = _norm(item.get("price_type") or "fixed").lower()
            if price_type not in {"fixed", "from"}:
                price_type = "fixed"

            services.append({
                "name": name,
                "raw_sub_direction": name,
                "price": price,
                "price_type": price_type,
                "matched_subcategory_id": None,
            })

        # If this turn is explicitly answering the services question, preserve
        # the user's service text even when Groq temporarily returns no items.
        if pending_field == "services" and not services and _norm(text):
            services = [{
                "name": _norm(text),
                "raw_sub_direction": _norm(text),
                "price": None,
                "price_type": "unknown",
                "matched_subcategory_id": None,
            }]

        data["services"] = services

        # Recover only generic factual fields that Groq may omit.
        data = _recover_obvious_facts(combined_text, data)

        # Classification is deliberately isolated from extraction.
        # A vector/DB/provider problem must NEVER erase a successful Groq
        # extraction. The registration form must still receive the facts and
        # services even when semantic classification is temporarily unavailable.
        data["services"] = services
        data["master_category_id"] = None
        data["direction"] = None
        data["classification_confidence"] = 0
        data["classification_ambiguities"] = []
        data["classification_needs_review"] = True

        try:
            catalog_rows = get_catalog(db)
            classified_services = await _semantic_catalog_match(
                services,
                catalog_rows,
                threshold=float(os.getenv("CATALOG_EMBEDDING_THRESHOLD", "0.72")),
            )
            data["services"] = classified_services

            matched = [
                s for s in classified_services
                if _safe_int(s.get("matched_subcategory_id")) is not None
            ]

            if matched:
                catalog_map = {
                    _safe_int(row.get("category_id")): row
                    for row in catalog_rows
                    if _safe_int(row.get("category_id")) is not None
                }
                first_row = catalog_map.get(
                    _safe_int(matched[0].get("matched_subcategory_id"))
                )

                if first_row:
                    data["master_category_id"] = _safe_int(first_row.get("master_id"))
                    data["direction"] = (
                        _norm(first_row.get("master_am"))
                        or _norm(first_row.get("master_ru"))
                        or _norm(first_row.get("master_en"))
                        or None
                    )

                    for service in classified_services:
                        row = catalog_map.get(
                            _safe_int(service.get("matched_subcategory_id"))
                        )
                        if not row:
                            continue
                        service["direction_id"] = _safe_int(row.get("master_id"))
                        service["direction_name"] = (
                            _norm(row.get("master_am"))
                            or _norm(row.get("master_ru"))
                            or _norm(row.get("master_en"))
                            or None
                        )
                        service["subcategory_name"] = (
                            _norm(row.get("category_am"))
                            or _norm(row.get("category_ru"))
                            or _norm(row.get("category_en"))
                            or None
                        )

                    data["classification_confidence"] = max(
                        float(s.get("match_confidence") or 0)
                        for s in matched
                    )
                    data["classification_ambiguities"] = []
                    data["classification_needs_review"] = False
                else:
                    data["classification_ambiguities"] = ["subcategory_not_in_catalog"]
            else:
                data["classification_ambiguities"] = ["subcategory_not_matched"]
        except Exception as classification_exc:
            import logging
            logging.getLogger(__name__).warning(
                "Partner registration semantic classification skipped; extraction preserved: %s",
                classification_exc,
            )

        import logging
        logging.getLogger(__name__).info(
            "PARTNER_CLASSIFICATION: services=%s catalog_rows=%s matched=%s unresolved=%s",
            len(data.get("services") or []),
            len(catalog_rows),
            len(matched),
            len(data.get("services") or []) - len(matched),
        )

    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning(
            "Partner registration extraction/classification failed: %s",
            exc,
        )

        # Safe fallback: recover facts already present in the conversation,
        # but never invent category IDs.
        data = dict(previous_profile)
        data.setdefault("business_name", None)
        data.setdefault("marz", None)
        data.setdefault("city", None)
        data.setdefault("address", None)
        data.setdefault("phone", None)
        data.setdefault("working_hours", None)
        data.setdefault("document_type", None)
        data.setdefault("description", combined_text)

        data = _recover_obvious_facts(combined_text, data)

        fallback_services = []
        for item in data.get("services") or []:
            if not isinstance(item, dict):
                continue
            name = _norm(
                item.get("name")
                or item.get("service_name")
                or item.get("service")
            )
            if not name:
                continue
            fallback_services.append({
                **item,
                "name": name,
                "raw_sub_direction": _norm(
                    item.get("raw_sub_direction") or name
                ),
                "matched_subcategory_id": None,
            })

        if pending_field == "services" and not fallback_services and _norm(text):
            fallback_services = [{
                "name": _norm(text),
                "raw_sub_direction": _norm(text),
                "price": None,
                "price_type": "unknown",
                "matched_subcategory_id": None,
            }]

        data["services"] = fallback_services
        data["master_category_id"] = None
        data["direction"] = None
        data["classification_needs_review"] = bool(fallback_services)

    data["missing"] = [
        field
        for field in ("business_name", "marz", "city", "address", "phone", "services")
        if not data.get(field)
    ]
    data["ready"] = not data["missing"]
    return data

def missing_question(data: dict, lang: str) -> str:
    """Ask only for information that is actually missing.

    The application may contain many structured fields, but the partner never
    has to follow a numbered questionnaire. One natural message can fill any
    number of missing fields, and the next prompt is generated from what is
    still absent.
    """
    missing = [str(x) for x in (data.get("missing") or [])]
    labels = {
        "hy": {
            "business_name": "բիզնեսի անունը",
            "marz": "տեղադրությունը",
            "city": "քաղաքը կամ բնակավայրը",
            "address": "հասցեն",
            "phone": "հեռախոսահամարը",
            "services": "ծառայությունները և, եթե հայտնի է, դրանց գները",
        },
        "ru": {
            "business_name": "название бизнеса",
            "marz": "местоположение",
            "city": "город или населённый пункт",
            "address": "адрес",
            "phone": "телефон",
            "services": "услуги и, если известны, их цены",
        },
        "en": {
            "business_name": "business name",
            "marz": "location",
            "city": "city or locality",
            "address": "address",
            "phone": "phone number",
            "services": "services and, if known, their prices",
        },
    }
    if not missing:
        return {
            "hy": "Հայտը պատրաստ է։",
            "ru": "Заявка готова.",
            "en": "The application is ready.",
        }.get(lang, "Заявка готова.")

    lang_labels = labels.get(lang, labels["ru"])
    items = [lang_labels[x] for x in missing if x in lang_labels]
    if lang == "hy":
        return "Որպեսզի հայտը ամբողջական լինի, նշեք նաև՝ " + ", ".join(items) + "։ Կարող եք ամեն ինչ գրել մեկ հաղորդագրությամբ։"
    if lang == "en":
        return "To complete the application, please provide: " + ", ".join(items) + ". You can send everything in one message."
    return "Чтобы завершить заявку, укажите ещё: " + ", ".join(items) + ". Можно написать всё одним сообщением."