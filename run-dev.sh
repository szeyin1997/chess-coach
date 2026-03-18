#!/usr/bin/env bash
set -euo pipefail

# Simple dev runner: starts FastAPI backend and Vite frontend.
# - Activates backend venv if present
# - Cleans up backend when you stop the script (Ctrl+C)

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$ROOT_DIR/chess-coach-backend"
FRONTEND_DIR="$ROOT_DIR/chess-coach-frontend"

cd "$BACKEND_DIR"

# Activate venv if available
if [[ -f .venv/bin/activate ]]; then
  # shellcheck source=/dev/null
  source .venv/bin/activate
fi

# Load backend .env if present (to pick up OPENAI_API_KEY, etc.)
if [[ -f .env ]]; then
  set -a
  # shellcheck source=/dev/null
  source .env
  set +a
fi

# Brief hint if OPENAI_API_KEY isn't set
if [[ -n "${GEMINI_API_KEY:-}" ]]; then
  echo "[dev] GEMINI_API_KEY detected; Gemini summaries enabled."
elif [[ -n "${OPENAI_API_KEY:-}" ]]; then
  echo "[dev] OPENAI_API_KEY detected; OpenAI summaries enabled."
else
  echo "[dev] Note: no AI API key found (GEMINI_API_KEY/OPENAI_API_KEY); summaries will fallback."
fi

# Prefer uvicorn module via python -m for portability
echo "[dev] Starting backend: http://127.0.0.1:8000"
python -m uvicorn server:app --reload --host 127.0.0.1 --port 8000 &
BACKEND_PID=$!

cleanup() {
  echo
  echo "[dev] Stopping backend (pid $BACKEND_PID)"
  kill "$BACKEND_PID" 2>/dev/null || true
  wait "$BACKEND_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

cd "$FRONTEND_DIR"
echo "[dev] Starting frontend (Vite dev) on http://127.0.0.1:5173"
npm run dev -- --host 127.0.0.1 --port 5173 --strictPort
