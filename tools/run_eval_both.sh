#!/bin/sh
# Run eval.py against both providers and emit a side-by-side Markdown report.
#
# Requires:
#   - .env with OPENAI_API_KEY + GEMINI_API_KEY + Supabase keys
#   - Both `embedding` and `embedding_gem` columns populated for the corpus
#     under test (run the top-up + dual ingest first)
#
# Output:
#   logs/eval-openai.json
#   logs/eval-gemini.json
#   logs/compare_openai_vs_gemini.md
#
# Usage:
#   sh tools/run_eval_both.sh

set -e
cd "$(dirname "$0")/.."

if [ -z "$GEMINI_API_KEY" ]; then
  echo "GEMINI_API_KEY env var required" >&2; exit 1
fi

run_one() {
  provider="$1"
  echo "=== Eval run: $provider ==="

  LLM_PROVIDER="$provider" GEMINI_API_KEY="$GEMINI_API_KEY" \
    py -3.11 chat_server.py > "logs/server-${provider}.log" 2>&1 &
  SVR=$!
  trap "kill $SVR 2>/dev/null || true" EXIT

  # Wait for the server to be listening on 8081
  for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    if curl -fsS -o /dev/null "http://localhost:8081/api/health" 2>/dev/null; then
      break
    fi
    sleep 1
  done

  py -3.11 eval.py --tag "$provider" --concurrency 1

  kill $SVR 2>/dev/null || true
  wait $SVR 2>/dev/null || true
  trap - EXIT
  echo "=== Done: $provider ==="
}

mkdir -p logs

run_one openai
run_one gemini

py -3.11 tools/compare_eval.py \
  logs/eval-openai.json logs/eval-gemini.json \
  --out logs/compare_openai_vs_gemini.md \
  --label-a "OpenAI (gpt-4o)" \
  --label-b "Gemini (gemma-4-26b-a4b-it)"

echo "Report -> logs/compare_openai_vs_gemini.md"
