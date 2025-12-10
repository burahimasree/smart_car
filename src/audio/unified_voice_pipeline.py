"""Unified Voice Pipeline: Wakeword → STT → LLM integration with shared audio.

This module provides a single-process voice assistant pipeline that:
1. Owns the microphone exclusively via UnifiedAudioCapture
2. Runs wakeword detection continuously in background
3. Switches to STT capture when wakeword triggers
4. Publishes STT results to the orchestrator

PIPELINE STATES:
    LISTENING → [wakeword detected] → CAPTURING → [silence] → PROCESSING → LISTENING
                                                              ↓
                                                         STT Result Published

THREAD MODEL:
    Main Thread: Event loop, state machine, ZMQ pub/sub
    Audio Thread: UnifiedAudioCapture (ring buffer writer)
    Wakeword Thread: Porcupine processing
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
import threading
import time
import wave
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import zmq

# Project imports
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.audio.unified_audio import (
    AudioConfig,
    AudioState,
    UnifiedAudioCapture,
    get_unified_audio,
)
from src.core.config_loader import load_config
from src.core.ipc import (
    TOPIC_CMD_LISTEN_START,
    TOPIC_CMD_LISTEN_STOP,
    TOPIC_STT,
    TOPIC_WW_DETECTED,
    make_publisher,
    make_subscriber,
    publish_json,
)
from src.core.logging_setup import get_logger


class PipelineState(Enum):
    """Voice pipeline state machine."""
    IDLE = auto()           # Waiting for wakeword
    CAPTURING = auto()      # Recording user speech
    TRANSCRIBING = auto()   # Running STT inference
    COOLDOWN = auto()       # Brief pause after TTS


@dataclass
class VoiceConfig:
    """Configuration for the voice pipeline."""
    # Wakeword settings
    wakeword_sensitivity: float = 0.6
    wakeword_model_path: Optional[Path] = None
    pv_access_key: Optional[str] = None
    
    # STT settings
    silence_threshold: float = 0.35
    silence_duration_ms: int = 900
    max_capture_seconds: float = 10.0
    min_capture_seconds: float = 0.5
    stt_language: str = "en"
    stt_model: str = "tiny.en"
    stt_compute_type: str = "int8"
    stt_device: str = "cpu"
    stt_beam_size: int = 1
    
    # Audio settings
    sample_rate: int = 16000
    chunk_ms: int = 30


class UnifiedVoicePipeline:
    """Single-process voice pipeline with shared microphone.
    
    This class replaces the separate wakeword and STT services,
    eliminating microphone resource conflicts.
    """
    
    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path
        self.raw_config = load_config(config_path)
        
        # Setup logging
        log_dir = Path(self.raw_config.get("logs", {}).get("directory", "logs"))
        if not log_dir.is_absolute():
            log_dir = PROJECT_ROOT / log_dir
        self.logger = get_logger("voice.pipeline", log_dir)
        
        # Parse configuration
        self.voice_cfg = self._parse_config()
        
        # Initialize audio capture
        audio_cfg = AudioConfig(
            sample_rate=self.voice_cfg.sample_rate,
            chunk_ms=self.voice_cfg.chunk_ms,
            device_keyword=self.raw_config.get("audio", {}).get("preferred_device_substring", ""),
        )
        self.audio = get_unified_audio(audio_cfg, self.logger)
        
        # State machine
        self._state = PipelineState.IDLE
        self._state_lock = threading.Lock()
        
        # Wakeword detector
        self._porcupine = None
        self._wakeword_consumer_id = "wakeword"
        
        # STT model (lazy loaded)
        self._stt_model = None
        self._stt_consumer_id = "stt"
        
        # Capture buffer for STT
        self._capture_buffer: List[np.ndarray] = []
        self._capture_start_ts: float = 0.0
        self._silence_frames: int = 0
        
        # ZMQ sockets
        self.pub = make_publisher(self.raw_config, channel="upstream")
        self.cmd_sub = make_subscriber(
            self.raw_config, 
            channel="downstream",
            topic=TOPIC_CMD_LISTEN_START
        )
        self.cmd_sub.setsockopt(zmq.SUBSCRIBE, TOPIC_CMD_LISTEN_STOP)
        
        # Control
        self._running = True
        
    def _parse_config(self) -> VoiceConfig:
        """Extract voice configuration from raw config."""
        ww_cfg = self.raw_config.get("wakeword", {}) or {}
        stt_cfg = self.raw_config.get("stt", {}) or {}
        fw_cfg = stt_cfg.get("engines", {}).get("faster_whisper", {}) or {}
        
        # Find wakeword model
        model_path = None
        for key in ["model", "model_path"]:
            if ww_cfg.get(key):
                model_path = Path(ww_cfg[key])
                break
        if not model_path:
            mp = ww_cfg.get("model_paths", {})
            if mp.get("porcupine_keyword"):
                model_path = Path(mp["porcupine_keyword"])
        
        # Access key
        access_key = (
            ww_cfg.get("access_key") 
            or os.environ.get("PV_ACCESS_KEY")
        )
        
        return VoiceConfig(
            wakeword_sensitivity=float(ww_cfg.get("sensitivity", 0.6)),
            wakeword_model_path=model_path,
            pv_access_key=access_key,
            silence_threshold=float(stt_cfg.get("silence_threshold", 0.35)),
            silence_duration_ms=int(stt_cfg.get("silence_duration_ms", 900)),
            max_capture_seconds=float(stt_cfg.get("max_capture_seconds", 10.0)),
            stt_language=stt_cfg.get("language", "en"),
            stt_model=fw_cfg.get("model", "tiny.en"),
            stt_compute_type=fw_cfg.get("compute_type", "int8"),
            stt_device=fw_cfg.get("device", "cpu"),
            stt_beam_size=int(fw_cfg.get("beam_size", 1)),
            sample_rate=int(stt_cfg.get("sample_rate", 16000)),
            chunk_ms=int(self.raw_config.get("audio", {}).get("wakeword_frame_ms", 30)),
        )
    
    # ─────────────────────────────────────────────────────────────────
    # Lifecycle
    # ─────────────────────────────────────────────────────────────────
    
    def start(self) -> bool:
        """Initialize and start the voice pipeline."""
        # Start audio capture
        if not self.audio.start():
            self.logger.error("Failed to start audio capture")
            return False
        
        # Initialize wakeword detector
        if not self._init_wakeword():
            self.logger.error("Failed to initialize wakeword detector")
            return False
        
        # Register consumers
        self.audio.register_consumer(self._wakeword_consumer_id, priority=5)
        self.audio.register_consumer(self._stt_consumer_id, priority=10)
        
        self.logger.info("Voice pipeline started successfully")
        return True
    
    def stop(self) -> None:
        """Stop the voice pipeline."""
        self._running = False
        self.audio.unregister_consumer(self._wakeword_consumer_id)
        self.audio.unregister_consumer(self._stt_consumer_id)
        
        if self._porcupine:
            try:
                self._porcupine.delete()
            except Exception:
                pass
            self._porcupine = None
        
        self.logger.info("Voice pipeline stopped")
    
    def _init_wakeword(self) -> bool:
        """Initialize Porcupine wakeword detector."""
        if not self.voice_cfg.pv_access_key:
            self.logger.error("Porcupine access key not configured")
            return False
            
        if not self.voice_cfg.wakeword_model_path:
            self.logger.error("Wakeword model path not configured")
            return False
            
        if not self.voice_cfg.wakeword_model_path.exists():
            self.logger.error(f"Wakeword model not found: {self.voice_cfg.wakeword_model_path}")
            return False
        
        try:
            import pvporcupine
            self._porcupine = pvporcupine.create(
                access_key=self.voice_cfg.pv_access_key,
                keyword_paths=[str(self.voice_cfg.wakeword_model_path)],
                sensitivities=[self.voice_cfg.wakeword_sensitivity],
            )
            self.logger.info(
                f"Porcupine initialized: frame_length={self._porcupine.frame_length}, "
                f"sample_rate={self._porcupine.sample_rate}"
            )
            return True
        except Exception as e:
            self.logger.error(f"Failed to initialize Porcupine: {e}")
            return False
    
    def _ensure_stt_model(self) -> None:
        """Lazy-load the faster-whisper model."""
        if self._stt_model is not None:
            return
            
        try:
            from faster_whisper import WhisperModel
            
            download_root = PROJECT_ROOT / "third_party/whisper-fast"
            self.logger.info(
                f"Loading faster-whisper: model={self.voice_cfg.stt_model}, "
                f"device={self.voice_cfg.stt_device}"
            )
            
            self._stt_model = WhisperModel(
                self.voice_cfg.stt_model,
                device=self.voice_cfg.stt_device,
                compute_type=self.voice_cfg.stt_compute_type,
                download_root=str(download_root),
            )
            self.logger.info("STT model loaded successfully")
        except Exception as e:
            self.logger.error(f"Failed to load STT model: {e}")
            raise
    
    # ─────────────────────────────────────────────────────────────────
    # State Machine
    # ─────────────────────────────────────────────────────────────────
    
    def _set_state(self, state: PipelineState) -> None:
        """Transition to a new state."""
        with self._state_lock:
            old = self._state
            self._state = state
            self.logger.info(f"State: {old.name} → {state.name}")
    
    def _get_state(self) -> PipelineState:
        """Get current state."""
        with self._state_lock:
            return self._state
    
    # ─────────────────────────────────────────────────────────────────
    # Main Event Loop
    # ─────────────────────────────────────────────────────────────────
    
    def run(self) -> None:
        """Main event loop."""
        self.logger.info("Voice pipeline running")
        
        # ZMQ poller for commands
        poller = zmq.Poller()
        poller.register(self.cmd_sub, zmq.POLLIN)
        
        while self._running:
            # Check for commands (non-blocking)
            self._process_commands(poller)
            
            # Process based on current state
            state = self._get_state()
            
            if state == PipelineState.IDLE:
                self._process_wakeword()
            elif state == PipelineState.CAPTURING:
                self._process_capture()
            elif state == PipelineState.TRANSCRIBING:
                # Transcription happens synchronously
                pass
            elif state == PipelineState.COOLDOWN:
                time.sleep(0.1)
                self._set_state(PipelineState.IDLE)
    
    def _process_commands(self, poller: zmq.Poller) -> None:
        """Process incoming ZMQ commands."""
        try:
            socks = dict(poller.poll(timeout=0))
        except zmq.ZMQError:
            return
            
        if self.cmd_sub not in socks:
            return
            
        while True:
            try:
                topic, raw = self.cmd_sub.recv_multipart(zmq.NOBLOCK)
            except zmq.Again:
                break
            
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
                
            if topic == TOPIC_CMD_LISTEN_START:
                # Manual trigger from orchestrator
                self.logger.info("Manual listen start received")
                self._trigger_capture()
            elif topic == TOPIC_CMD_LISTEN_STOP:
                # Cancel capture
                self.logger.info("Listen stop received")
                if self._get_state() == PipelineState.CAPTURING:
                    self._finalize_capture()
    
    def _process_wakeword(self) -> None:
        """Process audio for wakeword detection."""
        if self._porcupine is None:
            time.sleep(0.01)
            return
            
        frame_length = self._porcupine.frame_length
        samples = self.audio.read_chunk(
            self._wakeword_consumer_id,
            num_samples=frame_length,
            blocking=True,
            timeout_ms=100
        )
        
        if samples is None or len(samples) < frame_length:
            return
            
        try:
            result = self._porcupine.process(samples.tolist())
            if result >= 0:
                self._on_wakeword_detected()
        except Exception as e:
            self.logger.error(f"Wakeword processing error: {e}")
    
    def _on_wakeword_detected(self) -> None:
        """Handle wakeword detection."""
        self.logger.info("🎤 Wakeword detected!")
        
        # Publish detection event
        ww_cfg = self.raw_config.get("wakeword", {}) or {}
        payload = {
            "timestamp": int(time.time()),
            "keyword": ww_cfg.get("payload_keyword", "wakeword"),
            "variant": ww_cfg.get("payload_variant", "wakeword"),
            "confidence": 0.99,
            "source": "unified_pipeline",
        }
        publish_json(self.pub, TOPIC_WW_DETECTED, payload)
        
        # Start capturing for STT
        self._trigger_capture()
    
    def _trigger_capture(self) -> None:
        """Start capturing audio for STT."""
        self._capture_buffer.clear()
        self._capture_start_ts = time.monotonic()
        self._silence_frames = 0
        self._set_state(PipelineState.CAPTURING)
        self.audio.set_state(AudioState.CAPTURING_STT)
    
    def _process_capture(self) -> None:
        """Capture audio for STT with silence detection."""
        chunk_samples = int(self.voice_cfg.sample_rate * self.voice_cfg.chunk_ms / 1000)
        
        samples = self.audio.read_chunk(
            self._stt_consumer_id,
            num_samples=chunk_samples,
            blocking=True,
            timeout_ms=100
        )
        
        if samples is None:
            return
            
        self._capture_buffer.append(samples)
        
        # Check capture duration
        elapsed = time.monotonic() - self._capture_start_ts
        
        if elapsed >= self.voice_cfg.max_capture_seconds:
            self.logger.info("Max capture duration reached")
            self._finalize_capture()
            return
        
        # Silence detection (RMS-based)
        rms = self._calc_rms(samples)
        silence_frames_threshold = int(
            self.voice_cfg.silence_duration_ms / self.voice_cfg.chunk_ms
        )
        
        if rms < self.voice_cfg.silence_threshold:
            self._silence_frames += 1
            if (self._silence_frames >= silence_frames_threshold and 
                elapsed >= self.voice_cfg.min_capture_seconds):
                self.logger.info("Silence detected, finalizing capture")
                self._finalize_capture()
        else:
            self._silence_frames = 0
    
    def _finalize_capture(self) -> None:
        """Finalize capture and run STT."""
        self._set_state(PipelineState.TRANSCRIBING)
        self.audio.set_state(AudioState.IDLE)
        
        capture_ms = int((time.monotonic() - self._capture_start_ts) * 1000)
        
        if not self._capture_buffer:
            self._publish_empty_transcription(capture_ms)
            self._set_state(PipelineState.IDLE)
            return
        
        # Concatenate captured audio
        audio_data = np.concatenate(self._capture_buffer)
        self._capture_buffer.clear()
        
        # Run transcription
        try:
            self._ensure_stt_model()
            text, confidence, whisper_ms = self._transcribe(audio_data)
            
            total_ms = capture_ms + whisper_ms
            payload = {
                "timestamp": int(time.time()),
                "text": text.strip(),
                "confidence": float(confidence),
                "language": self.voice_cfg.stt_language,
                "durations_ms": {
                    "capture": capture_ms,
                    "whisper": whisper_ms,
                    "total": total_ms,
                },
                "kind": "final",
            }
            
            self.logger.info(
                f"📝 Transcription: '{text[:50]}...' "
                f"(conf={confidence:.2f}, total={total_ms}ms)"
            )
            publish_json(self.pub, TOPIC_STT, payload)
            
        except Exception as e:
            self.logger.error(f"Transcription failed: {e}")
            self._publish_empty_transcription(capture_ms)
        
        self._set_state(PipelineState.COOLDOWN)
    
    def _transcribe(self, audio: np.ndarray) -> tuple[str, float, int]:
        """Run faster-whisper transcription."""
        # Write to temp WAV file
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            wav_path = Path(f.name)
        
        try:
            with wave.open(str(wav_path), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(self.voice_cfg.sample_rate)
                wf.writeframes(audio.astype("<i2").tobytes())
            
            start = time.monotonic()
            segments, info = self._stt_model.transcribe(
                str(wav_path),
                language=self.voice_cfg.stt_language,
                beam_size=self.voice_cfg.stt_beam_size,
                vad_filter=False,
            )
            
            text_parts = []
            logprobs = []
            for seg in segments:
                text_parts.append(seg.text.strip() if seg.text else "")
                if seg.avg_logprob is not None:
                    logprobs.append(seg.avg_logprob)
            
            text = " ".join(p for p in text_parts if p)
            
            if logprobs:
                confidence = max(0.0, min(1.0, math.exp(sum(logprobs) / len(logprobs))))
            else:
                confidence = 0.8 if text else 0.0
            
            whisper_ms = int((time.monotonic() - start) * 1000)
            return text, confidence, whisper_ms
            
        finally:
            try:
                wav_path.unlink()
            except Exception:
                pass
    
    def _publish_empty_transcription(self, capture_ms: int) -> None:
        """Publish an empty transcription result."""
        payload = {
            "timestamp": int(time.time()),
            "text": "",
            "confidence": 0.0,
            "language": self.voice_cfg.stt_language,
            "durations_ms": {
                "capture": capture_ms,
                "whisper": 0,
                "total": capture_ms,
            },
            "kind": "final",
        }
        publish_json(self.pub, TOPIC_STT, payload)
    
    @staticmethod
    def _calc_rms(samples: np.ndarray) -> float:
        """Calculate RMS amplitude (0.0 to 1.0)."""
        if len(samples) == 0:
            return 0.0
        energy = np.mean(samples.astype(np.float32) ** 2)
        return min(1.0, math.sqrt(energy) / 32768.0)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Unified Voice Pipeline (Wakeword + STT)"
    )
    parser.add_argument(
        "--config", 
        default="config/system.yaml",
        help="Path to system configuration"
    )
    args = parser.parse_args()
    
    pipeline = UnifiedVoicePipeline(Path(args.config))
    
    if not pipeline.start():
        print("[voice] Failed to start pipeline", file=sys.stderr)
        sys.exit(1)
    
    try:
        pipeline.run()
    except KeyboardInterrupt:
        pass
    finally:
        pipeline.stop()


if __name__ == "__main__":
    main()
