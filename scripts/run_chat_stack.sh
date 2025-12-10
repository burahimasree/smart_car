#!/usr/bin/env bash
set -euo pipefail

# Supervisor to launch LLM runner, Piper TTS, and optional chat CLI
# Usage:
#   ./scripts/run_chat_stack.sh [--no-chat] [--test]
#   --no-chat : don't start interactive chat CLI (start only services)
#   --test    : send a single test prompt through the IPC bus and wait for response

ROOT_DIR=$(cd -- "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
LOG_DIR="$ROOT_DIR/logs"
mkdir -p "$LOG_DIR"

PY_CORE="$ROOT_DIR/.venvs/core/bin/python"
PY_LLME="$ROOT_DIR/.venvs/llme/bin/python"
PY_TTSE="$ROOT_DIR/.venvs/ttse/bin/python"

LLM_MODEL="$ROOT_DIR/models/llm/tinyllama-1.1b-chat.Q4_K_M.gguf"

IPC_UP="tcp://127.0.0.1:6620"
IPC_DOWN="tcp://127.0.0.1:6621"

NO_CHAT=0
DO_TEST=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-chat)
      NO_CHAT=1; shift ;;
    --test)
      DO_TEST=1; shift ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

export IPC_UPSTREAM="$IPC_UP"
export IPC_DOWNSTREAM="$IPC_DOWN"
export PYTHONPATH="$ROOT_DIR"
export STT_ENGINE_DISABLED=1

PIDS=()

cleanup() {
  echo "Shutting down services..."
  for pid in "${PIDS[@]}"; do
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" || true
      wait "$pid" 2>/dev/null || true
    fi
  done
  echo "All services stopped"
}
trap cleanup EXIT INT TERM

start_llm() {
  echo "Starting LLM runner..."
  "$PY_LLME" src/llm/llm_runner.py >"$LOG_DIR/llm_stack.log" 2>&1 &
  PIDS+=("$!")
}

start_tts() {
  # If Piper binary or model missing, run a lightweight TTS stub that accepts TTS text and replies done
  local piper_bin="$ROOT_DIR/.venvs/ttse/bin/piper"
  local tts_model
  # prefer configured path
  tts_model=$(python - <<PY
from pathlib import Path
import yaml, sys
cfg = yaml.safe_load(Path('config/system.yaml').read_text())
print(cfg.get('tts', {}).get('model_path', ''))
PY
)
  if [[ -x "$piper_bin" && -n "$tts_model" && -f "$tts_model" ]]; then
    echo "Starting Piper TTS runner..."
    "$PY_TTSE" src/tts/piper_runner.py >"$LOG_DIR/piper_stack.log" 2>&1 &
    PIDS+=("$!")
  else
    echo "Starting TTS stub (piper missing or model not found)"
    "$PY_CORE" - <<'PY' >"$LOG_DIR/piper_stack.log" 2>&1 &
import os, json, time
import zmq
up = os.environ.get('IPC_UPSTREAM')
down = os.environ.get('IPC_DOWNSTREAM')
ctx = zmq.Context.instance()
sub = ctx.socket(zmq.SUB)
sub.connect(down)
sub.setsockopt(zmq.SUBSCRIBE, b'tts.speak')
pub = ctx.socket(zmq.PUB)
pub.connect(up)
while True:
    topic, data = sub.recv_multipart()
    try:
        msg = json.loads(data)
    except Exception:
        continue
    time.sleep(0.5)
    pub.send_multipart([b'tts.speak', json.dumps({'done': True, 'timestamp': int(time.time())}).encode('utf-8')])
PY
    PIDS+=("$!")
  fi
}

start_chat() {
  echo "Starting interactive chat CLI (attach to this terminal)..."
  exec "$PY_CORE" src/tools/chat_llm_cli.py --tts-wait
}

# Launch services
start_llm
start_tts

start_ipc_broker() {
  echo "Starting IPC broker (XSUB <-> XPUB proxy)..."
  "$PY_CORE" - <<'PY' >"$LOG_DIR/ipc_broker.log" 2>&1 &
import os
import zmq
ctx = zmq.Context.instance()
up = os.environ['IPC_UPSTREAM']
down = os.environ['IPC_DOWNSTREAM']
print('Binding XSUB ->', up)
print('Binding XPUB ->', down)
xsock = ctx.socket(zmq.XSUB)
xpsock = ctx.socket(zmq.XPUB)
xsock.bind(up)
xpsock.bind(down)
try:
    zmq.proxy(xsock, xpsock)
except Exception:
    pass
PY
  PIDS+=("$!")
}

start_ipc_broker

# Optionally run test or interactive chat
if [[ "$DO_TEST" -eq 1 ]]; then
  # Wait briefly for runner to be ready
  echo "Waiting for LLM runner to become ready (this may take up to 2 minutes while model loads)..."
  # wait for llm runner to announce readiness in logs
  ready=0
  deadline=$((SECONDS+120))
  while [[ $SECONDS -lt $deadline ]]; do
    if grep -q "LLM runner listening for requests" "$LOG_DIR/llm.runner.log" 2>/dev/null || grep -q "LLM runner listening for requests" "$LOG_DIR/llm_stack.log" 2>/dev/null; then
      ready=1
      break
    fi
    sleep 2
  done
  if [[ $ready -ne 1 ]]; then
    echo "Warning: LLM runner did not announce readiness in time (check $LOG_DIR/llm_stack.log and $LOG_DIR/llm.runner.log)"
  else
    echo "LLM runner ready"
  fi
  # give extra time for the model to finish loading if needed
  sleep 15
  echo "Sending test prompt via IPC..."
  "$PY_CORE" - <<PY
import os, time, json
import zmq
ctx = zmq.Context.instance()
pub = ctx.socket(zmq.PUB)
pub.connect(os.environ['IPC_UPSTREAM'])
sub = ctx.socket(zmq.SUB)
sub.connect(os.environ['IPC_DOWNSTREAM'])
sub.setsockopt(zmq.SUBSCRIBE, b'llm.response')
# send prompt
pub.send_multipart([b'llm.request', json.dumps({'text':'What is 2+2?'}).encode('utf-8')])
# wait for llm.response
poller = zmq.Poller(); poller.register(sub, zmq.POLLIN)
deadline = time.time() + 30
while time.time() < deadline:
    events = dict(poller.poll(500))
    if sub in events:
        topic, data = sub.recv_multipart()
        print('LLM response:', data.decode())
        break
else:
    print('LLM response timeout')
# wait for TTS done
sub_tts = ctx.socket(zmq.SUB)
sub_tts.connect(os.environ['IPC_DOWNSTREAM'])
sub_tts.setsockopt(zmq.SUBSCRIBE, b'tts.speak')
poller = zmq.Poller(); poller.register(sub_tts, zmq.POLLIN)
deadline = time.time() + 30
while time.time() < deadline:
    events = dict(poller.poll(500))
    if sub_tts in events:
        topic, data = sub_tts.recv_multipart()
        print('TTS event:', data.decode())
        break
else:
    print('TTS timeout')
PY
  echo "Test complete; cleaning up"
  exit 0
fi

if [[ "$NO_CHAT" -eq 1 ]]; then
  echo "Services started in background with logs in $LOG_DIR. PID list: ${PIDS[*]}"
  # wait until killed
  wait
else
  start_chat
fi
