"""Minimal STT service that consumes cmd.listen.start/stop events.

Uses sounddevice with device NAME (not index) to properly use ALSA plugin chain
including dsnoop for shared microphone access with wakeword service.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np
import sounddevice as sd
from scipy import signal
import zmq

from src.core.config_loader import load_config
from src.core.ipc import (
    TOPIC_CMD_LISTEN_START,
    TOPIC_CMD_LISTEN_STOP,
    TOPIC_STT,
    make_publisher,
    make_subscriber,
    publish_json,
)
from src.core.logging_setup import get_logger
from src.stt.faster_whisper_runner import load_fast_model, transcribe_fast


# Native sample rate of USB mic - dsnoop runs at this rate
NATIVE_SAMPLE_RATE = 44100
# STT/Whisper prefers 16kHz
TARGET_SAMPLE_RATE = 16000


class STTService:
    def __init__(self, config_path: Path) -> None:
        self.config = load_config(config_path)
        log_dir = Path(self.config.get("logs", {}).get("directory", "logs"))
        if not log_dir.is_absolute():
            project_root = Path(os.environ.get("PROJECT_ROOT", Path(__file__).resolve().parents[2]))
            log_dir = project_root / log_dir
        self.logger = get_logger("stt.service", log_dir)

        self.sample_rate = TARGET_SAMPLE_RATE  # Output rate after resampling
        self.frame_ms = int(self.config.get("audio", {}).get("wakeword_frame_ms", 30))
        self.frames_per_buffer = int(TARGET_SAMPLE_RATE * self.frame_ms / 1000)
        self.native_frames_per_buffer = int(NATIVE_SAMPLE_RATE * self.frame_ms / 1000)
        self.silence_threshold = float(self.config.get("stt", {}).get("silence_threshold", 0.35))
        self.silence_duration_ms = int(self.config.get("stt", {}).get("silence_duration_ms", 900))
        self.max_capture_seconds = float(self.config.get("stt", {}).get("max_capture_seconds", 10))
        self.language = self.config.get("stt", {}).get("language", "en")
        self.mic_device = str(self.config.get("stt", {}).get("mic_device") or "smartcar_capture")

        # Open sounddevice stream with ALSA device NAME for dsnoop sharing
        self.stream = self._open_stream()

        self.publisher = make_publisher(self.config, channel="upstream")
        self.cmd_sub = make_subscriber(self.config, channel="downstream", topic=TOPIC_CMD_LISTEN_START)
        self.cmd_sub.setsockopt(zmq.SUBSCRIBE, TOPIC_CMD_LISTEN_STOP)

        self._capture_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._session_lock = threading.Lock()
        self._model = None
        
        # Preload whisper model at startup to avoid timeout on first request
        self._ensure_model()

    # --------------------- public API ---------------------

    def run(self) -> None:
        self.logger.info("STT service running (native=%dHz, target=%dHz, device=%s, model=loaded)", 
                        NATIVE_SAMPLE_RATE, TARGET_SAMPLE_RATE, self.mic_device)
        while True:
            topic, raw = self.cmd_sub.recv_multipart()
            if topic == TOPIC_CMD_LISTEN_START:
                self.logger.info("cmd.listen.start received")
                self._start_session()
            elif topic == TOPIC_CMD_LISTEN_STOP:
                self.logger.info("cmd.listen.stop received")
                self._stop_session()

    # --------------------- session handling ---------------------

    def _start_session(self) -> None:
        with self._session_lock:
            if self._capture_thread and self._capture_thread.is_alive():
                self.logger.info("STT session already active")
                return
            self._stop_event.clear()
            self._capture_thread = threading.Thread(target=self._capture_loop, name="STTSession", daemon=True)
            self._capture_thread.start()

    def _stop_session(self) -> None:
        self._stop_event.set()
        if self._capture_thread:
            self._capture_thread.join(timeout=0.5)

    def _capture_loop(self) -> None:
        start_ts = time.time()
        frames: list[bytes] = []
        silence_ms = 0
        min_active_ms = 2000  # Minimum 2s capture before allowing silence cutoff
        speech_started = False  # Track if we've heard any speech
        initial_wait_ms = 3000  # Wait up to 3s for speech to start
        
        while not self._stop_event.is_set():
            elapsed_ms = int((time.time() - start_ts) * 1000)
            
            if time.time() - start_ts >= self.max_capture_seconds:
                self.logger.info("STT capture reached max duration")
                break
            try:
                # Read at native 44100Hz
                data, overflowed = self.stream.read(self.native_frames_per_buffer)
                if overflowed:
                    self.logger.debug("Audio buffer overflow (non-fatal)")
                
                # Convert to float for resampling
                samples_float = data.flatten().astype(np.float32) / 32768.0
                
                # Resample 44100 -> 16000
                resampled = signal.resample(samples_float, self.frames_per_buffer)
                
                # Convert back to int16 PCM bytes
                samples_16k = (resampled * 32768.0).astype(np.int16)
                pcm = samples_16k.tobytes()
            except Exception as exc:
                self.logger.error("STT capture read failed: %s", exc)
                break
            frames.append(pcm)
            samples = np.frombuffer(pcm, dtype=np.int16)
            amplitude = float(np.max(np.abs(samples))) / 32768.0 if samples.size else 0.0
            
            # Track if speech has started
            if amplitude >= self.silence_threshold:
                speech_started = True
                silence_ms = 0
            else:
                silence_ms += self.frame_ms
            
            # Only check for silence cutoff AFTER:
            # 1. Speech has started, OR we've waited initial_wait_ms
            # 2. We have at least min_active_ms of audio
            # 3. We've had silence_duration_ms of continuous silence
            can_check_silence = (speech_started or elapsed_ms >= initial_wait_ms) and elapsed_ms >= min_active_ms
            
            if can_check_silence and silence_ms >= self.silence_duration_ms:
                self.logger.info("Silence tail reached after %dms (speech_started=%s); closing session", 
                               elapsed_ms, speech_started)
                break
                
        self._emit_transcription(frames, start_ts)

    # --------------------- transcription ---------------------

    def _emit_transcription(self, frames: list[bytes], start_ts: float) -> None:
        capture_ms = int((time.time() - start_ts) * 1000)
        if not frames:
            payload = self._build_payload("", 0.0, capture_ms, 0, capture_ms)
            publish_json(self.publisher, TOPIC_STT, payload)
            return
        audio = b"".join(frames)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp:
            tmp_path = Path(tmp.name)
        try:
            self._write_wav(tmp_path, audio)
            whisper_ms, text, confidence = self._transcribe(tmp_path)
        finally:
            tmp_path.unlink(missing_ok=True)
        total_ms = int((time.time() - start_ts) * 1000)
        payload = self._build_payload(text, confidence, capture_ms, whisper_ms, total_ms)
        publish_json(self.publisher, TOPIC_STT, payload)

    def _transcribe(self, wav_path: Path) -> tuple[int, str, float]:
        self._ensure_model()
        start = time.time()
        text, confidence = transcribe_fast(self._model, wav_path, self.language)
        return int((time.time() - start) * 1000), text.strip(), float(confidence)

    def _ensure_model(self) -> None:
        if self._model is not None:
            return
        stt_cfg = self.config.get("stt", {})
        fw_cfg = stt_cfg.get("engines", {}).get("faster_whisper", {})
        model_name = fw_cfg.get("model") or fw_cfg.get("model_path") or "tiny.en"
        compute_type = fw_cfg.get("compute_type", "int8")
        device = fw_cfg.get("device", "cpu")
        download_root = fw_cfg.get("download_root") or (Path(os.environ.get("PROJECT_ROOT", ".")) / "third_party/whisper-fast")
        self.logger.info("Loading faster-whisper model %s (device=%s, compute=%s)", model_name, device, compute_type)
        self._model = load_fast_model(model_name, device=device, compute_type=compute_type, download_root=Path(download_root))

    # --------------------- utilities ---------------------

    def _write_wav(self, path: Path, pcm: bytes) -> None:
        import wave

        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(self.sample_rate)
            wf.writeframes(pcm)

    def _build_payload(self, text: str, confidence: float, capture_ms: int, whisper_ms: int, total_ms: int) -> Dict[str, Any]:
        return {
            "timestamp": int(time.time()),
            "text": text,
            "confidence": float(confidence),
            "language": self.language,
            "durations_ms": {
                "capture": capture_ms,
                "whisper": whisper_ms,
                "total": total_ms,
            },
            "kind": "final",
        }

    def _open_stream(self) -> sd.InputStream:
        """Open sounddevice stream by device NAME for proper ALSA dsnoop usage."""
        # Use device name directly - this enables ALSA plugin chain (dsnoop)
        device = self.mic_device if self.mic_device != "default" else None
        
        try:
            stream = sd.InputStream(
                device=device,
                samplerate=NATIVE_SAMPLE_RATE,
                channels=1,
                dtype='int16',
                blocksize=self.native_frames_per_buffer,
            )
            stream.start()
            self.logger.info(
                "Opened audio stream: device=%s, rate=%d, blocksize=%d",
                self.mic_device, NATIVE_SAMPLE_RATE, self.native_frames_per_buffer
            )
            return stream
        except Exception as exc:
            self.logger.error("Failed to open STT capture stream on '%s': %s", self.mic_device, exc)
            raise RuntimeError("Unable to open microphone for STT") from exc


def main() -> None:
    parser = argparse.ArgumentParser(description="STT service (faster-whisper)")
    parser.add_argument("--config", default="config/system.yaml")
    args = parser.parse_args()

    service = STTService(Path(args.config))
    try:
        service.run()
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(f"[stt] Fatal error: {exc}", file=sys.stderr)
        raise


def entrypoint() -> None:
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        print(f"[stt] Unhandled exception: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    entrypoint()
