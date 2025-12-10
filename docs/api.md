# API Surface

## Core Orchestrator
### `OfflineAssistant`
- `bootstrap()`: Initializes STT, TTS, LLM, Vision, and UI subsystems.
- `handle_query(audio_frames: list[bytes]) -> str`: Runs STT -> LLM -> TTS roundtrip and returns generated response.

## STT
### `SpeechRecognizer`
- `bootstrap()`: Validates model path and prepares backend.
- `transcribe(audio_frames: Iterable[bytes]) -> str`: Converts audio to text.

## TTS
### `SpeechSynthesizer`
- `bootstrap()`: Validates Piper model.
- `speak(prompt: str, output_path: Optional[Path]) -> Path`: Synthesizes speech and returns file path.

## LLM
### `LlamaRunner`
- `ensure_ready()`: Validates presence of model + llama.cpp binary.
- `generate(prompt: str, max_tokens: int) -> str`: Returns generated text.

## Vision
### `VisionPipeline`
- `bootstrap()`: Loads YOLO weights.
- `run(stream: Iterator[bytes]) -> Iterator[List[Detection]]`: Processes frames.

## UI
### `WaveshareDisplay`
- `initialize()`: Configures SPI display.
- `draw_frame(buffer: bytes)`: Renders raw frame buffer.

## Config Loader
### `ConfigLoader`
- `load() -> OrchestratorConfig`: Parses YAML/JSON configuration into dataclasses.

## Logging Helper
### `get_logger(name: str, log_path: Path, level: int)`
- Returns configured rotating logger instance.
