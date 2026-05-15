# Viessmann RAG Chatbot — production image for Hugging Face Spaces (Docker SDK).
#
# HF Spaces conventions baked in here:
#   - Runs as non-root user (UID 1000); $HOME owned by that user so writes
#     (logs/, temp files) don't EACCES.
#   - Single web server on PORT (default 7860, set by the platform).
#   - All runtime config from "Variables and secrets" in the Space UI:
#         SUPABASE_URL, SUPABASE_SERVICE_KEY      (required)
#         OPENAI_API_KEY                          (required)
#         CHAT_USERNAME, CHAT_PASSWORD            (login)
#         FLASK_SECRET_KEY                        (any random string)
#     The Drive-ingest env vars (GOOGLE_*) are NOT needed for the deployed
#     chatbot — ingest is run locally; the Space only reads from Supabase.

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Non-root user (HF convention). `-m` creates /home/user owned by user.
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH
WORKDIR $HOME/app

# Layer 1: Python deps (cached across edits — only invalidated by requirements.txt).
COPY --chown=user:user requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

# Layer 2: app source.
COPY --chown=user:user . .

ENV PORT=7860
EXPOSE 7860

# gunicorn + gthread workers — sync workers buffer responses, which breaks the
# SSE streaming endpoint. gthread keeps the connection open and yields tokens
# as they arrive. Single worker is fine for HF free-tier hardware; threads
# handle concurrent SSE clients.
CMD ["sh", "-c", "gunicorn \
    --bind 0.0.0.0:${PORT:-7860} \
    --workers 1 \
    --threads 8 \
    --worker-class gthread \
    --timeout 300 \
    --access-logfile - \
    --error-logfile - \
    'viessmann_rag.chat.server:create_app()'"]
