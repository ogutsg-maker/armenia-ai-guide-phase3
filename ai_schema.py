"""Live PostgreSQL schema inspector for the AI agent.

The inspector reads the actual public schema from the connected Supabase/PostgreSQL
database. It intentionally exposes only a small allow-listed set of tables and
only metadata needed by the AI/data-tool layer.
"""
from __future__ import annotations

import logging
from typing import Any

import platform_db

logger = logging.getLogger(__name__)

TARGET_TABLES = (
    "partner_applications",
    "partners",
    "partner_businesses",
    "master_categories",
    "categories",
    "partner_verification_documents",
    "services",
)


class LiveSchemaInspector:
    def __init__(self) -> None:
        self._snapshot: dict[str, dict[str, Any]] = {}

    def get_snapshot(self) -> dict[str, dict[str, Any]]:
        if not self._snapshot:
            self.hydrate()
        return self._snapshot

    def hydrate(self) -> dict[str, dict[str, Any]]:
        logger.info("🤖 Live Schema Inspector: hydrating %s", TARGET_TABLES)
        new_snapshot: dict[str, dict[str, Any]] = {}

        try:
            for table in TARGET_TABLES:
                columns_rows = platform_db.rows(
                    """
                    SELECT column_name, data_type, is_nullable, column_default
                    FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = %s
                    ORDER BY ordinal_position
                    """,
                    (table,),
                )

                if not columns_rows:
                    logger.warning(
                        "Live Schema Inspector: table %s was not found in public schema",
                        table,
                    )
                    continue

                columns = {
                    row["column_name"]: {
                        "type": row["data_type"],
                        "nullable": row["is_nullable"] == "YES",
                        "default": row["column_default"],
                    }
                    for row in columns_rows
                }

                pk_rows = platform_db.rows(
                    """
                    SELECT kcu.column_name
                    FROM information_schema.table_constraints tc
                    JOIN information_schema.key_column_usage kcu
                      ON tc.constraint_name = kcu.constraint_name
                     AND tc.table_schema = kcu.table_schema
                    WHERE tc.constraint_type = 'PRIMARY KEY'
                      AND tc.table_schema = 'public'
                      AND tc.table_name = %s
                    ORDER BY kcu.ordinal_position
                    """,
                    (table,),
                )
                primary_keys = [row["column_name"] for row in pk_rows]

                fk_rows = platform_db.rows(
                    """
                    SELECT
                        kcu.column_name AS source_column,
                        ccu.table_name AS target_table,
                        ccu.column_name AS target_column
                    FROM information_schema.table_constraints tc
                    JOIN information_schema.key_column_usage kcu
                      ON tc.constraint_name = kcu.constraint_name
                     AND tc.table_schema = kcu.table_schema
                    JOIN information_schema.constraint_column_usage ccu
                      ON ccu.constraint_name = tc.constraint_name
                     AND ccu.table_schema = tc.table_schema
                    WHERE tc.constraint_type = 'FOREIGN KEY'
                      AND tc.table_schema = 'public'
                      AND tc.table_name = %s
                    ORDER BY kcu.ordinal_position
                    """,
                    (table,),
                )
                foreign_keys = [
                    {
                        "from": row["source_column"],
                        "to_table": row["target_table"],
                        "to_column": row["target_column"],
                    }
                    for row in fk_rows
                ]

                new_snapshot[table] = {
                    "columns": columns,
                    "primary_key": primary_keys,
                    "foreign_keys": foreign_keys,
                }

            self._snapshot = new_snapshot
            logger.info(
                "✅ Live Schema Snapshot hydrated: %d/%d tables",
                len(new_snapshot),
                len(TARGET_TABLES),
            )
            return self._snapshot

        except Exception:
            logger.exception("❌ Live Schema Inspector hydration failed")
            # Do not break application startup because metadata inspection failed.
            # The existing DataTools remain responsible for safe/allow-listed access.
            return self._snapshot


inspector = LiveSchemaInspector()
