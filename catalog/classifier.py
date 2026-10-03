import re
from difflib import SequenceMatcher
from data.core import catalog
def norm(s): return re.sub(r"[^\w\u0530-\u058f]+"," ",(s or "").lower()).strip()
def classify(text):
 q=set(norm(text).split()); scored=[]
 for row in catalog():
  names=[row.get("name_am"),row.get("name_ru"),row.get("name_en"),row.get("slug")]; names=[x for x in names if x]
  toks=set().union(*(set(norm(x).split()) for x in names)) if names else set()
  overlap=len(q&toks)/max(1,len(q|toks)); sim=max((SequenceMatcher(None,norm(text),norm(x)).ratio() for x in names),default=0)
  scored.append((.65*overlap+.35*sim,row))
 scored.sort(key=lambda x:x[0],reverse=True)
 if not scored:return None
 margin=scored[0][0]-(scored[1][0] if len(scored)>1 else 0)
 return scored[0][1] if scored[0][0]>=.70 and margin>=.10 else None