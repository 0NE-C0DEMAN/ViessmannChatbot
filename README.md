# Viessmann RAG Chatbot

A retrieval-augmented chatbot over Viessmann technical PDFs (Vitocal / Vitodens product lines).
Answers technical questions from the documentation in Croatian or English and cites the exact
page each fact comes from.

```
PDFs ──► layout-preserving extract ──► page chunks ──► Supabase + pgvector
                                                            │
                                                            ▼
                            Hybrid search → diversify → LLM rerank
                                                            │
                                                            ▼
                                                    gpt-4o with citations
```

## What's inside

| File              | What it does                                                    |
|-------------------|-----------------------------------------------------------------|
| `pdf_parser.py`   | pdfplumber `extract_text(layout=True)` + `extract_tables(lines_strict)` as markdown |
| `ingest.py`       | Walks a local dir **or** a Google Drive folder, embeds each page, upserts to Supabase |
| `retrieval.py`    | Multi-query expansion → hybrid search RPC → per-file diversify → LLM rerank |
| `prompts.py`      | System prompt (citation rules, column-counting rules, refusal rules) |
| `chat_server.py`  | Flask app on port 8081, serves the frontend + chat API           |
| `migration.sql`   | Supabase schema + the `search_chunks_v2` hybrid-search function  |
| `index.html`, `static/` | Frontend (vanilla HTML/CSS/JS, no build step)              |

## Quick start

### 1 — Install Python 3.11 and dependencies

```powershell
py -3.11 -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt
```

### 2 — Configure secrets

```powershell
copy .env.example .env
notepad .env   # fill in your Supabase, OpenAI, (optional) Google Drive keys
```

See [`.env.example`](.env.example) for the full list of variables and where to get each.

### 3 — Apply the Supabase migration

Open the SQL editor in your Supabase project:

```
https://supabase.com/dashboard/project/<your-project>/sql
```

Paste the contents of [`migration.sql`](migration.sql) and click **Run**.

If Supabase warns about RLS, choose **"Run without RLS"** — the app only ever talks to the
database with the service-role key (server-side), so RLS policies aren't needed.

### 4 — Ingest your PDFs

**From a local folder:**

```powershell
py -3.11 ingest.py --dir "C:\path\to\pdfs"
```

**From Google Drive** (uses the same `GOOGLE_*` keys as the original processor):

```powershell
py -3.11 ingest.py --drive             # one-shot
py -3.11 ingest.py --drive --loop      # poll every POLL_INTERVAL_SECONDS
```

A browser window opens for OAuth consent on first Drive run; the token is saved to
`google_token.json` next to the script. Subsequent runs reuse it silently.

### 5 — Run the chat server

```powershell
py -3.11 chat_server.py
```

Open <http://localhost:8081> in your browser. Default login is `viessmann` / `carrier`
(change via `CHAT_USERNAME` / `CHAT_PASSWORD` in `.env`).

## How retrieval works

For every user question:

1. **Query expansion** — `gpt-4o-mini` rewrites the question into a Croatian paraphrase and a
   keyword-rich variant. (English questions get a Croatian translation; Croatian questions get
   an English paraphrase.) This is what closes the cross-language gap when the question is in
   English but the docs are in Croatian.
2. **Hybrid search** — for each variant, the `search_chunks_v2` SQL function combines three
   signals: cosine similarity (pgvector HNSW), full-text rank (`ts_rank_cd`), and trigram
   similarity (helps with model codes like `101.A14`).
3. **Union + diversify** — candidates from all query variants are unioned (dedup by chunk
   id), sorted by hybrid score, then capped at 4 chunks per source file when several files
   compete.
4. **LLM rerank** — `gpt-4o-mini` scores each candidate 0–10 against the **original** question
   (not the expansions). Used to **order** the final top-10, not to filter — passing complementary
   pages to the LLM is safer than filtering them out.
5. **Answer** — `gpt-4o` reads all 10 chunks (each chunk begins with `[Document: foo.pdf · Page N]`)
   and answers with `(file.pdf, p.N)` citations.

## Architecture choices

- **Per-page chunks, not per-N-words.** Technical PDFs are heavily tabular; word-based
  chunking on whitespace-aligned spec tables destroys columns.
- **`extract_text(layout=True)`, no regex post-processing.** Numbers, model codes, and unit
  symbols are preserved exactly as drawn. The v1 ingestion had a `clean_text` regex that
  stripped every 1–4 digit number — destroying every value the user would ever ask about.
- **Tables as markdown.** `extract_tables(lines_strict)` only fires on tables with real ruling
  lines (the type-overview tables on page 3, the cable-spec tables on page 9). These get
  rendered as `[TABLE N]` markdown and appended to the page text. The detailed spec tables
  (which use whitespace alignment, not ruling lines) are preserved by `layout=True` text alone.
- **Hybrid retrieval with trigram fallback.** Vector similarity alone misses queries about
  exact model codes; full-text alone misses paraphrases; trigram catches partial substrings.
- **Multi-query expansion.** Single biggest fix for cross-language retrieval — recall jumps
  when you embed the Croatian paraphrase too.

## Configuration reference

All settings live in `.env`. See [`.env.example`](.env.example) for the documented template.

| Variable | Required | Notes |
|---|---|---|
| `SUPABASE_URL` | yes | Project URL from Supabase API settings |
| `SUPABASE_SERVICE_KEY` | yes | Service role key (server-side only — never expose to a browser) |
| `OPENAI_API_KEY` | yes | Account needs billing credit |
| `CHAT_USERNAME`, `CHAT_PASSWORD` | yes | Login for the web UI |
| `FLASK_SECRET_KEY` | yes | Any random string |
| `CHAT_PORT` | optional | Default `8081` |
| `GOOGLE_CLIENT_ID` | only for `--drive` | OAuth client (Desktop app) from Google Cloud Console |
| `GOOGLE_CLIENT_SECRET` | only for `--drive` | OAuth client secret |
| `GOOGLE_ROOT_FOLDER_ID` | only for `--drive` | Root Drive folder ID — all subfolders are scanned |
| `POLL_INTERVAL_SECONDS` | optional | Default `60` — polling interval for `--drive --loop` |

## Costs (OpenAI)

Per-query cost with the default models:

| Component | Tokens | Cost |
|---|---|---|
| Query expansion (gpt-4o-mini) | ~200 in, ~100 out | $0.0001 |
| 3× embeddings (text-embedding-3-small) | ~60 each | $0.0001 |
| Rerank (gpt-4o-mini) | ~10k in, ~200 out | $0.002 |
| Answer (gpt-4o) | ~30k in, ~600 out | $0.085 |
| **Total** | | **~$0.09 / query** |

Ingest cost: roughly $0.00004 per page (text-embedding-3-small). Twenty 10-page PDFs ≈ $0.008.

If the OpenAI account hits the credit limit, the API returns `insufficient_quota`. The chat
server detects this and returns a clear Croatian error message instead of swallowing it.
Top up credit at <https://platform.openai.com/account/billing>.

## Troubleshooting

**"Asistent je trenutno preopterećen"** — gpt-4o hit your per-minute token limit. The server
already retries with parsed backoff; if you see this in production, raise your usage tier on
OpenAI or reduce concurrency.

**"OpenAI API kvota je iscrpljena"** — the OpenAI account is out of credit. Top up.

**Ingest is slow** — `extract_text(layout=True)` is per-character layout analysis, so a
15-MB installation manual can take 5–10 minutes. The pipeline is otherwise CPU-bound; you can
run ingest on a beefier machine and just point it at the same Supabase project.

**Drive auth keeps re-prompting** — delete `google_token.json` and re-run. If the OAuth
client was deleted from Google Cloud Console, you'll also need a fresh `GOOGLE_CLIENT_ID` /
`GOOGLE_CLIENT_SECRET`.

**"column reference id is ambiguous" from the RPC** — this means an old version of
`search_chunks_v2` is still in your database. Re-run `migration.sql` (the `DROP FUNCTION IF
EXISTS` at the top handles the cleanup).

## License

MIT. See [LICENSE](LICENSE).
