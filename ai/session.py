import json
from db import run, exec

def get(uid):
    return run("SELECT * FROM aig_ai_sessions WHERE telegram_id=%s", (uid,))

def save(uid, context, pending):
    value = json.dumps(pending, ensure_ascii=False) if pending is not None else None
    exec("INSERT INTO aig_ai_sessions(telegram_id,context,pending,updated_at) VALUES(%s,%s,%s,now()) ON CONFLICT(telegram_id) DO UPDATE SET context=EXCLUDED.context,pending=EXCLUDED.pending,updated_at=now()", (uid, context, value))

def clear(uid):
    exec("UPDATE aig_ai_sessions SET pending=NULL,updated_at=now() WHERE telegram_id=%s", (uid,))
