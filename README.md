# Offline Raspberry Pi AI Assistant

**Status: 75% Complete - Core functionality implemented and tested!**

Modular, offline-first voice assistant for Raspberry Pi 4 that combines Whisper.cpp STT, Piper TTS, llama.cpp LLM, YOLO vision, and a Waveshare 3.5" UI panel. Every subsystem lives in its own package under `src/`, and the orchestrator bridges them via ZeroMQ IPC.

## Implementation Status

✅ **Working Now:**
- Speech-to-Text (Whisper.cpp with ggml-small.en-q5_1 model)
- Language Model (llama.cpp + TinyLlama 1.1B)
- Wakeword Detection (Porcupine)
- Event-driven Orchestrator (wakeword → STT → LLM → TTS → vision)
- IPC messaging (ZeroMQ PUB/SUB)
- UART bridge for navigation
- Test suite (10/10 passing)

⚠️ **Needs Setup:**
- Piper TTS installation (~15 min)
- YOLO vision models (~5 min)
- Hardware: audio devices, display, UART

📖 **See [QUICK_START_IMPLEMENTATION.md](QUICK_START_IMPLEMENTATION.md) for step-by-step setup**
📊 **See [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md) for detailed component status**

## Highlights
- Six isolated Python 3.11+ virtual environments (`stte`, `ttse`, `llme`, `visn`, `core`, `dise`)
- YAML/JSON-driven configuration with environment variable expansion
- Rotating log pipeline writing to `logs/`
- Pytest auto-discovery - all tests passing
- Simulation modes for all components (no hardware required for testing)

## Layout
```
project_root/
├── config/
│   ├── logging.yaml
│   ├── system.yaml
│   └── system.local.json
├── docs/
│   ├── apis.md
│   ├── architecture.md
│   └── modules.md
├── logs/
├── src/
│   ├── core/
│   ├── llm/
│   ├── stt/
│   ├── tests/
│   ├── tools/
│   ├── tts/
│   ├── ui/
│   └── vision/
├── setup_envs.sh
├── requirements.txt
└── update.txt
```

## Quick Start

### 1. Test Current Implementation (No Hardware)
```bash
# Run test suite
source .venvs/stte/bin/activate
pytest src/tests -v
# Result: 10 passed

# Demo all working components
./scripts/demo_implementation.sh

# Interactive chat with LLM
./scripts/run_chat_test.sh
# Note: First prompt may timeout while model loads (~30s)
# Second prompt will respond immediately
```

### 2. Bootstrap From Scratch
```bash
cd /home/dev/project_root
chmod +x setup_envs.sh
./setup_envs.sh

# All virtual environments are already created:
# .venvs/stte - Speech-to-Text + Wakeword (Python 3.11.9)
# .venvs/ttse - Text-to-Speech
# .venvs/llme - Language Model
# .venvs/visn - Vision processing
# .venvs/core - Orchestrator
# .venvs/dise - Display engine
```

## Usage

### Run Individual Components
```bash
# Wakeword detection (simulation mode)
source .venvs/stte/bin/activate
python -m src.wakeword.porcupine_runner --sim

# Speech-to-Text (simulation mode)
python -m src.stt.whisper_runner --sim

# LLM server
source .venvs/llme/bin/activate
python -m src.llm.llm_runner

# TTS stub (real Piper needs installation - see quick start guide)
source .venvs/ttse/bin/activate
python -m src.tts.piper_runner
```

### Run Full System
```bash
# Start all services
./scripts/run.sh

# Monitor logs
tail -f logs/run.log
tail -f logs/orchestrator.log

# Stop all services
kill $(cat logs/*.pid)
```

### Run Tests
```bash
source .venvs/stte/bin/activate
pytest src/tests -v
# Expected: 10 passed in ~4s
```

## What's Already Built

### Binaries & Models
- ✅ `whisper.cpp` compiled: `third_party/whisper.cpp/build/bin/whisper-cli`
- ✅ `llama.cpp` compiled: `third_party/llama.cpp/bin/llama-server`
- ✅ Whisper model: `models/whisper/ggml-small.en-q5_1.bin` (181MB)
- ✅ LLM model: `models/llm/tinyllama-1.1b-chat.Q4_K_M.gguf` (637MB)
- ⚠️ Piper TTS: Needs installation (see quick start guide)
- ⚠️ YOLO vision: Needs model download (5 min)

### Implementation Details
- **STT**: Full whisper.cpp integration with streaming
- **LLM**: HTTP-based llama.cpp server with JSON intent extraction
- **Wakeword**: Porcupine integration (requires PV_ACCESS_KEY)
- **Orchestrator**: Complete state machine for conversation flow
- **IPC**: ZeroMQ PUB/SUB on tcp://127.0.0.1:6010-6011

## Next Steps

### For End-to-End Demo (< 1 hour)
1. ✅ Core system is ready - all tests passing
2. ⏱️ Install Piper TTS (15 min) - see [QUICK_START_IMPLEMENTATION.md](QUICK_START_IMPLEMENTATION.md) Step 1
3. ⏱️ Download YOLO model (5 min) - see Step 2
4. ⏱️ Configure audio devices (15 min) - see Step 3
5. ⏱️ Setup Waveshare display (30 min) - see Step 4

### For Production Deployment
1. Create systemd service files
2. Configure auto-start on boot
3. Add health monitoring
4. Set up remote logging
5. Create backup/restore procedures

## Architecture

```
Wakeword → STT → LLM → NAV + TTS
             ↓          ↓
          Vision    UART/Motors
```

**IPC Topics:**
- `ww.detected` - Wake word events
- `stt.transcript` - Speech transcriptions
- `llm.request` / `llm.response` - Intent processing
- `tts.speak` - Speech synthesis
- `nav.cmd` - Navigation commands
- `visn.detection` - Object detection
- `cmd.listen.start` / `cmd.listen.stop` - STT control
- `cmd.pause_vision` - Vision pause/resume

## Documentation

- [QUICK_START_IMPLEMENTATION.md](QUICK_START_IMPLEMENTATION.md) - Step-by-step setup guide
- [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md) - Detailed component status
- [REPO_SUMMARY.md](REPO_SUMMARY.md) - Repository structure and architecture
- [docs/architecture.md](docs/architecture.md) - System architecture details
- [docs/modules.md](docs/modules.md) - Module documentation
- [docs/apis.md](docs/apis.md) - API specifications
```
