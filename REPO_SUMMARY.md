Repository summary — Offline Raspberry Pi Assistant
=================================================

This document describes the repository layout, the big-picture architecture, and a per-file analysis for the primary files in the workspace. It is written to help a developer or an AI coding agent become productive immediately.

Top-level layout
----------------

```
project_root/
├── .github/
│   └── copilot-instructions.md
├── config/
│   ├── logging.yaml
│   ├── settings.json
   │   ├── settings.yaml
├── docs/
│   ├── apis.md
│   ├── architecture.md
│   └── modules.md
├── logs/
├── models/
│   └── piper-samples/
├── scripts/
├── src/
│   ├── core/
│   ├── llm/
│   ├── stt/
│   ├── tts/
│   ├── uart/
│   ├── vision/
│   ├── wakeword/
│   └── tests/
├── requirements*.txt
├── setup_envs.sh
└── scripts/run.sh
```

Big-picture architecture
------------------------

- Purpose: an offline-first voice assistant (Raspberry Pi) that composes multiple subsystems (wakeword, STT, LLM, TTS, vision, UART) as isolated Python services coordinated by an orchestrator.
- Communication: ZeroMQ PUB/SUB with two logical channels: `upstream` (workers -> orchestrator) and `downstream` (orchestrator -> workers). Topic constants and helpers live in `src/core/ipc.py`.
- Orchestration: `src/core/orchestrator.py` runs the router/state machine to pause/resume vision, start/stop STT listening, forward STT text to LLM, forward LLM responses to TTS and NAV, and resume vision after TTS completes.

Configuration & environments
----------------------------

- Central configuration: `config/system.yaml` with optional local overrides in `config/system.local.json`. Use `src/core/config_loader.py` to load and expand environment tokens.
- Virtual environments: multiple venvs (`.venvs/{core,stte,ttse,llme,visn}`) used to isolate heavy native dependencies. Bootstrap via `./setup_envs.sh` or `scripts/recreate_venvs.sh`.
- Run all services: `./scripts/run.sh` which starts each service from the corresponding venv and writes PIDs to `logs/*.pid`.

Logging
-------

- Configured in `config/logging.yaml`. Use `src/core/logging_setup.get_logger(name, log_dir)` to get a rotating file handler per service.

Per-file analysis (key files)
-----------------------------

The entries below describe intent, important behaviors, and integration notes for each significant file.

- `README.md`
  - High-level description, suggested bootstrap steps, minimal run and test commands.

- `.github/copilot-instructions.md`
  - Repo-specific guidance for AI coding agents: quick start, architecture pointers, IPC conventions, config and logging references, and example files.

- `config/system.yaml`
  - Central runtime configuration for STT, TTS, LLM, vision, display, and logging. Contains relative model paths used by runners. Services rely on presence of these files and will exit with logged errors if binaries or models are missing.

- `config/system.local.json`
  - Example local overrides pointing to absolute model paths. Useful for device-specific setup (e.g., `/home/pi/models`).

- `scripts/run.sh`
  - Launches full set of services using `.venvs`. Creates `logs/run.log` and writes per-service PID files under `logs/`. If replicating a multi-process test environment, this is the canonical script to use.

- `scripts/recreate_venvs.sh` and `setup_envs.sh`
  - Create multiple virtual environments for the different runtime roles. `recreate_venvs.sh` is more opinionated (Python 3.11, installs requirements-*.txt when present) and useful for CI/dev machines.

- `src/core/config_loader.py`
  - Loads YAML configs and expands variables `${PROJECT_ROOT}` and `${ENV:VAR}`; loads `.env` if present. Raise errors when PyYAML missing or file not found. Use this helper in all services to normalize config loading.

- `src/core/ipc.py`
  - Central place for IPC topic constants and ZeroMQ helper wrappers:
    - Topics are bytes: `TOPIC_WW_DETECTED`, `TOPIC_STT`, `TOPIC_LLM_REQ`, `TOPIC_LLM_RESP`, `TOPIC_TTS`, `TOPIC_VISN`, `TOPIC_NAV`, `TOPIC_CMD_LISTEN_START`, `TOPIC_CMD_LISTEN_STOP`, `TOPIC_CMD_PAUSE_VISION`.
    - `make_publisher(config, channel, bind)` and `make_subscriber(config, topic, channel, bind)` choose `upstream`/`downstream` addresses from config or `IPC_*` env vars and either bind or connect the socket.
    - `publish_json` wraps sending a multipart message (topic + JSON byte payload).
  - Important detail: `make_subscriber` sets `zmq.SUBSCRIBE` to the provided topic; callers typically create separate subscribers per topic when running blocking receive loops.

- `src/core/logging_setup.py`
  - Provides `get_logger(name, log_dir, level=logging.INFO)` which creates a `RotatingFileHandler` and stops propagation. Use for consistent file-based logging.

- `src/core/orchestrator.py`
  - The orchestrator is the runtime glue. Key behaviors:
    - loads `config/system.yaml` with `load_config`;
    - `cmd_pub` = downstream publisher (for commands); `events_sub` = upstream subscriber (for events);
    - state machine fields: `vision_paused`, `stt_active`, `llm_pending`, `tts_pending`, `last_transcript`, `last_visn`;
    - `on_wakeword`: pauses vision and publishes `TOPIC_CMD_LISTEN_START` to start STT;
    - `on_stt`: if `stt_active`, forwards text to `TOPIC_LLM_REQ` and sets `llm_pending`;
    - `on_llm`: receives `TOPIC_LLM_RESP` payloads, extracts `speak` body and navigation intent (supports multiple shapes), publishes NAV commands and `TOPIC_TTS`, sets `tts_pending`, stops STT (`TOPIC_CMD_LISTEN_STOP`), and defers resuming vision until TTS completion;
    - `on_tts`: waits for `done` / `final` markers and then resumes vision by publishing `TOPIC_CMD_PAUSE_VISION` with `pause=false`.
  - Notes: orchestrator uses a simple blocking loop on `self.events_sub.recv_multipart()`. Implementations of services must send JSON payloads that match what orchestrator expects.

- `src/llm/llama_wrapper.py`
  - Lightweight wrapper dataclass `LlamaConfig` and `LlamaRunner` stub. `ensure_ready()` checks model and binary; `generate()` returns a placeholder string. Use this file as the reference interface if implementing a Python-native wrapper.

- `src/llm/llama_server.py`
  - Implements a simple LLM server that:
    - subscribes to `TOPIC_LLM_REQ` on downstream channel;
    - builds a `SYSTEM_PROMPT` asking for JSON-only responses with `{intent, slots, speak}`;
    - calls a configured `llama.cpp` binary via subprocess with prompt and context args;
    - extracts the first JSON object from stdout via regex and publishes `TOPIC_LLM_RESP` with `{"ok": bool, "json": dict, "raw": str}`.
  - Important integration detail: this server expects `bin_path` and `model_path` to exist; otherwise it logs an error and exits. Results may be brittle if `llama.cpp` output is not strictly JSON-wrapped.

- `src/stt/engine.py`
  - Abstraction `SpeechRecognizer` and `RecognizerConfig` dataclass. Supports backends `WHISPER_CPP` and `VOSK` (enum). Methods are scaffolded: `bootstrap()` validates model file; `transcribe()` is a placeholder returning a descriptive string. Real implementations should preserve the public interface.

- `src/stt/whisper_runner.py`
  - A runnable service that:
    - loads config and builds a `WhisperRunnerConfig` with `bin_path`, `model_path`, `threads`, `language`, `sample_rate`;
    - creates a publisher on upstream and subscribers for `TOPIC_CMD_LISTEN_START` and `TOPIC_CMD_LISTEN_STOP` on downstream;
    - starts/stops a subprocess running a `whisper.cpp` stream binary in response to `start`/`stop` commands;
    - parses lines from the `whisper.cpp` stream for `text:` segments and publishes `TOPIC_STT` payloads: `{"timestamp": <int>, "text": str, "confidence": float, "language": "en"}`.
  - Important: the runner expects a compiled `whisper.cpp` binary and model files. It provides a `--sim` flag in the CLI for simulated runs.

- `src/tts/engine.py`
  - `SpeechSynthesizer` abstraction and `TTSConfig`. `bootstrap()` checks `model_path`; `speak()` writes a placeholder file and returns a path. This is a reference implementation for integrating the Piper runner.

- `src/tts/piper_runner.py`
  - Implements a runtime that subscribes to `TOPIC_TTS` (downstream), invokes the Piper binary, and pipes audio into a playback command (default `aplay`). Expects `bin_path` and `model_path` configured; logs and exits if missing.

- `src/uart/bridge.py`
  - Opens a serial port (via `pyserial`) and listens for `TOPIC_NAV` messages on downstream. It maps `direction` names to configured device commands in `config` and writes lines to the UART. If `pyserial` is missing or the port fails to open, it logs an error and returns.

- `src/uart/sim_uart.py`
  - Simple TCP-based simulator that listens on `127.0.0.1:33333`, prints received bytes, and replies with `ACK`. Useful for testing without hardware.

- `src/tools/cli.py`
  - Developer CLI entrypoints. Contains a `main()` that uses a `ConfigLoader` and an `OfflineAssistant` (note: there are naming inconsistencies / alternate orchestrator implementations in the repo; see `src/core/orchestrator.py`). It provides a simple bootstrap flow used by dev tooling.

- `src/tests/` (various test modules)
  - Pytest-based tests exist (e.g., `test_config_loader.py`, `test_ipc_contract.py`, `test_orchestrator_flow.py`, `test_wakeword_sim.py`). Tests help document expected shapes and behavior (e.g., IPC topics and config expansion). Run tests inside `stte` venv: `pytest src/tests`.

Integration notes & common pitfalls
---------------------------------

- Many components are scaffolds/stubs: `LlamaRunner.generate`, `SpeechRecognizer.transcribe`, and `SpeechSynthesizer.speak` intentionally implement placeholder logic. When replacing with real binaries or libs, preserve the public signatures and the JSON message shapes used by the orchestrator.
- The system is designed for offline, on-device execution: compiled binaries (whisper.cpp stream, llama.cpp) and large model files (GGUF, ONNX, VOSK) are expected to be placed under `./models/` or device-local paths. Failing to provide these artifacts causes early process exit with logged errors.
- IPC topology: ensure a single authoritative `upstream` and `downstream` pair are used across processes (configured in `config/system.yaml` or via `IPC_UPSTREAM` / `IPC_DOWNSTREAM` environment variables). `make_subscriber` often uses `topic=...` to subscribe to a single topic and block on `recv_multipart()`.

Developer quick-check commands
-----------------------------

Activate the core/test venv and run tests:

```bash
source .venvs/stte/bin/activate
pytest src/tests
deactivate
```

Run a single component in simulation mode (example):

```bash
source .venvs/stte/bin/activate
python -m src.stt.whisper_runner --sim
deactivate
```

Start the whole system (dev):

```bash
chmod +x scripts/run.sh
./scripts/run.sh
```

Next steps / recommended tasks for contributors
---------------------------------------------

- Implement production-ready adapters for LLM (robust parsing of `llama.cpp` output), STT backends (stable stream extraction), and TTS (piper binary integration with error handling and duration/completion markers).
- Add integration tests that spin up the `run.sh` services in a controlled environment (use `sim_uart` for nav and a simulated `whisper_runner` for STT) and assert orchestrator state transitions.

Contact & context
------------------

This summary was generated from the repository source files and `docs/architecture.md`. If any file was modified after this summary was created, re-run a short scan to keep this summary current.

End of summary.
