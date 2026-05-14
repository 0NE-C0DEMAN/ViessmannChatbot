-- ============================================================================
-- Viessmann RAG Chatbot — Supabase schema migration
--
-- HOW TO APPLY:
--   1. Open the SQL editor in your Supabase project:
--      https://supabase.com/dashboard/project/<your-project>/sql
--   2. Paste the contents of this file and click "Run".
--   3. If Supabase warns about RLS, choose "Run without RLS" — this app
--      only ever talks to the DB with the service-role key (server-side),
--      so RLS policies aren't needed.
--
-- WHAT THIS CREATES:
--   • Extensions: vector (pgvector), pg_trgm
--   • Tables: document_registry_v2, document_chunks_v2
--   • Indexes: HNSW (vector), GIN (full-text + trigram), btree filters
--   • Function: search_chunks_v2(...)   — hybrid search RPC
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;


-- ─── Registry (one row per source PDF) ───────────────────────────────────
CREATE TABLE IF NOT EXISTS document_registry_v2 (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    file_id           text UNIQUE NOT NULL,   -- Drive file_id OR local filename stem
    file_name         text NOT NULL,
    product_line      text,                   -- e.g. "Vitocal 100-S informacijski list"
    document_type     text,                   -- e.g. "informacijski_list", "upute_za_montazu"
    md5_checksum     text,
    page_count        int,
    status            text DEFAULT 'active',  -- 'active' or 'deleted'
    created_at        timestamptz DEFAULT now(),
    last_processed_at timestamptz DEFAULT now()
);


-- ─── Chunks (one row per page) ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS document_chunks_v2 (
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

CREATE INDEX IF NOT EXISTS idx_chunks_v2_file        ON document_chunks_v2 (file_id);
CREATE INDEX IF NOT EXISTS idx_chunks_v2_doctype     ON document_chunks_v2 (document_type);
CREATE INDEX IF NOT EXISTS idx_chunks_v2_productline ON document_chunks_v2 (product_line);
CREATE INDEX IF NOT EXISTS idx_chunks_v2_fts         ON document_chunks_v2 USING gin (fts);
CREATE INDEX IF NOT EXISTS idx_chunks_v2_text_trgm   ON document_chunks_v2 USING gin (chunk_text gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_chunks_v2_embedding   ON document_chunks_v2 USING hnsw (embedding vector_cosine_ops);


-- ─── Hybrid search RPC ───────────────────────────────────────────────────
-- Combines cosine similarity (vector) + full-text rank (BM25-like) +
-- a trigram fallback (helps with model codes like "101.A14" and part
-- numbers that may not tokenize cleanly).
--
-- Parameters are prefixed `q_` / `f_` / `w_` / `n` to avoid the plpgsql
-- variable-name shadow with RETURNS TABLE columns.
-- Returned `chunk_id` is the row id (the OUT-param name `id` is reserved).
DROP FUNCTION IF EXISTS search_chunks_v2(vector(1536), text, int, text, text, float);

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
           (w_sem * COALESCE(s.s, 0)
            + (1 - w_sem) * LEAST(COALESCE(k.k, 0), 1.0)
            + 0.1 * COALESCE(t.t, 0))::float
    FROM   document_chunks_v2 c
    LEFT JOIN semantic s ON s.cid = c.id
    LEFT JOIN keyword  k ON k.cid = c.id
    LEFT JOIN trigram  t ON t.cid = c.id
    WHERE  s.cid IS NOT NULL OR k.cid IS NOT NULL OR t.cid IS NOT NULL
    ORDER  BY 12 DESC
    LIMIT  n;
$func$;
