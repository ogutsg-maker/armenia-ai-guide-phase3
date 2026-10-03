CREATE TABLE IF NOT EXISTS aig_users(
 telegram_id BIGINT PRIMARY KEY, username TEXT, full_name TEXT, role TEXT CHECK(role IN('client','partner','admin')), lang TEXT NOT NULL DEFAULT 'hy', created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS aig_partners(
 id BIGSERIAL PRIMARY KEY, telegram_id BIGINT UNIQUE NOT NULL REFERENCES aig_users(telegram_id), phone TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS aig_companies(
 id BIGSERIAL PRIMARY KEY, partner_id BIGINT NOT NULL REFERENCES aig_partners(id) ON DELETE CASCADE, name TEXT NOT NULL, archived BOOLEAN NOT NULL DEFAULT false, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS aig_addresses(
 id BIGSERIAL PRIMARY KEY, company_id BIGINT NOT NULL REFERENCES aig_companies(id) ON DELETE CASCADE, marz TEXT, city TEXT, district TEXT, address TEXT, lat DOUBLE PRECISION, lng DOUBLE PRECISION, is_base BOOLEAN NOT NULL DEFAULT false
);
CREATE TABLE IF NOT EXISTS aig_services(
 id BIGSERIAL PRIMARY KEY, company_id BIGINT NOT NULL REFERENCES aig_companies(id) ON DELETE CASCADE, name TEXT NOT NULL, price_type TEXT NOT NULL CHECK(price_type IN('fixed','from')), price_amd NUMERIC(12,2) NOT NULL, hours TEXT, at_client BOOLEAN NOT NULL DEFAULT false, territory TEXT, address_id BIGINT REFERENCES aig_addresses(id), internal_phone TEXT, required_document BOOLEAN NOT NULL DEFAULT false, status TEXT NOT NULL DEFAULT 'PENDING_ADMIN', catalog_category_id BIGINT, classification_confidence NUMERIC(5,4), rejection_reason TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS aig_service_documents(
 id BIGSERIAL PRIMARY KEY, service_id BIGINT NOT NULL REFERENCES aig_services(id) ON DELETE CASCADE, file_name TEXT NOT NULL, mime_type TEXT NOT NULL, storage_ref TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING_ADMIN', created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS aig_service_applications(
 id BIGSERIAL PRIMARY KEY, service_id BIGINT NOT NULL REFERENCES aig_services(id) ON DELETE CASCADE, status TEXT NOT NULL DEFAULT 'PENDING_ADMIN', submitted_at TIMESTAMPTZ NOT NULL DEFAULT now(), reviewed_at TIMESTAMPTZ, reviewer BIGINT, reason TEXT
);
CREATE TABLE IF NOT EXISTS aig_potential_partners(
 id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL, phone TEXT, city TEXT, source TEXT, source_ref TEXT, status TEXT NOT NULL DEFAULT 'new', notes TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS aig_client_requests(
 id BIGSERIAL PRIMARY KEY, client_telegram_id BIGINT NOT NULL REFERENCES aig_users(telegram_id), service_text TEXT NOT NULL, city TEXT, district TEXT, status TEXT NOT NULL DEFAULT 'searching', created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS aig_negotiations(
 id BIGSERIAL PRIMARY KEY, request_id BIGINT NOT NULL REFERENCES aig_client_requests(id) ON DELETE CASCADE, service_id BIGINT NOT NULL REFERENCES aig_services(id), partner_id BIGINT NOT NULL REFERENCES aig_partners(id), status TEXT NOT NULL DEFAULT 'active', agreed_min NUMERIC(12,2), agreed_max NUMERIC(12,2), agreed_price NUMERIC(12,2), commission_base NUMERIC(12,2), partner_interest_at TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE(request_id,partner_id)
);
CREATE TABLE IF NOT EXISTS aig_messages(
 id BIGSERIAL PRIMARY KEY, negotiation_id BIGINT NOT NULL REFERENCES aig_negotiations(id) ON DELETE CASCADE, sender_role TEXT NOT NULL CHECK(sender_role IN('client','partner')), sender_telegram_id BIGINT NOT NULL, message TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS aig_bookings(
 id BIGSERIAL PRIMARY KEY, negotiation_id BIGINT UNIQUE NOT NULL REFERENCES aig_negotiations(id), status TEXT NOT NULL DEFAULT 'PENDING_PARTNER_CONFIRMATION', service_start TIMESTAMPTZ, agreed_price NUMERIC(12,2) NOT NULL, commission_amd NUMERIC(12,2) NOT NULL DEFAULT 0, payment_ref TEXT, payment_confirmed_at TIMESTAMPTZ, qr_token TEXT UNIQUE, qr_expires_at TIMESTAMPTZ, checked_in_at TIMESTAMPTZ, completed_at TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS aig_reviews(
 id BIGSERIAL PRIMARY KEY, booking_id BIGINT UNIQUE NOT NULL REFERENCES aig_bookings(id), client_telegram_id BIGINT NOT NULL, rating INT CHECK(rating BETWEEN 1 AND 5), text TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS aig_arbitrations(
 id BIGSERIAL PRIMARY KEY, booking_id BIGINT NOT NULL REFERENCES aig_bookings(id), opened_by BIGINT NOT NULL, status TEXT NOT NULL DEFAULT 'OPEN', issue TEXT NOT NULL, resolution TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), closed_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS aig_notifications(
 id BIGSERIAL PRIMARY KEY, telegram_id BIGINT NOT NULL, kind TEXT NOT NULL, payload JSONB NOT NULL DEFAULT '{}'::jsonb, read_at TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS aig_ai_sessions(
 telegram_id BIGINT PRIMARY KEY, context TEXT NOT NULL, pending JSONB, updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS aig_ai_costs(
 id BIGSERIAL PRIMARY KEY, telegram_id BIGINT, provider TEXT, model TEXT, operation TEXT, input_tokens INT DEFAULT 0, output_tokens INT DEFAULT 0, usd NUMERIC(12,8) DEFAULT 0, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS aig_services_active_idx ON aig_services(status,company_id);
CREATE INDEX IF NOT EXISTS aig_notifications_user_idx ON aig_notifications(telegram_id,read_at);
