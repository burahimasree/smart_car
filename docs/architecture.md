# Offline AI Assistant Architecture

## Overview
The assistant is an offline, voice-controlled robotic system on Raspberry Pi 4, integrating wakeword detection, STT, LLM intent parsing, TTS, vision, and UART navigation. All components communicate via ZeroMQ PUB/SUB, orchestrated by an asyncio event loop.

## Subsystems
1. **Wakeword (`src/wakeword`)** – Porcupine (single keyword .ppn) detects wakeword and publishes `ww.detected`.
2. **STT (`src/stt`)** – `whisper_runner.py` captures ALSA PCM, runs whisper.cpp (`tiny.en-q5_1.gguf`), and publishes `stt.transcription` with confidence + language metadata.
3. **LLM (`src/llm`)** – `llm_runner.py` supervises `llama-server`, POSTs prompts to `/completion`, and publishes `llm.response` payloads `{timestamp,text,tokens,latency_ms}`.
4. **TTS (`src/tts`)** – Piper ONNX voices piped to ALSA, subscribes to `tts.speak`, then publishes `{done: true}` upstream after playback.
5. **Vision (`src/vision`)** – YOLO ONNX detection on camera frames, publishes `visn.object`, pauses on wakeword.
6. **UART (`src/uart`)** – Bridge for navigation commands to ESP32.
7. **Core (`src/core`)** – Async orchestrator routing events, config/loader/logging.

## Flow Diagram
```
Wakeword
   | (ww.detected => pause vision, start STT runner)
   v
STT (whisper.cpp) ---> LLM ------> TTS --> Resume vision
        |                    \
        |                     -> NAV (direction intent)
        v
Vision (remains paused until TTS completion event)
```

> NOTE: Vision stays paused from wakeword detection until the TTS module confirms playback completion, guaranteeing microphones & speakers are prioritized during the entire dialog turn.

## Configuration & Logging
- `config/system.yaml` centralizes all settings.
- Logging to `logs/` with rotation.

## Testing Strategy
- Unit tests in `src/tests/`, smoke tests in `run-tests/`.

## Deployment Notes
- Venvs: stte, ttse, llme, visn, core.
- Scripts build/fetch models, run.sh starts all services.
