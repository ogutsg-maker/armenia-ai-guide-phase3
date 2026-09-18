#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ПОЛНЫЙ СБРОС (обнуление) базы данных Armenia AI Guide.

Удаляет ВСЕ таблицы проекта (схема public) вместе с данными.
Необратимо. Служебные объекты Supabase (auth/storage/extensions) не трогаются.

Запуск:
    python reset_database.py            # спросит подтверждение
    python reset_database.py --yes      # без вопросов (для CI)

DATABASE_URL берётся из .env / переменных окружения.
"""
from __future__ import annotations

import os
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
SQL_FILE = os.path.join(HERE, "reset_database.sql")


def main() -> None:
    if "--yes" not in sys.argv and "-y" not in sys.argv:
        ans = input(
            "\u26a0\ufe0f  Будут УДАЛЕНЫ ВСЕ таблицы проекта вместе с данными.\n"
            "    Продолжить? введите 'yes': "
        ).strip().lower()
        if ans != "yes":
            print("Отменено.")
            sys.exit(0)

    if not os.getenv("DATABASE_URL"):
        print("❌ DATABASE_URL не задан (проверьте .env).")
        sys.exit(1)

    with open(SQL_FILE, "r", encoding="utf-8") as fh:
        sql = fh.read()

    # Используем тот же коннектор, что и приложение (sslmode и т.п.).
    from database import _connect

    print("→ Подключение к базе и сброс таблиц…")
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    print("✅ База очищена. Теперь запустите: python setup_fresh_supabase.py")


if __name__ == "__main__":
    main()
