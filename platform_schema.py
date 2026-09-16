"""Non-destructive PostgreSQL schema for the new Armenia AI Guide architecture."""
from __future__ import annotations

import os
import psycopg


def db_url() -> str:
    value = os.getenv("DATABASE_URL", "").strip()
    if not value:
        raise RuntimeError("DATABASE_URL is not configured")
    return value


def _connect():
    # Render/Supabase may use PgBouncer transaction pooling. Disable Psycopg
    # automatic prepared statements for schema/bootstrap work.
    return psycopg.connect(db_url(), prepare_threshold=None)


def ensure_platform_schema() -> None:
    """Create only new architecture tables; never delete legacy marketplace data."""
    sql = r'''
    CREATE TABLE IF NOT EXISTS partners (
        id BIGSERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL UNIQUE REFERENCES users(telegram_id) ON DELETE CASCADE,
        business_name TEXT NOT NULL DEFAULT '',
        business_description TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'draft'
            CHECK (status IN ('draft','pending','under_review','approved','rejected','suspended','blocked')),
        verification_status TEXT NOT NULL DEFAULT 'not_submitted'
            CHECK (verification_status IN ('not_submitted','pending','approved','rejected')),
        rejection_reason TEXT,
        contact_share_policy TEXT NOT NULL DEFAULT 'after_booking'
            CHECK (contact_share_policy IN ('after_booking','premium','never')),
        contact_sharing_enabled BOOLEAN NOT NULL DEFAULT TRUE,
        premium_contact_sharing_enabled BOOLEAN NOT NULL DEFAULT TRUE,
        profile_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS partner_locations (
        id BIGSERIAL PRIMARY KEY,
        partner_id BIGINT NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
        country TEXT NOT NULL DEFAULT 'Armenia',
        marz TEXT,
        city TEXT,
        village TEXT,
        address TEXT,
        location_type TEXT NOT NULL DEFAULT 'fixed'
            CHECK (location_type IN ('fixed','mobile','online','outbound')),
        data_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS catalog_subcategories (
        id BIGSERIAL PRIMARY KEY,
        category_id INT NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
        name_am TEXT NOT NULL,
        name_ru TEXT NOT NULL,
        name_en TEXT NOT NULL DEFAULT '',
        slug TEXT NOT NULL UNIQUE,
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE(category_id, name_ru)
    );

    CREATE TABLE IF NOT EXISTS partner_objects (
        id BIGSERIAL PRIMARY KEY,
        partner_id BIGINT NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
        object_name TEXT NOT NULL,
        address TEXT,
        city TEXT,
        marz TEXT,
        data_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS partner_services (
        id BIGSERIAL PRIMARY KEY,
        partner_id BIGINT NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
        category_id INT REFERENCES categories(id) ON DELETE SET NULL,
        object_id BIGINT REFERENCES partner_objects(id) ON DELETE SET NULL,
        service_name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        base_price NUMERIC,
        min_price NUMERIC,
        max_price NUMERIC,
        duration_minutes INT,
        price_type TEXT NOT NULL DEFAULT 'fixed',
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS services (
        id BIGSERIAL PRIMARY KEY,
        partner_id BIGINT NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
        category_id INT REFERENCES categories(id) ON DELETE SET NULL,
        subcategory_id BIGINT REFERENCES catalog_subcategories(id) ON DELETE SET NULL,
        name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        price NUMERIC,
        currency TEXT NOT NULL DEFAULT 'AMD',
        duration_minutes INT,
        status TEXT NOT NULL DEFAULT 'draft'
            CHECK (status IN ('draft','pending','approved','rejected','frozen','deleted')),
        data_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS service_packages (
        id BIGSERIAL PRIMARY KEY,
        service_id BIGINT NOT NULL REFERENCES services(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        price NUMERIC NOT NULL DEFAULT 0,
        currency TEXT NOT NULL DEFAULT 'AMD',
        description TEXT NOT NULL DEFAULT '',
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS service_options (
        id BIGSERIAL PRIMARY KEY,
        service_id BIGINT NOT NULL REFERENCES services(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        price_delta NUMERIC NOT NULL DEFAULT 0,
        currency TEXT NOT NULL DEFAULT 'AMD',
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS service_schedule (
        id BIGSERIAL PRIMARY KEY,
        service_id BIGINT NOT NULL REFERENCES services(id) ON DELETE CASCADE,
        weekday INT NOT NULL CHECK (weekday BETWEEN 0 AND 6),
        start_time TIME,
        end_time TIME,
        is_available BOOLEAN NOT NULL DEFAULT TRUE,
        UNIQUE(service_id, weekday)
    );

    CREATE TABLE IF NOT EXISTS ai_sessions (
        id BIGSERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
        role TEXT NOT NULL CHECK (role IN ('client','partner','admin')),
        session_type TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        context_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    CREATE UNIQUE INDEX IF NOT EXISTS ux_ai_active_session
        ON ai_sessions(user_id, role, session_type) WHERE status='active';

    CREATE TABLE IF NOT EXISTS ai_messages (
        id BIGSERIAL PRIMARY KEY,
        session_id BIGINT NOT NULL REFERENCES ai_sessions(id) ON DELETE CASCADE,
        sender_role TEXT NOT NULL CHECK (sender_role IN ('user','ai','admin','system')),
        message_text TEXT NOT NULL,
        data_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_ai_messages_session ON ai_messages(session_id, created_at);

    CREATE TABLE IF NOT EXISTS ai_catalog_proposals (
        id BIGSERIAL PRIMARY KEY,
        source TEXT NOT NULL DEFAULT 'partner_ai',
        partner_id BIGINT REFERENCES partners(id) ON DELETE SET NULL,
        direction_id BIGINT,
        proposed_master_category TEXT,
        proposed_category TEXT,
        proposed_subcategory TEXT,
        proposed_service TEXT,
        description TEXT NOT NULL DEFAULT '',
        reason TEXT NOT NULL DEFAULT '',
        payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending','edited','clarification','approved','rejected','merged')),
        admin_comment TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        reviewed_at TIMESTAMPTZ,
        reviewed_by BIGINT
    );
    CREATE INDEX IF NOT EXISTS idx_ai_catalog_proposals_status ON ai_catalog_proposals(status, created_at DESC);

    CREATE TABLE IF NOT EXISTS potential_partners (
        id BIGSERIAL PRIMARY KEY,
        source TEXT NOT NULL DEFAULT 'ai_research',
        business_name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        direction TEXT,
        category TEXT,
        subcategory TEXT,
        country TEXT DEFAULT 'Armenia',
        marz TEXT,
        city TEXT,
        village TEXT,
        phone TEXT,
        website TEXT,
        email TEXT,
        social_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        services_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        prices_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        source_urls_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        ai_reason TEXT NOT NULL DEFAULT '',
        ai_confidence NUMERIC,
        status TEXT NOT NULL DEFAULT 'new'
            CHECK (status IN ('new','researched','ready_for_review','contacted','interested','invited','registered','approved','active','rejected','archived')),
        partner_id BIGINT REFERENCES partners(id) ON DELETE SET NULL,
        admin_comment TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_potential_partners_status ON potential_partners(status, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_potential_partners_location ON potential_partners(marz, city, village);

    CREATE TABLE IF NOT EXISTS potential_partner_sources (
        id BIGSERIAL PRIMARY KEY,
        potential_partner_id BIGINT NOT NULL REFERENCES potential_partners(id) ON DELETE CASCADE,
        source_type TEXT NOT NULL,
        url TEXT,
        title TEXT,
        raw_text TEXT,
        captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS admin_clarifications (
        id BIGSERIAL PRIMARY KEY,
        proposal_id BIGINT REFERENCES ai_catalog_proposals(id) ON DELETE CASCADE,
        partner_id BIGINT REFERENCES partners(id) ON DELETE CASCADE,
        admin_id BIGINT,
        message TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'sent' CHECK (status IN ('sent','answered','closed')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        answered_at TIMESTAMPTZ
    );

    CREATE TABLE IF NOT EXISTS client_profiles (
        user_id BIGINT PRIMARY KEY REFERENCES users(telegram_id) ON DELETE CASCADE,
        preferences_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS service_requests (
        id BIGSERIAL PRIMARY KEY,
        client_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
        category_id INT REFERENCES categories(id) ON DELETE SET NULL,
        subcategory_id BIGINT REFERENCES catalog_subcategories(id) ON DELETE SET NULL,
        status TEXT NOT NULL DEFAULT 'discovery'
            CHECK (status IN ('discovery','searching','options_found','selected','waiting_partner','negotiating','confirmed','payment','booked','completed','cancelled','dispute')),
        language TEXT DEFAULT 'hy',
        city TEXT,
        summary TEXT NOT NULL DEFAULT '',
        preferences_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS request_candidates (
        id BIGSERIAL PRIMARY KEY,
        request_id BIGINT NOT NULL REFERENCES service_requests(id) ON DELETE CASCADE,
        partner_id BIGINT NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
        service_id BIGINT REFERENCES services(id) ON DELETE SET NULL,
        rank_score NUMERIC,
        match_reason TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'suggested',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE(request_id, partner_id, service_id)
    );

    CREATE TABLE IF NOT EXISTS negotiations (
        id BIGSERIAL PRIMARY KEY,
        request_id BIGINT NOT NULL REFERENCES service_requests(id) ON DELETE CASCADE,
        client_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
        partner_id BIGINT NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
        status TEXT NOT NULL DEFAULT 'active'
            CHECK (status IN ('active','agreed','declined','expired','cancelled')),
        state_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS negotiation_messages (
        id BIGSERIAL PRIMARY KEY,
        negotiation_id BIGINT NOT NULL REFERENCES negotiations(id) ON DELETE CASCADE,
        sender_role TEXT NOT NULL CHECK (sender_role IN ('client','partner','ai')),
        sender_id BIGINT,
        message TEXT NOT NULL,
        data_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS partner_employees (
        id BIGSERIAL PRIMARY KEY,
        partner_id BIGINT NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
        full_name TEXT NOT NULL,
        role_title TEXT NOT NULL DEFAULT '',
        phone TEXT,
        object_id BIGINT REFERENCES partner_objects(id) ON DELETE SET NULL,
        skills_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_partner_employees_partner ON partner_employees(partner_id);

    CREATE TABLE IF NOT EXISTS notifications (
        id BIGSERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
        audience TEXT NOT NULL DEFAULT 'user'
            CHECK (audience IN ('client','partner','admin','user')),
        kind TEXT NOT NULL DEFAULT 'info',
        title TEXT NOT NULL DEFAULT '',
        body TEXT NOT NULL DEFAULT '',
        data_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        is_read BOOLEAN NOT NULL DEFAULT FALSE,
        delivered_telegram BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, is_read, created_at DESC);

    CREATE TABLE IF NOT EXISTS admin_settings (
        key TEXT PRIMARY KEY,
        value_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS ai_research_tasks (
        id BIGSERIAL PRIMARY KEY,
        created_by BIGINT,
        marz TEXT,
        city TEXT,
        village TEXT,
        direction TEXT,
        subcategory TEXT,
        target_count INT NOT NULL DEFAULT 10,
        invite_mode TEXT NOT NULL DEFAULT 'admin'
            CHECK (invite_mode IN ('admin','ai')),
        status TEXT NOT NULL DEFAULT 'new'
            CHECK (status IN ('new','running','completed','failed','cancelled')),
        found_count INT NOT NULL DEFAULT 0,
        params_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        result_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        error TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_ai_research_tasks_status ON ai_research_tasks(status, created_at DESC);

    CREATE INDEX IF NOT EXISTS idx_services_partner_status ON services(partner_id, status);
    CREATE INDEX IF NOT EXISTS idx_partner_locations_partner ON partner_locations(partner_id);
    CREATE INDEX IF NOT EXISTS idx_service_requests_client ON service_requests(client_id, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_negotiations_request ON negotiations(request_id, status);

    -- Phase 3: reviews & ratings -------------------------------------------
    CREATE TABLE IF NOT EXISTS reviews (
        id BIGSERIAL PRIMARY KEY,
        booking_id BIGINT,
        client_id  BIGINT NOT NULL,
        partner_id BIGINT NOT NULL,
        service_id BIGINT,
        rating SMALLINT NOT NULL CHECK (rating BETWEEN 1 AND 5),
        comment TEXT NOT NULL DEFAULT '',
        partner_reply TEXT,
        status TEXT NOT NULL DEFAULT 'published'
            CHECK (status IN ('published','hidden','flagged')),
        data_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE(booking_id, client_id)
    );
    CREATE INDEX IF NOT EXISTS idx_reviews_partner ON reviews(partner_id, status);
    CREATE INDEX IF NOT EXISTS idx_reviews_client ON reviews(client_id, created_at DESC);

    -- Phase 3: support tickets ---------------------------------------------
    CREATE TABLE IF NOT EXISTS support_tickets (
        id BIGSERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL,
        audience TEXT NOT NULL DEFAULT 'client'
            CHECK (audience IN ('client','partner')),
        subject TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'open'
            CHECK (status IN ('open','ai_answered','escalated','resolved','closed')),
        priority TEXT NOT NULL DEFAULT 'normal',
        data_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_support_tickets_status ON support_tickets(status, updated_at DESC);
    CREATE INDEX IF NOT EXISTS idx_support_tickets_user ON support_tickets(user_id, updated_at DESC);

    CREATE TABLE IF NOT EXISTS support_ticket_messages (
        id BIGSERIAL PRIMARY KEY,
        ticket_id BIGINT NOT NULL REFERENCES support_tickets(id) ON DELETE CASCADE,
        sender_role TEXT NOT NULL CHECK (sender_role IN ('user','ai','admin')),
        sender_id BIGINT,
        message TEXT NOT NULL,
        data_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_support_ticket_messages ON support_ticket_messages(ticket_id, created_at);
    '''
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
