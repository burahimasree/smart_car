#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT=${PROJECT_ROOT:-"$(cd "$(dirname "$0")/.." && pwd)"}
LOG_DIR="$PROJECT_ROOT/logs"
LOG_FILE="$LOG_DIR/run.log"
PID_DIR="$LOG_DIR"

mkdir -p "$LOG_DIR"
: > "$LOG_FILE"  # Truncate log

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

cleanup() {
  log "Stopping all services..."
  for pidfile in "$PID_DIR"/*.pid; do
    [ -f "$pidfile" ] || continue
    pid=$(cat "$pidfile")
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
      log "Stopped $(basename "$pidfile" .pid) (PID $pid)"
    fi
    rm -f "$pidfile"
  done
  log "Cleanup complete"
  exit 0
}

trap cleanup SIGINT SIGTERM

start_service() {
  local venv="$1"
  local cmd="$2"
  local name="$3"
  local extra_args="${4:-}"
  
  if [ ! -d "$venv" ]; then
    log "WARNING: venv not found: $venv - skipping $name"
    return 1
  fi
  
  log "Starting $name..."
  nohup "$venv/bin/python" -m "$cmd" $extra_args >> "$LOG_DIR/${name}.log" 2>&1 &
  local pid=$!
  echo "$pid" > "$PID_DIR/${name}.pid"
  sleep 0.5
  if kill -0 "$pid" 2>/dev/null; then
    log "  -> $name started (PID $pid)"
  else
    log "  -> WARNING: $name may have failed to start"
  fi
}

# Kill any existing services
for pidfile in "$PID_DIR"/*.pid; do
  [ -f "$pidfile" ] || continue
  pid=$(cat "$pidfile")
  kill "$pid" 2>/dev/null || true
  rm -f "$pidfile"
done

log "========================================="
log "Starting Offline Pi Assistant Services"
log "PROJECT_ROOT: $PROJECT_ROOT"
log "========================================="

# AudioManager (central mic owner)
start_service "$PROJECT_ROOT/.venvs/stte" "src.audio.audio_manager" "audio-manager"
sleep 1

# Core orchestrator (must start first to bind IPC sockets)
start_service "$PROJECT_ROOT/.venvs/core" "src.core.orchestrator" "orchestrator"
sleep 1  # Give orchestrator time to bind sockets

# Wakeword detection (via AudioManager)
start_service "$PROJECT_ROOT/.venvs/stte" "src.wakeword.porcupine_runner" "wakeword" "--use-audio-manager"

# STT wrapper (consumes AudioManager audio)
start_service "$PROJECT_ROOT/.venvs/stte" "src.stt.stt_wrapper_runner" "stt-wrapper"

# Vision (YOLO object detection)
start_service "$PROJECT_ROOT/.venvs/visn" "src.vision.vision_runner" "vision"

# LLM (llama.cpp)
start_service "$PROJECT_ROOT/.venvs/llme" "src.llm.llama_server" "llm"

# TTS (Piper)
start_service "$PROJECT_ROOT/.venvs/ttse" "src.tts.piper_runner" "tts"

# UART Motor Bridge
start_service "$PROJECT_ROOT/.venvs/core" "src.uart.motor_bridge" "uart" "--sim"

# Display service
start_service "$PROJECT_ROOT/.venvs/dise" "src.ui.display_runner" "display" "--sim"

log "========================================="
log "All services started!"
log "Logs: $LOG_DIR/<service>.log"
log "PIDs: $PID_DIR/<service>.pid"
log "Press Ctrl+C to stop all services"
log "========================================="

# Wait and monitor
while true; do
  sleep 5
  # Check if any critical services died
  for svc in orchestrator wakeword stt llm tts; do
    pidfile="$PID_DIR/${svc}.pid"
    [ -f "$pidfile" ] || continue
    pid=$(cat "$pidfile")
    if ! kill -0 "$pid" 2>/dev/null; then
      log "WARNING: $svc (PID $pid) is no longer running!"
    fi
  done
done