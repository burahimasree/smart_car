# Multi-Perspective System Audit

**Date:** December 11, 2025  
**Scope:** Full system audit from Developer, Tester, and Robot perspectives

---

## 🔧 DEVELOPER PERSPECTIVE

### Architecture Flow
```
[Wakeword] ─┐
            ├──> [Orchestrator] ──> [LLM] ──> [TTS] ──> Speaker
[STT] ──────┘       │    │
                    │    └──> [NAV] ──> [motor_bridge] ──> [ESP32] ──> Motors
[Vision] ───────────┘                         │
                                              ▲
[ESP32 Sensors] ─────────────────────────────┘
```

### IPC Topics
| Topic | Direction | Publisher | Subscriber |
|-------|-----------|-----------|------------|
| `ww.detected` | ↑ | porcupine_runner | orchestrator |
| `stt.transcription` | ↑ | whisper_runner | orchestrator |
| `llm.request` | ↓ | orchestrator | gemini_runner |
| `llm.response` | ↑ | gemini_runner | orchestrator |
| `tts.speak` | ↓ | orchestrator | piper_runner |
| `nav.command` | ↓ | orchestrator | motor_bridge |
| `esp32.raw` | ↑ | motor_bridge | orchestrator |
| `visn.object` | ↑ | vision_runner | orchestrator |
| `cmd.pause.vision` | ↓ | orchestrator | vision_runner |

### Issues Found & Fixed

#### 1. ❌ BLOCKING RECV IN MOTOR_BRIDGE (CRITICAL)
**Problem:** `motor_bridge.run()` used blocking `recv_multipart()` which blocked the main loop, preventing ESP32 sensor data processing.

**Before:**
```python
topic, data = self.sub.recv_multipart()  # BLOCKING!
```

**After:**
```python
if self.sub.poll(timeout=50):  # Non-blocking with 50ms timeout
    topic, data = self.sub.recv_multipart(zmq.NOBLOCK)
```

#### 2. ❌ CONFIG COMMAND MISMATCH (CRITICAL)
**Problem:** Config had `forward: "FWD"` but ESP32 expects `FORWARD`.

**Fixed:** Updated `config/system.yaml`:
```yaml
commands:
  forward: "FORWARD"
  backward: "BACKWARD"
  # ... correct commands
```

#### 3. ❌ LLM DIRECTION MISMATCH
**Problem:** LLM prompt said `"back"` but motor_bridge expects `"backward"`.

**Fixed:** Updated `conversation_memory.py` system prompt to use `"backward"`.

#### 4. ❌ VISUAL SERVOING IGNORES OBSTACLES
**Problem:** Orchestrator's visual tracking sent forward commands without checking ESP32 obstacle status.

**Fixed:** Added obstacle check before sending forward in tracking mode:
```python
if direction == "forward" and (self.state.get("esp_obstacle") or self.state.get("esp_warning")):
    direction = "stop"
```

#### 5. ❌ RX LOOP BUSY-SPIN
**Problem:** `_rx_loop()` had no sleep when no data available, wasting CPU.

**Fixed:** Added 5ms sleep when no data.

---

## 🧪 TESTER PERSPECTIVE

### Critical Test Scenarios

#### A. Voice Command Pipeline
| Test | Input | Expected | Latency Budget |
|------|-------|----------|----------------|
| Wakeword | "Hey Genny" | ww.detected published | <500ms |
| STT | Speech audio | stt.transcription published | <3s |
| LLM | "go forward" | nav.command {direction: forward} | <2s |
| TTS | Response text | Audio playback | <1s |

**Total voice→action latency: ~5-7 seconds**

#### B. Collision Avoidance
| Test | Setup | Expected |
|------|-------|----------|
| Emergency stop | Object at 8cm | Motors stop within 50ms |
| Warning block | Object at 15cm | FORWARD command returns BLOCKED |
| Clear zone | Object at 25cm | FORWARD works |
| Backward escape | Obstacle detected | BACKWARD always works |

#### C. Visual Servoing
| Test | Setup | Expected |
|------|-------|----------|
| Track person | Person visible | Robot follows |
| Track + obstacle | Person + wall | Robot stops, doesn't crash |
| Lost target | Person leaves frame | Robot stops |

### Timing Analysis
```
ESP32 Loop:     50ms cycle (20Hz sensor updates)
Vision:         66ms cycle (15 FPS)
Orchestrator:   100ms poll timeout
Motor Bridge:   50ms poll timeout
UART:           5ms RX check interval
```

**Worst case command latency:** 100ms (orchestrator) + 50ms (bridge) + 50ms (ESP32) = 200ms

---

## 🤖 ROBOT PERSPECTIVE (What the Robot "Experiences")

### Sensory Loop (Every 50ms)
```
[ESP32 THINKS]
1. Read S1, S2, S3 ultrasonic distances
2. Check: Any distance < 10cm?
   - YES → EMERGENCY STOP! Tell Pi "ALERT:COLLISION:EMERGENCY_STOP"
   - NO → Continue
3. Check: Any distance < 20cm?
   - YES → Set warning flag, block forward commands
   - NO → All clear, allow movement
4. Send sensor data to Pi
5. Check for commands from Pi
6. If FORWARD command and I'm in warning zone → REFUSE
7. Execute allowed commands
```

### What Robot "Feels" During Voice Command
```
T+0.0s:   [Hear wakeword] "Hey Genny"
T+0.5s:   [Start listening] Vision pauses, display shows "listening"
T+3.0s:   [STT complete] Heard "go forward"
T+3.1s:   [Ask LLM] "What should I do with 'go forward'?"
T+5.0s:   [LLM responds] {direction: "forward", speak: "Moving forward"}
T+5.1s:   [TTS starts] "Moving forward"
T+5.2s:   [NAV command sent] {direction: "forward"}
T+5.25s:  [ESP32 receives FORWARD]
T+5.25s:  [Collision check] Is it safe?
          - If obstacle: "Sorry, I can't. There's something in front."
          - If clear: Motors activate!
T+5.3s:   [Robot moves or blocks]
```

### Obstacle Scenario Timeline
```
T+0:      Robot moving forward at full speed
T+50ms:   ESP32 reads S2=18cm (warning zone!)
T+50ms:   ESP32 sets inWarningZone=true
T+50ms:   ESP32 sends "ALERT:COLLISION:WARNING_ZONE"
T+100ms:  Pi receives alert, updates state
T+150ms:  Next FORWARD command from tracking → BLOCKED
T+200ms:  S2=8cm (emergency!)
T+200ms:  ESP32 immediately calls stopMotors()
T+200ms:  Motors stop (zero latency from sensor read)
T+250ms:  Pi receives emergency alert
T+250ms:  Orchestrator cancels tracking mode
```

### Key Safety Invariants
1. **ESP32 is first responder** - Stops motors in same loop cycle as detection
2. **Pi is backup** - Blocks commands if ESP32 reports obstacle
3. **Backward always works** - Escape route never blocked
4. **Visual tracking respects obstacles** - Won't chase into walls

---

## 📊 Latency Budget Analysis

### Voice Command to Movement
| Stage | Component | Typical | Worst |
|-------|-----------|---------|-------|
| Wake detect | Porcupine | 30ms | 100ms |
| Audio capture | arecord | 2-15s | 15s |
| STT inference | Whisper | 1-3s | 10s |
| LLM call | Gemini API | 500ms | 5s |
| TTS synthesis | Piper | 200ms | 500ms |
| IPC routing | ZMQ | <10ms | 50ms |
| UART TX | Serial | <5ms | 20ms |
| **Total** | | ~5s | ~30s |

### Collision Response
| Stage | Typical | Worst |
|-------|---------|-------|
| Sensor read | 3ms | 10ms |
| Collision check | <1ms | <1ms |
| Motor stop | <1ms | <1ms |
| **Total ESP32** | **~5ms** | **~12ms** |

---

## 🔄 Control Flow Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                        RASPBERRY PI                              │
├─────────────────────────────────────────────────────────────────┤
│  ┌──────────────┐     ┌──────────────┐     ┌──────────────┐    │
│  │  Porcupine   │────▶│ Orchestrator │────▶│   Gemini     │    │
│  │  (wakeword)  │     │   (hub)      │◀────│   (LLM)      │    │
│  └──────────────┘     └──────┬───────┘     └──────────────┘    │
│  ┌──────────────┐            │             ┌──────────────┐    │
│  │   Whisper    │────────────┤             │    Piper     │    │
│  │   (STT)      │            │             │    (TTS)     │    │
│  └──────────────┘            │             └──────────────┘    │
│  ┌──────────────┐            │             ┌──────────────┐    │
│  │    YOLO      │────────────┤             │ motor_bridge │◀───┤
│  │  (vision)    │            │             │   (UART)     │    │
│  └──────────────┘            │             └──────┬───────┘    │
│                              │                    │            │
│         TOPIC_ESP ───────────┴────────────────────┤            │
│                                                   │            │
└───────────────────────────────────────────────────┼────────────┘
                                                    │ UART 115200
┌───────────────────────────────────────────────────┼────────────┐
│                         ESP32                      │            │
├───────────────────────────────────────────────────┴────────────┤
│  ┌─────────────────────────────────────────────────────────┐   │
│  │                    MAIN LOOP (50ms)                      │   │
│  │  1. readDistance(S1), readDistance(S2), readDistance(S3) │   │
│  │  2. checkCollision() ← FIRST PRIORITY                    │   │
│  │  3. Send DATA to Pi                                      │   │
│  │  4. Process commands (with collision blocking)           │   │
│  └─────────────────────────────────────────────────────────┘   │
│                              │                                  │
│           ┌──────────────────┼──────────────────┐              │
│           ▼                  ▼                  ▼              │
│     ┌─────────┐        ┌─────────┐        ┌─────────┐         │
│     │ Sensor1 │        │ Sensor2 │        │ Sensor3 │         │
│     │ (Left)  │        │ (Center)│        │ (Right) │         │
│     └─────────┘        └─────────┘        └─────────┘         │
│                              │                                  │
│                              ▼                                  │
│                     ┌───────────────┐                          │
│                     │ Motor Driver  │                          │
│                     │  L298N        │                          │
│                     └───────────────┘                          │
└─────────────────────────────────────────────────────────────────┘
```

---

## ✅ Issues Fixed in This Audit

| # | Issue | Severity | File | Fix |
|---|-------|----------|------|-----|
| 1 | Blocking recv in motor_bridge | CRITICAL | motor_bridge.py | Use poll() with timeout |
| 2 | Command name mismatch FWD→FORWARD | CRITICAL | system.yaml | Fixed config |
| 3 | Direction "back" vs "backward" | HIGH | conversation_memory.py | Unified to "backward" |
| 4 | Visual tracking ignores obstacles | HIGH | orchestrator.py | Added obstacle check |
| 5 | RX loop CPU spin | MEDIUM | motor_bridge.py | Added 5ms sleep |
| 6 | Orchestrator unaware of ESP32 | MEDIUM | orchestrator.py | Added TOPIC_ESP handler |

---

## 🎯 Remaining Recommendations

1. **Add scan command to LLM** - Robot can say "let me look around" and trigger SCAN
2. **Gas sensor alerting** - MQ2 readings not used yet
3. **Battery monitoring** - ESP32 could report battery level
4. **Stuck detection** - If motors running but no position change (needs encoders)
5. **Recovery behaviors** - What to do when stuck (backup, turn, retry)
