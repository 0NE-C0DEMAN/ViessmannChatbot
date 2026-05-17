-- ============================================================================
-- One-shot RESET — run this in Supabase SQL editor after a schema/dimension
-- mismatch (e.g. someone re-embedded with a different model and broke the
-- vector column dim).
--
-- HOW TO APPLY:
--   1. Open https://supabase.com/dashboard/project/<your-project>/sql
--   2. Paste this file. Click "Run".
--   3. If asked about RLS, choose "Run without RLS".
--   4. Re-run ingest:  python ingest.py --drive
--
-- WHAT THIS DOES:
--   • Drops the chunks table + the search RPC (clean slate for vector dim).
--   • Recreates `document_chunks_v2` with embedding vector(1536) — matches
--     text-embedding-3-small, our production embedding model.
--   • Recreates indexes (HNSW vector, GIN fulltext, GIN trigram, btree).
--   • Recreates the `search_chunks_v2(...)` RPC.
--   • Truncates `document_registry_v2` so ingest doesn't md5-dedupe and skip
--     everything (we want every PDF re-embedded fresh).
--
-- WHAT THIS LEAVES ALONE:
--   • `document_chunks` / `document_registry` — the unprefixed tables from
--     an unrelated experiment. Not referenced by the app. Drop them in the
--     UI later if you want a tidy schema; they don't cost much.
-- ============================================================================

-- 1. Drop the function FIRST (it references the table).
DROP FUNCTION IF EXISTS search_chunks_v2(vector(1536), text, int, text, text, float);
DROP FUNCTION IF EXISTS search_chunks_v2(vector(768),  text, int, text, text, float);
DROP FUNCTION IF EXISTS search_chunks_v2;

-- 2. Drop the chunks table (whatever shape it's in now).
DROP TABLE IF EXISTS document_chunks_v2 CASCADE;

-- 3. Clear out the registry so md5-dedupe doesn't skip every PDF.
--    (Keep the table; just wipe the rows. The migration creates it; if it
--    doesn't exist for some reason this DELETE is a no-op safe.)
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'document_registry_v2') THEN
        EXECUTE 'DELETE FROM document_registry_v2';
    END IF;
END $$;

-- 4. Ensure extensions are present.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- 5. Re-create the registry table if it was also messed with.
CREATE TABLE IF NOT EXISTS document_registry_v2 (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    file_id           text UNIQUE NOT NULL,
    file_name         text NOT NULL,
    product_line      text,
    document_type     text,
    md5_checksum     text,
    page_count        int,
    status            text DEFAULT 'active',
    created_at        timestamptz DEFAULT now(),
    last_processed_at timestamptz DEFAULT now()
);

-- 6. Recreate the chunks table with the CORRECT 1536-dim vector column.
CREATE TABLE document_chunks_v2 (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    file_id          text NOT NULL,
    file_name        text NOT NULL,
    product_line     text,
    document_type    text,
    page_number      int  NOT NULL,
    section_heading  text,
    chunk_text       text NOT NULL,
    has_table        boolean DEFAULT false,
    token_estimate   int,
    embedding        vector(1536) NOT NULL,
    fts              tsvector GENERATED ALWAYS AS
                       (to_tsvector('simple', coalesce(chunk_text, ''))) STORED,
    created_at       timestamptz DEFAULT now()
);

CREATE INDEX idx_chunks_v2_file        ON document_chunks_v2 (file_id);
CREATE INDEX idx_chunks_v2_doctype     ON document_chunks_v2 (document_type);
CREATE INDEX idx_chunks_v2_productline ON document_chunks_v2 (product_line);
CREATE INDEX idx_chunks_v2_fts         ON document_chunks_v2 USING gin (fts);
CREATE INDEX idx_chunks_v2_text_trgm   ON document_chunks_v2 USING gin (chunk_text gin_trgm_ops);
CREATE INDEX idx_chunks_v2_embedding   ON document_chunks_v2 USING hnsw (embedding vector_cosine_ops);

-- 7. Recreate the hybrid-search RPC (latest version with doctype boost).
CREATE FUNCTION search_chunks_v2(
    q_embedding      vector(1536),
    q_text           text,
    n                int     DEFAULT 30,
    f_product_line   text    DEFAULT NULL,
    f_document_type  text    DEFAULT NULL,
    w_sem            float   DEFAULT 0.7
)
RETURNS TABLE (
    chunk_id         uuid,
    file_id          text,
    file_name        text,
    product_line     text,
    document_type    text,
    page_number      int,
    section_heading  text,
    chunk_text       text,
    has_table        boolean,
    semantic_score   float,
    keyword_score    float,
    hybrid_score     float
)
LANGUAGE sql STABLE
AS $func$
    WITH semantic AS (
        SELECT c.id AS cid, (1 - (c.embedding <=> q_embedding))::float AS s
        FROM   document_chunks_v2 c
        WHERE  (f_product_line  IS NULL OR c.product_line  = f_product_line)
          AND  (f_document_type IS NULL OR c.document_type = f_document_type)
        ORDER  BY c.embedding <=> q_embedding
        LIMIT  GREATEST(n * 3, 60)
    ),
    keyword AS (
        SELECT c.id AS cid,
               ts_rank_cd(c.fts, plainto_tsquery('simple', q_text))::float AS k
        FROM   document_chunks_v2 c
        WHERE  (f_product_line  IS NULL OR c.product_line  = f_product_line)
          AND  (f_document_type IS NULL OR c.document_type = f_document_type)
          AND  c.fts @@ plainto_tsquery('simple', q_text)
        ORDER  BY k DESC
        LIMIT  GREATEST(n * 3, 60)
    ),
    trigram AS (
        SELECT c.id AS cid,
               similarity(c.chunk_text, q_text)::float AS t
        FROM   document_chunks_v2 c
        WHERE  (f_product_line  IS NULL OR c.product_line  = f_product_line)
          AND  (f_document_type IS NULL OR c.document_type = f_document_type)
          AND  c.chunk_text % q_text
        ORDER  BY t DESC
        LIMIT  20
    )
    SELECT c.id, c.file_id, c.file_name, c.product_line, c.document_type,
           c.page_number, c.section_heading, c.chunk_text, c.has_table,
           COALESCE(s.s, 0)::float,
           COALESCE(k.k, 0)::float,
           -- Doctype boost: favor `informacijski_list` (canonical datasheet)
           -- by +0.05 over procedural manuals when relevance is similar.
           (w_sem * COALESCE(s.s, 0)
            + (1 - w_sem) * LEAST(COALESCE(k.k, 0), 1.0)
            + 0.1 * COALESCE(t.t, 0)
            + CASE WHEN c.document_type = 'informacijski_list' THEN 0.05 ELSE 0.0 END
           )::float
    FROM   document_chunks_v2 c
    LEFT JOIN semantic s ON s.cid = c.id
    LEFT JOIN keyword  k ON k.cid = c.id
    LEFT JOIN trigram  t ON t.cid = c.id
    WHERE  s.cid IS NOT NULL OR k.cid IS NOT NULL OR t.cid IS NOT NULL
    ORDER  BY 12 DESC
    LIMIT  n;
$func$;

-- 8. Sanity check — these should all return 0/empty.
--    (Click "Run" again after; if you see numbers, paste the row counts to me.)
SELECT 'chunks_after_reset' AS marker, COUNT(*) FROM document_chunks_v2;
SELECT 'registry_after_reset' AS marker, COUNT(*) FROM document_registry_v2;
