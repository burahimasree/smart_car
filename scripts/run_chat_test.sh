#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
LOG_DIR="$ROOT_DIR/logs"
mkdir -p "$LOG_DIR"

# Paths to venvs and scripts
CORE_PY="$ROOT_DIR/.venvs/core/bin/python"
LLME_PY="$ROOT_DIR/.venvs/llme/bin/python"
TTSE_PY="$ROOT_DIR/.venvs/ttse/bin/python"
LLM_RUNNER="$ROOT_DIR/src/llm/llm_runner.py"
PIPER_RUNNER="$ROOT_DIR/src/tts/piper_runner.py"
CHAT_CLI="$ROOT_DIR/src/tools/chat_llm_cli.py"

# Check if venvs exist
if [[ ! -x "$CORE_PY" ]]; then
  echo "Missing core venv at $CORE_PY" >&2
  exit 1
fi
if [[ ! -x "$LLME_PY" ]]; then
  echo "Missing llme venv at $LLME_PY" >&2
  exit 1
fi
if [[ ! -x "$TTSE_PY" ]]; then
  echo "Missing ttse venv at $TTSE_PY" >&2
  exit 1
fi

# Function to cleanup background processes
cleanup() {
  echo "Shutting down services..."
  for pid in ${LLM_PID:-} ${PIPER_PID:-}; do
    if [[ -n "$pid" ]]; then
      kill "$pid" >/dev/null 2>&1 || true
      wait "$pid" 2>/dev/null || true
    fi
  done
}
trap cleanup EXIT

cd "$ROOT_DIR"

# Start LLM runner in background
"$LLME_PY" "$LLM_RUNNER" >"$LOG_DIR/llm_runner_chat.log" 2>&1 &
LLM_PID=$!
echo "Started LLM runner (PID $LLM_PID)"

# Start Piper TTS in background
"$TTSE_PY" "$PIPER_RUNNER" >"$LOG_DIR/piper_runner_chat.log" 2>&1 &
PIPER_PID=$!
echo "Started Piper TTS (PID $PIPER_PID)"

# Give services time to start
sleep 3

# Run chat CLI in foreground
echo "Starting chat CLI. Type prompts, /exit to quit."
"$CORE_PY" "$CHAT_CLI" --tts-wait

echo "Chat session ended."