from db import run,exec
def get(uid): return run("SELECT * FROM aig_partners WHERE telegram_id=%s",(uid,))
def companies(pid): return run("SELECT * FROM aig_companies WHERE partner_id=%s AND archived=false ORDER BY id",(pid,),True)
def register(uid,name,phone):
 exec("UPDATE aig_users SET role='partner' WHERE telegram_id=%s",(uid,))
 p=run("INSERT INTO aig_partners(telegram_id,phone) VALUES(%s,%s) ON CONFLICT(telegram_id) DO UPDATE SET phone=EXCLUDED.phone RETURNING id",(uid,phone))
 return p,run("INSERT INTO aig_companies(partner_id,name) VALUES(%s,%s) RETURNING *",(p["id"],name))