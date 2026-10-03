from __future__ import annotations
import json
from contextlib import contextmanager
import psycopg
from config import DATABASE_URL
SCHEMA=open("schema.sql",encoding="utf-8").read()
@contextmanager
def conn():
    c=psycopg.connect(DATABASE_URL)
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback(); raise
    finally: c.close()
def init():
    with conn() as c: c.execute(SCHEMA)
def one(sql,args=()):
    with conn() as c:
        cur=c.execute(sql,args)
        r=cur.fetchone()
        if not r:return None
        cols=[x.name for x in cur.description]
        return dict(zip(cols,r))
def all(sql,args=()):
    with conn() as c:
        cur=c.execute(sql,args); cols=[x.name for x in cur.description]
        return [dict(zip(cols,r)) for r in cur.fetchall()]
def exec(sql,args=(),returning=False):
    with conn() as c:
        cur=c.execute(sql,args)
        if not returning:return None
        cols=[x.name for x in cur.description]
        r=cur.fetchone()
        return dict(zip(cols,r)) if r else None
def jsonable(v):
    if isinstance(v,(dict,list)): return json.dumps(v,ensure_ascii=False)
    return v
