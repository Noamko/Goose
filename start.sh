#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

# Create venv if it doesn't exist
if [ ! -d ".venv" ]; then
  echo "Creating virtual environment..."
  python3 -m venv .venv
fi

source .venv/bin/activate

echo "Installing dependencies..."
pip install -q -r requirements.txt

# Check for .env
if [ ! -f ".env" ]; then
  cp .env.example .env
  echo ""
  echo "  Created .env — please add your OPENAI_API_KEY:"
  echo "  Edit .env and set OPENAI_API_KEY=sk-..."
  echo ""
  exit 1
fi

# Validate key is set
if grep -q "your-key-here" .env; then
  echo ""
  echo "  OPENAI_API_KEY not set. Edit .env and set your real key."
  echo ""
  exit 1
fi

echo ""
echo "  Starting Goose dashboard at http://localhost:8000"
echo "  (process manager active — server restarts automatically)"
echo ""

# Process manager loop: restart the server if it exits unexpectedly
# This lets the Goose dev agent restart the server without losing the manager.
while true; do
  uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
  EXIT_CODE=$?
  if [ $EXIT_CODE -eq 0 ]; then
    # Clean exit (Ctrl-C propagates SIGINT to uvicorn, which exits 0 via the reloader)
    break
  fi
  echo "  Server exited (code $EXIT_CODE) — restarting in 2s..."
  sleep 2
done
