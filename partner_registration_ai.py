"""AI-first partner onboarding for Armenia AI Guide."""
from __future__ import annotations

import json
import os
import re
from typing import Any

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


def get_catalog(db) -> list[dict]:
    rows = []
    try:
        for master in db.get_all_master_categories() or []:
            mid = _safe_int(master.get("id"))
            if mid is None:
                continue
            for sub in db.get_subcategories_by_master(mid) or []:
                cid = _safe_int(sub.get("id"))
                if cid is None:
                    continue
                rows.append({
                    "master_id": mid,
                    "master_am": master.get("name_am") or master.get("name_hy") or "",
                    "master_ru": master.get("name_ru") or "",
                    "master_en": master.get("name_en") or "",
                    "master_slug": master.get("slug") or "",
                    "category_id": sub.get("id"),
                    "category_am": sub.get("name_am") or sub.get("name_hy") or "",
                    "category_ru": sub.get("name_ru") or "",
                    "category_en": sub.get("name_en") or "",
                    "category_slug": sub.get("slug") or "",
                })
    except Exception:
        return []
    return rows


def get_master_catalog(db) -> list[dict]:
    """Return only top-level directions for the first AI classification step."""
    rows = []
    try:
        for master in db.get_all_master_categories() or []:
            mid = _safe_int(master.get("id"))
            if mid is None:
                continue
            rows.append({
                "master_id": mid,
                "master_am": master.get("name_am") or master.get("name_hy") or "",
                "master_ru": master.get("name_ru") or "",
                "master_en": master.get("name_en") or "",
                "master_slug": master.get("slug") or "",
            })
    except Exception:
        return []
    return rows


def get_catalog_for_master(db, master_id: int | None) -> list[dict]:
    """Load subcategories only from the already selected direction."""
    mid = _safe_int(master_id)
    if mid is None:
        return []
    return [row for row in get_catalog(db) if _safe_int(row.get("master_id")) == mid]


def _heuristic(text: str) -> dict:
    low = _norm(text).lower()
    aliases = {
        "Недвижимость": ["недвиж", "квартир", "дом", "участок", "аренд", "продаж", "real estate"],
        "Красота и уход": ["салон", "парикмах", "маникюр", "педикюр", "барбер", "космет", "beauty", "hair"],
        "Питание и кулинария": ["ресторан", "кафе", "пицц", "еда", "кухн", "food"],
        "Пассажирские перевозки и такси": ["такси", "трансфер", "перевоз", "transport"],
        "Автоуслуги": ["авто", "машин", "шиномонтаж", "автосервис", "car"],
        "Туризм и путешествия": ["экскурс", "гид", "тур", "поездк", "travel", "tour"],
        "Спорт и фитнес": ["спорт", "фитнес", "тренер", "fitness"],
        "IT и цифровые услуги": ["it", "програм", "сайт", "компьютер", "software", "digital"],
    }
    direction = next((name for name, words in aliases.items() if any(w in low for w in words)), None)
    return {"business_name": None, "city": None, "district": None, "direction": direction,
            "master_category_id": None, "subcategory_names": [], "description": _norm(text),
            "services": [], "missing": ["business_name", "marz", "city", "address", "phone", "services"], "ready": False}


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
        for pattern in city_patterns:
            m = re.search(pattern, raw, flags=re.I)
            if not m:
                continue
            candidate = m.group(1).strip(" .,;:()")
            if candidate.lower() not in {"the", "city", "ես"} and len(candidate) >= 3:
                out["city"] = candidate
                break

    if not out.get("business_name"):
        name_patterns = [
            r"[«\"']([^«»\"']{1,100})[»\"']\s+անունով\s+(?:սրահ|բիզնես|կազմակերպություն)",
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

    # Deterministic recovery of contact/location facts if Groq fails.
    if not out.get("phone"):
        m = re.search(r"(?:հեռախոս(?:ահամար)?|телефон|phone)\s*[:՝-]?\s*(0\d[\d\s().-]{6,})", raw, flags=re.I)
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
            r"(?:ամեն\s+օր)\s*(?:՝|:|-)?\s*(?:ժամը\s*)?(\d{1,2}:\d{2})\s*(?:-ից|ից)?\s*(?:մինչև|[-–—])\s*(\d{1,2}:\d{2})\s*(?:-ը|ը)?",
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

    if not out.get("direction") and re.search(
        r"ֆոտոստուդ|լուսանկար|ֆոտոսեսիա|տեսանկարահանում|տեսանյութ|фотостуд|фотосес|видеосъём|видеосъем|photograph|video",
        low, flags=re.I
    ):
        out["direction"] = "📸 Ֆոտո և տեսանյութ"

    if not out.get("marz") and out.get("city"):
        city_key = _norm(out["city"]).lower()
        marz_by_city = {
            "հրազդան": "Կոտայք", "ռազդան": "Կոտայք", "hrazdan": "Կոտայք",
            "աբովյան": "Կոտայք", "abovyan": "Կոտայք",
            "չարենցավան": "Կոտայք", "charentsavan": "Կոտայք",
            "գյումրի": "Շիրակ", "gyumri": "Շիրակ",
            "վանաձոր": "Լոռի", "vanadzor": "Լոռի",
            "արմավիր": "Արմավիր", "armavir": "Արմավիր",
            "էջմիածին": "Արմավիր", "ejmiatsin": "Արմավիր",
            "արտաշատ": "Արարատ", "artashat": "Արարատ",
            "գավառ": "Գեղարքունիք", "gavar": "Գեղարքունիք",
            "դիլիջան": "Տավուշ", "dilijan": "Տավուշ",
            "իջևան": "Տավուշ", "ijevan": "Տավուշ",
            "ապարան": "Արագածոտն", "aparan": "Արագածոտն",
            "աշտարակ": "Արագածոտն", "ashtarak": "Արագածոտն",
            "կապան": "Սյունիք", "kapan": "Սյունիք",
            "գորիս": "Սյունիք", "goris": "Սյունիք",
            "ջերմուկ": "Վայոց ձոր", "jermuk": "Վայոց ձոր",
            "վայք": "Վայոց ձոր", "vayk": "Վայոց ձոր",
        }
        if city_key in marz_by_city:
            out["marz"] = marz_by_city[city_key]

    if out.get("city"):
        city = _norm(out["city"])
        m = re.fullmatch(r"([\u0531-\u058F]+?)(?:անում|ենում|ում)", city, flags=re.I)
        if m and len(m.group(1)) >= 3:
            out["city"] = m.group(1)

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
        r"(?<!\d)(\d{3,6})(?:[.,]\d{1,2})?\s*(?:դրամ(?:ից|ով|ի)?|դր\.?|֏|amd|dram|драм(?:ов|а)?|амд)\b",
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
    """Recover explicit service/price facts without depending on the LLM."""
    text = " ".join(
        _norm(x.get("content") or "")
        for x in history
        if str(x.get("role") or "").lower() == "user"
    )
    if not text:
        return []
    found = []
    for clause in re.split(r"[,;.!?։\n]+", text):
        clause = _norm(clause).strip(" —–-:;")
        if not clause:
            continue
        m = re.search(
            r"(?P<name>.+?)\s*(?:\u055D|:|—|–|-|\b(?:սկսվում\s+են|սկսվում\s+է|արժե|գինն\s+է|от|from|starting\s+at)\b)?\s*"
            r"(?P<price>\d[\d\s.,]*)\s*(?P<currency>դրամ(?:ից)?|֏|amd|dram)\b",
            clause, flags=re.I
        )
        if not m:
            continue
        name = _norm(m.group("name")).strip(" —–-:;")
        # Remove an introductory list label before the actual service, e.g.
        # «Հիմնական ծառայություններն են՝ ավտոմեքենայի ախտորոշում».
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
        # Remove conversational wrappers so a whole sentence can never become
        # the service name (e.g. "Ռազդանում ունեմ BYUTI անունով սրահ").
        name = re.sub(
            r"^(?:Ես\s+[^,;.!?։]*?\s+)?(?:ունեմ|ունենք|կատարում\s+ենք|անում\s+ենք|մատուցում\s+ենք|"
            r"առաջարկում\s+ենք|мы\s+делаем|оказываем|предлагаем|we\s+(?:do|offer|provide))\s+",
            "", name, flags=re.I
        ).strip()
        name = re.sub(r"^(?:սրահում|մեզ\s+մոտ)\s+", "", name, flags=re.I).strip()
        name = re.sub(r"^(?:ինչպես\s+նաև|նաև|և|ու)\s+", "", name, flags=re.I).strip()
        # Armenian conversational/inflected forms -> clean service noun.
        arm_clean = {
            "սանրվածքները": "Սանրվածք", "սանրվածքը": "Սանրվածք",
            "գունավորումը": "Գունավորում", "գունավորումը": "Գունավորում",
            "ոճավորումը": "Ոճավորում", "ոճավորումը": "Ոճավորում",
            "մատնահարդարումը": "Մատնահարդարում",
            "պեդիկյուրը": "Պեդիկյուր", "պեդիկյուրը": "Պեդիկյուր",
            "դիմահարդարումը": "Դիմահարդարում",
        }
        clean_key = name.lower().strip("՝:- ")
        if clean_key in arm_clean:
            name = arm_clean[clean_key]
        else:
            # Keep only the final noun-like fragment if punctuation/connector
            # text survived extraction; never keep a location or business intro.
            name = re.sub(r"^(?:և|ու|ինչպես\s+նաև|then|and|и|а|also)\s+", "", name, flags=re.I).strip()
        if not name:
            continue
        try:
            price = float(m.group("price").replace(" ", "").replace(",", "."))
        except ValueError:
            continue
        full = clause.lower()
        is_from = bool(
            "ից" in full or re.search(
                r"\b(?:от|from|starting\s+at|սկսվում\s+են|սկսվում\s+է)\b", full, re.I
            )
        )
        is_per_unit = bool(
            re.search(r"\b(?:քմ|քառակուսի\s*մետր|кв\.?\s*м|за\s+кв\.?\s*м|պարապմունք|занят(?:ие|ия)|за\s+занятие)\b", full, re.I)
        )
        if is_per_unit:
            price_type = "from_per_unit" if is_from else "fixed_per_unit"
        else:
            price_type = "from" if is_from else "fixed"
        found.append({"name": name, "raw_sub_direction": name, "price": price, "price_type": price_type, "matched_subcategory_id": None})
    result=[]; seen=set()
    for item in found:
        key=(item["name"].lower(),item["price"],item["price_type"])
        if key not in seen:
            seen.add(key); result.append(item)
    return result


def _fallback_catalog_match(services: list[dict], catalog: list[dict]) -> list[dict]:
    """Deterministic safety net when Groq catalog matching rejects a request.
    It only returns IDs whose Armenian/Russian/English names are actually in the
    supplied active catalogue; it never invents an ID.
    """
    out = [dict(x) for x in services]
    rules = [
        # Armenian women-haircut phrases are semantically the catalogue
        # subcategory "Կանացի սանրվածք", even when the catalogue wording
        # does not contain the literal word "կտրում".
        (("կանացի մազերի կտրում", "կանանց մազերի կտրում", "կանացի մազերի կտրել",
          "կանացի սանրվածքի կտրում", "կանացի կտրում"),
         ("կանացի սանրվածք",)),
        (("սանրվածք", "սանրվածքները", "ստриж", "стриж", "haircut"), ("սանրված", "парикмах", "haircut")),
        (("գունավորում", "գունավորումը", "ներկում", "ներկ", "окраш", "волос", "coloring"), ("ներկ", "окраш", "color")),
        (("ոճավորում", "ոճավորումը", "դասավորում", "уклад", "styling"), ("դասավորում", "уклад", "styling")),
        (("մատնահարդարում", "маникюр", "manicure"), ("մատնահարդարում", "маникюр", "manicure")),
        (("պեդիկյուր", "педикюр", "pedicure"), ("ոտնահարդարում", "педикюр", "pedicure")),
        (("դիմահարդարում", "դիմահարդարումը", "макияж", "makeup"), ("դիմահարդարում", "макияж", "makeup")),
        (("հարսանեկան ֆոտոսեսիա", "հարսանեկան լուսանկար", "wedding photo", "wedding photography"), ("հարսանեկան լուսանկարիչ", "wedding photographer", "свадебный фотограф")),
        (("միջոցառումների լուսանկարահանում", "միջոցառման լուսանկար", "event photo", "event photography"), ("լուսանկարիչ", "photographer", "фотограф")),
        (("անհատական ֆոտոսեսիա", "անձնական ֆոտոսեսիա", "portrait", "individual photo"), ("լուսանկարիչ", "photographer", "фотограф")),
        (("տեսանկարահանում", "տեսանյութ", "video shooting", "videography"), ("տեսագրահանող", "videographer", "видеограф")),
    ]
    for item in out:
        if _safe_int(item.get("matched_subcategory_id")) is not None:
            continue
        name = _norm(item.get("name")).lower()
        if not name:
            continue
        needles = None
        targets = None
        for src, dst in rules:
            if any(x in name for x in src):
                needles, targets = src, dst
                break
        if not targets:
            continue
        for row in catalog:
            labels = [_norm(row.get("category_am")), _norm(row.get("category_ru")), _norm(row.get("category_en"))]
            low = [x.lower() for x in labels if x]
            if any(any(t in label for t in targets) for label in low):
                cid = _safe_int(row.get("category_id"))
                if cid is not None:
                    item["matched_subcategory_id"] = cid
                    item["match_confidence"] = 0.85
                    item["match_reason"] = "Deterministic multilingual fallback matched an explicit service term to the active catalogue."
                    break
    return out


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
    """Call Groq for structured JSON without turning transient/rate-limit errors into a second bad request.

    GPT-OSS supports strict JSON Schema, but a schema/model/API mismatch can still
    return HTTP 400. A 429 is a rate-limit condition and MUST NOT be immediately
    retried as another request. The caller already has deterministic extraction
    fallbacks, so we return control quickly in that case.
    """
    models = []
    for candidate in (str(model or "").strip(), "openai/gpt-oss-20b"):
        if candidate and candidate not in models:
            models.append(candidate)

    last_error = None
    for active_model in models:
        base = {
            "model": active_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.0,
            "max_tokens": max_tokens,
        }

        # 1) Preferred path: strict Structured Outputs.
        try:
            response = await client.chat.completions.create(
                **base,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": schema_name,
                        "schema": schema,
                        "strict": True,
                    },
                },
            )
            return _parse_json(response.choices[0].message.content or "{}")
        except Exception as exc:
            last_error = exc
            status = getattr(exc, "status_code", None)

            # A rate limit is not a schema/model error. Do not immediately send
            # another request and turn one 429 into a 429 + 400 sequence.
            if status == 429:
                raise

            # 2) For a 400 caused by schema validation/support, try JSON Object
            # Mode once. This is supported by GPT-OSS and is intentionally less
            # strict; _parse_json validates the returned syntax.
            if status == 400:
                try:
                    response = await client.chat.completions.create(
                        **base,
                        response_format={"type": "json_object"},
                    )
                    return _parse_json(response.choices[0].message.content or "{}")
                except Exception as exc2:
                    last_error = exc2
                    if getattr(exc2, "status_code", None) == 429:
                        raise

    raise last_error or RuntimeError("Groq request failed")

async def _ai_match_services(client, model, services: list[dict], catalog: list[dict]) -> list[dict]:
    """Use Groq for semantic service -> real catalogue matching inside one direction.
    The model receives only the active subcategories of the already selected
    direction and may return only IDs supplied in that catalogue.
    """
    if not services or not catalog:
        return services

    unresolved = []
    for index, item in enumerate(services):
        if not isinstance(item, dict):
            continue
        if _safe_int(item.get("matched_subcategory_id")) is not None:
            continue
        unresolved.append({
            "index": index,
            "service": _norm(item.get("name")),
            "specialization": _norm(item.get("raw_sub_direction") or item.get("name")),
        })
    if not unresolved:
        return services

    catalogue = [
        {
            "id": _safe_int(row.get("category_id")),
            "am": _norm(row.get("category_am")),
            "ru": _norm(row.get("category_ru")),
            "en": _norm(row.get("category_en")),
        }
        for row in catalog
        if _safe_int(row.get("category_id")) is not None
    ]
    valid_ids = {x["id"] for x in catalogue}

    schema = {
        "type": "object",
        "properties": {
            "matches": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "service_index": {"type": "integer"},
                        "matched_subcategory_id": {"type": ["integer", "null"]},
                        "confidence": {"type": "number"},
                        "reason": {"type": "string"},
                    },
                    "required": ["service_index", "matched_subcategory_id", "confidence", "reason"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["matches"],
        "additionalProperties": False,
    }

    system = """You classify partner services against a real catalogue.
Understand Armenian, Russian and English semantically, including inflected forms,
synonyms and ordinary service wording.

IMPORTANT:
- The catalogue below belongs to ONE already selected top-level direction.
- Match each service by meaning, not literal spelling.
- Armenian/Russian/English names are equivalent labels for the same catalogue item.
- A service phrase may be very different from the catalogue wording.
- Example: "տրանսֆեր դեպի Ծաղկաձոր" means the catalogue item "Թրանսֆեր".
- Example: "յոգայի դասեր" means the catalogue item for Yoga.
- Example: "գիպսաստվարաթղթի աշխատանքներ" means the catalogue item for drywall/plasterboard work.
- Example: "տեսանկարահանում" means the catalogue item for video filming/video recording.
- Example: "տորթերի պատվերներ" means the catalogue item for cakes/cake orders.
- Never invent an ID.
- matched_subcategory_id MUST be one of the IDs in the supplied catalogue or null.
- If one catalogue item is clearly the semantic match, select it.
- Do not choose a merely similar but different service.
- Return one result for every supplied service index."""

    user = "SERVICES:\n" + json.dumps(unresolved, ensure_ascii=False) +            "\n\nACTIVE SUBCATEGORIES OF THIS DIRECTION:\n" + json.dumps(catalogue, ensure_ascii=False)

    try:
        result = await _groq_json(
            client, model, system, user,
            "partner_service_catalog_match", schema, 900
        )
    except Exception:
        return services

    out = [dict(x) for x in services]
    for match in result.get("matches") or []:
        try:
            idx = int(match.get("service_index"))
        except (TypeError, ValueError):
            continue
        if idx < 0 or idx >= len(out):
            continue
        cid = _safe_int(match.get("matched_subcategory_id"))
        if cid is None or cid not in valid_ids:
            continue
        confidence = float(match.get("confidence") or 0)
        if confidence >= 0.55:
            out[idx]["matched_subcategory_id"] = cid
            out[idx]["match_confidence"] = min(1.0, max(0.0, confidence))
            out[idx]["match_reason"] = _norm(match.get("reason")) or "Semantic catalogue match."
    return out


def _match_services_universal(db, services, master_id):
    """Resolve each AI service phrase to a real active subcategory in PostgreSQL.

    Groq supplies semantic text; PostgreSQL owns the final ID decision.
    No subcategory IDs are invented by the model.
    """
    if not services or _safe_int(master_id) is None:
        return services

    out = [dict(x) for x in services]
    for item in out:
        if _safe_int(item.get("matched_subcategory_id")) is not None:
            continue

        query = _norm(item.get("raw_sub_direction") or item.get("name"))
        if not query:
            continue

        try:
            matches = db.find_similar_subcategories(_safe_int(master_id), query, limit=3)
        except Exception:
            matches = []

        if not matches:
            continue

        best = matches[0]
        score = float(best.get("match_score") or 0)
        if score > 0.60:
            item["matched_subcategory_id"] = _safe_int(best.get("id"))
            item["match_confidence"] = min(1.0, score)
            item["match_reason"] = "PostgreSQL pg_trgm high-confidence match."
        elif score >= 0.30:
            item["catalog_candidates"] = [
                {
                    "id": _safe_int(row.get("id")),
                    "name_am": _norm(row.get("name_am")),
                    "name_ru": _norm(row.get("name_ru")),
                    "name_en": _norm(row.get("name_en")),
                    "match_score": float(row.get("match_score") or 0),
                }
                for row in matches
            ]
            item["match_confidence"] = score
            item["match_reason"] = "Several catalogue candidates are close; partner/admin can choose."
        else:
            item["match_confidence"] = score
            item["match_reason"] = "No sufficiently similar active catalogue subcategory was found."

    return out


async def extract(text: str, history: list[dict], db, previous_profile: dict | None = None, pending_field: str | None = None) -> dict:
    previous_profile = previous_profile or {}
    # Partner Intake AI deliberately does NOT classify the business into the
    # platform catalogue. Catalogue classification is a separate Admin
    # Classification AI step executed after the partner profile is understood.
    master_catalog = []
    key = os.getenv("GROQ_API_KEY", "").strip()

    if not key or AsyncGroq is None:
        data = _heuristic(text)
        if pending_field in {"business_name", "marz", "city", "address", "phone", "district"}:
            data[pending_field] = _norm(text)
        elif pending_field == "services":
            data["services"] = [{"name": _norm(text), "price": None, "price_type": "unknown", "matched_subcategory_id": None}]
        combined_text = " ".join([str(x.get("content") or "") for x in history] + [text])
        data = _recover_obvious_facts(combined_text, data)
        data = _recover_master_category(db, combined_text, data)
        recovered = _recover_services_from_history(history + [{"role": "user", "content": text}])
        if recovered:
            data["services"] = recovered
        # Business name is mandatory even when Groq is unavailable.
        data["missing"] = [
            key for key in ("business_name", "marz", "city", "phone", "services")
            if not data.get(key)
        ]
        data["ready"] = not data["missing"]
        return data

    model = os.getenv("PARTNER_ONBOARDING_MODEL", os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")).strip() or "openai/gpt-oss-20b"
    if model in {"llama-3.1-8b-instant", "llama-3.3-70b-versatile"}:
        model = "openai/gpt-oss-20b"
    client = AsyncGroq(api_key=key)

    schema = {
        "type": "object",
        "properties": {
            "business_name": {"type": ["string", "null"]},
            "marz": {"type": ["string", "null"]},
            "address": {"type": ["string", "null"]},
            "phone": {"type": ["string", "null"]},
            "working_hours": {"type": ["string", "null"]},
            "business_action": {"type": "string"},
            "proposed_business_name": {"type": ["string", "null"]},
            "city": {"type": ["string", "null"]},
            "district": {"type": ["string", "null"]},
            "direction": {"type": ["string", "null"]},
            "master_category_id": {"type": ["integer", "null"]},
            "subcategory_names": {"type": "array", "items": {"type": "string"}},
            "description": {"type": "string"},
            "confidence": {"type": "number"},
            "ambiguities": {"type": "array", "items": {"type": "string"}},
            "needs_review": {"type": "boolean"},
            "services": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "raw_sub_direction": {"type": ["string", "null"]},
                    "price": {"type": ["number", "null"]},
                    "price_type": {"type": "string"},
                    "matched_subcategory_id": {"type": ["integer", "null"]},
                },
                "required": ["name", "raw_sub_direction", "price", "price_type", "matched_subcategory_id"],
                "additionalProperties": False,
            }},
                    },
        "required": ["business_name", "marz", "city", "address", "phone", "working_hours", "business_action", "proposed_business_name", "district", "direction",
                     "master_category_id", "subcategory_names", "description",
                     "confidence", "ambiguities", "needs_review", "services"],
        "additionalProperties": False,
    }

    system = """You are the AI registration concierge for Armenia AI Guide.
Understand Armenian, Russian and English.
Extract facts from the partner's current message and accumulated history.
Extract marz/region, exact address, business phone, and working hours when stated. Never invent them.
WORKING HOURS: preserve the stated schedule. For «ամեն օր՝ ժամը 09:00-ից մինչև 19:00-ը» return «Ամեն օր՝ 09:00–19:00». Never leave working_hours null when an explicit schedule is present.
You are doing strict Named Entity Recognition (NER) and classification, not free-form form filling.
Never copy a complete sentence into a field.
BUSINESS NAME: extract only the proper business/organization name. For "BYUTI անունով սրահ" return "BYUTI", never "սրահ BYUTI" and never surrounding context.
LOCATION: normalize Armenian/Russian/English inflected place names to the canonical city name. For example "Հրազդանում" -> "Հրազդան". Derive marz only from a known city-to-marz relationship or an explicitly stated marz; never invent an address.
SERVICE EXTRACTION IS STRICT AND COUNTED: every explicit monetary amount in the source must correspond to exactly one atomic service object. Count price-bearing services, not arbitrary numbers. Phone numbers, house numbers and district numbers are NOT prices. If a service is introduced by "և", "ու", "նաև", "and", "also", "и", or "а", remove only the connector and preserve the complete service phrase and its price.
SERVICE NAMES: every price-bearing service is a separate atomic entity. One complete service + its price = ONE object. Never split a complete service phrase into fragments. For example, "կանացի մազերի կտրում՝ 3000 դրամից" is exactly ONE service; do NOT also create "կտրում" with 3000. Likewise "մազերի ներկում՝ 5000 դրամից" is ONE service; do NOT also create "ներկում". Strip only introductions, conjunctions, location text, business context, punctuation and grammatical endings; preserve meaningful modifiers such as "կանացի", "երեկոյան", "հարսանեկան" when they distinguish the service. Use a clean noun phrase suitable for a price list: "կանացի մազերի կտրում" -> "կանացի մազերի կտրում"; "մազերի ներկում" -> "մազերի ներկում"; "սանրվածք" -> "սանրվածք"; "մատնահարդարում" -> "մատնահարդարում"; "պեդիկյուր" -> "պեդիկյուր"; "երեկոյան դիմահարդարում" -> "երեկոյան դիմահարդարում". Never put words such as "սկսվում է", "դրամից", "սրահում", "ունեմ", "անունով", a city, or the business name into service_name.
ARMENIAN FEW-SHOT SERVICE EXAMPLES:
Input: "կանացի մազերի կտրում՝ 3000 դրամից"
Output: one service: {"name":"կանացի մազերի կտրում","price":3000,"price_type":"from"}
Input: "մազերի ներկում՝ 5000 դրամից"
Output: one service: {"name":"մազերի ներկում","price":5000,"price_type":"from"}
Input: "երեկոյան դիմահարդարում՝ 5000 դրամից"
Output: one service: {"name":"երեկոյան դիմահարդարում","price":5000,"price_type":"from"}
If the same text contains a complete phrase and one of its component words, treat the complete phrase as the service and never create a second fragment with the same price/context.
PRICES: output only the numeric amount. "3000 դրամից", "սկսվում է 3000 դրամից", "от 3000", "from 3000" => price=3000 and price_type="from". An exact "3000 դրամ" => price_type="fixed".
KEEP ENTITIES SEPARATE: business, city, address, phone, direction, subcategory, service, price and working hours are different fields. Do not merge them.
Keep every stated service as a separate object, including several services in one sentence.
For every service also return raw_sub_direction: 1-3 clean Armenian words describing the narrow service specialization, in base/nominative form. This is a search phrase for the database, not a new category. Examples: "Ֆոտոստուդիա", "Հարսանեկան լուսանկարում", "Անհատական ֆոտոսեսիա", "Տեսանկարահանում".
Do not invent a catalogue ID. matched_subcategory_id must remain null in this extraction step.
If a current business is supplied in PREVIOUS PROFILE, decide whether the new request belongs to that same business or clearly describes a separate organization. Return business_action as same_business or new_business and proposed_business_name when new_business.
Determine the platform direction yourself; never ask the partner to choose it.
Do not invent missing information: use null and let the form collect it.
If the message clearly describes another organization than PREVIOUS PROFILE, use business_action="new_business".
If it is clearly another service of the same organization, use business_action="same_business".
If genuinely ambiguous, use same_business, set needs_review=true, and explain the ambiguity.

TOP-LEVEL DIRECTIONS:
Do NOT classify the partner into the platform catalogue in this step.
The catalogue is handled by a separate Admin Classification AI after the
partner profile is complete. Keep direction/master_category_id null.
""" + json.dumps(master_catalog, ensure_ascii=False) + """
If the business does not genuinely fit any supplied direction, return master_category_id=null.
matched_subcategory_id MUST remain null in this extraction step.
Return only the supplied JSON schema."""

    user_content = (
        "PREVIOUS PROFILE:\n" + json.dumps(previous_profile, ensure_ascii=False)
        + "\nPENDING FIELD:\n" + str(pending_field or "")
        + "\nHISTORY:\n" + json.dumps([
            {"role": str(x.get("role") or ""), "content": _norm(x.get("content") or "")[:1200]}
            for x in history[-4:]
        ], ensure_ascii=False)
        + "\nNEW MESSAGE:\n" + _norm(text)[:3000]
    )

    try:
        ai_data = await _groq_json(client, model, system, user_content,
                                   "partner_onboarding_extract", schema, 1100)
        data = dict(previous_profile)

        for field in ("business_name", "marz", "address", "phone", "working_hours", "business_action", "proposed_business_name", "city", "district", "direction",
                      "master_category_id", "description", "confidence", "ambiguities", "needs_review"):
            value = ai_data.get(field)
            if value not in (None, ""):
                data[field] = value

        ai_services = ai_data.get("services")
        if isinstance(ai_services, list) and ai_services:
            data["services"] = ai_services
        elif "services" not in data:
            data["services"] = []

        combined_text = " ".join([str(x.get("content") or "") for x in history] + [text])

        # Deterministic price counting is a guard against lost services. It
        # recognizes monetary amounts only, so phone/address numbers are not
        # counted as services.
        price_mentions = _extract_price_mentions(combined_text)
        recovered = _recover_services_from_history(
            history + [{"role": "user", "content": text}]
        )
        if recovered and (not data.get("services") or len(recovered) >= len(data.get("services") or [])):
            # The history parser is authoritative when it can account for all
            # explicit price-bearing services. It does not invent catalogue IDs.
            data["services"] = recovered

        if price_mentions and len(data.get("services") or []) < len(price_mentions):
            data["services"] = await _recover_missing_services(
                client, model, combined_text, data.get("services") or [], price_mentions
            )

        # Deterministic service recovery is authoritative whenever it can
        # account for the explicit monetary amounts. This prevents Groq from
        # silently dropping one service from a multi-service sentence.
        if recovered and len(recovered) == len(price_mentions):
            data["services"] = recovered
        elif price_mentions and len(data.get("services") or []) < len(price_mentions):
            data["services"] = await _recover_missing_services(
                client, model, combined_text, data.get("services") or [], price_mentions
            )
        data = _recover_obvious_facts(combined_text, data)
        # Catalogue classification is intentionally deferred to Admin Classification AI.

        # Normalize service objects so the form always receives a stable shape,
        # even when Groq uses legacy service_name instead of name.
        normalized_services = []
        for item in (data.get("services") or []):
            if not isinstance(item, dict):
                continue
            item = dict(item)
            item["name"] = _norm(item.get("name") or item.get("service_name"))
            if not item["name"]:
                continue
            item["raw_sub_direction"] = _norm(item.get("raw_sub_direction") or item["name"])
            if item.get("price") not in (None, ""):
                try:
                    item["price"] = float(item["price"])
                except (TypeError, ValueError):
                    item["price"] = None
            item["price_type"] = _norm(item.get("price_type") or "fixed") or "fixed"
            item["matched_subcategory_id"] = _safe_int(item.get("matched_subcategory_id"))
            normalized_services.append(item)
        data["services"] = normalized_services

        # Catalogue classification is intentionally deferred to the second AI stage.
        data["master_category_id"] = None
        for service in data.get("services") or []:
            if isinstance(service, dict):
                service["matched_subcategory_id"] = None
                service.pop("match_confidence", None)
                service.pop("match_reason", None)

        if pending_field in {"business_name", "city", "district"} and not data.get(pending_field):
            data[pending_field] = _norm(text)
        if pending_field == "services" and not data.get("services"):
            data["services"] = [{
                "name": _norm(text), "raw_sub_direction": _norm(text), "price": None, "price_type": "unknown",
                "matched_subcategory_id": None
            }]

        data["ready"] = bool(
            str(data.get("business_name") or "").strip()
            and str(data.get("marz") or "").strip()
            and str(data.get("city") or "").strip()
            and str(data.get("address") or "").strip()
            and str(data.get("phone") or "").strip()
            and data.get("services")
        )
        data["missing"] = [] if data["ready"] else [
            key for key in ("business_name", "marz", "city", "address", "phone", "services") if not data.get(key)
        ]
        return data

    except Exception as exc:
        try:
            import logging
            logging.getLogger(__name__).warning(
                "Groq partner extraction failed: %s", exc
            )
        except Exception:
            pass
        # Groq can fail because of transient 429s or structured-output validation
        # errors. The partner form must still receive a complete deterministic
        # extraction from the text already supplied by the partner.
        data = _heuristic(text)
        combined_text = " ".join([str(x.get("content") or "") for x in history] + [text])
        data = _recover_obvious_facts(combined_text, data)
        data = _recover_master_category(db, combined_text, data)
        recovered = _recover_services_from_history(
            history + [{"role": "user", "content": text}]
        )
        if recovered:
            data["services"] = recovered
        if pending_field in {"business_name", "city", "district"} and not data.get(pending_field):
            data[pending_field] = _norm(text)

        # Return the same canonical shape as the successful Groq path. This is
        # critical for the WebApp: it prevents a Groq failure from producing
        # an application object with empty service rows while the real facts
        # are already present in the partner's message.
        data["marz"] = _norm(data.get("marz") or data.get("region"))
        data["city"] = _norm(data.get("city") or data.get("location_city") or data.get("settlement"))
        data["phone"] = _norm(data.get("phone") or data.get("phone_number"))
        normalized = []
        for item in data.get("services") or []:
            if not isinstance(item, dict):
                continue
            name = _norm(item.get("name") or item.get("service_name") or item.get("service"))
            if not name:
                continue
            price = item.get("price")
            try:
                price = float(price) if price not in (None, "") else None
            except (TypeError, ValueError):
                price = None
            price_type = _norm(item.get("price_type") or "fixed").lower()
            if price_type in {"starting", "starting_from", "from_price"}:
                price_type = "from"
            normalized.append({
                **item,
                "name": name,
                "raw_sub_direction": _norm(item.get("raw_sub_direction") or name),
                "price": price,
                "price_type": price_type,
                "matched_subcategory_id": None,
            })
        data["services"] = normalized
        data["master_category_id"] = None
        data["classification_needs_review"] = True
        # Business name is mandatory for a valid partner application.
        # Never silently treat an unnamed business as ready.
        data["missing"] = [
            key for key in ("business_name", "marz", "city", "phone", "services")
            if not data.get(key)
        ]
        data["ready"] = not data["missing"]
        return data

async def classify_profile_catalog(db, profile: dict) -> dict:
    """Second AI stage: classify an already extracted partner profile.

    Classification is deliberately split into two small Groq requests:
    1) choose one top-level direction from the 22 master categories;
    2) match services only against subcategories of that direction.

    The old implementation sent all ~320 subcategories (with three language
    labels) in one prompt and could exceed Groq's request-size limit (HTTP 413).
    Never send the full catalogue to Groq in one request.
    """
    key = os.getenv("GROQ_API_KEY", "").strip()
    services_profile = list(profile.get("services") or [])
    if not key or AsyncGroq is None:
        return {
            "master_category_id": None,
            "services": services_profile,
            "confidence": 0.0,
            "ambiguities": ["classification_ai_unavailable"],
            "needs_review": True,
        }

    model = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b").strip() or "openai/gpt-oss-20b"
    if model in {"llama-3.1-8b-instant", "llama-3.3-70b-versatile"}:
        model = "openai/gpt-oss-20b"

    client = AsyncGroq(api_key=key)
    master_catalog = get_master_catalog(db)
    if not master_catalog:
        return {
            "master_category_id": None,
            "services": services_profile,
            "confidence": 0.0,
            "ambiguities": ["catalog_empty"],
            "needs_review": True,
        }

    # STEP 1: send only the 22 master directions, never the full 320-row
    # catalogue. This keeps the request small and makes direction selection
    # independent from subcategory wording.
    master_schema = {
        "type": "object",
        "properties": {
            "master_category_id": {"type": ["integer", "null"]},
            "confidence": {"type": "number"},
            "reason": {"type": "string"},
        },
        "required": ["master_category_id", "confidence", "reason"],
        "additionalProperties": False,
    }
    master_system = """You are Admin Classification AI for Armenia AI Guide.
Choose the ONE real top-level direction that best represents this partner
business and its services.

Rules:
- Understand Armenian, Russian and English semantically.
- Use the supplied master catalogue only.
- master_category_id MUST be one of the supplied IDs or null.
- Do not classify individual services yet.
- Never invent an ID.
- If the business genuinely does not fit any direction, return null.
"""
    master_user = (
        "ACTIVE MASTER DIRECTIONS:\n"
        + json.dumps(master_catalog, ensure_ascii=False)
        + "\n\nPARTNER PROFILE:\n"
        + json.dumps({
            "business_name": profile.get("business_name"),
            "description": profile.get("description"),
            "marz": profile.get("marz"),
            "city": profile.get("city"),
            "address": profile.get("address"),
            "services": [
                {
                    "index": i,
                    "name": _norm(x.get("name")),
                    "specialization": _norm(x.get("raw_sub_direction") or x.get("name")),
                }
                for i, x in enumerate(services_profile)
                if isinstance(x, dict)
            ],
        }, ensure_ascii=False)
    )

    try:
        master_result = await _groq_json(
            client, model, master_system, master_user,
            "admin_partner_master_classification", master_schema, 500
        )
    except Exception:
        return {
            "master_category_id": None,
            "services": services_profile,
            "confidence": 0.0,
            "ambiguities": ["master_classification_failed"],
            "needs_review": True,
        }

    master_id = _safe_int(master_result.get("master_category_id"))
    valid_master_ids = {
        _safe_int(row.get("master_id"))
        for row in master_catalog
        if _safe_int(row.get("master_id")) is not None
    }
    if master_id not in valid_master_ids:
        master_id = None

    if master_id is None:
        return {
            "master_category_id": None,
            "services": services_profile,
            "confidence": float(master_result.get("confidence") or 0),
            "ambiguities": [
                _norm(master_result.get("reason")) or "Top-level direction requires admin review."
            ],
            "needs_review": True,
        }

    # STEP 2: only this direction's active subcategories are sent to Groq.
    # For MOTOR servis this is the compact Авտոծառայություններ catalogue,
    # including the exact IDs such as diagnosis, engine repair, brakes, oil,
    # tyres, etc.
    direction_catalog = get_catalog_for_master(db, master_id)
    if not direction_catalog:
        return {
            "master_category_id": master_id,
            "services": services_profile,
            "confidence": float(master_result.get("confidence") or 0),
            "ambiguities": ["direction_catalog_empty"],
            "needs_review": True,
        }

    matched = await _ai_match_services(
        client, model, services_profile, direction_catalog
    )

    # Deterministic fallback is intentionally applied only after the compact
    # AI request. It can rescue obvious multilingual phrases if Groq leaves
    # one unresolved, while still accepting IDs only from this real direction.
    matched = _fallback_catalog_match(matched, direction_catalog)

    valid_subcats = {
        _safe_int(x.get("category_id"))
        for x in direction_catalog
        if _safe_int(x.get("category_id")) is not None
    }
    for item in matched:
        cid = _safe_int(item.get("matched_subcategory_id"))
        if cid not in valid_subcats:
            item["matched_subcategory_id"] = None

    needs_review = any(
        _safe_int(x.get("matched_subcategory_id")) is None
        for x in matched
        if isinstance(x, dict)
    )

    ambiguities = []
    if needs_review:
        ambiguities.append("one_or_more_services_need_admin_review")

    return {
        "master_category_id": master_id,
        "services": matched,
        "confidence": float(master_result.get("confidence") or 0),
        "ambiguities": ambiguities,
        "needs_review": needs_review,
    }

def match_catalog(db, profile: dict) -> tuple[int | None, list[int]]:
    """Validate AI-selected catalogue IDs against the real database.

    The AI does the semantic classification. Python does NOT guess a category
    from the first row or from substring matching. It only validates the IDs
    returned by Structured Outputs and derives the master direction from them.
    """
    catalog = get_catalog(db)
    by_category = {}
    for row in catalog:
        cid = _safe_int(row.get("category_id"))
        mid = _safe_int(row.get("master_id"))
        if cid is not None:
            by_category[cid] = mid

    selected: list[int] = []
    for item in profile.get("services") or []:
        if not isinstance(item, dict):
            continue
        cid = _safe_int(item.get("matched_subcategory_id"))
        if cid is not None and cid in by_category and cid not in selected:
            selected.append(cid)

    explicit_master = _safe_int(profile.get("master_category_id"))
    if explicit_master is not None:
        valid_master_ids = {row["master_id"] for row in catalog}
        if explicit_master not in valid_master_ids:
            explicit_master = None

    # If AI selected valid subcategories, their parent direction is authoritative.
    parent_ids = {by_category[cid] for cid in selected if cid in by_category}
    master_id = explicit_master
    if parent_ids:
        if master_id not in parent_ids:
            # Prefer the parent of the selected service categories. This avoids
            # trusting an inconsistent master ID returned alongside valid IDs.
            master_id = next(iter(parent_ids))
        selected = [
            cid for cid in selected
            if by_category.get(cid) == master_id
        ]

    # If the model identified a valid master but no subcategory, return the
    # master with an empty category list. The persistence layer must NOT invent
    # a category as a fallback.
    return master_id, selected


def match_subcategories(db, names: list[str]) -> list[int]:
    """Compatibility helper: exact catalogue-name lookup for legacy callers.

    Active onboarding uses matched_subcategory_id from the AI and does not use
    this function for semantic classification.
    """
    catalog = get_catalog(db)
    wanted = {_norm(name).lower() for name in names if _norm(name)}
    result = []
    for row in catalog:
        names3 = {
            _norm(row.get("category_am")).lower(),
            _norm(row.get("category_ru")).lower(),
            _norm(row.get("category_en")).lower(),
        }
        if wanted & names3:
            cid = _safe_int(row.get("category_id"))
            if cid is not None and cid not in result:
                result.append(cid)
    return result

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
            "marz": "մարզը",
            "city": "քաղաքը/բնակավայրը",
            "address": "ամբողջական հասցեն",
            "phone": "հեռախոսահամարը",
            "services": "ծառայությունները և, եթե հայտնի է, դրանց գները",
        },
        "ru": {
            "business_name": "название бизнеса",
            "marz": "марз",
            "city": "город/населённый пункт",
            "address": "полный адрес",
            "phone": "телефон",
            "services": "услуги и, если известны, их цены",
        },
        "en": {
            "business_name": "business name",
            "marz": "region/marz",
            "city": "city/locality",
            "address": "full address",
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
