# 🚗 Smart Car - Complete System Architecture

> Comprehensive documentation of all services, integrations, LED states, timeouts, and message flows.

---

## 📋 Table of Contents

- [System Overview](#system-overview)
- [Services](#services)
- [IPC Architecture](#ipc-architecture)
- [Message Flow](#message-flow)
- [Microphone Sharing](#microphone-sharing)
- [LED Ring States](#led-ring-states)
- [Display Face States](#display-face-states)
- [Timeouts & Timing](#timeouts--timing)
- [Conversation Memory](#conversation-memory)
- [Safety Mechanisms](#safety-mechanisms)
- [Quick Start](#quick-start)

---

## System Overview

### High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         SMART CAR ARCHITECTURE                          │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│    ┌──────────┐     ┌──────────┐     ┌──────────┐     ┌──────────┐     │
│    │   MIC    │     │  CAMERA  │     │   TFT    │     │   LED    │     │
│    │  (USB)   │     │  (CSI)   │     │ DISPLAY  │     │   RING   │     │
│    └────┬─────┘     └────┬─────┘     └────┬─────┘     └────┬─────┘     │
│         │                │                │                │           │
│         ▼                ▼                ▼                ▼           │
│    ┌──────────┐     ┌──────────┐     ┌──────────┐     ┌──────────┐     │
│    │  VOICE   │     │  VISION  │     │ DISPLAY  │     │ LED RING │     │
│    │ PIPELINE │     │  RUNNER  │     │  RUNNER  │     │ SERVICE  │     │
│    └────┬─────┘     └────┬─────┘     └────┬─────┘     └────┬─────┘     │
│         │                │                │                │           │
│         └────────────────┴────────┬───────┴────────────────┘           │
│                                   │                                     │
│                          ┌────────▼────────┐                           │
│                          │   ORCHESTRATOR  │                           │
│                          │  (State Machine)│                           │
│                          └────────┬────────┘                           │
│                                   │                                     │
│              ┌────────────────────┼────────────────────┐               │
│              │                    │                    │               │
│         ┌────▼─────┐        ┌─────▼────┐        ┌─────▼────┐          │
│         │   LLM    │        │   TTS    │        │   UART   │          │
│         │ (Gemini) │        │ (Piper)  │        │  Bridge  │          │
│         └──────────┘        └──────────┘        └────┬─────┘          │
│                                                      │                 │
│                                                 ┌────▼─────┐          │
│                                                 │  ESP32   │          │
│                                                 │ (Motors) │          │
│                                                 └──────────┘          │
└─────────────────────────────────────────────────────────────────────────┘
```

### Hardware Components

| Component | Model | Connection | Purpose |
|-----------|-------|------------|---------|
| SBC | Raspberry Pi 4 (4GB+) | - | Main compute |
| Microphone | USB Audio Device | USB (hw:3,0) | Voice input |
| Display | Waveshare 3.5" TFT | SPI (/dev/fb0) | Face UI |
| LED Ring | NeoPixel 8-pixel | GPIO D12 | Status indication |
| Motor Controller | ESP32 | UART (/dev/serial0) | Motor control |
| Camera | Pi Camera / USB | CSI / USB | Object detection |

---

## Services

### All Services Overview

| Service | Systemd Unit | Python Module | Virtual Env | Purpose |
|---------|--------------|---------------|-------------|---------|
| **Orchestrator** | `orchestrator.service` | `src.core.orchestrator` | stte | Central event router & state machine |
| **Voice Pipeline** | `voice-pipeline.service` | `src.audio.unified_voice_pipeline` | stte | Wakeword + STT (unified) |
| **LLM** | `llm.service` | `src.llm.llm_runner` | llme | Gemini API for natural language |
| **TTS** | `tts.service` | `src.tts.piper_runner` | ttse | Text-to-speech (Piper) |
| **Vision** | `vision.service` | `src.vision.vision_runner` | visn-py313 | YOLO object detection |
| **UART** | `uart.service` | `src.uart.uart_runner` | stte | ESP32 motor control |
| **Display** | `display.service` | `src.ui.face_fb` | visn-py313 | Kawaii face on TFT |
| **LED Ring** | `led-ring.service` | `src.piled.led_ring_service` | visn-py313 | NeoPixel status ring |

### Service Dependencies

```
                         ┌──────────────────┐
                         │   orchestrator   │
                         │   (must start    │
                         │    first)        │
                         └────────┬─────────┘
                                  │
        ┌────────────┬────────────┼────────────┬────────────┐
        ▼            ▼            ▼            ▼            ▼
   ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐
   │  voice  │ │   llm   │ │   tts   │ │  vision │ │   uart  │
   │pipeline │ │         │ │         │ │         │ │         │
   └─────────┘ └─────────┘ └─────────┘ └─────────┘ └─────────┘
        │
   Conflicts with:
   - wakeword.service (legacy)
   - stt.service (legacy)
```

### Virtual Environments

```
.venvs/
├── stte/        # STT + Wakeword (pvporcupine, faster-whisper)
├── ttse/        # TTS (piper-tts)
├── llme/        # LLM (google-generativeai)
└── visn-py313/  # Vision + Display (opencv, pygame, neopixel)
```

---

## IPC Architecture

### ZeroMQ PUB/SUB Dual-Channel Bus

| Channel | Address | Direction | Purpose |
|---------|---------|-----------|---------|
| **Upstream** | `tcp://127.0.0.1:6010` | Services → Orchestrator | Events |
| **Downstream** | `tcp://127.0.0.1:6011` | Orchestrator → Services | Commands |

### ZeroMQ Topics

```python
# ═══════════════════════════════════════════════════════════════════════
# UPSTREAM TOPICS (Events: Services → Orchestrator)
# ═══════════════════════════════════════════════════════════════════════
TOPIC_WW_DETECTED      = b"ww.detected"        # Wakeword triggered
TOPIC_STT              = b"stt.transcription"  # Speech-to-text result
TOPIC_LLM_RESP         = b"llm.response"       # LLM response
TOPIC_TTS              = b"tts.speak"          # TTS completion
TOPIC_VISN             = b"visn.object"        # Vision detection
TOPIC_ESP              = b"esp32.raw"          # ESP32 sensor data
TOPIC_HEALTH           = b"system.health"      # Health status

# ═══════════════════════════════════════════════════════════════════════
# DOWNSTREAM TOPICS (Commands: Orchestrator → Services)
# ═══════════════════════════════════════════════════════════════════════
TOPIC_LLM_REQ          = b"llm.request"        # Request LLM response
TOPIC_NAV              = b"nav.command"        # Motor command
TOPIC_CMD_LISTEN_START = b"cmd.listen.start"   # Start STT capture
TOPIC_CMD_LISTEN_STOP  = b"cmd.listen.stop"    # Stop STT capture
TOPIC_CMD_PAUSE_VISION = b"cmd.pause.vision"   # Pause/resume vision
TOPIC_CMD_VISN_CAPTURE = b"cmd.visn.capture"   # Request vision snapshot
TOPIC_DISPLAY_STATE    = b"display.state"      # UI state change
TOPIC_CMD_TTS_SPEAK    = b"cmd.tts.speak"      # Request TTS
```

### Message Payload Examples

```json
// ww.detected
{"timestamp": 1705312800, "keyword": "hey genny", "confidence": 0.92, "source": "porcupine"}

// stt.transcription
{"text": "go forward", "confidence": 0.85, "language": "en"}

// llm.request
{"text": "go forward", "direction": "stopped", "track": null, "vision": null}

// llm.response
{"json": {"speak": "Moving forward!", "direction": "forward", "track": ""}, "text": "Moving forward!"}

// nav.command
{"direction": "forward", "target": "person"}

// esp32.raw
{"data": {"s1": 45, "s2": 30, "s3": 60, "obstacle": false, "warning": true, "min_distance": 30}}

// display.state
{"state": "listening"}  // idle, listening, thinking, speaking, tracking, error
```

---

## Message Flow

### Complete Voice Interaction Sequence

```
┌─────────────────────────────────────────────────────────────────────────┐
│                   COMPLETE VOICE INTERACTION FLOW                       │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  ══════════════════════════════════════════════════════════════════    │
│  PHASE 1: WAKE WORD DETECTION                                          │
│  ══════════════════════════════════════════════════════════════════    │
│                                                                         │
│  [USB MIC] → UnifiedAudioCapture → Porcupine → ww.detected             │
│                        │                            │                   │
│                        │                            ▼                   │
│                   Ring Buffer              ┌──────────────┐            │
│                   (10s @ 16kHz)            │ ORCHESTRATOR │            │
│                                            └──────┬───────┘            │
│                                                   │                     │
│                                     ┌─────────────┼─────────────┐      │
│                                     ▼             ▼             ▼      │
│                              stt_active=True  vision_paused  display   │
│                                              cmd.pause.vision  .state  │
│                                                   │         "listening"│
│                                                   ▼                     │
│                                            LED: Blue spinning          │
│                                            FACE: Attentive             │
│                                                                         │
│  ══════════════════════════════════════════════════════════════════    │
│  PHASE 2: SPEECH CAPTURE & TRANSCRIPTION                               │
│  ══════════════════════════════════════════════════════════════════    │
│                                                                         │
│  [Voice Pipeline] captures audio until:                                │
│    - Silence detected (1200ms @ threshold 0.20)                        │
│    - Max duration reached (15s)                                        │
│    - Timeout triggered (15s)                                           │
│                        │                                                │
│                        ▼                                                │
│              faster-whisper (tiny.en, int8, CPU)                       │
│                        │                                                │
│                        ▼                                                │
│              stt.transcription {text: "go forward", confidence: 0.85}  │
│                        │                                                │
│                        ▼                                                │
│                 ORCHESTRATOR                                            │
│                 ├─ Validate: 0.85 > 0.5 (min_confidence) ✓            │
│                 ├─ Store: last_transcript = "go forward"               │
│                 ├─ Publish: llm.request                                │
│                 ├─ Publish: cmd.listen.stop                            │
│                 └─ Publish: display.state = "thinking"                 │
│                        │                                                │
│                        ▼                                                │
│                 LED: Purple wave                                        │
│                 FACE: Thinking expression                               │
│                                                                         │
│  ══════════════════════════════════════════════════════════════════    │
│  PHASE 3: LLM PROCESSING (with Conversation Memory)                    │
│  ══════════════════════════════════════════════════════════════════    │
│                                                                         │
│  [LLM Runner] receives llm.request                                     │
│         │                                                               │
│         ▼                                                               │
│  ConversationMemory.build_context():                                   │
│  ┌─────────────────────────────────────────────────────────────┐      │
│  │ [System Prompt]     ~300 tokens                             │      │
│  │   "You are GENNY, an AI assistant controlling a robot..."   │      │
│  │                                                             │      │
│  │ [Robot State]       ~100 tokens                             │      │
│  │   "Navigation: stopped, Vision: person (92%)"               │      │
│  │                                                             │      │
│  │ [Conversation History] ~500 tokens                          │      │
│  │   User: "what do you see?"                                  │      │
│  │   GENNY: {"speak": "I see a person", ...}                   │      │
│  │                                                             │      │
│  │ [Current Query]     ~100 tokens                             │      │
│  │   "go forward"                                              │      │
│  └─────────────────────────────────────────────────────────────┘      │
│         │                                                               │
│         ▼                                                               │
│  Gemini API (gemini-2.5-flash, temp=0.2)                               │
│         │                                                               │
│         ▼                                                               │
│  Response: {"speak": "Moving forward!", "direction": "forward"}        │
│         │                                                               │
│         ▼                                                               │
│  ConversationMemory.add_assistant_message()                            │
│         │                                                               │
│         ▼                                                               │
│  Publish: llm.response                                                  │
│                                                                         │
│  ══════════════════════════════════════════════════════════════════    │
│  PHASE 4: ACTION EXECUTION                                             │
│  ══════════════════════════════════════════════════════════════════    │
│                                                                         │
│  ORCHESTRATOR.on_llm():                                                │
│  ├─ Extract direction = "forward"                                      │
│  ├─ Publish: nav.command {direction: "forward"}                        │
│  ├─ Publish: tts.speak {text: "Moving forward!"}                       │
│  ├─ Set: tts_pending = True                                            │
│  └─ Publish: display.state = "speaking"                                │
│         │                                                               │
│         ├──────────────────┬──────────────────┐                        │
│         ▼                  ▼                  ▼                        │
│     UART Bridge       TTS Runner         LED Ring                      │
│         │                  │                  │                        │
│         ▼                  ▼                  ▼                        │
│  "FORWARD\n" → ESP32   Piper → aplay    Green pulsing                 │
│                                                                         │
│  ══════════════════════════════════════════════════════════════════    │
│  PHASE 5: COMPLETION & RETURN TO IDLE                                  │
│  ══════════════════════════════════════════════════════════════════    │
│                                                                         │
│  [TTS Runner] → tts.speak {done: true}                                 │
│         │                                                               │
│         ▼                                                               │
│  ORCHESTRATOR.on_tts():                                                │
│  ├─ Set: tts_pending = False                                           │
│  ├─ Publish: cmd.pause.vision {pause: false}                           │
│  └─ Publish: display.state = "idle"                                    │
│         │                                                               │
│         ▼                                                               │
│  LED: Cyan breathing                                                    │
│  FACE: Happy expression                                                 │
│  VISION: Resumes object detection                                       │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

### Orchestrator State Machine

```python
# Orchestrator internal state dictionary
state = {
    "vision_paused": False,      # Vision detection paused during listening
    "stt_active": False,         # Currently recording speech
    "llm_pending": False,        # Waiting for LLM response
    "tts_pending": False,        # TTS playback in progress
    "last_transcript": "",       # Last STT result
    "last_visn": None,           # Last vision detection
    "stt_started_ts": None,      # STT session start time (for timeout)
    "vision_capture_pending": None,  # Pending vision capture request
    "vision_request_text": "",   # Text associated with vision request
    "tracking_target": None,     # Visual servoing target (e.g., "person")
    "last_nav_direction": "stopped",  # Last navigation direction
    
    # ESP32 sensor state (collision awareness)
    "esp_obstacle": False,       # ESP32 detected obstacle
    "esp_warning": False,        # ESP32 warning zone
    "esp_min_distance": -1,      # Minimum sensor distance (cm)
}
```

---

## Microphone Sharing

### The Problem

Raspberry Pi USB microphones can only be opened by **one process at a time**. Running separate wakeword and STT services causes resource conflicts.

### The Solution: UnifiedVoicePipeline

The recommended approach uses `UnifiedAudioCapture` with a ring buffer:

```
┌─────────────────────────────────────┐
│         USB MICROPHONE              │
│         (Physical Device)           │
└─────────────────┬───────────────────┘
                  │
                  ▼
┌─────────────────────────────────────┐
│     UnifiedAudioCapture             │
│     (SINGLE OWNER - Thread)         │
│     Opens PyAudio ONCE              │
│     Writes to Ring Buffer           │
└─────────────────┬───────────────────┘
                  │
    ┌─────────────┼─────────────┐
    │             │             │
    ▼             ▼             ▼
┌─────────────┐ ┌─────────────┐ ┌─────────────┐
│ Wakeword    │ │   STT       │ │   Future    │
│ Consumer    │ │ Consumer    │ │  Consumer   │
│ read_idx A  │ │ read_idx B  │ │ read_idx C  │
│ Porcupine   │ │ faster-     │ │    ...      │
│             │ │ whisper     │ │             │
└─────────────┘ └─────────────┘ └─────────────┘
```

### Key Features

| Feature | Description |
|---------|-------------|
| **Single PyAudio instance** | No resource conflicts |
| **Lock-free ring buffer** | 10 seconds @ 16kHz (160,000 samples) |
| **Independent read pointers** | Each consumer reads at own pace |
| **Automatic resampling** | 48kHz hardware → 16kHz for Porcupine/Whisper |
| **Graceful degradation** | Slow consumers skip old audio |

### Alternative Approaches Comparison

| Approach | File | Latency | Production Ready |
|----------|------|---------|------------------|
| **UnifiedVoicePipeline** | `src/audio/unified_voice_pipeline.py` | ~30ms | ✅ Yes |
| **AudioManager** | `src/audio/audio_manager.py` | ~50ms | ⚠️ Partial |
| **ALSA dsnoop** | `tools/fix_audio_config.py` | ~30ms | ✅ Yes |
| **Direct PyAudio** | `scripts/test_wakeword_*.py` | ~30ms | ❌ Test only |

---

## LED Ring States

The 8-pixel NeoPixel ring (GPIO D12) provides visual status feedback.

### State Reference

| State | Color | RGB Values | Animation | Trigger |
|-------|-------|------------|-----------|---------|
| **idle** | Cyan | `(0, 8-48, 13-53)` | Slow breathing pulse (1.5Hz sine) | System ready |
| **wakeword** | Amber | `(120, 70, 0)` | Blinking on/off (8Hz) | Wake word detected |
| **listening** | Blue | `(0, 0, 25-145)` | Spinning chase (6 pos/sec) | Recording speech |
| **llm** | Purple | `(50-90, 5-20, 30-150)` | Wave pattern (2Hz) | Waiting for Gemini |
| **tts_queue** | Yellow-Green | `(20-100, 40-120, 0)` | Sweep loader (5/sec) | TTS queued |
| **speaking** | Green | `(0, 40-190, 10)` | Fast pulse (4Hz sine) | TTS playing |
| **error** | Red | `(150, 0, 0)` | Fast blink (4Hz) | Health check failed |

### State Transition Diagram

```
                              ┌───────────────────┐
                              │       idle        │
                              │   (cyan breath)   │
                              └─────────┬─────────┘
                                        │
                                 [ww.detected]
                                        │
                                        ▼
                              ┌───────────────────┐
                              │     wakeword      │
                              │  (amber blink)    │
                              │    hold: 1.2s     │
                              └─────────┬─────────┘
                                        │
                                  [timeout]
                                        │
                                        ▼
                              ┌───────────────────┐
                              │    listening      │
                              │  (blue spinner)   │
                              └─────────┬─────────┘
                                        │
                              [stt.transcription]
                                        │
                                        ▼
                              ┌───────────────────┐
                              │       llm         │
                              │  (purple wave)    │
                              └─────────┬─────────┘
                                        │
                                [llm.response]
                                        │
                                        ▼
                              ┌───────────────────┐
                              │    tts_queue      │
                              │ (yellow-green)    │
                              │    hold: 0.5s     │
                              └─────────┬─────────┘
                                        │
                                  [timeout]
                                        │
                                        ▼
                              ┌───────────────────┐
                              │     speaking      │
                              │  (green pulse)    │
                              └─────────┬─────────┘
                                        │
                               [tts.speak done]
                                        │
                                        ▼
                              ┌───────────────────┐
                              │       idle        │
                              └───────────────────┘
```

### Animation Code Reference

```python
# From src/piled/led_ring_service.py

def _render_idle(self, now: float) -> None:
    phase = 0.5 + 0.5 * math.sin(now * 1.5)  # 1.5Hz breathing
    level = int(8 + 40 * phase)
    self.hw.fill((0, level, level + 5))  # Cyan

def _render_listening(self, now: float) -> None:
    pos = (now * 6) % self.hw.pixel_count  # 6 positions/sec spinner
    colors = []
    for idx in range(self.hw.pixel_count):
        delta = min((idx - pos) % 8, (pos - idx) % 8)
        fade = max(0.0, 1.0 - delta / 2.5)
        value = int(25 + 120 * fade)
        colors.append((0, 0, value))  # Blue
    self.hw.show(colors)
```

---

## Display Face States

The Waveshare 3.5" TFT (480x320) displays a kawaii-style animated face.

### Expression Reference

| State | Pupil Position | Mouth | Blush | Eye Scale | Description |
|-------|----------------|-------|-------|-----------|-------------|
| **BASE** | `(0.0, 0.0)` | soft_smile | 100% | 0.95 | Default happy |
| **HAPPY** | `(0.0, -0.15)` | beam | 130% | 1.0 | Extra happy |
| **LISTENING** | `(0.0, 0.1)` | open_small | 80% | 1.0 | Attentive |
| **THINKING** | `(0.2, -0.1)` | pursed | 60% | 0.9 | Processing |
| **SPEAKING** | `(0.0, 0.0)` | open_talk | 100% | 1.0 | Animated mouth |
| **SURPRISED** | `(0.0, -0.2)` | open_wide | 120% | 1.2 | Shocked |
| **ERROR** | `(0.0, 0.2)` | frown | 0% | 0.85 | Sad |

### Display Configuration

```yaml
# config/system.yaml
display:
  resolution: [480, 320]
  rotation: 90
  spi_bus: 0
  spi_device: 0
```

### Rendering Pipeline

```python
# From src/ui/face_fb.py

# 1. Render to pygame Surface (RGB888)
surface = pygame.Surface((480, 320))
draw_face(surface, state="LISTENING")

# 2. Convert to RGB565
rgb565 = _surface_to_rgb565(surface)

# 3. Write to framebuffer via mmap
fb_writer.write_surface(surface)
```

---

## Timeouts & Timing

### Complete Timing Reference

#### Voice Pipeline

| Parameter | Value | Config Key | Purpose |
|-----------|-------|------------|---------|
| STT Timeout | **15.0s** | `stt.timeout_seconds` | Max listen session before auto-cancel |
| Silence Duration | **1200ms** | `stt.silence_duration_ms` | Silence to end speech capture |
| Max Capture | **15s** | `stt.max_capture_seconds` | Max recording duration |
| Silence Threshold | **0.20** | `stt.silence_threshold` | Audio RMS level for silence |
| Min Confidence | **0.5** | `stt.min_confidence` | Discard low-quality STT |
| Wakeword Frame | **30ms** | `audio.wakeword_frame_ms` | Porcupine chunk size (~512 samples) |
| STT Chunk | **500ms** | `audio.stt_chunk_ms` | Whisper chunk size |
| HW Buffer | **20ms** | `audio.hw_buffer_ms` | ALSA buffer size |

#### LLM / Conversation Memory

| Parameter | Value | Config Key | Purpose |
|-----------|-------|------------|---------|
| Memory Turns | **10** | `llm.memory_max_turns` | Max conversation history |
| Conversation Timeout | **120s** | `llm.conversation_timeout_s` | Memory reset on inactivity |
| Temperature | **0.2** | `llm.temperature` | Response randomness |
| Top-P | **0.9** | `llm.top_p` | Nucleus sampling |

#### Orchestrator

| Parameter | Value | Location | Purpose |
|-----------|-------|----------|---------|
| Auto-trigger Interval | **60s** | `orchestrator.auto_trigger_interval` | Force listen if idle |
| Poll Timeout | **100ms** | hardcoded | ZMQ event loop poll |

#### LED Ring Timing

| Animation | Hold Time | Fallback State |
|-----------|-----------|----------------|
| Wakeword | **1.2s** | listening |
| TTS Queue | **0.5s** | speaking |
| Render Rate | **60 FPS** | - |

#### Systemd Services

| Setting | Value | Service | Purpose |
|---------|-------|---------|---------|
| RestartSec | **3s** | all | Wait before restart |
| TimeoutStartSec | **60s** | voice-pipeline | Startup timeout |
| MemoryMax | **512MB** | voice-pipeline | Memory limit |
| CPUQuota | **80%** | voice-pipeline | CPU limit |

#### UART / Motor Control

| Parameter | Value | Purpose |
|-----------|-------|---------|
| Baud Rate | **115200** | Serial communication |
| Read/Write Timeout | **1.0s** | Serial timeout |
| Stop Distance | **10cm** | Emergency stop |
| Warning Distance | **20cm** | Slow down |

---

## Conversation Memory

### Memory Architecture

The LLM is stateless (cloud API), so local memory management is required:

```
┌─────────────────────────────────────────────────────────────┐
│                    CONTEXT WINDOW (~1200 tokens)            │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  [System Prompt]     ~300 tokens                            │
│    "You are GENNY, an AI assistant controlling a robot..."  │
│    "RESPONSE FORMAT: {speak, direction, track}"             │
│                                                             │
│  [Robot State]       ~100 tokens                            │
│    "Navigation: forward"                                    │
│    "Tracking: person"                                       │
│    "Vision: person (confidence: 92%)"                       │
│                                                             │
│  [Summary]           ~200 tokens (compressed old turns)     │
│    "User asked about movement... Assistant responded..."    │
│                                                             │
│  [Recent History]    ~500 tokens (last 10 exchanges)        │
│    User: "what do you see?"                                 │
│    GENNY: {"speak": "I see a person ahead", ...}            │
│    User: "follow them"                                      │
│    GENNY: {"speak": "Tracking person", "track": "person"}   │
│                                                             │
│  [Current Query]     ~100 tokens                            │
│    "go forward"                                             │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### Conversation States

```python
class ConversationState(Enum):
    IDLE = auto()       # No active conversation
    ACTIVE = auto()     # Mid-conversation (processing)
    FOLLOW_UP = auto()  # Expecting user response (within timeout)
```

### Memory Lifecycle

1. **New conversation**: User speaks after 120s timeout
2. **Active**: Processing user message → waiting for LLM
3. **Follow-up**: LLM responded, expecting user follow-up
4. **Timeout**: 120s inactivity → clear history, reset to IDLE

---

## Safety Mechanisms

### Collision Avoidance (ESP32 → Pi)

```python
# ESP32 sensor data published on esp32.raw topic
{
    "data": {
        "s1": 45,           # Ultrasonic sensor 1 (cm)
        "s2": 30,           # Ultrasonic sensor 2 (cm)
        "s3": 60,           # Ultrasonic sensor 3 (cm)
        "mq2": 0,           # Gas sensor
        "obstacle": False,  # True if any sensor < 10cm
        "warning": True,    # True if any sensor < 20cm
        "min_distance": 30  # Minimum of all sensors
    }
}
```

### Visual Servoing Safety

```python
# In orchestrator.on_visn()
def on_visn(self, payload):
    target = self.state.get("tracking_target")
    if target and target in payload.get("label", "").lower():
        direction = calculate_direction(payload["bbox"])
        
        # Safety check: don't move forward if obstacle detected
        if direction == "forward":
            if self.state["esp_obstacle"] or self.state["esp_warning"]:
                logger.warning("Visual servoing blocked by obstacle")
                direction = "stop"
        
        self._send_nav(direction)
```

### Emergency Stop

```python
# ESP32 collision alert handling
def on_esp(self, payload):
    alert = payload.get("alert")
    if alert == "COLLISION":
        if "EMERGENCY" in payload.get("alert_data", ""):
            logger.critical("ESP32 EMERGENCY STOP!")
            self.state["esp_obstacle"] = True
            
            # Cancel visual tracking
            if self.state.get("tracking_target"):
                self.state["tracking_target"] = None
                self._send_display_state("idle")
```

---

## Quick Start

### Start All Services

```bash
# Enable services
sudo systemctl enable orchestrator voice-pipeline llm tts vision uart led-ring display

# Start services
sudo systemctl start orchestrator voice-pipeline llm tts vision uart led-ring display
```

### Check Status

```bash
# All services status
sudo systemctl status orchestrator voice-pipeline llm tts vision uart

# View logs
tail -f /home/dev/smart_car/logs/orchestrator.log
tail -f /home/dev/smart_car/logs/voice_pipeline.log

# Follow all logs
tail -f /home/dev/smart_car/logs/*.log
```

### Test Individual Components

```bash
cd /home/dev/smart_car

# Test wakeword
.venvs/stte/bin/python scripts/test_wakeword_loop.py

# Test TTS
.venvs/ttse/bin/python scripts/send_tts.py "Hello, I am Genny"

# Simulate wakeword event
.venvs/stte/bin/python -m src.wakeword.porcupine_runner --sim

# Test vision
.venvs/visn-py313/bin/python -m src.vision.vision_runner --config config/system.yaml

# Test UART
.venvs/stte/bin/python scripts/test_uart_nav.py
```

### Debug ZeroMQ Traffic

```bash
# Monitor all upstream messages
python3 -c "
import zmq
ctx = zmq.Context()
sub = ctx.socket(zmq.SUB)
sub.connect('tcp://127.0.0.1:6010')
sub.setsockopt(zmq.SUBSCRIBE, b'')
while True:
    topic, data = sub.recv_multipart()
    print(f'{topic.decode()}: {data.decode()[:100]}...')
"
```

### Check Audio Devices

```bash
# List ALSA capture devices
arecord -l

# List PyAudio devices
python3 -c "
import pyaudio
p = pyaudio.PyAudio()
for i in range(p.get_device_count()):
    info = p.get_device_info_by_index(i)
    if info['maxInputChannels'] > 0:
        print(f'{i}: {info[\"name\"]} ({info[\"maxInputChannels\"]} ch)')
"
```

---

## Configuration Reference

### Main Configuration: `config/system.yaml`

```yaml
# ═══════════════════════════════════════════════════════════════════════
# IPC CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════
ipc:
  upstream: tcp://127.0.0.1:6010     # Events: services → orchestrator
  downstream: tcp://127.0.0.1:6011   # Commands: orchestrator → services

# ═══════════════════════════════════════════════════════════════════════
# AUDIO CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════
audio:
  use_unified_pipeline: true         # Use UnifiedVoicePipeline (recommended)
  use_audio_manager: false           # Legacy AudioManager mode
  hw_sample_rate: 48000              # Hardware capture rate
  preferred_device_substring: "USB Audio"
  wakeword_frame_ms: 30              # Porcupine frame size
  stt_chunk_ms: 500                  # Whisper chunk size

# ═══════════════════════════════════════════════════════════════════════
# WAKEWORD CONFIGURATION (Porcupine)
# ═══════════════════════════════════════════════════════════════════════
wakeword:
  engine: porcupine
  access_key: ${ENV:PV_ACCESS_KEY}
  sensitivity: 0.75
  model: ${PROJECT_ROOT}/models/wakeword/hey_robo.ppn

# ═══════════════════════════════════════════════════════════════════════
# STT CONFIGURATION (faster-whisper)
# ═══════════════════════════════════════════════════════════════════════
stt:
  engine: faster_whisper
  sample_rate: 16000
  silence_threshold: 0.20
  silence_duration_ms: 1200
  max_capture_seconds: 15
  min_confidence: 0.5
  timeout_seconds: 15.0
  engines:
    faster_whisper:
      model: tiny.en
      compute_type: int8
      device: cpu

# ═══════════════════════════════════════════════════════════════════════
# TTS CONFIGURATION (Piper)
# ═══════════════════════════════════════════════════════════════════════
tts:
  voice: en-us-amy-medium
  model_path: ${PROJECT_ROOT}/models/piper/en_US-amy-medium.onnx

# ═══════════════════════════════════════════════════════════════════════
# LLM CONFIGURATION (Google Gemini)
# ═══════════════════════════════════════════════════════════════════════
llm:
  engine: gemini
  gemini_model: gemini-2.5-flash
  gemini_api_key: ${ENV:GEMINI_API_KEY}
  temperature: 0.2
  memory_max_turns: 10
  conversation_timeout_s: 120

# ═══════════════════════════════════════════════════════════════════════
# VISION CONFIGURATION (YOLO)
# ═══════════════════════════════════════════════════════════════════════
vision:
  model_path_onnx: ${PROJECT_ROOT}/models/vision/yolo11n.onnx
  confidence: 0.25
  target_fps: 15

# ═══════════════════════════════════════════════════════════════════════
# NAVIGATION / MOTOR CONTROL
# ═══════════════════════════════════════════════════════════════════════
nav:
  uart_device: /dev/serial0
  baud_rate: 115200
  commands:
    forward: "FORWARD"
    backward: "BACKWARD"
    left: "LEFT"
    right: "RIGHT"
    stop: "STOP"
```

### Environment Variables (`.env`)

```bash
# Picovoice (wakeword)
PV_ACCESS_KEY=your_picovoice_access_key

# Google Gemini (LLM)
GEMINI_API_KEY=your_gemini_api_key

# Azure Speech (optional)
AZURE_SPEECH_KEY=your_azure_key
AZURE_SPEECH_REGION=eastus

# Project paths
PROJECT_ROOT=/home/dev/smart_car
```

---

*Generated from codebase analysis - January 2026*
