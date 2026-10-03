-- Direction-specific partner document and review lifecycle
ALTER TABLE aig_services DROP CONSTRAINT IF EXISTS aig_services_status_check;
ALTER TABLE aig_services ADD CONSTRAINT aig_services_status_check
  CHECK(status IN ('PENDING_ADMIN','CLASSIFICATION_PENDING','DOCUMENT_PENDING','NEEDS_CORRECTION','ACTIVE','REJECTED','ARCHIVED'));

ALTER TABLE aig_services ADD COLUMN IF NOT EXISTS description TEXT;
ALTER TABLE aig_services ADD COLUMN IF NOT EXISTS direction_category_id BIGINT REFERENCES aig_catalog_categories(id);
ALTER TABLE aig_services ADD COLUMN IF NOT EXISTS marzes TEXT[];
ALTER TABLE aig_services ADD COLUMN IF NOT EXISTS cities TEXT[];
ALTER TABLE aig_services ADD COLUMN IF NOT EXISTS districts TEXT[];

ALTER TABLE aig_service_applications DROP CONSTRAINT IF EXISTS aig_service_applications_status_check;
ALTER TABLE aig_service_applications ADD CONSTRAINT aig_service_applications_status_check
  CHECK(status IN ('PENDING_ADMIN','CLASSIFICATION_PENDING','DOCUMENT_PENDING','NEEDS_CORRECTION','APPROVED','REJECTED','DELETED'));

CREATE TABLE IF NOT EXISTS aig_direction_documents(
  id BIGSERIAL PRIMARY KEY,
  company_id BIGINT NOT NULL REFERENCES aig_companies(id) ON DELETE CASCADE,
  catalog_category_id BIGINT NOT NULL REFERENCES aig_catalog_categories(id),
  file_name TEXT NOT NULL,
  mime_type TEXT NOT NULL,
  size_bytes BIGINT NOT NULL CHECK(size_bytes > 0 AND size_bytes <= 10485760),
  content BYTEA NOT NULL,
  status TEXT NOT NULL DEFAULT 'PENDING_ADMIN' CHECK(status IN ('PENDING_ADMIN','ACTIVE','REJECTED','ARCHIVED')),
  uploaded_by BIGINT REFERENCES aig_users(telegram_id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  reviewed_at TIMESTAMPTZ,
  reviewer BIGINT REFERENCES aig_users(telegram_id),
  reason TEXT
);

CREATE INDEX IF NOT EXISTS aig_direction_documents_lookup
  ON aig_direction_documents(company_id,catalog_category_id,status);

ALTER TABLE aig_service_documents ADD COLUMN IF NOT EXISTS company_id BIGINT REFERENCES aig_companies(id);
ALTER TABLE aig_service_documents ADD COLUMN IF NOT EXISTS catalog_category_id BIGINT REFERENCES aig_catalog_categories(id);

CREATE INDEX IF NOT EXISTS aig_service_applications_status_idx ON aig_service_applications(status);
