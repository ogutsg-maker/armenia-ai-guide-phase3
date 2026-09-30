-- Phase 3: freeze the ai_messages runtime contract.
-- Apply once to existing installations before removing runtime DDL.
ALTER TABLE ai_messages
    ADD COLUMN IF NOT EXISTS tool_call_id TEXT;

ALTER TABLE ai_messages
    DROP CONSTRAINT IF EXISTS ai_messages_sender_role_check;

ALTER TABLE ai_messages
    ADD CONSTRAINT ai_messages_sender_role_check
    CHECK (sender_role IN ('user','ai','admin','system','assistant','tool','client','partner'));
