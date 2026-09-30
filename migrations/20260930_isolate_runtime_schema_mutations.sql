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

-- partner_directions_api.py: structural changes
ALTER TABLE master_categories ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE master_categories ADD COLUMN IF NOT EXISTS name_en TEXT;
ALTER TABLE categories ADD COLUMN IF NOT EXISTS name_en TEXT;
ALTER TABLE partner_verification_documents
        ADD COLUMN IF NOT EXISTS partner_direction_id BIGINT REFERENCES partner_directions(id) ON DELETE SET NULL;
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
      AND NOT EXISTS (H
    ;

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
