import psycopg
from psycopg.rows import dict_row
from config import DATABASE_URL
def run(sql,args=(),many=False):
    with psycopg.connect(DATABASE_URL,row_factory=dict_row) as c:
        with c.cursor() as cur:
            cur.execute(sql,args); return cur.fetchall() if many else cur.fetchone()
def exec(sql,args=()):
    with psycopg.connect(DATABASE_URL) as c:c.execute(sql,args)