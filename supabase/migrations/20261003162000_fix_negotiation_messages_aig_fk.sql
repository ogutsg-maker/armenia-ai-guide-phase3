-- negotiation_messages belongs to the current AI negotiation lifecycle.
-- The previous FK pointed to the legacy negotiations table while the API uses aig_negotiations.
ALTER TABLE public.negotiation_messages
  DROP CONSTRAINT IF EXISTS negotiation_messages_negotiation_id_fkey;

ALTER TABLE public.negotiation_messages
  ADD CONSTRAINT negotiation_messages_negotiation_id_fkey
  FOREIGN KEY (negotiation_id)
  REFERENCES public.aig_negotiations(id)
  ON DELETE CASCADE;
