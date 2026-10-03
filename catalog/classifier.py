import re
from difflib import SequenceMatcher

from data.core import catalog

THRESHOLD = 0.62
MARGIN = 0.06


def norm(value):
    return re.sub(r"[^\w\u0530-\u058f]+", " ", (value or "").lower()).strip()


def _tokens(value):
    return set(norm(value).split())


def _stems(tokens):
    out = set()
    for token in tokens:
        if len(token) <= 3:
            out.add(token)
            continue
        # Lightweight Armenian/Russian inflection normalization; no catalog IDs or hardcoded directions.
        variants = {token}
        for suffix in (
            "ների","ներով","ներից","ին","ի","ը","ն","ով","ից","ում","ական","ային",
            "ами","ями","ов","ев","ы","и","а","я","у","ю","ом","ем","ой","ый","ий",
        ):
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

    # Token/root evidence is deterministic and independent of any LLM.
    return 0.45 * token_overlap + 0.40 * stem_overlap + 0.15 * similarity


def _candidate(row, text):
    names = [row.get("name_am"), row.get("name_ru"), row.get("name_en")]
    names = [str(x) for x in names if x]
    scores = [_score(text, name) for name in names]
    if not scores:
        return 0.0, 0
    votes = sum(score >= THRESHOLD for score in scores)
    return max(scores), votes


def classify(text):
    candidates = []
    for row in catalog():
        confidence, votes = _candidate(row, text)
        candidates.append((confidence, votes, row))

    candidates.sort(key=lambda item: (item[1], item[0]), reverse=True)
    if not candidates:
        return None

    best_confidence, best_votes, best_row = candidates[0]
    second_confidence = candidates[1][0] if len(candidates) > 1 else 0.0
    margin = best_confidence - second_confidence

    if best_confidence < THRESHOLD or margin < MARGIN:
        return None

    result = dict(best_row)
    result["classification_confidence"] = round(best_confidence, 4)
    result["classification_margin"] = round(margin, 4)
    result["classification_votes"] = best_votes
    return result
