import re
from difflib import SequenceMatcher

from data.core import catalog

THRESHOLD = 0.70
MARGIN = 0.10


def norm(value):
    return re.sub(r"[^\w\u0530-\u058f]+", " ", (value or "").lower()).strip()


def _tokens(value):
    return set(norm(value).split())


def _score(query, name):
    q = norm(query)
    n = norm(name)
    if not q or not n:
        return 0.0
    if q == n:
        return 1.0
    qt = _tokens(q)
    nt = _tokens(n)
    overlap = len(qt & nt) / max(1, len(qt))
    similarity = SequenceMatcher(None, q, n).ratio()
    return 0.65 * overlap + 0.35 * similarity


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
