from __future__ import annotations

"""Universal, schema-aware read-only data tools for the admin AI agent.

The model may select a business operation, table and validated fields, but never
supplies executable SQL. Identifiers are accepted only when present in the live
Schema Inspector snapshot; values always go through psycopg parameters.
"""

import json
import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import platform_db
from ai_schema import inspector

logger = logging.getLogger(__name__)

SENSITIVE_COLUMNS = {
    "file_data",
    "password",
    "password_hash",
    "token",
    "secret",
    "service_role_key",
}

MAX_LIMIT = 50

def clean_db_value(value: Any) -> Any:
    """Convert every psycopg/PostgreSQL value into JSON-safe AI-contract data."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(k): clean_db_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [clean_db_value(v) for v in value]
    # psycopg can expose PostgreSQL/custom extension types that are not JSON
    # serializable. Never let such a value break a Re-planning turn.
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except (TypeError, ValueError):
        return str(value)

def clean_db_row_for_ai(row: dict[str, Any] | None) -> dict[str, Any]:
    return {str(k): clean_db_value(v) for k, v in (row or {}).items()}


def data_contract(
    *,
    tool_executed: str,
    status: str = "success",
    data: list[dict[str, Any]] | None = None,
    system_notice: str | None = None,
) -> dict[str, Any]:
    rows = data if isinstance(data, list) else []
    out = {
        "status": status if status in {"success", "error", "incomplete"} else "error",
        "tool_executed": str(tool_executed),
        "extracted_records_count": len(rows),
        "data": rows,
    }
    if system_notice:
        out["system_notice"] = str(system_notice)[:1000]
    return out


class AdminDataTools:
    """Schema-aware generic tools.

    This class is deliberately read-only. Mutations remain separate from this
    layer and require the existing confirmation/action pipeline.
    """

    def __init__(self, schema_snapshot: dict[str, Any] | None = None):
        self.schema = schema_snapshot if isinstance(schema_snapshot, dict) else inspector.get_snapshot()

    def _table(self, table: str) -> dict[str, Any] | None:
        return self.schema.get(str(table or "").strip())

    def _columns(self, table: str) -> set[str]:
        meta = self._table(table)
        return set((meta or {}).get("columns", {}).keys())

    def _validate_columns(self, table: str, columns: list[str]) -> bool:
        if not self._table(table):
            return False
        allowed = self._columns(table)
        return all(str(c) in allowed and str(c) not in SENSITIVE_COLUMNS for c in columns)

    @staticmethod
    def _safe_value(value: Any) -> Any:
        return DataToolsSafe.safe(value)

    def _select_columns(self, table: str, requested: list[str] | None = None) -> list[str]:
        allowed = [
            c for c in self._columns(table)
            if c not in SENSITIVE_COLUMNS
        ]
        if requested:
            requested = [str(c) for c in requested]
            if not self._validate_columns(table, requested):
                raise ValueError("column_validation_failed")
            return requested
        return allowed

    def search(self, table: str, filters: dict[str, Any] | None = None,
               columns: list[str] | None = None, limit: int = 20) -> dict[str, Any]:
        table = str(table or "").strip()
        if not self._table(table):
            return data_contract(tool_executed="SEARCH", status="error",
                                 system_notice="table_not_allowed")

        try:
            select_cols = self._select_columns(table, columns)
            if not select_cols:
                return data_contract(tool_executed="SEARCH", status="error",
                                     system_notice="no_safe_columns")

            filters = filters if isinstance(filters, dict) else {}
            where = []
            params: list[Any] = []

            for field, condition in filters.items():
                field = str(field)
                if field not in self._columns(table) or field in SENSITIVE_COLUMNS:
                    return data_contract(tool_executed="SEARCH", status="error",
                                         system_notice=f"column_not_allowed:{field}")

                if isinstance(condition, dict):
                    for op in ("eq", "contains", "gt", "gte", "lt", "lte", "neq", "in"):
                        if op not in condition:
                            continue
                        value = condition[op]
                        if op == "contains":
                            where.append(f'"{field}" ILIKE %s')
                            params.append(f"%{value}%")
                        elif op == "eq":
                            where.append(f'"{field}" = %s')
                            params.append(value)
                        elif op == "neq":
                            where.append(f'"{field}" <> %s')
                            params.append(value)
                        elif op in {"gt", "gte", "lt", "lte"}:
                            symbol = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[op]
                            where.append(f'"{field}" {symbol} %s')
                            params.append(value)
                        elif op == "in":
                            values = value if isinstance(value, list) else []
                            if not values or len(values) > 50:
                                return data_contract(tool_executed="SEARCH", status="error",
                                                     system_notice="invalid_in_filter")
                            where.append(f'"{field}" IN ({",".join(["%s"] * len(values))})')
                            params.extend(values)
                        break
                else:
                    where.append(f'"{field}" = %s')
                    params.append(condition)

            safe_limit = max(1, min(int(limit or 20), MAX_LIMIT))
            query = f'SELECT {",".join(chr(34)+c+chr(34) for c in select_cols)} FROM "public"."{table}"'
            if where:
                query += " WHERE " + " AND ".join(where)
            query += " LIMIT %s"
            params.append(safe_limit)

            rows = platform_db.rows(query, tuple(params))
            records = [
                {
                    "row_index": idx,
                    "table": table,
                    "fields": clean_db_row_for_ai(row),
                }
                for idx, row in enumerate(rows)
            ]
            return data_contract(tool_executed="SEARCH", data=records)
        except Exception as exc:
            logger.exception("AdminDataTools SEARCH failed")
            return data_contract(tool_executed="SEARCH", status="error",
                                 system_notice=f"search_failed:{str(exc)[:300]}")

    def analyze(self, table: str, record_id: Any, aspects: list[str] | None = None) -> dict[str, Any]:
        table = str(table or "").strip()
        meta = self._table(table)
        if not meta:
            return data_contract(tool_executed="ANALYZE", status="error",
                                 system_notice="table_not_allowed")

        pks = list(meta.get("primary_key") or [])
        if len(pks) != 1:
            return data_contract(tool_executed="ANALYZE", status="error",
                                 system_notice="single_primary_key_required")

        pk = pks[0]
        if not self._validate_columns(table, [pk]):
            return data_contract(tool_executed="ANALYZE", status="error",
                                 system_notice="primary_key_not_allowed")

        try:
            cols = self._select_columns(table)
            row = platform_db.one(
                f'SELECT {",".join(chr(34)+c+chr(34) for c in cols)} '
                f'FROM "public"."{table}" WHERE "{pk}"=%s',
                (record_id,),
            )
            if not row:
                return data_contract(tool_executed="ANALYZE", status="incomplete",
                                     system_notice="record_not_found")

            checks = []
            for fk in meta.get("foreign_keys") or []:
                source = fk.get("from")
                target_table = fk.get("to_table")
                target_column = fk.get("to_column")
                value = row.get(source)
                if value is None:
                    checks.append({
                        "aspect": "foreign_key",
                        "field": source,
                        "status": "incomplete",
                        "target": f"{target_table}.{target_column}",
                    })
                    continue
                target_meta = self._table(target_table)
                if not target_meta or not self._validate_columns(target_table, [target_column]):
                    checks.append({
                        "aspect": "foreign_key",
                        "field": source,
                        "status": "error",
                        "message": "foreign_key_target_not_available",
                    })
                    continue
                target = platform_db.one(
                    f'SELECT "{target_column}" FROM "public"."{target_table}" '
                    f'WHERE "{target_column}"=%s',
                    (value,),
                )
                checks.append({
                    "aspect": "foreign_key",
                    "field": source,
                    "status": "ok" if target else "error",
                    "target": f"{target_table}.{target_column}",
                    "value": value,
                })

            return data_contract(
                tool_executed="ANALYZE",
                data=[{
                    "row_index": 0,
                    "table": table,
                    "fields": row,
                    "analysis_flags": {
                        "primary_key": pk,
                        "foreign_keys": checks,
                    },
                }],
                system_notice="Live-schema analysis completed.",
            )
        except Exception as exc:
            logger.exception("AdminDataTools ANALYZE failed")
            return data_contract(tool_executed="ANALYZE", status="error",
                                 system_notice=f"analyze_failed:{str(exc)[:300]}")

    def check(self, table: str, record_id: Any, aspects: list[str] | None = None) -> dict[str, Any]:
        result = self.analyze(table, record_id, aspects)
        result["tool_executed"] = "CHECK"
        return result

    def compare(self, table: str, ids: list[Any], columns: list[str] | None = None) -> dict[str, Any]:
        table = str(table or "").strip()
        if not self._table(table) or not ids or len(ids) > 20:
            return data_contract(tool_executed="COMPARE", status="error",
                                 system_notice="invalid_compare_request")
        try:
            select_cols = self._select_columns(table, columns)
            pk = (self._table(table).get("primary_key") or [None])[0]
            if not pk or not self._validate_columns(table, [pk]):
                return data_contract(tool_executed="COMPARE", status="error",
                                     system_notice="primary_key_required")
            placeholders = ",".join(["%s"] * len(ids))
            rows = platform_db.rows(
                f'SELECT {",".join(chr(34)+c+chr(34) for c in select_cols)} '
                f'FROM "public"."{table}" WHERE "{pk}" IN ({placeholders})',
                tuple(ids),
            )
            return data_contract(
                tool_executed="COMPARE",
                data=[{"row_index": i, "table": table, "fields": clean_db_row_for_ai(row)} for i, row in enumerate(rows)],
            )
        except Exception as exc:
            return data_contract(tool_executed="COMPARE", status="error",
                                 system_notice=f"compare_failed:{str(exc)[:300]}")

    def suggest(self, table: str, query: str, columns: list[str] | None = None) -> dict[str, Any]:
        """Generic candidate search; semantic ranking remains AI-owned."""
        query = str(query or "").strip()
        if not query:
            return data_contract(tool_executed="SUGGEST", status="error",
                                 system_notice="query_required")
        candidate_columns = columns or [
            c for c in self._columns(table)
            if c in {"name", "name_am", "name_ru", "name_en", "slug", "business_name", "service_name"}
        ]
        if not self._validate_columns(table, candidate_columns):
            return data_contract(tool_executed="SUGGEST", status="error",
                                 system_notice="suggest_columns_not_allowed")
        filters = {"_semantic_query": query}
        # Build a deterministic OR search over known textual candidate fields.
        clauses = [f'"{c}" ILIKE %s' for c in candidate_columns]
        try:
            rows = platform_db.rows(
                f'SELECT {",".join(chr(34)+c+chr(34) for c in candidate_columns)} '
                f'FROM "public"."{table}" WHERE ' + " OR ".join(clauses) + " LIMIT %s",
                tuple([f"%{query}%"] * len(clauses) + [MAX_LIMIT]),
            )
            return data_contract(
                tool_executed="SUGGEST",
                data=[{"row_index": i, "table": table, "fields": row} for i, row in enumerate(rows)],
            )
        except Exception as exc:
            return data_contract(tool_executed="SUGGEST", status="error",
                                 system_notice=f"suggest_failed:{str(exc)[:300]}")


class DataToolsSafe:
    @staticmethod
    def safe(value: Any) -> Any:
        from decimal import Decimal
        from datetime import date, datetime
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, Decimal):
            return float(value)
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        if isinstance(value, dict):
            return {str(k): DataToolsSafe.safe(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [DataToolsSafe.safe(v) for v in value]
        return str(value)
