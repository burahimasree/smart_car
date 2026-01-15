"""Process-level controller for on-device STT runners (faster-whisper default)."""
from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional


RUNNER_SCRIPTS: Dict[str, str] = {
    "faster_whisper": "src/stt/faster_whisper_runner.py",
    "azure_speech": "src/stt/azure_speech_runner.py",
}
DEFAULT_ENGINE = "faster_whisper"


@dataclass(slots=True)
class STTConfig:
    engine: str
    mic_hw: str
    sample_rate: int
    silence_threshold: float
    silence_duration_ms: int
    max_capture_seconds: int
    runner_venv: Path
    language: str = "en"
    min_confidence: float = 0.0
    engine_options: Dict[str, Dict[str, Any]] = field(default_factory=dict)


class STTEngine:
    """Lightweight wrapper that spawns/closes the whisper_runner process."""

    def __init__(self, config: STTConfig, project_root: Path) -> None:
        self.config = config
        self.project_root = project_root
        self._proc: Optional[subprocess.Popen[str]] = None
        self._lock = threading.Lock()
        engine_name = (config.engine or DEFAULT_ENGINE).lower()
        if engine_name in {"whisper_fast", "whisperfast"}:
            engine_name = "faster_whisper"
        self._engine_key = engine_name
        runner_path = RUNNER_SCRIPTS.get(self._engine_key, RUNNER_SCRIPTS[DEFAULT_ENGINE])
        self._runner = project_root / runner_path
        self._started_ts: float | None = None

    @classmethod
    def from_config(cls, config_map: dict, project_root: Optional[Path] = None) -> "STTEngine":
        project_root = project_root or Path(__file__).resolve().parents[2]
        stt_cfg = config_map.get("stt", {})
        engines_cfg = stt_cfg.get("engines")
        if not engines_cfg:
            engines_cfg = {}
            if stt_cfg.get("fast_whisper"):
                engines_cfg["faster_whisper"] = stt_cfg.get("fast_whisper", {})
            legacy_whisper: Dict[str, Any] = {}
            if stt_cfg.get("model_path"):
                legacy_whisper["model_path"] = stt_cfg.get("model_path")
            if stt_cfg.get("bin_path"):
                legacy_whisper["bin_path"] = stt_cfg.get("bin_path")
            if legacy_whisper:
                engines_cfg["whispercpp"] = legacy_whisper
        engines_normalized: Dict[str, Dict[str, Any]] = {
            str(k).lower(): dict(v or {}) for k, v in engines_cfg.items()
        }
        cfg = STTConfig(
            engine=stt_cfg.get("engine", DEFAULT_ENGINE),
            mic_hw=stt_cfg.get("mic_hw", "plughw:1,0"),
            sample_rate=int(stt_cfg.get("sample_rate", 16000)),
            silence_threshold=float(stt_cfg.get("silence_threshold", 0.3)),
            silence_duration_ms=int(stt_cfg.get("silence_duration_ms", 900)),
            max_capture_seconds=int(stt_cfg.get("max_capture_seconds", 15)),
            runner_venv=Path(stt_cfg.get("runner_venv", project_root / ".venvs/stte/bin/python")),
            language=stt_cfg.get("language", "en"),
            min_confidence=float(stt_cfg.get("min_confidence", 0.0)),
            engine_options=engines_normalized,
        )
        return cls(cfg, project_root)

    def start_session(self, ipc_addr: str, *, debug: bool = False) -> bool:
        with self._lock:
            if self._proc and self._proc.poll() is None:
                return False
            if os.environ.get("STT_ENGINE_DISABLED") == "1":
                self._started_ts = time.time()
                return True
            self._ensure_paths()
            options = self._engine_options()
            if self._engine_key == "faster_whisper":
                cmd = [
                    str(self.config.runner_venv),
                    str(self._runner),
                    "--mic",
                    self.config.mic_hw,
                    "--ipc",
                    ipc_addr,
                    "--sample-rate",
                    str(self.config.sample_rate),
                    "--silence-threshold",
                    str(self.config.silence_threshold),
                    "--silence-duration-ms",
                    str(self.config.silence_duration_ms),
                    "--max-capture-seconds",
                    str(self.config.max_capture_seconds),
                    "--language",
                    self.config.language,
                ]
                model = options.get("model") or options.get("model_path") or "tiny.en"
                if model:
                    cmd += ["--fast-model", str(model)]
                if options.get("compute_type"):
                    cmd += ["--compute-type", str(options["compute_type"])]
                if options.get("device"):
                    cmd += ["--device", str(options["device"])]
                if options.get("beam_size") is not None:
                    cmd += ["--beam-size", str(options["beam_size"])]
                download_root = options.get("download_root")
                if download_root:
                    cmd += ["--download-root", str(download_root)]
            elif self._engine_key == "azure_speech":
                cmd = [
                    str(self.config.runner_venv),
                    str(self._runner),
                    "--ipc",
                    ipc_addr,
                    "--language",
                    str(options.get("language") or self.config.language or "en-US"),
                    "--min-confidence",
                    str(self.config.min_confidence),
                ]
                mic_device = options.get("mic_device") or options.get("mic") or self.config.mic_hw
                if mic_device and mic_device.lower() != "default":
                    cmd += ["--mic", str(mic_device)]
                region = options.get("region") or os.environ.get("AZURE_SPEECH_REGION")
                if region:
                    cmd += ["--region", str(region)]
                endpoint = options.get("endpoint") or os.environ.get("AZURE_SPEECH_ENDPOINT")
                if endpoint:
                    cmd += ["--endpoint", str(endpoint)]
                if options.get("continuous"):
                    cmd.append("--continuous")
            else:
                model_path = self._path_option("model_path", options)
                bin_path = self._path_option("bin_path", options)
                if model_path is None or bin_path is None:
                    raise RuntimeError("whispercpp engine requires model_path and bin_path options")
                cmd = [
                    str(self.config.runner_venv),
                    str(self._runner),
                    "--mic",
                    self.config.mic_hw,
                    "--model",
                    str(model_path),
                    "--ipc",
                    ipc_addr,
                    "--sample-rate",
                    str(self.config.sample_rate),
                    "--silence-threshold",
                    str(self.config.silence_threshold),
                    "--silence-duration-ms",
                    str(self.config.silence_duration_ms),
                    "--max-capture-seconds",
                    str(self.config.max_capture_seconds),
                    "--language",
                    self.config.language,
                    "--bin-path",
                    str(bin_path),
                ]
            if debug:
                cmd.append("--debug")

            env = os.environ.copy()
            env.setdefault("PROJECT_ROOT", str(self.project_root))
            env.setdefault("PYTHONUNBUFFERED", "1")
            if self._engine_key == "azure_speech":
                key_value = options.get("key") or os.environ.get("AZURE_SPEECH_KEY")
                if key_value:
                    env.setdefault("AZURE_SPEECH_KEY", str(key_value))
            self._proc = subprocess.Popen(
                cmd,
                cwd=self.project_root,
                env=env,
                text=True,
            )
            self._started_ts = time.time()
            return True

    def stop_session(self, timeout: float = 5.0) -> None:
        with self._lock:
            if os.environ.get("STT_ENGINE_DISABLED") == "1":
                self._proc = None
                self._started_ts = None
                return
            if not self._proc:
                return
            if self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
            self._proc = None
            self._started_ts = None

    def is_running(self) -> bool:
        with self._lock:
            return bool(self._proc and self._proc.poll() is None)

    def _ensure_paths(self) -> None:
        if not self.config.runner_venv.exists():
            raise FileNotFoundError(f"STT venv python missing at {self.config.runner_venv}")
        if not self._runner.exists():
            raise FileNotFoundError(f"whisper_runner not found at {self._runner}")
        if self._engine_key == "whispercpp":
            options = self._engine_options()
            model_path = self._path_option("model_path", options)
            bin_path = self._path_option("bin_path", options)
            if not model_path or not model_path.exists():
                raise FileNotFoundError("STT model missing for whispercpp engine")
            if not bin_path or not bin_path.exists():
                raise FileNotFoundError("whisper.cpp binary missing for whispercpp engine")
        elif self._engine_key == "azure_speech":
            options = self._engine_options()
            key_value = options.get("key") or os.environ.get("AZURE_SPEECH_KEY")
            region = options.get("region") or os.environ.get("AZURE_SPEECH_REGION")
            if not key_value:
                raise ValueError("Azure Speech key not configured (set stt.engines.azure_speech.key or AZURE_SPEECH_KEY)")
            if not region:
                raise ValueError("Azure Speech region not configured (set stt.engines.azure_speech.region or AZURE_SPEECH_REGION)")

    def _engine_options(self) -> Dict[str, Any]:
        return self.config.engine_options.get(self._engine_key, {})

    @staticmethod
    def _path_option(key: str, options: Dict[str, Any]) -> Optional[Path]:
        value = options.get(key)
        if not value:
            return None
        if isinstance(value, Path):
            return value
        return Path(value)


# ---------------------------------------------------------------------------
# Legacy / test-facing types (RecognizerConfig & STTBackend) expected by
# older test scaffolding. Added here to avoid introducing a new module.
# ---------------------------------------------------------------------------

class STTBackend(Enum):
    WHISPER_CPP = "whisper_cpp"
    OTHER = "other"


@dataclass(slots=True)
class RecognizerConfig:
    backend: STTBackend
    model_path: Path

