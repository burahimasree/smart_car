# Services Reference

## Document Information

| Attribute | Value |
|-----------|-------|
| Document | 05_services_reference.md |
| Version | 1.0 |
| Last Updated | 2026-02-01 |

---

## Overview

This document provides detailed reference for each systemd service that runs on the Raspberry Pi. Each service entry includes configuration, responsibilities, IPC topics, and operational notes.

---

## Service Summary

| Service | Purpose | Port | Venv |
|---------|---------|------|------|
| orchestrator | Central FSM hub | 6010, 6011 | default |
| remote-interface | HTTP API | 8770 | default |
| uart | ESP32 bridge | USB serial | default |
| vision | Object detection | - | visn-py313 |
| llm | Azure OpenAI | - | llme |
| tts | Azure TTS | - | ttse |
| voice-pipeline | Wakeword + STT | - | stte |
| display | Waveshare OLED | - | default |
| led-ring | NeoPixel LEDs | - | default |

---

## orchestrator.service

### Overview

The central coordinator for the smart_car system. Implements a finite state machine (FSM) that orchestrates all other services.

### Service Definition

**File**: `systemd/orchestrator.service`

```ini
[Unit]
Description=Orchestrator Service
After=network.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/smart_car
ExecStart=/usr/bin/python3 -m src.core.orchestrator
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

### Module

**File**: `src/core/orchestrator.py`

### State Machine

| Phase | Description | LED Color |
|-------|-------------|-----------|
| IDLE | Waiting for wakeword | Blue (slow pulse) |
| LISTENING | Recording speech | Green (fast pulse) |
| THINKING | LLM processing | Yellow (spinning) |
| SPEAKING | TTS playback | Purple (solid) |

### IPC Behavior

**Binds**:
- Port 6010 (upstream SUB)
- Port 6011 (downstream PUB)

**Subscribes to**:
- `ww.detected` - Wakeword trigger
- `stt.transcription` - Speech result
- `llm.response` - LLM completion
- `tts.speak` - TTS completion
- `visn.object` - Vision detections
- `esp32.raw` - Sensor data
- `remote.intent` - Mobile app intents
- `remote.session` - Session state

**Publishes**:
- `llm.request` - Request LLM inference
- `tts.speak` - Request speech synthesis
- `nav.command` - Motor direction
- `cmd.listen.*` - Voice control
- `cmd.vision.*` - Vision control
- `display.state` - Phase updates
- `display.text` - Text updates
- `remote.event` - Client notifications

### Configuration

```yaml
# config/system.yaml
orchestrator:
  enabled: true
  # Additional settings in individual sections
```

### Dependencies

- ZeroMQ
- All other services connect to it

### Logging

```
/home/pi/smart_car/logs/orchestrator.log
```

---

## remote-interface.service

### Overview

HTTP server that bridges the mobile Android app to the internal IPC bus. Provides REST endpoints for supervision and control.

### Service Definition

**File**: `systemd/remote-interface.service`

```ini
[Unit]
Description=Remote Interface Service
After=network.target orchestrator.service

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/smart_car
ExecStart=/usr/bin/python3 -m src.remote.remote_interface
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

### Module

**File**: `src/remote/remote_interface.py`

### HTTP Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Returns `{"status": "ok"}` |
| `/status` | GET | Full telemetry snapshot |
| `/telemetry` | GET | Alias for /status |
| `/intent` | POST | Submit control intent |
| `/stream/mjpeg` | GET | Live video stream |

### Telemetry Response

```json
{
  "connected": true,
  "display_state": "idle",
  "display_text": "Ready",
  "vision_mode": "detection",
  "vision_paused": false,
  "last_detection": {
    "label": "person",
    "confidence": 0.87,
    "bbox": [100, 50, 200, 300],
    "ts": 1769931845.123
  },
  "sensor": {
    "s1": 16,
    "s2": 12,
    "s3": -1,
    "mq2": 478,
    "min_distance": 12,
    "obstacle": false,
    "is_safe": false
  },
  "last_llm_response": "Moving forward now",
  "last_tts_text": "Moving forward now",
  "last_tts_status": "done",
  "session_active": true,
  "last_session_touch": 1769931845
}
```

### Intent Schema

```json
{
  "intent": "start|stop|left|right|listen|capture|vision_mode|pause_vision",
  "extras": {}
}
```

### Intent Mapping

| Intent | Action |
|--------|--------|
| `start` | nav.command → forward |
| `stop` | nav.command → stopped |
| `left` | nav.command → left |
| `right` | nav.command → right |
| `listen` | cmd.listen.start |
| `capture` | cmd.visn.capture |
| `vision_mode` | cmd.vision.mode + extras.mode |
| `pause_vision` | cmd.pause.vision + extras.paused |

### IPC Behavior

**Connects to**:
- Port 6010 (PUB)
- Port 6011 (SUB)

**Subscribes to**:
- `display.state`, `display.text`
- `cmd.vision.mode`, `cmd.pause.vision`
- `visn.object`, `visn.frame`
- `esp32.raw`
- `llm.response`, `tts.speak`
- `remote.event`

**Publishes**:
- `remote.intent` - Incoming intents
- `remote.session` - Session lifecycle

### Session Management

- Sessions have a configurable timeout (default: 300s)
- Last activity tracked via `last_session_touch`
- Session expires if no activity

### Configuration

```yaml
# config/system.yaml
remote_interface:
  enabled: true
  port: 8770
  host: 0.0.0.0
  mjpeg_fps: 10
  session_timeout_sec: 300
```

### Logging

```
/home/pi/smart_car/logs/remote-interface.log
```

---

## uart.service

### Overview

Bridge between ZeroMQ IPC and ESP32 over serial UART. Translates navigation commands to motor control strings and parses sensor data.

### Service Definition

**File**: `systemd/uart.service`

```ini
[Unit]
Description=UART Motor Bridge Service
After=network.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/smart_car
ExecStart=/usr/bin/python3 -m src.uart.motor_bridge
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

### Module

**File**: `src/uart/motor_bridge.py`

### UART Configuration

| Parameter | Value |
|-----------|-------|
| Port | /dev/ttyUSB0 (or /dev/serial0) |
| Baud | 115200 |
| Bits | 8N1 |
| Timeout | 1.0s |

### Command Translation

| nav.command.direction | UART Command |
|-----------------------|--------------|
| forward | FORWARD\n |
| backward | BACKWARD\n |
| left | LEFT\n |
| right | RIGHT\n |
| stopped | STOP\n |
| scan | SCAN\n |

### Sensor Parsing

Input: `DATA:S1:16,S2:12,S3:-1,MQ2:478,SERVO:90,LMOTOR:255,RMOTOR:255,OBSTACLE:0,WARNING:0`

Output:
```json
{
  "s1": 16,
  "s2": 12,
  "s3": -1,
  "mq2": 478,
  "servo": 90,
  "lmotor": 255,
  "rmotor": 255,
  "obstacle": false,
  "warning": false,
  "min_distance": 12,
  "is_safe": true
}
```

### IPC Behavior

**Connects to**:
- Port 6010 (PUB)
- Port 6011 (SUB)

**Subscribes to**:
- `nav.command` - Motor direction commands

**Publishes**:
- `esp32.raw` - Parsed sensor data

### Safety Logic

- Computes `min_distance` from S1, S2, S3
- Sets `is_safe` based on distance thresholds
- Monitors gas sensor (MQ2)

### Configuration

```yaml
# config/system.yaml
uart:
  port: /dev/ttyUSB0
  baudrate: 115200
  timeout: 1.0
  buffer_size: 20
```

### Logging

```
/home/pi/smart_car/logs/uart.log
```

---

## vision.service

### Overview

Real-time object detection using YOLO on Picamera2 frames. Publishes detections and JPEG frames for mobile streaming.

### Service Definition

**File**: `systemd/vision.service`

```ini
[Unit]
Description=Vision Runner Service
After=network.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/smart_car
ExecStart=/home/pi/smart_car/venv-visn-py313/bin/python -m src.vision.vision_runner
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

### Module

**File**: `src/vision/vision_runner.py`

### Vision Modes

| Mode | Description |
|------|-------------|
| detection | Object detection (YOLO) |
| capture | Single frame capture |
| off | Disabled |

### Camera Configuration

| Parameter | Value |
|-----------|-------|
| Library | Picamera2 |
| Resolution | 640×480 |
| FPS | 10 |
| Format | RGB888 → JPEG |

### Model Configuration

| Parameter | Value |
|-----------|-------|
| Model | YOLOv8n |
| Weights | `models/yolov8n.pt` (or onnx) |
| Confidence | 0.5 |
| Classes | COCO 80 classes |

### IPC Behavior

**Connects to**:
- Port 6010 (PUB)
- Port 6011 (SUB)

**Subscribes to**:
- `cmd.pause.vision` - Pause/resume
- `cmd.vision.mode` - Mode change
- `cmd.visn.capture` - Single capture

**Publishes**:
- `visn.object` - Detection results
- `visn.frame` - JPEG frames
- `visn.capture` - Capture completion

### Configuration

```yaml
# config/system.yaml
vision:
  enabled: true
  model: yolov8n
  confidence: 0.5
  target_fps: 10
  resolution: [640, 480]
  camera_backend: picamera2
```

### Virtual Environment

```
/home/pi/smart_car/venv-visn-py313/
```

Uses Python 3.13 due to specific OpenCV/Picamera2 requirements.

### Logging

```
/home/pi/smart_car/logs/vision.log
```

---

## llm.service

### Overview

Azure OpenAI GPT-4 integration for natural language understanding and intent extraction.

### Service Definition

**File**: `systemd/llm.service`

```ini
[Unit]
Description=LLM Runner Service
After=network.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/smart_car
ExecStart=/home/pi/smart_car/venv-llme/bin/python -m src.llm.azure_openai_runner
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

### Module

**File**: `src/llm/azure_openai_runner.py`

### Azure Configuration

| Parameter | Value |
|-----------|-------|
| Provider | Azure OpenAI |
| Model | gpt-4o |
| API Version | 2024-12-01-preview |
| Max Tokens | 150 |
| Temperature | 0.7 |

### System Prompt

The LLM receives a system prompt defining:
- Robot persona
- Available actions (forward, backward, left, right, stopped)
- Response format (JSON with "speak" and "direction")
- Safety constraints

### Request Schema

```json
{
  "text": "user transcription",
  "direction": "current direction",
  "world_context": {
    "last_detection": {...},
    "sensor": {...}
  },
  "context_note": "system_observation_only_last_known_state",
  "source": "orchestrator"
}
```

### Response Schema

```json
{
  "speak": "verbal response to user",
  "direction": "forward|backward|left|right|stopped"
}
```

### IPC Behavior

**Connects to**:
- Port 6010 (PUB)
- Port 6011 (SUB)

**Subscribes to**:
- `llm.request` - Inference requests

**Publishes**:
- `llm.response` - Inference results

### Configuration

```yaml
# config/system.yaml
llm:
  enabled: true
  provider: azure
  model: gpt-4o
  max_tokens: 150
  temperature: 0.7
  timeout: 10
  system_prompt: "..."
```

### Environment Variables

```bash
AZURE_OPENAI_ENDPOINT=...
AZURE_OPENAI_API_KEY=...
AZURE_OPENAI_DEPLOYMENT=...
```

### Virtual Environment

```
/home/pi/smart_car/venv-llme/
```

### Logging

```
/home/pi/smart_car/logs/llm.log
```

---

## tts.service

### Overview

Azure Text-to-Speech synthesis for verbal responses.

### Service Definition

**File**: `systemd/tts.service`

```ini
[Unit]
Description=TTS Runner Service
After=network.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/smart_car
ExecStart=/home/pi/smart_car/venv-ttse/bin/python -m src.tts.azure_tts_runner
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

### Module

**File**: `src/tts/azure_tts_runner.py`

### Azure Configuration

| Parameter | Value |
|-----------|-------|
| Provider | Azure Cognitive Services |
| Voice | en-US-JennyNeural |
| Format | audio-16khz-32kbitrate-mono-mp3 |
| Output | SDL2 audio playback |

### IPC Behavior

**Connects to**:
- Port 6010 (PUB)
- Port 6011 (SUB)

**Subscribes to**:
- `tts.speak` - Speech requests

**Publishes**:
- `tts.speak` - Completion notification (done: true)

### Configuration

```yaml
# config/system.yaml
tts:
  enabled: true
  provider: azure
  voice: en-US-JennyNeural
  rate: "+0%"
  pitch: "+0Hz"
  volume: "+0dB"
  audio_device: "default"
```

### Environment Variables

```bash
AZURE_SPEECH_KEY=...
AZURE_SPEECH_REGION=...
```

### Virtual Environment

```
/home/pi/smart_car/venv-ttse/
```

### Logging

```
/home/pi/smart_car/logs/tts.log
```

---

## voice-pipeline.service

### Overview

Wakeword detection (Porcupine) and speech-to-text (Azure STT) pipeline.

### Service Definition

**File**: `systemd/voice-pipeline.service`

```ini
[Unit]
Description=Voice Pipeline Service
After=network.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/smart_car
ExecStart=/home/pi/smart_car/venv-stte/bin/python -m src.audio.voice_service
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

### Module

**File**: `src/audio/voice_service.py`

### Wakeword Configuration

| Parameter | Value |
|-----------|-------|
| Engine | Porcupine (Picovoice) |
| Keyword | "hey robo" (custom .ppn) |
| Sensitivity | 0.5 |
| Sample Rate | 16 kHz |
| Frame Size | 512 samples |

### STT Configuration

| Parameter | Value |
|-----------|-------|
| Provider | Azure Cognitive Services |
| Language | en-US |
| Mode | Continuous recognition |
| Timeout | 5s silence |

### Audio Pipeline

```
┌─────────────┐    ┌─────────────┐    ┌─────────────┐
│ Microphone  │───►│  Resample   │───►│  Porcupine  │
│  48 kHz     │    │  → 16 kHz   │    │  Wakeword   │
└─────────────┘    └─────────────┘    └──────┬──────┘
                                             │
                                      wakeword detected
                                             │
                                             ▼
                                      ┌─────────────┐
                                      │  Azure STT  │
                                      │  Streaming  │
                                      └──────┬──────┘
                                             │
                                       transcription
                                             │
                                             ▼
                                      ┌─────────────┐
                                      │    IPC      │
                                      │  Publish    │
                                      └─────────────┘
```

### IPC Behavior

**Connects to**:
- Port 6010 (PUB)
- Port 6011 (SUB)

**Subscribes to**:
- `cmd.listen.start` - Start listening
- `cmd.listen.stop` - Stop listening

**Publishes**:
- `ww.detected` - Wakeword trigger
- `stt.transcription` - Speech result

### Configuration

```yaml
# config/system.yaml
wakeword:
  enabled: true
  engine: porcupine
  model_path: models/wakeword/hey-robo_en_raspberry-pi_v3_0_0.ppn
  sensitivity: 0.5

stt:
  enabled: true
  provider: azure
  language: en-US
  timeout: 5
```

### Virtual Environment

```
/home/pi/smart_car/venv-stte/
```

### Logging

```
/home/pi/smart_car/logs/voice-pipeline.log
```

---

## display.service

### Overview

Waveshare OLED display driver for showing system state and messages.

### Service Definition

**File**: `systemd/display.service`

```ini
[Unit]
Description=Display Service
After=network.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/smart_car
ExecStart=/usr/bin/python3 -m src.display.display_runner
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

### Module

**File**: `src/display/display_runner.py`

### Hardware

| Parameter | Value |
|-----------|-------|
| Type | Waveshare 1.3" OLED |
| Resolution | 128×64 |
| Interface | SPI |
| Driver | SSD1306 |

### Display Content

- Phase name (IDLE, LISTENING, etc.)
- Status text
- Simple icons/animations

### IPC Behavior

**Connects to**:
- Port 6011 (SUB)

**Subscribes to**:
- `display.state` - Phase updates
- `display.text` - Text updates

**Publishes**:
- None

### Configuration

```yaml
# config/system.yaml
display:
  enabled: true
  type: waveshare_oled
  width: 128
  height: 64
```

### Logging

```
/home/pi/smart_car/logs/display.log
```

---

## led-ring.service

### Overview

NeoPixel LED ring driver for visual status indication.

### Service Definition

**File**: `systemd/led-ring.service`

```ini
[Unit]
Description=LED Ring Service
After=network.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/smart_car
ExecStart=/usr/bin/python3 -m src.led.led_ring_runner
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

### Module

**File**: `src/led/led_ring_runner.py`

### Hardware

| Parameter | Value |
|-----------|-------|
| Type | NeoPixel WS2812B |
| Count | 16 LEDs |
| GPIO | GPIO18 (PWM) |
| Protocol | WS2812B |

### LED Patterns

| Phase | Pattern |
|-------|---------|
| IDLE | Blue slow pulse |
| LISTENING | Green fast pulse |
| THINKING | Yellow spinning |
| SPEAKING | Purple solid |
| ERROR | Red flash |

### IPC Behavior

**Connects to**:
- Port 6011 (SUB)

**Subscribes to**:
- `display.state` - Phase updates

**Publishes**:
- None

### Configuration

```yaml
# config/system.yaml
led_ring:
  enabled: true
  pin: 18
  count: 16
  brightness: 0.5
```

### Logging

```
/home/pi/smart_car/logs/led-ring.log
```

---

## Service Dependencies

```
                    ┌─────────────────────┐
                    │    orchestrator     │
                    │   (binds 6010/6011) │
                    └──────────┬──────────┘
                               │
           ┌───────────────────┼───────────────────┐
           │                   │                   │
           ▼                   ▼                   ▼
    ┌─────────────┐     ┌─────────────┐     ┌─────────────┐
    │   remote    │     │    uart     │     │   vision    │
    │  interface  │     │   bridge    │     │   runner    │
    └─────────────┘     └─────────────┘     └─────────────┘
                               │
                               ▼
                        ┌─────────────┐
                        │   ESP32     │
                        │  (hardware) │
                        └─────────────┘

    ┌─────────────┐     ┌─────────────┐     ┌─────────────┐
    │    voice    │     │     llm     │     │     tts     │
    │  pipeline   │     │   runner    │     │   runner    │
    └─────────────┘     └─────────────┘     └─────────────┘

    ┌─────────────┐     ┌─────────────┐
    │   display   │     │  led-ring   │
    │   runner    │     │   runner    │
    └─────────────┘     └─────────────┘
```

---

## References

| Document | Purpose |
|----------|---------|
| [04_ipc_and_data_flow.md](04_ipc_and_data_flow.md) | Topic details |
| [06_configuration_reference.md](06_configuration_reference.md) | Full config |
| [09_deployment_and_operations.md](09_deployment_and_operations.md) | Operations |
