-- Versioned database changes extracted from runtime startup.
-- This migration is NOT executed by application startup.
-- Apply it once through the controlled migration process before production.

-- platform_schema.py: partner-user trigger dependency
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

-- platform_schema.py: structural changes
ALTER TABLE partner_objects
        ADD COLUMN IF NOT EXISTS working_hours JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE ai_messages
        ADD COLUMN IF NOT EXISTS tool_call_id TEXT;
ALTER TABLE ai_messages
        DROP CONSTRAINT IF EXISTS ai_messages_sender_role_check;
ALTER TABLE ai_messages
        ADD CONSTRAINT ai_messages_sender_role_check
        CHECK (sender_role IN ('user','ai','admin','system','assistant','tool','client','partner'));
ALTER TABLE services ADD COLUMN IF NOT EXISTS commission_type TEXT;
ALTER TABLE services ADD COLUMN IF NOT EXISTS commission_value NUMERIC;
ALTER TABLE services ADD COLUMN IF NOT EXISTS object_id BIGINT;
ALTER TABLE services ADD COLUMN IF NOT EXISTS contact_phone TEXT;
ALTER TABLE partner_objects ADD COLUMN IF NOT EXISTS phone TEXT;
ALTER TABLE master_categories ADD COLUMN IF NOT EXISTS commission_type TEXT NOT NULL DEFAULT 'on_top';
ALTER TABLE master_categories ADD COLUMN IF NOT EXISTS commission_value NUMERIC NOT NULL DEFAULT 10;
ALTER TABLE services DROP CONSTRAINT IF EXISTS fk_services_object;
ALTER TABLE services ADD CONSTRAINT fk_services_object
        FOREIGN KEY (object_id) REFERENCES partner_objects(id) ON DELETE SET NULL;
ALTER TABLE ai_usage_ledger ADD COLUMN IF NOT EXISTS total_cost_amd NUMERIC(18,4) NOT NULL DEFAULT 0;
ALTER TABLE ai_usage_ledger ADD COLUMN IF NOT EXISTS exchange_rate_amd NUMERIC(18,6) NOT NULL DEFAULT 0;

-- platform_schema.py: data backfills / reference initialization
UPDATE partner_objects
       SET working_hours = COALESCE(data_json->'working_hours', '{}'::jsonb)
     WHERE working_hours = '{}'::jsonb
       AND COALESCE(data_json->'working_hours', '{}'::jsonb) <> '{}'::jsonb;
INSERT INTO category_settings(category_id)
    SELECT id FROM categories
    ON CONFLICT(category_id) DO NOTHING;

-- ---------------------------------------------------------------------
-- partner_business_application_api.py: business/application migration
-- Strict order: DDL -> entities -> base backfill -> application
-- reconciliation -> repair/cleanup -> final constraints/indexes.
-- ---------------------------------------------------------------------

-- 01. Business container.
CREATE TABLE IF NOT EXISTS partner_businesses(
    id BIGSERIAL PRIMARY KEY,
    partner_id BIGINT NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT,
    phone TEXT,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK(status IN ('active','pending','suspended','archived')),
    is_default BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 02. Structural expansion. partner_direction_id remains a plain BIGINT.
ALTER TABLE partner_directions
    ADD COLUMN IF NOT EXISTS business_id BIGINT
        REFERENCES partner_businesses(id) ON DELETE CASCADE;
ALTER TABLE services
    ADD COLUMN IF NOT EXISTS business_id BIGINT
        REFERENCES partner_businesses(id) ON DELETE CASCADE;
ALTER TABLE partner_verification_documents
    ADD COLUMN IF NOT EXISTS business_id BIGINT
        REFERENCES partner_businesses(id) ON DELETE CASCADE;
ALTER TABLE partner_verification_documents
    ADD COLUMN IF NOT EXISTS application_id BIGINT;
ALTER TABLE partner_verification_documents
    ADD COLUMN IF NOT EXISTS is_current BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE partner_verification_documents
    ADD COLUMN IF NOT EXISTS replaced_by BIGINT;
ALTER TABLE partner_objects
    ADD COLUMN IF NOT EXISTS business_id BIGINT
        REFERENCES partner_businesses(id) ON DELETE CASCADE;
ALTER TABLE partner_locations
    ADD COLUMN IF NOT EXISTS business_id BIGINT
        REFERENCES partner_businesses(id) ON DELETE CASCADE;
ALTER TABLE bookings
    ADD COLUMN IF NOT EXISTS business_id BIGINT
        REFERENCES partner_businesses(id) ON DELETE SET NULL;
ALTER TABLE service_direction_requests
    ADD COLUMN IF NOT EXISTS business_id BIGINT
        REFERENCES partner_businesses(id) ON DELETE CASCADE;

ALTER TABLE services DROP CONSTRAINT IF EXISTS services_status_check;
ALTER TABLE services ADD CONSTRAINT services_status_check
    CHECK (status IN ('draft','pending','approved','active','inactive','suspended','rejected','deleted'));

-- 03. Application table.
CREATE TABLE IF NOT EXISTS partner_applications(
    id BIGSERIAL PRIMARY KEY,
    partner_id BIGINT NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
    business_id BIGINT REFERENCES partner_businesses(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK(status IN ('draft','pending_partner','pending_admin','document_pending','document_under_review','approved','rejected','needs_clarification')),
    business_name TEXT,
    location_marz TEXT,
    location_city TEXT,
    location_village TEXT,
    address TEXT,
    phone TEXT,
    direction_name TEXT,
    master_category_id INT REFERENCES master_categories(id) ON DELETE SET NULL,
    subcategory_name TEXT,
    category_id INT REFERENCES categories(id) ON DELETE SET NULL,
    service_name TEXT,
    price NUMERIC,
    description TEXT,
    object_name TEXT,
    object_id BIGINT,
    document_id BIGINT REFERENCES partner_verification_documents(id) ON DELETE SET NULL,
    ai_reason TEXT,
    admin_note TEXT,
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    reviewed_by BIGINT,
    reviewed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 04. Entities: one default business for legacy partners without one.
INSERT INTO partner_businesses(partner_id,name,description,is_default)
SELECT p.id, COALESCE(NULLIF(p.business_name,''),'Իմ բիզնեսը'),
       p.business_description, TRUE
FROM partners p
WHERE p.status IN ('approved','suspended','blocked')
  AND NOT EXISTS (
      SELECT 1 FROM partner_businesses b
      WHERE b.partner_id=p.id
  );

UPDATE partner_businesses b SET is_default=TRUE, updated_at=NOW()
WHERE b.id IN (
    SELECT DISTINCT ON(partner_id) id
    FROM partner_businesses
    ORDER BY partner_id, is_default DESC, id
)
AND NOT EXISTS (
    SELECT 1 FROM partner_businesses x
    WHERE x.partner_id=b.partner_id
      AND x.is_default=TRUE
      AND x.id<>b.id
);

-- 05. Base backfill to the default company.
UPDATE partner_directions pd
SET business_id=b.id
FROM partner_businesses b
WHERE b.partner_id=pd.partner_id
  AND b.is_default=TRUE
  AND pd.business_id IS NULL;

UPDATE services s
SET business_id=b.id
FROM partner_businesses b
WHERE b.partner_id=s.partner_id
  AND b.is_default=TRUE
  AND s.business_id IS NULL;

UPDATE partner_verification_documents d
SET business_id=b.id
FROM partner_businesses b
WHERE b.partner_id=d.partner_id
  AND b.is_default=TRUE
  AND d.business_id IS NULL;

UPDATE partner_objects o
SET business_id=b.id
FROM partner_businesses b
WHERE b.partner_id=o.partner_id
  AND b.is_default=TRUE
  AND o.business_id IS NULL;

UPDATE partner_locations l
SET business_id=b.id
FROM partner_businesses b
WHERE b.partner_id=l.partner_id
  AND b.is_default=TRUE
  AND l.business_id IS NULL;

UPDATE service_direction_requests r
SET business_id=b.id
FROM partner_businesses b
WHERE b.partner_id=r.partner_id
  AND b.is_default=TRUE
  AND r.business_id IS NULL;

-- 06. Reconcile approved applications with their company.
UPDATE partner_directions pd
SET business_id=a.business_id
FROM partner_applications a
WHERE a.status='approved'
  AND a.business_id IS NOT NULL
  AND pd.partner_id=a.partner_id
  AND pd.master_category_id=a.master_category_id
  AND pd.business_id IS DISTINCT FROM a.business_id;

UPDATE partner_verification_documents d
SET business_id=a.business_id
FROM partner_applications a
WHERE a.status='approved'
  AND a.business_id IS NOT NULL
  AND d.partner_id=a.partner_id
  AND (
      d.id=a.document_id
      OR d.partner_direction_id IN (
          SELECT pd.id
          FROM partner_directions pd
          WHERE pd.partner_id=a.partner_id
            AND pd.master_category_id=a.master_category_id
            AND pd.business_id=a.business_id
      )
  )
  AND d.business_id IS DISTINCT FROM a.business_id;

-- 07. Reconcile directions from real active/approved services.
UPDATE partner_directions pd
SET business_id=s.business_id,
    status=CASE WHEN pd.status='deleted' THEN 'approved' ELSE pd.status END,
    updated_at=NOW()
FROM (
    SELECT DISTINCT ON (partner_id,master_category_id)
           partner_id,business_id,c.master_category_id
    FROM services s
    JOIN categories c ON c.id=s.category_id
    WHERE s.business_id IS NOT NULL
      AND s.status IN ('active','approved')
      AND c.master_category_id IS NOT NULL
    ORDER BY partner_id,master_category_id,s.id DESC
) s
WHERE pd.partner_id=s.partner_id
  AND pd.master_category_id=s.master_category_id
  AND pd.business_id IS DISTINCT FROM s.business_id;

INSERT INTO partner_directions(partner_id,business_id,master_category_id,status)
SELECT DISTINCT s.partner_id,s.business_id,c.master_category_id,'approved'
FROM services s
JOIN categories c ON c.id=s.category_id
JOIN partner_businesses b
  ON b.id=s.business_id AND b.partner_id=s.partner_id
WHERE s.business_id IS NOT NULL
  AND s.status IN ('active','approved')
  AND c.master_category_id IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM partner_directions pd
      WHERE pd.partner_id=s.partner_id
        AND pd.business_id=s.business_id
        AND pd.master_category_id=c.master_category_id
  );

INSERT INTO partner_direction_categories(partner_direction_id,category_id)
SELECT pd.id,s.category_id
FROM services s
JOIN partner_directions pd
  ON pd.partner_id=s.partner_id
 AND pd.business_id=s.business_id
JOIN categories c
  ON c.id=s.category_id
 AND c.master_category_id=pd.master_category_id
WHERE s.business_id IS NOT NULL
  AND s.status IN ('active','approved')
ON CONFLICT(partner_direction_id,category_id) DO NOTHING;

-- 08. Document-to-direction reconciliation: DML only, no FK.
UPDATE partner_verification_documents d
SET partner_direction_id=pd.id
FROM partner_directions pd
WHERE d.business_id=pd.business_id
  AND d.partner_id=pd.partner_id
  AND d.partner_direction_id IS NULL
  AND pd.status IN ('approved','pending')
  AND EXISTS (
      SELECT 1
      FROM partner_direction_categories pdc
      WHERE pdc.partner_direction_id=pd.id
  );

-- 09. Rebuild legacy approved objects.
INSERT INTO partner_objects(partner_id,business_id,object_name,address,city,marz,data_json)
SELECT a.partner_id,a.business_id,
       COALESCE(NULLIF(a.object_name,''),NULLIF(a.business_name,''),b.name),
       NULLIF(a.address,''),NULLIF(a.location_city,''),NULLIF(a.location_marz,''),
       jsonb_build_object('source','approved_partner_application','application_id',a.id)
FROM partner_applications a
JOIN partner_businesses b ON b.id=a.business_id
WHERE a.status='approved'
  AND a.business_id IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM partner_objects o
      WHERE o.partner_id=a.partner_id
        AND o.business_id=a.business_id
  )
  AND a.id=(
      SELECT aa.id
      FROM partner_applications aa
      WHERE aa.partner_id=a.partner_id
        AND aa.business_id=a.business_id
        AND aa.status='approved'
      ORDER BY aa.created_at DESC,aa.id DESC
      LIMIT 1
  );

-- 10. Company description/phone reconciliation.
UPDATE partner_businesses b
SET description=COALESCE(
        substring(a.description from 'Մենք զբաղվում ենք ([^։]+)'),
        substring(a.description from 'Мы занимаемся ([^.]+)'),
        substring(a.description from 'We provide ([^.]+)'),
        b.description
    ),
    phone=COALESCE(
        NULLIF(a.phone,''),
        NULLIF(a.payload_json->>'phone',''),
        NULLIF(substring(a.description from '(?:Հեռախոս|Телефон|Phone)[[:space:]]*[:\-]?[[:space:]]*([+0-9][0-9 ()-]{7,})'),''),
        b.phone
    ),
    updated_at=NOW()
FROM partner_applications a
WHERE a.id=(
    SELECT aa.id
    FROM partner_applications aa
    WHERE aa.business_id=b.id AND aa.status='approved'
    ORDER BY aa.created_at DESC,aa.id DESC
    LIMIT 1
)
AND a.description IS NOT NULL
AND trim(a.description)<>'';

UPDATE partner_businesses
SET description=trim(substring(description from 'Мы занимаемся ([^.]+)'))
WHERE description ~ 'Мы занимаемся [^.]+';

UPDATE partner_businesses
SET description=trim(substring(description from 'We provide ([^.]+)'))
WHERE description ~ 'We provide [^.]+';

UPDATE partner_businesses
SET description=trim(substring(description from '^(.+?)։[[:space:]]*Հիմնական ծառայություններն'))
WHERE description ~ '։[[:space:]]*Հիմնական ծառայություններն';

UPDATE services s
SET description='', updated_at=NOW()
FROM partner_businesses b
JOIN partners p ON p.id=b.partner_id
WHERE s.business_id=b.id
  AND b.is_default=TRUE
  AND s.description IS NOT NULL
  AND p.business_description IS NOT NULL
  AND trim(s.description)=trim(p.business_description);

-- 11. Remove stale service proposals from archived companies.
DELETE FROM partner_applications
WHERE status NOT IN ('approved','rejected')
  AND COALESCE(payload_json->>'source','')='partner_service'
  AND business_id IN (
      SELECT id FROM partner_businesses WHERE status='archived'
  );

-- 12. Repair duplicate directions before final uniqueness.
UPDATE partner_directions pd
SET status='deleted', updated_at=NOW()
WHERE pd.business_id IS NOT NULL
  AND pd.master_category_id IS NOT NULL
  AND pd.status <> 'deleted'
  AND EXISTS (
      SELECT 1
      FROM partner_directions newer
      WHERE newer.business_id=pd.business_id
        AND newer.master_category_id=pd.master_category_id
        AND newer.status <> 'deleted'
        AND newer.id>pd.id
  );

-- 13. Final constraints and indexes, only after all repairs.
ALTER TABLE partner_directions
    DROP CONSTRAINT IF EXISTS partner_directions_partner_id_master_category_id_key;

CREATE UNIQUE INDEX IF NOT EXISTS uq_partner_direction_business_master
    ON partner_directions(business_id,master_category_id)
    WHERE status <> 'deleted';

CREATE INDEX IF NOT EXISTS idx_partner_businesses_partner
    ON partner_businesses(partner_id,status);
CREATE INDEX IF NOT EXISTS idx_partner_documents_application
    ON partner_verification_documents(application_id,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_partner_documents_business
    ON partner_verification_documents(business_id,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_partner_directions_business
    ON partner_directions(business_id,status);
CREATE INDEX IF NOT EXISTS idx_services_business
    ON services(business_id,status);
CREATE INDEX IF NOT EXISTS idx_partner_applications_admin
    ON partner_applications(status,created_at DESC);
CREATE INDEX IF NOT EXISTS idx_partner_applications_partner
    ON partner_applications(partner_id,status,created_at DESC);

-- partner_directions_api.py: structural changes
ALTER TABLE master_categories ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE master_categories ADD COLUMN IF NOT EXISTS name_en TEXT;
ALTER TABLE categories ADD COLUMN IF NOT EXISTS name_en TEXT;
-- partner_verification_documents.partner_direction_id is intentionally kept
-- as a plain BIGINT. No FK is introduced here: historical/orphan references
-- are reconciled by DML only.
ALTER TABLE service_direction_requests ADD COLUMN IF NOT EXISTS proposed_subcategory_name TEXT;
ALTER TABLE service_direction_requests ADD COLUMN IF NOT EXISTS partner_direction_id BIGINT REFERENCES partner_directions(id) ON DELETE SET NULL;
ALTER TABLE service_direction_requests ADD COLUMN IF NOT EXISTS document_id BIGINT REFERENCES partner_verification_documents(id) ON DELETE SET NULL;
ALTER TABLE service_direction_requests ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

-- partner_directions_api.py: legacy direction backfills
INSERT INTO partner_directions(partner_id, master_category_id, status)
    SELECT DISTINCT p.id, c.master_category_id,
           CASE WHEN p.status='approved' AND p.verification_status='approved'
                THEN 'approved' ELSE 'pending' END
    FROM partners p
    JOIN master_skills ms ON ms.user_id=p.user_id AND ms.is_active=TRUE
    JOIN categories c ON c.id=ms.category_id
    WHERE c.master_category_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1
          FROM partner_directions pd
          WHERE pd.partner_id=p.id
            AND pd.master_category_id=c.master_category_id
      );

INSERT INTO partner_direction_categories(partner_direction_id, category_id)
    SELECT pd.id, ms.category_id
    FROM partner_directions pd
    JOIN partners p ON p.id=pd.partner_id
    JOIN master_skills ms ON ms.user_id=p.user_id AND ms.is_active=TRUE
    JOIN categories c ON c.id=ms.category_id AND c.master_category_id=pd.master_category_id
    ON CONFLICT(partner_direction_id, category_id) DO NOTHING
    
UPDATE partner_verification_documents d
       SET partner_direction_id = x.direction_id
      FROM (
        SELECT d2.id AS doc_id, MIN(pd.id) AS direction_id
        FROM partner_verification_documents d2
        JOIN partner_directions pd ON pd.partner_id=d2.partner_id
        WHERE d2.partner_direction_id IS NULL
        GROUP BY d2.id
      ) x
     WHERE d.id=x.doc_id AND d.partner_direction_id IS NULL
    ;

-- partner_lifecycle_schema.py: partner status synchronization trigger
CREATE OR REPLACE FUNCTION sync_partner_after_direction_change()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.status = 'approved' THEN
        UPDATE partners
           SET status='approved', verification_status='approved', rejection_reason=NULL, updated_at=NOW()
         WHERE id=NEW.partner_id;
    ELSIF NEW.status = 'rejected' THEN
        UPDATE partners
           SET verification_status=CASE
                WHEN EXISTS (SELECT 1 FROM partner_directions WHERE partner_id=NEW.partner_id AND status='approved') THEN 'approved'
                ELSE 'rejected' END,
               status=CASE
                WHEN EXISTS (SELECT 1 FROM partner_directions WHERE partner_id=NEW.partner_id AND status='approved') THEN 'approved'
                ELSE 'pending' END,
               rejection_reason=CASE WHEN EXISTS (SELECT 1 FROM partner_directions WHERE partner_id=NEW.partner_id AND status='approved') THEN NULL ELSE NEW.rejection_reason END,
               updated_at=NOW()
         WHERE id=NEW.partner_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql SET search_path = public, pg_temp;

DROP TRIGGER IF EXISTS trg_sync_partner_after_direction_change ON partner_directions;
CREATE TRIGGER trg_sync_partner_after_direction_change
AFTER INSERT OR UPDATE OF status ON partner_directions
FOR EACH ROW EXECUTE FUNCTION sync_partner_after_direction_change();
