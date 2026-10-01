-- Direction-level document verification (service-independent)
ALTER TABLE master_categories
  ADD COLUMN IF NOT EXISTS verification_required BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS verification_document_types JSONB NOT NULL DEFAULT '[]'::jsonb;

CREATE TABLE IF NOT EXISTS partner_direction_verification_cases (
    id BIGSERIAL PRIMARY KEY,
    partner_direction_id BIGINT NOT NULL REFERENCES partner_directions(id) ON DELETE CASCADE,
    partner_id BIGINT NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
    business_id BIGINT NOT NULL REFERENCES partner_businesses(id) ON DELETE CASCADE,
    master_category_id BIGINT NOT NULL REFERENCES master_categories(id),
    status TEXT NOT NULL DEFAULT 'awaiting_document',
    requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    submitted_at TIMESTAMPTZ,
    reviewed_at TIMESTAMPTZ,
    reviewed_by BIGINT,
    rejection_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pdvc_partner_status
  ON partner_direction_verification_cases(partner_id,status,updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_pdvc_direction_status
  ON partner_direction_verification_cases(partner_direction_id,status,updated_at DESC);

ALTER TABLE partner_verification_documents
  ADD COLUMN IF NOT EXISTS business_id BIGINT,
  ADD COLUMN IF NOT EXISTS partner_direction_id BIGINT,
  ADD COLUMN IF NOT EXISTS is_current BOOLEAN NOT NULL DEFAULT TRUE,
  ADD COLUMN IF NOT EXISTS replaced_by BIGINT;

CREATE INDEX IF NOT EXISTS idx_pvd_direction
  ON partner_verification_documents(partner_direction_id,status,created_at DESC);

-- Existing application-bound document references are legacy only.
-- New service verification never writes partner_applications.document_id.
