import os
from dotenv import load_dotenv
import psycopg

from database import DatabaseManager

load_dotenv()

print("=== DATABASE CATALOG CHECK ===")

conn = psycopg.connect(
    os.getenv("DATABASE_URL"),
    prepare_threshold=None
)

cur = conn.cursor()

cur.execute("""
    SELECT table_name, column_name
    FROM information_schema.columns
    WHERE table_name IN ('master_categories', 'categories')
    ORDER BY table_name, ordinal_position
""")

print("\n--- COLUMNS ---")
for row in cur.fetchall():
    print(f"{row[0]}.{row[1]}")

cur.execute("SELECT COUNT(*) FROM master_categories")
print("\nmaster_categories in DB =", cur.fetchone()[0])

cur.execute("SELECT COUNT(*) FROM categories")
print("categories in DB =", cur.fetchone()[0])

cur.execute("""
    SELECT m.id, m.name_ru, COUNT(c.id)
    FROM master_categories m
    LEFT JOIN categories c
        ON c.master_category_id = m.id
    GROUP BY m.id, m.name_ru
    ORDER BY m.id
""")

print("\n--- CATALOG ---")
for row in cur.fetchall():
    print(f"{row[0]} | {row[1]} | {row[2]}")

conn.close()

print("\n=== DATABASE MANAGER ===")

db = DatabaseManager()

master = db.get_all_master_categories()

print("DatabaseManager master categories =", len(master))

for item in master[:5]:
    print(item)

print("\n=== END ===")