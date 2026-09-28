-- Semantic catalogue matching.
-- Requires Supabase/PostgreSQL pgvector extension.
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA extensions;

ALTER TABLE categories
    ADD COLUMN IF NOT EXISTS embedding extensions.vector(1536);

ALTER TABLE categories
    ADD COLUMN IF NOT EXISTS embedding_model text;

ALTER TABLE categories
    ADD COLUMN IF NOT EXISTS embedding_source text;

ALTER TABLE categories
    ADD COLUMN IF NOT EXISTS embedding_updated_at timestamptz;

CREATE INDEX IF NOT EXISTS categories_embedding_hnsw_idx
    ON categories
    USING hnsw (embedding extensions.vector_cosine_ops)
    WHERE embedding IS NOT NULL;
