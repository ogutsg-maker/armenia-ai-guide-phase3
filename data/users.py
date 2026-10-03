from db import run,exec
def ensure(u):
 uid=int(u["id"]); name=" ".join(x for x in [u.get("first_name"),u.get("last_name")] if x)
 exec("INSERT INTO aig_users(telegram_id,username,full_name) VALUES(%s,%s,%s) ON CONFLICT(telegram_id) DO UPDATE SET username=EXCLUDED.username,full_name=EXCLUDED.full_name,updated_at=now()",(uid,u.get("username"),name))
 return run("SELECT * FROM aig_users WHERE telegram_id=%s",(uid,))