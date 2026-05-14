# Viessmann RAG Chatbot

[![Release](https://img.shields.io/github/v/release/0NE-C0DEMAN/ViessmannChatbot?display_name=tag&sort=semver)](https://github.com/0NE-C0DEMAN/ViessmannChatbot/releases)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

A retrieval-augmented chatbot over Viessmann technical PDFs (Vitocal heat
pumps, Vitodens boilers). Answers technical questions from the documentation
in Croatian or English and cites the exact page each fact comes from.

```
PDFs ──► layout-preserving extract ──► page chunks ──► Supabase + pgvector
                                                            │
                                                            ▼
                            Hybrid search → diversify → LLM rerank
                                                            │
                                                            ▼
                                                    gpt-4o with citations
```

## Project layout

```
viessmann-rag/
├── ingest.py                    Entry point — `python ingest.py --drive|--dir`
├── chat_server.py               Entry point — `python chat_server.py`
├── eval.py                      Manual eval harness (hits running server)
├── requirements.txt
├── .env.example                 Template — copy to `.env` and fill in
├── migrations/
│   └── 001_initial_schema.sql   Run this once in the Supabase SQL editor
├── web/
│   ├── index.html               Frontend (login + chat UI)
│   └── static/
│       ├── chat.js
│       └── style.css
└── viessmann_rag/               The Python package
    ├── config.py                Env loading + tuning constants
    ├── logging_setup.py
    ├── prompts.py               System prompt (citation + table rules)
    ├── supabase_client.py       REST helpers for tables + RPCs
    ├── openai_client.py         OpenAI wrapper + 429 retry + QuotaExhausted
    ├── pdf_parser.py            pdfplumber extraction (layout=True + tables)
    ├── ingest/
    │   ├── cli.py               Argparse + dispatch
    │   ├── metadata.py          Filename → product_line / document_type
    │   ├── pipeline.py          process_pdf_bytes (the hot path)
    │   ├── local.py             ingest_local()
    │   └── drive.py             ingest_drive() + OAuth + diff logic
    ├── retrieval/
    │   ├── expand.py            Multi-query expansion (gpt-4o-mini)
    │   ├── search.py            Hybrid search via search_chunks_v2 RPC
    │   ├── diversify.py         Per-file cap when several files compete
    │   ├── rerank.py            LLM rerank (gpt-4o-mini, scores 0-10)
    │   └── pipeline.py          retrieve() orchestrator
    └── chat/
        └── server.py            Flask app + auth + /api/chat endpoint
```

## Quick start

### 1 — Install

```powershell
py -3.11 -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt
```

### 2 — Configure secrets

```powershell
copy .env.example .env
notepad .env
```

Fill in Supabase, OpenAI, and (for Drive ingest) Google OAuth keys. See
[`.env.example`](.env.example) for what each variable means and where to get it.

### 3 — Apply the Supabase migration

Open the SQL editor in your Supabase project:

```
https://supabase.com/dashboard/project/<your-project>/sql
```

Paste the contents of [`migrations/001_initial_schema.sql`](migrations/001_initial_schema.sql)
and click **Run**.

If Supabase warns about RLS, choose **"Run without RLS"** — the app only ever
talks to the database with the service-role key (server-side), so RLS policies
aren't needed.

### 4 — Ingest your PDFs

```powershell
# Local folder (one-shot)
python ingest.py --dir "C:\path\to\pdfs"

# Local folder, single file
python ingest.py --dir "C:\path\to\pdfs" --file 5832352_info.pdf

# Google Drive (one-shot — uses the same OAuth keys as the original processor)
python ingest.py --drive

# Google Drive (continuous polling — same as the original processor's --loop)
python ingest.py --drive --loop
```

On the first `--drive` run, a browser window opens for OAuth consent. The token
is saved to `google_token.json` next to the script. Subsequent runs reuse it
silently. **You can copy an existing `google_token.json` from a previous
Viessmann project to skip the consent flow** — the OAuth scope is the same
(`drive.readonly`).

### 5 — Run the chat server

```powershell
python chat_server.py
```

Open <http://localhost:8081>. Login: `viessmann` / `carrier` (override via
`CHAT_USERNAME` / `CHAT_PASSWORD` in `.env`).

## How retrieval works

For every user question:

1. **Query expansion** — `gpt-4o-mini` rewrites the question into a Croatian
   paraphrase and a keyword-rich variant. English questions get a Croatian
   translation; Croatian questions get an English paraphrase. This closes the
   cross-language gap when querying English over Croatian docs.
2. **Hybrid search** — for each variant, the `search_chunks_v2` SQL function
   combines three signals: cosine similarity (pgvector HNSW), full-text rank
   (`ts_rank_cd`), and trigram similarity (helps with model codes like
   `101.A14`).
3. **Union + diversify** — candidates from all query variants are unioned
   (dedup by chunk id), sorted by hybrid score, then capped at 4 chunks per
   source file when several files compete.
4. **LLM rerank** — `gpt-4o-mini` scores each candidate 0–10 against the
   **original** question (not the expansions). Used to **order** the final
   top-10, not to filter — passing complementary pages to the LLM is safer
   than filtering them out.
5. **Answer** — `gpt-4o` reads all 10 chunks (each chunk begins with
   `[Document: foo.pdf · Page N]`) and answers with `(file.pdf, p.N)`
   citations.

## Architecture choices

- **Per-page chunks, not per-N-words.** Technical PDFs are heavily tabular;
  word-based chunking on whitespace-aligned spec tables destroys columns.
- **`extract_text(layout=True)`, no regex post-processing.** Numbers, model
  codes, and unit symbols are preserved exactly as drawn. (The original
  ingest had a `clean_text` regex that stripped every 1–4 digit number —
  destroying every value the user would ever ask about.)
- **Tables as markdown.** `extract_tables(lines_strict)` only fires on tables
  with real ruling lines (the type-overview tables, the cable-spec tables).
  These get rendered as `[TABLE N]` markdown and appended to the page text.
  Detailed spec tables that use whitespace alignment (not ruling lines) are
  preserved by `layout=True` text alone.
- **Hybrid retrieval with trigram fallback.** Vector similarity alone misses
  queries about exact model codes; full-text alone misses paraphrases;
  trigram catches partial substrings.
- **Multi-query expansion.** Single biggest fix for cross-language retrieval
  — recall jumps when the Croatian paraphrase is embedded too.

## Configuration reference

All settings live in `.env`. See [`.env.example`](.env.example) for the
documented template.

| Variable | Required | Notes |
|---|---|---|
| `SUPABASE_URL` | yes | Project URL from Supabase API settings |
| `SUPABASE_SERVICE_KEY` | yes | Service role key — server-side only |
| `OPENAI_API_KEY` | yes | Account needs billing credit |
| `CHAT_USERNAME`, `CHAT_PASSWORD` | yes | Login for the web UI |
| `FLASK_SECRET_KEY` | yes | Any random string |
| `CHAT_PORT` | optional | Default `8081` |
| `GOOGLE_CLIENT_ID` | for `--drive` only | OAuth client (Desktop app) |
| `GOOGLE_CLIENT_SECRET` | for `--drive` only | OAuth client secret |
| `GOOGLE_ROOT_FOLDER_ID` | for `--drive` only | Root Drive folder — subfolders are scanned |
| `POLL_INTERVAL_SECONDS` | optional | Default `60` — for `--drive --loop` |

Retrieval / ingest tuning constants live in
[`viessmann_rag/config.py`](viessmann_rag/config.py) (`HYBRID_CANDIDATE_COUNT`,
`DIVERSIFY_MAX_PER_FILE`, `RERANK_TOP_K`, `SEMANTIC_WEIGHT`, etc.). Change
them there, not in business logic.

## Cost (OpenAI)

Per-query cost with the default models:

| Component | Tokens | Cost |
|---|---|---|
| Query expansion (gpt-4o-mini) | ~200 in, ~100 out | $0.0001 |
| 3× embeddings (text-embedding-3-small) | ~60 each | $0.0001 |
| Rerank (gpt-4o-mini) | ~10k in, ~200 out | $0.002 |
| Answer (gpt-4o) | ~30k in, ~600 out | $0.085 |
| **Total** | | **~$0.09 / query** |

Ingest cost: ~$0.00004 per page (text-embedding-3-small). Twenty 10-page
PDFs ≈ $0.008.

If the OpenAI account hits the credit limit, the API returns
`insufficient_quota`. The chat server detects this and returns a 503 with a
clear Croatian error message instead of swallowing it as a generic 500. Top
up at <https://platform.openai.com/account/billing>.

## Eval harness

[`eval.py`](eval.py) runs a fixed battery of 15 questions against a running
chat server and writes a JSON report under `logs/eval-<tag>.json`. Useful
when tweaking prompts, the rerank threshold, or model choices:

```powershell
# in terminal 1
python chat_server.py

# in terminal 2
python eval.py --tag baseline --concurrency 2
```

Add more cases to the `CASES` list at the top of `eval.py`.

## Troubleshooting

**"Asistent je trenutno preopterećen"** — gpt-4o hit a per-minute token
limit. The server already retries with parsed backoff; reduce concurrency or
raise your OpenAI usage tier if it's recurring.

**"OpenAI API kvota je iscrpljena"** — the account is out of credit. Top up.

**Ingest is slow** — `extract_text(layout=True)` is per-character layout
analysis, so a 15-MB installation manual can take 5–10 minutes. CPU-bound;
run ingest on a beefier machine and point it at the same Supabase project.

**Drive OAuth keeps re-prompting** — delete `google_token.json` and re-run.
If the OAuth client was deleted from Google Cloud Console, you also need
fresh `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` values.

**"column reference id is ambiguous" from the RPC** — old version of
`search_chunks_v2` is still in your database. Re-run the migration (the
`DROP FUNCTION IF EXISTS` at the top handles the cleanup).

## License

MIT. See [LICENSE](LICENSE).
