-- ============================================================================
-- Migration 002 — Document-type aware hybrid score
--
-- Why: when the corpus contains both a canonical specification document
-- (`informacijski_list`) and many redundant procedural docs (installation,
-- service, user manuals) that all mention the same model codes, search
-- frequently surfaces a procedural chunk that *mentions* the topic over the
-- canonical chunk that has the *value*. This biases the hybrid_score so the
-- canonical document wins when relevance is otherwise close.
--
-- Multipliers (tuned for the Vitocal corpus, easy to adjust):
--   informacijski_list       1.5   — canonical product datasheet
--   upute_za_projektiranje   1.2   — planning/engineering guides
--   upute_za_montazu         1.0   — installation procedures
--   upute_za_servis          1.0   — service procedures
--   upute_za_upotrebu        0.9   — end-user manuals (least technical)
--   (anything else)          1.0
--
-- Applied as a multiplier on the final hybrid_score, so document selection
-- still respects relevance — a clearly-better procedural chunk can still
-- beat a marginally-relevant canonical chunk.
-- ============================================================================

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
           (CASE c.document_type
                WHEN 'informacijski_list'     THEN 1.5
                WHEN 'upute_za_projektiranje' THEN 1.2
                WHEN 'upute_za_upotrebu'      THEN 0.9
                ELSE                               1.0
            END *
            ( w_sem * COALESCE(s.s, 0)
            + (1 - w_sem) * LEAST(COALESCE(k.k, 0), 1.0)
            + 0.1 * COALESCE(t.t, 0)
            )
           )::float AS hybrid_score
    FROM   document_chunks_v2 c
    LEFT JOIN semantic s ON s.cid = c.id
    LEFT JOIN keyword  k ON k.cid = c.id
    LEFT JOIN trigram  t ON t.cid = c.id
    WHERE  s.cid IS NOT NULL OR k.cid IS NOT NULL OR t.cid IS NOT NULL
    ORDER  BY 12 DESC
    LIMIT  n;
$func$;
