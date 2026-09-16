"""Safe PostgreSQL booking/payment/check-in schema migration.

The production database may already contain a bookings table created by
an earlier version of Armenia AI Guide.

CREATE TABLE IF NOT EXISTS does not modify an existing table, therefore
missing columns must be added explicitly and non-destructively.

IMPORTANT:
- existing tables are NOT dropped;
- existing rows are NOT deleted;
- existing columns are NOT recreated;
- missing columns are added only when necessary.

Supabase/managed PostgreSQL can use PgBouncer transaction pooling. Psycopg's
automatic prepared statements are unsafe with that setup because a prepared
statement can exist on one backend connection while the next statement is
routed to another backend. The schema migration therefore explicitly disables
automatic prepared statements for this connection.
"""

from __future__ import annotations

import os
import psycopg


def _db_url() -> str:
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL is not configured")
    return url


def _connect():
    # IMPORTANT: do not let psycopg create named prepared statements here.
    # Render/Supabase may route DATABASE_URL through PgBouncer transaction
    # pooling, where prepared statements can collide (DuplicatePreparedStatement).
    return psycopg.connect(_db_url(), prepare_threshold=None)


def _column_exists(cur, table: str, column: str) -> bool:
    cur.execute(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = %s
          AND column_name = %s
        LIMIT 1
        """,
        (table, column),
    )
    return cur.fetchone() is not None


def _add_column(cur, table: str, column: str, definition: str) -> None:
    if not _column_exists(cur, table, column):
        cur.execute(
            f'ALTER TABLE "{table}" ADD COLUMN "{column}" {definition}'
        )


def _ensure_bookings_table(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS bookings (
            id BIGSERIAL PRIMARY KEY,
            request_id BIGINT,
            negotiation_id BIGINT,
            client_id BIGINT,
            partner_id BIGINT,
            service_id BIGINT,
            package_id BIGINT,
            status TEXT DEFAULT 'pending',
            service_name TEXT DEFAULT '',
            agreed_price NUMERIC DEFAULT 0,
            currency TEXT DEFAULT 'AMD',
            commission_amount NUMERIC DEFAULT 0,
            partner_amount NUMERIC DEFAULT 0,
            scheduled_at TIMESTAMPTZ,
            client_note TEXT DEFAULT '',
            data_json JSONB DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )

    columns = {
        "request_id": "BIGINT",
        "negotiation_id": "BIGINT",
        "client_id": "BIGINT",
        "partner_id": "BIGINT",
        "service_id": "BIGINT",
        "package_id": "BIGINT",
        "status": "TEXT DEFAULT 'pending'",
        "service_name": "TEXT DEFAULT ''",
        "agreed_price": "NUMERIC DEFAULT 0",
        "currency": "TEXT DEFAULT 'AMD'",
        "commission_amount": "NUMERIC DEFAULT 0",
        "partner_amount": "NUMERIC DEFAULT 0",
        "scheduled_at": "TIMESTAMPTZ",
        "client_note": "TEXT DEFAULT ''",
        "data_json": "JSONB DEFAULT '{}'::jsonb",
        "created_at": "TIMESTAMPTZ DEFAULT NOW()",
        "updated_at": "TIMESTAMPTZ DEFAULT NOW()",
    }

    for column, definition in columns.items():
        _add_column(cur, "bookings", column, definition)

    cur.execute(
        """
        UPDATE bookings
        SET
            status = COALESCE(status, 'pending'),
            service_name = COALESCE(service_name, ''),
            agreed_price = COALESCE(agreed_price, 0),
            currency = COALESCE(currency, 'AMD'),
            commission_amount = COALESCE(commission_amount, 0),
            partner_amount = COALESCE(partner_amount, 0),
            client_note = COALESCE(client_note, ''),
            data_json = COALESCE(data_json, '{}'::jsonb),
            created_at = COALESCE(created_at, NOW()),
            updated_at = COALESCE(updated_at, NOW())
        """
    )

    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_bookings_partner_status
        ON bookings(partner_id, status, updated_at DESC)
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_bookings_client_status
        ON bookings(client_id, status, updated_at DESC)
        """
    )


def _ensure_payments(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS payments (
            id BIGSERIAL PRIMARY KEY,
            booking_id BIGINT,
            client_id BIGINT,
            partner_id BIGINT,
            payment_type TEXT DEFAULT 'commission',
            status TEXT DEFAULT 'pending',
            amount NUMERIC DEFAULT 0,
            currency TEXT DEFAULT 'AMD',
            provider TEXT,
            provider_payment_id TEXT,
            data_json JSONB DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )

    columns = {
        "booking_id": "BIGINT",
        "client_id": "BIGINT",
        "partner_id": "BIGINT",
        "payment_type": "TEXT DEFAULT 'commission'",
        "status": "TEXT DEFAULT 'pending'",
        "amount": "NUMERIC DEFAULT 0",
        "currency": "TEXT DEFAULT 'AMD'",
        "provider": "TEXT",
        "provider_payment_id": "TEXT",
        "data_json": "JSONB DEFAULT '{}'::jsonb",
        "created_at": "TIMESTAMPTZ DEFAULT NOW()",
        "updated_at": "TIMESTAMPTZ DEFAULT NOW()",
    }
    for column, definition in columns.items():
        _add_column(cur, "payments", column, definition)

    cur.execute("CREATE INDEX IF NOT EXISTS idx_payments_booking ON payments(booking_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_payments_partner ON payments(partner_id, status)")


def _ensure_checkins(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS booking_checkins (
            id BIGSERIAL PRIMARY KEY,
            booking_id BIGINT NOT NULL UNIQUE,
            token TEXT NOT NULL UNIQUE,
            status TEXT DEFAULT 'active',
            issued_at TIMESTAMPTZ DEFAULT NOW(),
            checked_in_at TIMESTAMPTZ,
            checked_in_by BIGINT,
            data_json JSONB DEFAULT '{}'::jsonb
        )
        """
    )
    columns = {
        "booking_id": "BIGINT",
        "token": "TEXT",
        "status": "TEXT DEFAULT 'active'",
        "issued_at": "TIMESTAMPTZ DEFAULT NOW()",
        "checked_in_at": "TIMESTAMPTZ",
        "checked_in_by": "BIGINT",
        "data_json": "JSONB DEFAULT '{}'::jsonb",
    }
    for column, definition in columns.items():
        _add_column(cur, "booking_checkins", column, definition)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_booking_checkins_booking ON booking_checkins(booking_id)")


def _ensure_cancellations(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS booking_cancellations (
            id BIGSERIAL PRIMARY KEY,
            booking_id BIGINT NOT NULL,
            cancelled_by TEXT DEFAULT 'system',
            reason TEXT DEFAULT '',
            refund_amount NUMERIC DEFAULT 0,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )
    columns = {
        "booking_id": "BIGINT",
        "cancelled_by": "TEXT DEFAULT 'system'",
        "reason": "TEXT DEFAULT ''",
        "refund_amount": "NUMERIC DEFAULT 0",
        "created_at": "TIMESTAMPTZ DEFAULT NOW()",
    }
    for column, definition in columns.items():
        _add_column(cur, "booking_cancellations", column, definition)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_booking_cancellations_booking ON booking_cancellations(booking_id, created_at DESC)")


def _ensure_financial_ledger(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS partner_financial_ledger (
            id BIGSERIAL PRIMARY KEY,
            partner_id BIGINT NOT NULL,
            booking_id BIGINT,
            entry_type TEXT DEFAULT 'sale',
            amount NUMERIC DEFAULT 0,
            currency TEXT DEFAULT 'AMD',
            description TEXT DEFAULT '',
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )
    columns = {
        "partner_id": "BIGINT",
        "booking_id": "BIGINT",
        "entry_type": "TEXT DEFAULT 'sale'",
        "amount": "NUMERIC DEFAULT 0",
        "currency": "TEXT DEFAULT 'AMD'",
        "description": "TEXT DEFAULT ''",
        "created_at": "TIMESTAMPTZ DEFAULT NOW()",
    }
    for column, definition in columns.items():
        _add_column(cur, "partner_financial_ledger", column, definition)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_partner_financial_ledger ON partner_financial_ledger(partner_id, created_at DESC)")


def _ensure_contact_disclosures(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS contact_disclosures (
            id BIGSERIAL PRIMARY KEY,
            client_id BIGINT NOT NULL,
            partner_id BIGINT NOT NULL,
            payment_id BIGINT,
            status TEXT DEFAULT 'pending',
            fee_amount NUMERIC DEFAULT 0,
            disclosure_scope TEXT DEFAULT 'limited',
            data_json JSONB DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )
    columns = {
        "client_id": "BIGINT",
        "partner_id": "BIGINT",
        "payment_id": "BIGINT",
        "status": "TEXT DEFAULT 'pending'",
        "fee_amount": "NUMERIC DEFAULT 0",
        "disclosure_scope": "TEXT DEFAULT 'limited'",
        "data_json": "JSONB DEFAULT '{}'::jsonb",
        "created_at": "TIMESTAMPTZ DEFAULT NOW()",
        "updated_at": "TIMESTAMPTZ DEFAULT NOW()",
    }
    for column, definition in columns.items():
        _add_column(cur, "contact_disclosures", column, definition)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_contact_disclosures_client ON contact_disclosures(client_id, created_at DESC)")


def _ensure_payouts(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS partner_payouts (
            id BIGSERIAL PRIMARY KEY,
            partner_id BIGINT NOT NULL,
            amount NUMERIC NOT NULL DEFAULT 0,
            currency TEXT NOT NULL DEFAULT 'AMD',
            status TEXT NOT NULL DEFAULT 'requested',
            method TEXT DEFAULT 'idram',
            destination TEXT DEFAULT '',
            note TEXT DEFAULT '',
            data_json JSONB DEFAULT '{}'::jsonb,
            requested_at TIMESTAMPTZ DEFAULT NOW(),
            processed_at TIMESTAMPTZ
        )
        """
    )
    columns = {
        "partner_id": "BIGINT",
        "amount": "NUMERIC NOT NULL DEFAULT 0",
        "currency": "TEXT NOT NULL DEFAULT 'AMD'",
        "status": "TEXT NOT NULL DEFAULT 'requested'",
        "method": "TEXT DEFAULT 'idram'",
        "destination": "TEXT DEFAULT ''",
        "note": "TEXT DEFAULT ''",
        "data_json": "JSONB DEFAULT '{}'::jsonb",
        "requested_at": "TIMESTAMPTZ DEFAULT NOW()",
        "processed_at": "TIMESTAMPTZ",
    }
    for column, definition in columns.items():
        _add_column(cur, "partner_payouts", column, definition)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_partner_payouts ON partner_payouts(partner_id, requested_at DESC)")


def ensure_booking_schema() -> None:
    """Create and safely upgrade booking-related PostgreSQL tables."""
    with _connect() as conn:
        with conn.cursor() as cur:
            _ensure_bookings_table(cur)
            _ensure_payments(cur)
            _ensure_checkins(cur)
            _ensure_cancellations(cur)
            _ensure_financial_ledger(cur)
            _ensure_contact_disclosures(cur)
            _ensure_payouts(cur)
        conn.commit()
