-- Runtime schema mutations extracted from application startup.
-- This file is intentionally NOT imported or executed by application startup.
-- Apply through the controlled migration process before enabling the refactored runtime.

-- platform_schema.py

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


    -- Canonical object-level weekly schedule. Kept separate from data_json so
    -- partner edits cannot be overwritten by legacy registration metadata.
    ALTER TABLE partner_objects
        ADD COLUMN IF NOT EXISTS working_hours JSONB NOT NULL DEFAULT '{}'::jsonb;


    UPDATE partner_objects
       SET working_hours = COALESCE(data_json->'working_hours', '{}'::jsonb)
     WHERE working_hours = '{}'::jsonb
       AND COALESCE(data_json->'working_hours', '{}'::jsonb) <> '{}'::jsonb;

-- partner_directions_api.py

    ALTER TABLE master_categories ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;

    ALTER TABLE master_categories ADD COLUMN IF NOT EXISTS name_en TEXT;

    ALTER TABLE categories ADD COLUMN IF NOT EXISTS name_en TEXT;

    ALTER TABLE partner_verification_documents
        ADD COLUMN IF NOT EXISTS partner_direction_id BIGINT REFERENCES partner_directions(id) ON DELETE SET NULL;

    ALTER TABLE service_direction_requests ADD COLUMN IF NOT EXISTS proposed_subcategory_name TEXT;

    ALTER TABLE service_direction_requests ADD COLUMN IF NOT EXISTS partner_direction_id BIGINT REFERENCES partner_directions(id) ON DELETE SET NULL;

    ALTER TABLE service_direction_requests ADD COLUMN IF NOT EXISTS document_id BIGINT REFERENCES partner_verification_documents(id) ON DELETE SET NULL;

    ALTER TABLE service_direction_requests ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
INSERT INTO partner_directions(partner_id, master_category_id, status)
    SELECT DISTINCT p.id, c.master_category_id,
           CASE WHEN p.status='approved' AND p.verification_status='approved'
                THEN 'approved' ELSE 'pending' END
    FROM partners p
    JOIN master_skills ms ON ms.user_id=p.user_id AND ms.is_active=TRUE
    JOIN categories c ON c.id=ms.category_id
    WHERE c.master_category_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM partner_directions pd
          WHERE pd.partner_id=p.id
            AND pd.master_category_id=c.master_category_id
      )
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

-- partner_lifecycle_schema.py
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
