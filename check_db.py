import psycopg
from config import DATABASE_URL

conn = psycopg.connect(DATABASE_URL)
cur = conn.cursor()
cur.execute("""
    SELECT table_name, column_name, data_type
    FROM information_schema.columns
    WHERE table_name IN ('master_categories', 'categories')
    ORDER BY table_name, ordinal_position
""")
for row in cur.fetchall():
    print(row)
conn.close()