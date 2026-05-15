#!/bin/sh
# Container entrypoint for Hugging Face Spaces.
#
# Drive polling now runs INSIDE the Flask process (see ingest/progress.py)
# so the chat UI can subscribe to live progress events over SSE. This script
# only has to:
#   1. Materialize the service-account JSON from the Space secret to a file
#      on disk (the google-auth client expects a file path).
#   2. Exec gunicorn in the foreground.

set -e

if [ -n "$GOOGLE_SERVICE_ACCOUNT_JSON" ]; then
    printf '%s' "$GOOGLE_SERVICE_ACCOUNT_JSON" > "$HOME/app/google_service_account.json"
    chmod 600 "$HOME/app/google_service_account.json"
    echo "[entrypoint] wrote service-account JSON ($(wc -c < "$HOME/app/google_service_account.json") bytes)"
else
    echo "[entrypoint] GOOGLE_SERVICE_ACCOUNT_JSON not set — Drive polling disabled"
fi

echo "[entrypoint] starting gunicorn on port ${PORT:-7860} (Drive poller runs in-process)"
exec gunicorn \
    --bind "0.0.0.0:${PORT:-7860}" \
    --workers 1 \
    --threads 8 \
    --worker-class gthread \
    --timeout 300 \
    --access-logfile - \
    --error-logfile - \
    'viessmann_rag.chat.server:create_app()'
