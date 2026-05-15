---
title: Viessmann RAG Chatbot
emoji: 🔥
colorFrom: red
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
short_description: RAG over Viessmann technical PDFs (Croatian/English)
---

# Viessmann RAG Chatbot

[![Release](https://img.shields.io/github/v/release/0NE-C0DEMAN/ViessmannChatbot?display_name=tag&sort=semver)](https://github.com/0NE-C0DEMAN/ViessmannChatbot/releases)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

A retrieval-augmented chatbot over Viessmann technical PDFs (Vitocal heat
pumps, Vitodens boilers). Answers technical questions from the documentation
in Croatian or English and cites the exact page each fact comes from.

---

## Quick start

> **Picking up an existing setup** (Supabase + ingest already done)?
> Skip to [Picking up an existing project](#picking-up-an-existing-project).

### 1. Install

```powershell
py -3.11 -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure

```powershell
copy .env.example .env
notepad .env
```

Fill in `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `OPENAI_API_KEY`, and
`GOOGLE_ROOT_FOLDER_ID`. The `.env.example` file walks through both Drive
auth modes step by step.

### 3. Apply the Supabase migration

In your Supabase SQL editor (`https://supabase.com/dashboard/project/<your-project>/sql`),
paste the contents of [`migrations/001_initial_schema.sql`](migrations/001_initial_schema.sql)
and click **Run**. If asked about RLS, choose **"Run without RLS"** — the app
only talks to Supabase with the service-role key.

### 4. Ingest your PDFs

```powershell
python ingest.py --drive         # from Google Drive (one-shot)
python ingest.py --drive --loop  # continuous polling
python ingest.py --dir "C:\pdfs" # or a local folder
```

Ingest is **md5-deduped** — re-runs skip content that's already in Supabase.

### 5. Run the chat server

```powershell
python chat_server.py
```

Open <http://localhost:8081>. Default login: `viessmann` / `carrier`
(override via `CHAT_USERNAME` / `CHAT_PASSWORD` in `.env`).

---

## Picking up an existing project

If a colleague has the migration applied and PDFs already ingested, your
setup is just two files + three commands. Both files are gitignored, so
they only live on your machine.

| Bring to the repo root | Where to get it |
|---|---|
| `.env` | Your existing one, or recreate from `.env.example`. |
| `google_service_account.json` *(service-account mode)* | Same JSON the team uses — it's tied to the Google Cloud project, not your machine. |

```powershell
py -3.11 -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt
python ingest.py --drive         # idempotent — nothing re-embedded
python chat_server.py
```

That's it. The first `--drive` run prints "nothing to do" because md5s
already match — no surprise OpenAI charges.

---

## Drive auth

The `--drive` mode supports two modes; pick one in `.env`.

### Option A — Service account (recommended for production)

1. Create a service account in [Google Cloud Console → IAM](https://console.cloud.google.com/iam-admin/serviceaccounts).
2. Generate a JSON key and save it at the repo root as
   `google_service_account.json`.
3. **Share your Drive root folder with the service account's `client_email`**
   (looks like `name@project.iam.gserviceaccount.com`) → grant **Viewer**.
4. Enable the [Drive API](https://console.cloud.google.com/apis/library/drive.googleapis.com).

No browser flow, no token expiry, no test-user limits. Headless-loop friendly.

### Option B — OAuth user (browser consent)

Leave `google_service_account.json` absent. Fill in `GOOGLE_CLIENT_ID` and
`GOOGLE_CLIENT_SECRET` in `.env` from an OAuth client of type "Desktop app".
On first run a browser opens for consent; the token is saved to
`google_token.json` and reused on subsequent runs. If Google ever revokes
the cached refresh token, the script auto-reopens consent.

---

## Configuration reference

| Variable | Required | Notes |
|---|---|---|
| `SUPABASE_URL` | yes | Project URL from Supabase API settings |
| `SUPABASE_SERVICE_KEY` | yes | Service-role key — server-side only |
| `OPENAI_API_KEY` | yes | Account needs billing credit |
| `CHAT_USERNAME`, `CHAT_PASSWORD` | yes | Login for the web UI |
| `FLASK_SECRET_KEY` | yes | Any random string |
| `CHAT_PORT` | optional | Default `8081` |
| `GOOGLE_ROOT_FOLDER_ID` | for `--drive` | Root Drive folder — subfolders scanned |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | for `--drive`, Option A | Default `google_service_account.json` |
| `GOOGLE_CLIENT_ID` / `_SECRET` | for `--drive`, Option B | OAuth Desktop-app client |
| `POLL_INTERVAL_SECONDS` | optional | Default `60` — for `--drive --loop` |

Retrieval tuning constants (rerank top-k, semantic weight, etc.) live in
[`viessmann_rag/config.py`](viessmann_rag/config.py).

---

# Architecture

```
PDFs ──► layout-preserving extract ──► page chunks ──► Supabase + pgvector
                                                            │
                                                            ▼
                            Hybrid search → diversify → LLM rerank
                                                            │
                                                            ▼
                                                    gpt-4o with citations
```

## How retrieval works

For every user question:

1. **Intent classifier** (`gpt-4o-mini`) picks the preferred document type
   (`spec`, `capability`, `install`, `service`, `user`, `design`).
2. **Query expansion + HyDE** generate a Croatian paraphrase, a keyword
   variant, and (for spec/capability) a hypothetical-answer paragraph —
   all in parallel.
3. **Hybrid search** (`search_chunks_v2` SQL function) combines pgvector
   cosine + Postgres full-text + pg_trgm fallback, with a doc-type boost
   that favors the canonical datasheet over procedural manuals.
4. **Diversify + LLM rerank** — `gpt-4o-mini` orders the top candidates
   against the original question.
5. **Answer** — `gpt-4o` reads 10 chunks (each carrying
   `[Document: foo.pdf · Page N]`) and replies with `(file.pdf, p.N)`
   citations. Streams via SSE.

## Design choices

- **Per-page chunks** preserve table structure — word-based chunking
  shreds whitespace-aligned spec tables.
- **`extract_text(layout=True)`, no regex.** Numbers, model codes, and
  units stay verbatim. The original ingest had a regex that stripped
  every 1-4 digit number, destroying every value a user would ask about.
- **Tables as markdown** for ruling-line tables; layout text for
  whitespace-aligned ones.
- **Doc-type weighting** in the hybrid score so canonical specs win
  over procedural mentions when relevance is close.
- **Md5-based idempotent ingest** — content match short-circuits
  re-embedding even when file_ids differ across ingest modes.

## Project layout

```
viessmann-rag/
├── ingest.py                Entry point — python ingest.py --drive|--dir
├── chat_server.py           Entry point — python chat_server.py
├── eval.py                  15-question manual eval harness
├── requirements.txt
├── .env.example             Template — copy to .env
├── migrations/
│   ├── 001_initial_schema.sql
│   └── 002_doctype_boost.sql
├── web/
│   ├── index.html           Frontend (login + chat UI)
│   └── static/              chat.js, style.css
└── viessmann_rag/
    ├── config.py            Env loading + tuning constants
    ├── prompts.py           System prompt
    ├── cache.py             LRU + TTL query cache
    ├── metrics.py           Per-query NDJSON metrics
    ├── pdf_parser.py        pdfplumber extraction
    ├── supabase_client.py   REST helpers
    ├── openai_client.py     OpenAI wrapper + retries
    ├── ingest/              metadata, pipeline, local, drive, cli
    ├── retrieval/           intent, expand, hyde, search, diversify, rerank
    └── chat/                server (SSE + REST)
```

## Cost (OpenAI)

Per-query: **~$0.09** end-to-end (intent + expand + HyDE + 3× embeddings +
rerank + gpt-4o answer). Cached repeats are free.

Ingest: **~$0.00004 per page**. The full Vitocal corpus (~1,800 pages) is
about $0.05.

If the OpenAI account runs out of credit, the chat endpoint returns 503
with a clear Croatian message instead of a generic 500. Top up at
<https://platform.openai.com/account/billing>.

## Eval harness

[`eval.py`](eval.py) runs a fixed 15-question battery against the running
server and writes results to `logs/eval-<tag>.json`:

```powershell
python chat_server.py                       # terminal 1
python eval.py --tag baseline --concurrency 2  # terminal 2
```

Useful when tweaking prompts, rerank thresholds, or models. Add cases to
the `CASES` list at the top.

## Troubleshooting

- **"Asistent je trenutno preopterećen"** — gpt-4o hit your TPM limit.
  Already retries with backoff; raise your OpenAI tier if recurring.
- **"OpenAI API kvota je iscrpljena"** — account out of credit. Top up.
- **Drive ingest reports 0 PDFs** — the Drive folder isn't shared with
  the service account's `client_email`.
- **Drive OAuth re-prompts every run** — token revoked; script
  re-opens consent automatically. If the OAuth client itself was deleted,
  generate fresh `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`.
- **"column reference id is ambiguous" from the RPC** — old function
  still in the DB. Re-run the migration; its `DROP FUNCTION IF EXISTS`
  cleans up.

## License

MIT. See [LICENSE](LICENSE).
