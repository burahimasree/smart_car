# Architecture Comparison: Smart Car vs Industry Voice Assistants

## Executive Summary

This document compares the Smart Car voice assistant orchestration with three major open-source projects:
- **Rhasspy** (MQTT/Hermes protocol)
- **OVOS (OpenVoiceOS)** (Message bus + skill system)
- **Wyoming Satellite** (Event-driven state machine)

## 1. Microphone Sharing Architecture

### The Problem
Multiple services need the microphone:
- **Wakeword detection** (continuous listening)
- **Speech-to-text** (triggered recording)
- **TTS feedback** (need to mute during playback)

### Industry Solutions

| Project | Pattern | Pros | Cons |
|---------|---------|------|------|
| **Rhasspy** | MQTT Audio Streaming | Decoupled services, distributed | Network overhead, latency |
| **OVOS** | Single AudioInput Module | Centralized, clean API | Tight coupling |
| **Wyoming** | Single Process + State Machine | Low latency, simple | Less flexible |
| **Smart Car** | Unified Pipeline ✅ | Best of all: single owner, state machine, low latency | N/A |

### Smart Car Architecture (Current)
```
┌─────────────────────────────────────────────────────────────┐
│              UnifiedVoicePipeline (Single Process)          │
├─────────────────────────────────────────────────────────────┤
│  [PyAudio Stream] → [Porcupine] → [Wake Detected?]         │
│                           │                                 │
│                           ↓ YES                             │
│                    [faster-whisper STT]                     │
│                           │                                 │
│                           ↓                                 │
│                    [ZMQ: stt.result]                        │
└─────────────────────────────────────────────────────────────┘
```

**Assessment**: Your unified pipeline is the CORRECT solution. It matches Wyoming's approach but with lower latency (no TCP streaming).

---

## 2. State Machine Comparison

### Wyoming Satellite States
```python
class State(StrEnum):
    NOT_STARTED = "not_started"
    STARTING = "starting"
    STARTED = "started"
    RESTARTING = "restarting"
    STOPPING = "stopping"
    STOPPED = "stopped"

# Runtime states
is_streaming: bool  # Gates audio forwarding to ASR
```

### OVOS Intent Pipeline
```
utterance → converse → adapt → padatious → common_qa → fallback
                ↓
         [Active Skill Session]
                ↓
         [ConverseService - 300s timeout]
```

### Your Current State (orchestrator.py)
```python
self._state = {
    "vision_paused": True,
    "stt_active": False,
    "llm_pending": False,
    "tts_pending": False,
    "last_transcript": "",
    "tracking_target": None,
}
```

### Gap Analysis

| Feature | Wyoming | OVOS | Smart Car |
|---------|---------|------|-----------|
| State enum | ✅ | ✅ | ⚠️ Dict flags |
| Multi-turn dialog | ✅ | ✅ | ❌ Missing |
| Conversation timeout | ✅ | ✅ (300s) | ❌ Missing |
| Memory/context | N/A (server) | ✅ Session | ❌ Missing |
| Skill activation | N/A | ✅ | N/A |

**Critical Gap**: No conversation memory or multi-turn tracking.

---

## 3. Conversation Memory (SOLVED)

### The Problem
Cloud LLMs (Gemini, OpenAI) are **stateless** - each API call is independent.

### OVOS Solution
```python
class ConverseService:
    def __init__(self):
        self.active_skills = {}  # skill_id → {timestamp, context}
        self.default_timeout = 300  # seconds
    
    def handle(self, message):
        for skill in self.active_skills:
            if skill.converse(message):
                return  # Handled by active skill
        self.route_to_intent()  # No active skill, fresh intent
```

### Smart Car Solution (Implemented)

`src/llm/conversation_memory.py`:
```python
class ConversationMemory:
    """Manages context for stateless cloud LLMs"""
    
    # Rolling buffer of messages
    _messages: deque[Message]
    
    # Compressed summary of old turns
    _summary: str
    
    # Robot state for context
    robot_state: RobotState  # direction, tracking, vision
    
    # Conversation state machine
    _state: ConversationState  # IDLE, ACTIVE, FOLLOW_UP
    
    def build_context(self) -> str:
        """Build full prompt with memory injection"""
```

**Architecture**:
```
┌─────────────────────────────────────────────────────────────┐
│                    CONTEXT WINDOW                           │
├─────────────────────────────────────────────────────────────┤
│  [System Prompt]     ~300 tokens (robot persona/rules)      │
│  [Robot State]       ~100 tokens (vision, nav, sensors)     │
│  [Summary]           ~200 tokens (compressed old turns)     │
│  [Recent History]    ~500 tokens (last 3-5 exchanges)       │
│  [Current Query]     ~100 tokens (user's new message)       │
├─────────────────────────────────────────────────────────────┤
│  Total Budget: ~1200 tokens (safe for most LLMs)            │
└─────────────────────────────────────────────────────────────┘
```

---

## 4. IPC Comparison

### Rhasspy: MQTT (Hermes Protocol)
```
hermes/hotword/toggleOn
hermes/hotword/detected
hermes/asr/startListening
hermes/asr/textCaptured
hermes/tts/say
```
- **Pros**: Standard protocol, language-agnostic, distributed
- **Cons**: Network overhead, broker dependency

### OVOS: Message Bus (WebSocket)
```python
bus.emit(Message('recognizer_loop:utterance', {"utterances": [...]}))
bus.on('speak', handle_speak)
```
- **Pros**: Rich message types, bidirectional
- **Cons**: WebSocket complexity

### Wyoming: TCP Events
```python
async def event_callback(event):
    if RunPipeline.is_type(event.type):
        self.trigger_streaming_start()
    elif Transcript.is_type(event.type):
        self.trigger_transcript(transcript)
```
- **Pros**: Simple protocol, async-native
- **Cons**: Custom implementation

### Smart Car: ZeroMQ PUB/SUB
```python
TOPIC_WAKEWORD = b"wakeword"
TOPIC_STT_RESULT = b"stt.result"
TOPIC_LLM_REQ = b"llm.request"
TOPIC_LLM_RESP = b"llm.response"
```
- **Pros**: Fast, no broker needed, battle-tested
- **Cons**: No message persistence

**Assessment**: ZeroMQ is EXCELLENT for your use case. Faster than MQTT, simpler than WebSocket.

---

## 5. Full System Flow Comparison

### Rhasspy Flow
```
mic → [Porcupine] → MQTT:hotword/detected
                           ↓
      MQTT:asr/startListening → [DeepSpeech/Kaldi]
                                       ↓
                             MQTT:asr/textCaptured
                                       ↓
                              [Intent Recognition]
                                       ↓
                             MQTT:intent/recognized
                                       ↓
                              [Home Assistant]
```

### OVOS Flow
```
mic → [Precise/Porcupine] → bus:recognizer_loop:wakeword
                                       ↓
           bus:recognizer_loop:record_begin → [Vosk/Whisper]
                                                    ↓
                             bus:recognizer_loop:utterance
                                       ↓
                              [IntentService Pipeline]
                              converse → adapt → fallback
                                       ↓
                              bus:speak → [Mimic/Piper TTS]
```

### Wyoming Satellite Flow
```
mic → [StreamingSatellite] → TCP:AudioChunk
                                  ↓
               TCP:RunPipeline (wake detected)
                                  ↓
               is_streaming = True → forward to ASR
                                  ↓
               TCP:Transcript → is_streaming = False
                                  ↓
               TCP:Synthesize → play audio (mute mic)
```

### Smart Car Flow (With Memory)
```
mic → [UnifiedVoicePipeline] → ZMQ:wakeword
                                    ↓
                PipelineState.CAPTURING → [faster-whisper]
                                    ↓
                    ZMQ:stt.result → [Orchestrator]
                                    ↓
                [Update ConversationMemory + Robot State]
                                    ↓
                    ZMQ:llm.request → [GeminiRunner]
                                    ↓
              [Build Context: System + Memory + History + Query]
                                    ↓
                    Gemini API → Parse JSON Response
                                    ↓
              [Update Memory with Assistant Response]
                                    ↓
                    ZMQ:llm.response → [Orchestrator]
                                    ↓
            ┌───────────────┬────────────────┬─────────────┐
            ↓               ↓                ↓             ↓
    ZMQ:nav.cmd      ZMQ:vision.track   ZMQ:tts.speak   Update State
    (UART→ESP32)     (YOLO tracking)    (Piper→aplay)
```

---

## 6. System Prompt Design

### Before (Generic)
```
You are a robot assistant controlling a physical robot.
You MUST reply with STRICT JSON only...
```

### After (Robot-Aware)
```
You are GENNY, an AI assistant controlling a physical robot car 
with camera and motors.

## YOUR CAPABILITIES:
- Move: forward, back, left, right, stop
- See: camera with object detection (YOLO)
- Track: follow a detected object visually
- Speak: respond via text-to-speech

## CURRENT ROBOT STATE:
Navigation: stopped
Vision: person (confidence: 92%)

## CONVERSATION CONTEXT:
User: What do you see?
GENNY: I see a person in front of me.
User: Follow them
```

This prompt style matches:
- **OVOS**: Skill context injection
- **Rhasspy**: Intent slots and context
- **Best practices**: Role, capabilities, constraints, state, history

---

## 7. Recommendations Implemented

| Issue | Solution | Status |
|-------|----------|--------|
| Stateless LLM | `ConversationMemory` class | ✅ Done |
| Generic system prompt | Rich robot-aware prompt template | ✅ Done |
| No memory injection | Updated `GeminiRunner` | ✅ Done |
| Missing robot state | `RobotState` dataclass | ✅ Done |
| No conversation timeout | 120s default, configurable | ✅ Done |

---

## 8. Configuration Updates

Add to `config/system.yaml`:
```yaml
llm:
  engine: gemini
  gemini_api_key: ${GEMINI_API_KEY}
  gemini_model: gemini-1.5-flash
  temperature: 0.2
  top_p: 0.9
  
  # NEW: Memory configuration
  memory_max_turns: 10        # Keep last 10 conversation turns
  conversation_timeout_s: 120 # Reset conversation after 2 min idle
```

---

## 9. Testing the Memory System

```bash
# Test conversation memory
cd ~/ptojects/smart_car
source /home/pi/venvs/llme/bin/activate
python -c "
from src.llm.conversation_memory import ConversationMemory

mem = ConversationMemory()
mem.update_robot_state(direction='stopped', vision={'label': 'person', 'confidence': 0.92})
mem.add_user_message('What do you see?')

print('=== CONTEXT FOR LLM ===')
print(mem.build_context())
"
```

---

## Conclusion

Your architecture is **fundamentally sound** and matches industry patterns:

1. **Mic Handling**: ✅ Unified pipeline (better than most)
2. **IPC**: ✅ ZeroMQ (fast, no broker)
3. **State Machine**: ✅ Event-driven
4. **Memory**: ✅ NOW IMPLEMENTED with `ConversationMemory`
5. **System Prompt**: ✅ NOW ROBOT-AWARE

The key addition was the conversation memory layer to compensate for Gemini's stateless API.
