from __future__ import annotations
import re
from difflib import SequenceMatcher
import db
STOP={"և","ու","համար","մի","ը","է","են","the","and","для","и","по"}
def norm(s):
    s=(s or "").lower().replace("ё","е")
    s=re.sub(r"[^0-9a-zа-яё԰-֏]+"," ",s)
    return " ".join(x for x in s.split() if x not in STOP)
def stem(w):
    for suf in ("ների","ներով","ներին","երից","երի","ները","ներ","ից","ով","ին","ի","ը","ն","եր"):
        if len(w)>len(suf)+2 and w.endswith(suf): return w[:-len(suf)]
    return w
def score(text,name):
    a=norm(text);b=norm(name)
    if not a or not b:return 0.0
    ta={stem(x) for x in a.split()};tb={stem(x) for x in b.split()}
    overlap=len(ta&tb)/max(1,len(ta|tb))
    seq=SequenceMatcher(None,a,b).ratio()
    containment=1.0 if a in b or b in a else 0.0
    return min(1.0,0.50*overlap+0.35*seq+0.15*containment)
def classify(service_name):
    cats=db.all("SELECT id,name_am,name_ru,name_en FROM aig_catalog_categories WHERE active=true")
    scored=[]
    for c in cats:
        scores=[score(service_name,c["name_am"]),score(service_name,c["name_ru"]),score(service_name,c["name_en"])]
        scored.append((max(scores),c))
    scored.sort(key=lambda x:x[0],reverse=True)
    if not scored:return None
    best,cat=scored[0];second=scored[1][0] if len(scored)>1 else 0.0
    margin=best-second
    if best<0.70 or margin<0.10:return {"category":None,"confidence":round(best,4),"margin":round(margin,4),"reason":"uncertain"}
    return {"category":cat,"confidence":round(best,4),"margin":round(margin,4),"reason":"matched"}
