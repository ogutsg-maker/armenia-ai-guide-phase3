"""Small lifecycle rules for the current partner architecture."""
from __future__ import annotations

from database import _connect


def ensure_partner_lifecycle_schema() -> None:
    # partner_directions is owned by the directions module. Ensure it exists
    # before installing the lifecycle trigger; this keeps startup order safe.
    from partner_directions_api import ensure_partner_direction_schema
    ensure_partner_direction_schema()

    sql = r'''
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
    '''
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
