"""Current PostgreSQL schema bootstrap for Armenia AI Guide.

The schema is intentionally idempotent: it can run on every application start.
It creates dependencies in the correct order so a clean Supabase database can
bootstrap without relying on another module being imported first.
"""
from __future__ import annotations

import os
import psycopg


def db_url() -> str:
    value = os.getenv("DATABASE_URL", "").strip()
    if not value:
        raise RuntimeError("DATABASE_URL is not configured")
    return value


def _connect():
    return psycopg.connect(db_url(), prepare_threshold=None)


def ensure_platform_schema() -> None:
    sql = r'''
    -- ---------------------------------------------------------------------
    -- Core partner tables
    -- ---------------------------------------------------------------------
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

    -- WebApp authentication can reach partner registration before the legacy
    -- bot registration path has inserted the user. Keep the FK strict, but
    -- automatically create the minimal users row first.
    CREATE OR REPLACE FUNCTION ensure_partner_user_exists()
    RETURNS TRIGGER AS $$
    BEGIN
        INSERT INTO users (telegram_id)
        VALUES (NEW.user_id)
        ON CONFLICT (telegram_id) DO NOTHING;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql SET search_path = public, pg_temp;

    DROP TRIGGER IF EXISTS trg_ensure_partner_user_exists ON partners;
    CREATE TRIGGER trg_ensure_partner_user_exists
    BEFORE INSERT ON partners
    FOR EACH ROW
    EXECUTE FUNCTION ensure_partner_user_exists();

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
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        total_cost_amd NUMERIC(18,4) NOT NULL DEFAULT 0,
        exchange_rate_amd NUMERIC(18,6) NOT NULL DEFAULT 0
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

    -- Canonical object-level weekly schedule. Kept separate from data_json so
    -- partner edits cannot be overwritten by legacy registration metadata.
    ALTER TABLE partner_objects
        ADD COLUMN IF NOT EXISTS working_hours JSONB NOT NULL DEFAULT '{}'::jsonb;

    UPDATE partner_objects
       SET working_hours = COALESCE(data_json->'working_hours', '{}'::jsonb)
     WHERE working_hours = '{}'::jsonb
       AND COALESCE(data_json->'working_hours', '{}'::jsonb) <> '{}'::jsonb;

    CREATE TABLE IF NOT EXISTS services (
        id BIGSERIAL PRIMARY KEY,
        partner_id BIGINT NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
        category_id INT REFERENCES categories(id) ON DELETE SET NULL,
        subcategory_id BIGINT REFERENCES categories(id) ON DELETE SET NULL,
        name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        price NUMERIC,
        currency TEXT NOT NULL DEFAULT 'AMD',
        duration_minutes INT,
        -- Per-service tariff OVERRIDE set by admin. NULL = inherit
        -- (subcategory override -> direction default). When admin sets e.g.
        -- 'fixed' here, it wins over the direction's initial 'inside'/'on_top'.
        commission_type TEXT DEFAULT NULL
            CHECK (commission_type IS NULL OR commission_type IN ('inside','on_top','fixed')),
        commission_value NUMERIC DEFAULT NULL,
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

    -- ---------------------------------------------------------------------
    -- Verification documents MUST exist before partner_directions_api adds
    -- partner_direction_id to it.
    -- ---------------------------------------------------------------------
    CREATE TABLE IF NOT EXISTS partner_verification_documents (
        id BIGSERIAL PRIMARY KEY,
        partner_id BIGINT NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
        document_type TEXT NOT NULL DEFAULT 'business_document',
        original_filename TEXT,
        storage_path TEXT UNIQUE,
        file_data BYTEA,
        mime_type TEXT,
        file_size BIGINT NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'pending',
        rejection_reason TEXT,
        reviewed_by BIGINT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        reviewed_at TIMESTAMPTZ,
        partner_direction_id BIGINT
    );
    CREATE INDEX IF NOT EXISTS idx_partner_verification_documents_partner
        ON partner_verification_documents(partner_id, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_partner_verification_documents_status
        ON partner_verification_documents(status, created_at DESC);

    CREATE TABLE IF NOT EXISTS admin_audit_log (
        id BIGSERIAL PRIMARY KEY,
        admin_telegram_id BIGINT NOT NULL,
        action TEXT NOT NULL,
        entity_type TEXT NOT NULL,
        entity_id BIGINT,
        details_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    -- ---------------------------------------------------------------------
    -- Direction tariff/settings table. Admin Directions depends on this.
    -- ---------------------------------------------------------------------
    CREATE TABLE IF NOT EXISTS category_settings (
        id BIGSERIAL PRIMARY KEY,
        category_id INT NOT NULL UNIQUE REFERENCES categories(id) ON DELETE CASCADE,
        bank_commission_type TEXT NOT NULL DEFAULT 'none'
            CHECK (bank_commission_type IN ('none','percent','fixed')),
        bank_commission_value NUMERIC NOT NULL DEFAULT 0,
        cancellation_policy TEXT NOT NULL DEFAULT 'no_refund'
            CHECK (cancellation_policy IN ('full_refund','half_refund','no_refund')),
        premium_contact_enabled BOOLEAN NOT NULL DEFAULT FALSE,
        premium_contact_fee NUMERIC NOT NULL DEFAULT 0,
        premium_disclosure_scope TEXT NOT NULL DEFAULT 'none',
        contact_reveal_after_booking BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    -- Ensure every catalog category has an explicit settings row so admin
    -- configuration is never silently missing.
    INSERT INTO category_settings(category_id)
    SELECT id FROM categories
    ON CONFLICT(category_id) DO NOTHING;

    -- ---------------------------------------------------------------------
    -- AI / catalog research
    -- ---------------------------------------------------------------------
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

    -- ---------------------------------------------------------------------
    -- Marketplace / negotiation
    -- ---------------------------------------------------------------------
    CREATE TABLE IF NOT EXISTS service_requests (
        id BIGSERIAL PRIMARY KEY,
        client_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
        category_id INT REFERENCES categories(id) ON DELETE SET NULL,
        subcategory_id BIGINT REFERENCES categories(id) ON DELETE SET NULL,
        status TEXT NOT NULL DEFAULT 'discovery'
            CHECK (status IN ('discovery','searching','options_found','selected','waiting_partner','negotiating','confirmed','payment','booked','completed','cancelled','dispute')),
        language TEXT DEFAULT 'hy',
        city TEXT,
        summary TEXT NOT NULL DEFAULT '',
        preferences_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS services_catalog_dummy_guard (
        id BIGSERIAL PRIMARY KEY
    );
    DROP TABLE IF EXISTS services_catalog_dummy_guard;

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

    CREATE TABLE IF NOT EXISTS project_expenses (
        id BIGSERIAL PRIMARY KEY,
        -- bookings are created by booking_schema later in bootstrap.
        -- Keep this column dependency-free so a clean install can initialize.
        booking_id BIGINT,
        partner_id BIGINT REFERENCES partners(id) ON DELETE SET NULL,
        expense_type TEXT NOT NULL CHECK (expense_type IN ('payment_fee','refund','other')),
        amount NUMERIC(18,4) NOT NULL DEFAULT 0,
        currency TEXT NOT NULL DEFAULT 'AMD',
        description TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT 'manual',
        data_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_project_expenses_booking ON project_expenses(booking_id, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_project_expenses_partner ON project_expenses(partner_id, created_at DESC);

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

    -- Provider/model-agnostic AI usage ledger. Pricing is supplied by
    -- AI_PRICING_JSON and is never hardcoded into business logic.
    CREATE TABLE IF NOT EXISTS ai_usage_ledger (
        id BIGSERIAL PRIMARY KEY,
        provider TEXT NOT NULL,
        model TEXT NOT NULL,
        chain TEXT NOT NULL DEFAULT 'unknown',
        stage TEXT NOT NULL DEFAULT 'unknown',
        operation TEXT NOT NULL DEFAULT 'chat',
        purpose TEXT NOT NULL DEFAULT '',
        user_id BIGINT,
        partner_id BIGINT REFERENCES partners(id) ON DELETE SET NULL,
        -- partner_businesses is created by the business-layer migration,
        -- which runs after this core schema. The FK is attached there so a
        -- clean database can bootstrap in dependency order.
        company_id BIGINT,
        order_id BIGINT,
        negotiation_id BIGINT,
        input_tokens BIGINT NOT NULL DEFAULT 0,
        output_tokens BIGINT NOT NULL DEFAULT 0,
        cached_input_tokens BIGINT NOT NULL DEFAULT 0,
        reasoning_tokens BIGINT NOT NULL DEFAULT 0,
        total_tokens BIGINT NOT NULL DEFAULT 0,
        input_cost_usd NUMERIC(18,10) NOT NULL DEFAULT 0,
        cached_input_cost_usd NUMERIC(18,10) NOT NULL DEFAULT 0,
        output_cost_usd NUMERIC(18,10) NOT NULL DEFAULT 0,
        total_cost_usd NUMERIC(18,10) NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'success',
        error TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_ai_usage_partner_created ON ai_usage_ledger(partner_id, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_ai_usage_provider_model ON ai_usage_ledger(provider, model, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_ai_usage_chain_stage ON ai_usage_ledger(chain, stage, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_ai_usage_created ON ai_usage_ledger(created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_ai_usage_order_created ON ai_usage_ledger(order_id, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_ai_usage_negotiation_created ON ai_usage_ledger(negotiation_id, created_at DESC);

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

    -- Backfill tariff-override columns on pre-existing services tables.
    ALTER TABLE services ADD COLUMN IF NOT EXISTS commission_type TEXT;
    ALTER TABLE services ADD COLUMN IF NOT EXISTS commission_value NUMERIC;
    -- A service is delivered at one physical partner object. Keep the
    -- location relation canonical instead of hiding it in data_json.
    ALTER TABLE services ADD COLUMN IF NOT EXISTS object_id BIGINT;
    ALTER TABLE services ADD COLUMN IF NOT EXISTS contact_phone TEXT;
    ALTER TABLE partner_objects ADD COLUMN IF NOT EXISTS phone TEXT;
    CREATE INDEX IF NOT EXISTS idx_services_object ON services(object_id);
    -- Backfill direction-level default tariff on pre-existing installs.
    ALTER TABLE master_categories ADD COLUMN IF NOT EXISTS commission_type TEXT NOT NULL DEFAULT 'on_top';
    ALTER TABLE master_categories ADD COLUMN IF NOT EXISTS commission_value NUMERIC NOT NULL DEFAULT 10;

    CREATE INDEX IF NOT EXISTS idx_services_partner_status ON services(partner_id, status);
    ALTER TABLE services DROP CONSTRAINT IF EXISTS fk_services_object;
    ALTER TABLE services ADD CONSTRAINT fk_services_object
        FOREIGN KEY (object_id) REFERENCES partner_objects(id) ON DELETE SET NULL;
    CREATE INDEX IF NOT EXISTS idx_partner_locations_partner ON partner_locations(partner_id);
    CREATE INDEX IF NOT EXISTS idx_service_requests_client ON service_requests(client_id, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_negotiations_request ON negotiations(request_id, status);

    -- ---------------------------------------------------------------------
    -- Phase 3: reviews / support
    -- ---------------------------------------------------------------------
    CREATE TABLE IF NOT EXISTS reviews (
        id BIGSERIAL PRIMARY KEY,
        booking_id BIGINT,
        client_id BIGINT NOT NULL,
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
    ALTER TABLE ai_usage_ledger ADD COLUMN IF NOT EXISTS total_cost_amd NUMERIC(18,4) NOT NULL DEFAULT 0;
    ALTER TABLE ai_usage_ledger ADD COLUMN IF NOT EXISTS exchange_rate_amd NUMERIC(18,6) NOT NULL DEFAULT 0;
    '''

    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
