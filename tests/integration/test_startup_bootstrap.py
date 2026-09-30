import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import psycopg
import pytest

from database import DatabaseManager
from runtime_platform_bootstrap import _bootstrap


DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://test_user:test_password@localhost:5432/test_db",
)
ROOT = Path(__file__).resolve().parents[2]


class StartupSQLViolation(RuntimeError):
    pass


class GuardedCursor(psycopg.Cursor):
    def execute(self, query, params=None, **kwargs):
        sql = str(query)
        normalized = " ".join(
            line.split("--", 1)[0] for line in sql.splitlines()
        ).strip()
        statements = [s.strip() for s in normalized.split(";") if s.strip()]

        allowed = (
            "CREATE TABLE IF NOT EXISTS",
            "CREATE INDEX IF NOT EXISTS",
            "CREATE EXTENSION IF NOT EXISTS",
            "SELECT",
            "SHOW",
            "SET",
            "BEGIN",
            "COMMIT",
            "ROLLBACK",
        )
        forbidden = (
            "ALTER TABLE",
            "UPDATE ",
            "INSERT ",
            "DELETE ",
            "DROP ",
            "TRUNCATE ",
            "CREATE TRIGGER",
            "DROP TRIGGER",
            "CREATE FUNCTION",
            "CREATE OR REPLACE FUNCTION",
            "DROP FUNCTION",
        )

        for statement in statements:
            upper = statement.upper()
            if any(upper.startswith(prefix) for prefix in forbidden):
                raise StartupSQLViolation(
                    f"Forbidden startup SQL: {statement}"
                )
            if not any(upper.startswith(prefix) for prefix in allowed):
                # Transaction/control statements not relevant to the bootstrap
                # are deliberately rejected rather than silently permitted.
                raise StartupSQLViolation(
                    f"Unexpected startup SQL: {statement}"
                )

        return super().execute(query, params, **kwargs)


class GuardedConnection(psycopg.Connection):
    def cursor(self, *args, **kwargs):
        kwargs.setdefault("cursor_factory", GuardedCursor)
        return super().cursor(*args, **kwargs)


@pytest.fixture
def guarded_psycopg(monkeypatch):
    original = psycopg.connect

    def connect(*args, **kwargs):
        kwargs.setdefault("connection_class", GuardedConnection)
        return original(*args, **kwargs)

    monkeypatch.setattr(psycopg, "connect", connect)
    yield


@pytest.fixture
def runtime_context():
    import __main__

    originals = {
        name: getattr(__main__, name, None)
        for name in ("db", "ai", "bot")
    }

    # DatabaseManager.__init__ executes its own init_db() path.
    db_manager = DatabaseManager()
    __main__.db = db_manager
    __main__.ai = MagicMock()
    __main__.bot = MagicMock()

    yield db_manager

    for name, value in originals.items():
        if value is None:
            __main__.__dict__.pop(name, None)
        else:
            setattr(__main__, name, value)


def reset_public_schema():
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE")
            cur.execute("CREATE SCHEMA public")
        conn.commit()


def apply_sql_file(path: Path):
    sql = path.read_text(encoding="utf-8")
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()


def schema_snapshot():
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_name, column_name, data_type, is_nullable
                FROM information_schema.columns
                WHERE table_schema = 'public'
                ORDER BY table_name, ordinal_position
                """
            )
            columns = cur.fetchall()

            cur.execute(
                """
                SELECT indexname, indexdef
                FROM pg_indexes
                WHERE schemaname = 'public'
                ORDER BY indexname
                """
            )
            indexes = cur.fetchall()

            cur.execute(
                """
                SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid)
                FROM pg_constraint
                WHERE connamespace = 'public'::regnamespace
                ORDER BY conrelid::regclass::text, conname
                """
            )
            constraints = cur.fetchall()

            return columns, indexes, constraints


def table_counts():
    tables = (
        "partners",
        "partner_businesses",
        "partner_applications",
        "partner_directions",
        "partner_direction_categories",
        "services",
        "partner_verification_documents",
        "partner_objects",
    )
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            result = {}
            for table in tables:
                cur.execute(
                    "SELECT to_regclass(%s)",
                    (f"public.{table}",),
                )
                if cur.fetchone()[0] is not None:
                    cur.execute(f"SELECT COUNT(*) FROM {table}")
                    result[table] = cur.fetchone()[0]
            return result


def seed_legacy_fixture():
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE")
            cur.execute("CREATE SCHEMA public")

            # Minimal dependency graph required by the real migration. The
            # migration itself is responsible for the modern business schema.
            cur.execute(
                """
                CREATE TABLE partners(
                    id BIGSERIAL PRIMARY KEY,
                    status TEXT,
                    verification_status TEXT,
                    user_id BIGINT,
                    business_description TEXT
                );
                CREATE TABLE master_categories(
                    id INT PRIMARY KEY
                );
                CREATE TABLE categories(
                    id INT PRIMARY KEY,
                    master_category_id INT
                );
                CREATE TABLE master_skills(
                    user_id BIGINT,
                    category_id INT,
                    is_active BOOLEAN
                );
                CREATE TABLE partner_directions(
                    id BIGSERIAL PRIMARY KEY,
                    partner_id BIGINT NOT NULL,
                    master_category_id INT NOT NULL,
                    status TEXT NOT NULL,
                    CONSTRAINT partner_directions_partner_id_master_category_id_key
                        UNIQUE(partner_id, master_category_id)
                );
                CREATE TABLE services(
                    id BIGSERIAL PRIMARY KEY,
                    partner_id BIGINT,
                    category_id INT,
                    status TEXT,
                    description TEXT
                );
                CREATE TABLE partner_verification_documents(
                    id BIGSERIAL PRIMARY KEY,
                    partner_id BIGINT,
                    partner_direction_id BIGINT,
                    status TEXT
                );
                CREATE TABLE partner_objects(
                    id BIGSERIAL PRIMARY KEY,
                    partner_id BIGINT,
                    object_name TEXT,
                    address TEXT,
                    city TEXT,
                    marz TEXT,
                    data_json JSONB DEFAULT '{}'::jsonb
                );
                CREATE TABLE partner_locations(
                    id BIGSERIAL PRIMARY KEY,
                    partner_id BIGINT
                );
                CREATE TABLE bookings(
                    id BIGSERIAL PRIMARY KEY,
                    partner_id BIGINT
                );
                CREATE TABLE service_direction_requests(
                    id BIGSERIAL PRIMARY KEY,
                    partner_id BIGINT,
                    requested_master_category_id INT,
                    requested_service_name TEXT,
                    status TEXT
                );
                """
            )
            cur.execute("INSERT INTO master_categories VALUES (500)")
            cur.execute(
                "INSERT INTO categories VALUES (5,500)"
            )
            cur.execute(
                "INSERT INTO partners(status,verification_status,user_id) "
                "VALUES ('approved','approved',9)"
            )
            cur.execute(
                "INSERT INTO master_skills VALUES (9,5,TRUE)"
            )
            # Two legacy duplicates deliberately exercise repair-before-index.
            cur.execute(
                """
                INSERT INTO partner_directions
                    (partner_id,master_category_id,status)
                VALUES
                    (1,500,'approved'),
                    (1,500,'pending')
                """
            )
            cur.execute(
                """
                INSERT INTO partner_verification_documents
                    (partner_id,partner_direction_id,status)
                VALUES (1,999999,'pending')
                """
            )
        conn.commit()


def test_scenario_a_legacy_migration():
    seed_legacy_fixture()
    apply_sql_file(
        ROOT / "migrations" / "20260930_isolate_runtime_schema_mutations.sql"
    )

    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name='partner_directions'
                  AND column_name='business_id'
                """
            )
            assert cur.fetchone() is not None

            cur.execute(
                """
                SELECT 1
                FROM pg_constraint
                WHERE conname='partner_directions_partner_id_master_category_id_key'
                """
            )
            assert cur.fetchone() is None

            cur.execute(
                """
                SELECT 1
                FROM pg_constraint
                WHERE conrelid='partner_verification_documents'::regclass
                  AND pg_get_constraintdef(oid) LIKE '%partner_direction_id%'
                """
            )
            assert cur.fetchone() is None

            cur.execute(
                """
                SELECT indexname
                FROM pg_indexes
                WHERE indexname='uq_partner_direction_business_master'
                """
            )
            assert cur.fetchone() is not None

            cur.execute(
                """
                SELECT partner_direction_id
                FROM partner_verification_documents
                WHERE partner_direction_id=999999
                """
            )
            assert cur.fetchone() is not None


@pytest.mark.asyncio
async def test_scenario_b_clean_boot(guarded_psycopg, runtime_context):
    reset_public_schema()

    # DatabaseManager's constructor/init_db is part of the real startup path.
    db_manager = runtime_context
    assert db_manager is not None

    app = MagicMock()
    await _bootstrap(app)


@pytest.mark.asyncio
async def test_scenario_c_runtime_idempotency(
    guarded_psycopg, runtime_context
):
    reset_public_schema()
    db_manager = runtime_context
    assert db_manager is not None

    app = MagicMock()
    await _bootstrap(app)
    snapshot_a = (schema_snapshot(), table_counts())

    await _bootstrap(app)
    await _bootstrap(app)
    snapshot_b = (schema_snapshot(), table_counts())

    assert snapshot_a == snapshot_b
