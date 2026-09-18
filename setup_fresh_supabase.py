#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""One-shot setup for a FRESH Supabase / PostgreSQL database.

Run this once against an empty database (DATABASE_URL must be set, e.g. in
.env). It builds the whole schema and seeds the 3-level catalogue, so you do
NOT have to run several scripts by hand.

What it does, in order:
  1. Core tables            (database.DatabaseManager.init_db):
     users, master_categories, categories, master_skills.
  2. Marketplace schema     (platform_schema.ensure_platform_schema):
     partners, partner_locations, partner_directions,
     partner_direction_categories, services, bookings, payments,
     notifications, ai_* tables, plus the commission ALTERs.
  3. Partner directions     (partner_directions_api.ensure_partner_direction_schema).
  4. Partner lifecycle       (partner_lifecycle_schema.ensure_partner_lifecycle_schema).
  5. Catalogue seed         (22 directions -> 320 subcategories, ru/hy/en).

Tariff model seeded here (matches the running cascade in
marketplace_flow_api._commission):
  * master_categories (direction)   -> DEFAULT tariff on_top / 10
    (the mandatory "initial setting").
  * categories (subcategory)         -> commission NULL  == inherit direction.
  * services                         -> commission NULL  == inherit subcategory/direction.
    A non-NULL value at any level overrides the level(s) below it, and a
    per-service value always wins.

Everything is idempotent (CREATE TABLE IF NOT EXISTS / ON CONFLICT), so a
re-run refreshes names without creating duplicates.

Usage:
    python setup_fresh_supabase.py
"""
from __future__ import annotations

import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass


def _step(n, title):
    print(f"\n─── [{n}/5] {title} ───")


def build_schema():
    _step(1, "Core tables (users, master_categories, categories, master_skills)")
    from database import DatabaseManager
    DatabaseManager()  # __init__ calls init_db()
    print("   ✓ core tables ready")

    _step(2, "Marketplace schema (partners, services, bookings, tariffs …)")
    from platform_schema import ensure_platform_schema
    ensure_platform_schema()
    print("   ✓ marketplace schema ready")

    _step(3, "Partner directions schema")
    from partner_directions_api import ensure_partner_direction_schema
    ensure_partner_direction_schema()
    print("   ✓ partner directions ready")

    _step(4, "Partner lifecycle schema")
    from partner_lifecycle_schema import ensure_partner_lifecycle_schema
    ensure_partner_lifecycle_schema()
    print("   ✓ partner lifecycle ready")


def seed_catalogue():
    _step(5, "Catalogue seed (22 directions → 320 subcategories, ru/hy/en)")
    # Reuse the single source of truth for the catalogue content.
    from seed_catalog import CATALOG, slugify
    from database import _connect

    total_m = total_s = 0
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE master_categories ADD COLUMN IF NOT EXISTS name_en TEXT DEFAULT ''")
            cur.execute("ALTER TABLE categories ADD COLUMN IF NOT EXISTS name_en TEXT DEFAULT ''")
            for m_ru, m_hy, m_en, m_slug, subs in CATALOG:
                cur.execute(
                    """
                    INSERT INTO master_categories (name_ru, name_am, name_en, slug, is_active,
                                                   commission_type, commission_value)
                    VALUES (%s, %s, %s, %s, TRUE, 'on_top', 10)
                    ON CONFLICT (slug) DO UPDATE SET
                        name_ru = EXCLUDED.name_ru,
                        name_am = EXCLUDED.name_am,
                        name_en = EXCLUDED.name_en
                    RETURNING id
                    """,
                    (m_ru, m_hy, m_en, m_slug),
                )
                mid = _first_id(cur)
                total_m += 1
                for s_ru, s_hy, s_en in subs:
                    s_slug = f"{m_slug}-{slugify(s_en)}"
                    # Subcategory tariff stays NULL => inherit the direction default.
                    cur.execute(
                        """
                        INSERT INTO categories
                            (master_category_id, name_ru, name_am, name_en, slug,
                             is_active, commission_type, commission_value)
                        VALUES (%s, %s, %s, %s, %s, TRUE, NULL, NULL)
                        ON CONFLICT (slug) DO UPDATE SET
                            master_category_id = EXCLUDED.master_category_id,
                            name_ru = EXCLUDED.name_ru,
                            name_am = EXCLUDED.name_am,
                            name_en = EXCLUDED.name_en
                        """,
                        (mid, s_ru, s_hy, s_en, s_slug),
                    )
                    total_s += 1
                print(f"   ✓ {m_ru} — {len(subs)} subcategories")
        conn.commit()
    print(f"\n🎉 Catalogue seeded: {total_m} directions, {total_s} subcategories (ru/hy/en)")


def _first_id(cur):
    """Return the id from the last RETURNING row, tolerant of dict/tuple rows."""
    row = cur.fetchone()
    if row is None:
        return None
    try:
        return row["id"]
    except (TypeError, KeyError):
        return row[0]


def main():
    print("Armenia AI Guide — fresh database setup")
    try:
        build_schema()
        seed_catalogue()
    except Exception as exc:  # pragma: no cover - surfaces config/DB errors
        print(f"\n❌ Setup failed: {exc}")
        sys.exit(1)
    print("\n✅ All done. The database is ready for the bot.")


if __name__ == "__main__":
    main()
