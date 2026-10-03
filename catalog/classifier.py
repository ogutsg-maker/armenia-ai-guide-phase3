import re
from difflib import SequenceMatcher

from db import run

# Deterministic, backend-only classifier. It never calls an LLM.
# Only real subdirections are candidates; direction is always derived from parent_id.
THRESHOLD = 0.58
MARGIN = 0.10


def norm(value):
    value = (value or "").lower()
    value = value.replace("ё", "е").replace("և", "եւ")
    return re.sub(r"[^\w\u0530-\u058f]+", " ", value).strip()


def _tokens(value):
    return set(norm(value).split())


def _stems(tokens):
    suffixes = (
        "ությունների", "ություններ", "ության", "ություններով", "ությունից", "ներին",
        "ների", "ներով", "ներից", "ային", "ական", "ման", "մամբ", "ություն",
        "ին", "ի", "ը", "ն", "ով", "ից", "ում",
        "ами", "ями", "ов", "ев", "ого", "ему", "ым", "ый", "ий", "ая", "ое",
        "ые", "ы", "и", "а", "я", "у", "ю", "ом", "ем", "ой",
    )
    out = set()
    for token in tokens:
        out.add(token)
        if len(token) < 5:
            continue
        for suffix in suffixes:
            if token.endswith(suffix) and len(token) - len(suffix) >= 3:
                out.add(token[:-len(suffix)])
                break
    return out


def _score(query, name):
    q, n = norm(query), norm(name)
    if not q or not n:
        return 0.0
    if q == n:
        return 1.0

    qt, nt = _tokens(q), _tokens(n)
    qs, ns = _stems(qt), _stems(nt)
    exact = len(qt & nt) / max(1, len(qt))
    stem = len(qs & ns) / max(1, len(qs))
    similarity = SequenceMatcher(None, q, n).ratio()

    # Require actual lexical evidence from the partner wording.
    return 0.45 * exact + 0.45 * stem + 0.10 * similarity


def _best_name_score(text, row):
    return max(
        (_score(text, row.get(k)) for k in ("name_am", "name_ru", "name_en") if row.get(k)),
        default=0.0,
    )


def classify(text):
    rows = run(
        """SELECT id,parent_id,name_am,name_ru,name_en,slug
           FROM aig_catalog_categories
           WHERE active=true AND parent_id IS NOT NULL
           ORDER BY id""",
        many=True,
    )
    if not rows:
        return None

    ranked = sorted(
        ((_best_name_score(text, row), row) for row in rows),
        key=lambda item: item[0],
        reverse=True,
    )
    best_score, best = ranked[0]
    second_score = ranked[1][0] if len(ranked) > 1 else 0.0
    margin = best_score - second_score

    if best_score < THRESHOLD or margin < MARGIN:
        return None

    parent = run(
        "SELECT id,parent_id,name_am,name_ru,name_en,slug FROM aig_catalog_categories "
        "WHERE id=%s AND parent_id IS NULL AND active=true",
        (best["parent_id"],),
    )
    if not parent:
        return None

    result = dict(best)
    result["classification_confidence"] = round(best_score, 4)
    result["classification_margin"] = round(margin, 4)
    result["classification_votes"] = 1
    result["classification_direction_id"] = parent["id"]
    result["direction"] = parent
    return result
