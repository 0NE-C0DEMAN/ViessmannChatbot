-- ============================================================================
-- Dual-embedding schema — supports both OpenAI (default) and Gemini.
--
-- HOW IT WORKS
--   • `embedding`      vector(1536) — produced by text-embedding-3-small (OpenAI)
--   • `embedding_gem`  vector(1536) — produced by gemini-embedding-001 with
--                                      output_dimensionality=1536 (kept the
--                                      same dim so both columns are
--                                      column-storage compatible).
--   • Ingest fills BOTH when running with INGEST_DUAL=true; chat retrieval
--     picks the column matching LLM_PROVIDER.
--   • Adding a column with NULL default is non-destructive — existing rows
--     stay intact.
--
-- HOW TO APPLY
--   1. Open https://supabase.com/dashboard/project/<your-project>/sql
--   2. Paste this file and click "Run" → "Run without RLS" (service-role
--      key bypasses RLS anyway).
--   3. After running, ingest with INGEST_DUAL=true to populate the new
--      column for every existing chunk.
-- ============================================================================

-- 1. Add the new vector column (nullable — old rows will have NULL here
--    until the next dual-mode ingest fills them in).
ALTER TABLE document_chunks_v2
    ADD COLUMN IF NOT EXISTS embedding_gem vector(1536);

-- 2. HNSW index for the gem column. Same cosine-distance opclass as the
--    existing column.
CREATE INDEX IF NOT EXISTS idx_chunks_v2_embedding_gem
    ON document_chunks_v2
    USING hnsw (embedding_gem vector_cosine_ops);

-- 3. Hybrid-search RPC that targets the gem column. Mirrors search_chunks_v2
--    one-to-one except:
--      • Uses `c.embedding_gem` instead of `c.embedding` for the semantic CTE
--      • Filters out rows where embedding_gem IS NULL so half-populated
--        states (gem ingest in progress) don't surface zero-similarity hits.
DROP FUNCTION IF EXISTS search_chunks_v2_gem(vector(1536), text, int, text, text, float);

CREATE FUNCTION search_chunks_v2_gem(
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
        SELECT c.id AS cid, (1 - (c.embedding_gem <=> q_embedding))::float AS s
        FROM   document_chunks_v2 c
        WHERE  c.embedding_gem IS NOT NULL
          AND  (f_product_line  IS NULL OR c.product_line  = f_product_line)
          AND  (f_document_type IS NULL OR c.document_type = f_document_type)
        ORDER  BY c.embedding_gem <=> q_embedding
        LIMIT  GREATEST(n * 3, 60)
    ),
    keyword AS (
        SELECT c.id AS cid,
               ts_rank_cd(c.fts, plainto_tsquery('simple', q_text))::float AS k
        FROM   document_chunks_v2 c
        WHERE  c.embedding_gem IS NOT NULL
          AND  (f_product_line  IS NULL OR c.product_line  = f_product_line)
          AND  (f_document_type IS NULL OR c.document_type = f_document_type)
          AND  c.fts @@ plainto_tsquery('simple', q_text)
        ORDER  BY k DESC
        LIMIT  GREATEST(n * 3, 60)
    ),
    trigram AS (
        SELECT c.id AS cid,
               similarity(c.chunk_text, q_text)::float AS t
        FROM   document_chunks_v2 c
        WHERE  c.embedding_gem IS NOT NULL
          AND  (f_product_line  IS NULL OR c.product_line  = f_product_line)
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

-- 4. Sanity check.
SELECT
    'chunks_total'        AS marker, COUNT(*) FROM document_chunks_v2
UNION ALL SELECT
    'chunks_with_oai_emb',           COUNT(*) FROM document_chunks_v2 WHERE embedding IS NOT NULL
UNION ALL SELECT
    'chunks_with_gem_emb',           COUNT(*) FROM document_chunks_v2 WHERE embedding_gem IS NOT NULL;
