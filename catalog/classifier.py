import re
from difflib import SequenceMatcher

from data.core import catalog

# Deterministic classifier: no Groq/LLM calls.
THRESHOLD = 0.42
MARGIN = 0.04


def norm(value):
    return re.sub(r"[^\w\u0530-\u058f]+", " ", (value or "").lower()).strip()


def _tokens(value):
    return set(norm(value).split())


def _stems(tokens):
    out = set()
    suffixes = (
        "ների","ներով","ներից","ներին","ների","ային","ական","ման","մամբ","ության",
        "ին","ի","ը","ն","ով","ից","ում","ումից",
        "ами","ями","ов","ев","ый","ий","ая","ое","ые","ы","и","а","я","у","ю","ом","ем","ой",
    )
    for token in tokens:
        variants = {token}
        if len(token) > 3:
            for suffix in suffixes:
                if token.endswith(suffix) and len(token) - len(suffix) >= 3:
                    variants.add(token[:-len(suffix)])
        out.update(variants)
    return out


def _score(query, name):
    q = norm(query)
    n = norm(name)
    if not q or not n:
        return 0.0
    if q == n:
        return 1.0

    qt, nt = _tokens(q), _tokens(n)
    qs, ns = _stems(qt), _stems(nt)
    token_overlap = len(qt & nt) / max(1, len(qt))
    stem_overlap = len(qs & ns) / max(1, len(qs))
    similarity = SequenceMatcher(None, q, n).ratio()

    # Exact/root token evidence carries more weight than whole-string similarity.
    return 0.40 * token_overlap + 0.50 * stem_overlap + 0.10 * similarity


def _best_name_score(text, row):
    return max((_score(text, row.get(k)) for k in ("name_am","name_ru","name_en") if row.get(k)), default=0.0)


def classify(text):
    rows = catalog()
    if not rows:
        return None

    # First try the concrete catalog service/subcategory.
    ranked = sorted(
        ((_best_name_score(text, row), row) for row in rows),
        key=lambda x: x[0],
        reverse=True,
    )
    best_score, best = ranked[0]
    second_score = ranked[1][0] if len(ranked) > 1 else 0.0
    margin = best_score - second_score

    if best_score < THRESHOLD or margin < MARGIN:
        # The partner may use a valid service wording that is not the catalog wording.
        # In that case classify by the parent direction using all of its live catalog names.
        parents = {}
        for row in rows:
            parent_id = row.get("parent_id")
            if not parent_id:
                continue
            parents.setdefault(parent_id, []).append(row)

        direction_ranked = []
        for parent_id, children in parents.items():
            scores = [_best_name_score(text, child) for child in children]
            direction_ranked.append((max(scores), parent_id))
        direction_ranked.sort(reverse=True)
        if not direction_ranked:
            return None

        dscore, direction_id = direction_ranked[0]
        dsecond = direction_ranked[1][0] if len(direction_ranked) > 1 else 0.0
        dmargin = dscore - dsecond
        if dscore < THRESHOLD or dmargin < MARGIN:
            return None

        # Return the best live child from the selected direction; the parent is the document direction.
        children = [x for x in rows if x.get("parent_id") == direction_id]
        best_child = max(children, key=lambda row: _best_name_score(text, row))
        result = dict(best_child)
        result["classification_confidence"] = round(dscore, 4)
        result["classification_margin"] = round(dmargin, 4)
        result["classification_votes"] = 1
        result["classification_direction_id"] = direction_id
        return result

    result = dict(best)
    result["classification_confidence"] = round(best_score, 4)
    result["classification_margin"] = round(margin, 4)
    result["classification_votes"] = 1
    return result
