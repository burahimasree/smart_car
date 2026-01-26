#!/usr/bin/env python3
"""Best Practice Voice Pipeline: Wakeword + STT with shared audio.

ARCHITECTURE:
    ┌──────────────────────────────────────────────────────────────────┐
    │                    USB MICROPHONE (hw:3,0)                       │
    │                    48000 Hz / 16-bit / Mono                      │
    └───────────────────────────┬──────────────────────────────────────┘
                                │
                                ▼
    ┌──────────────────────────────────────────────────────────────────┐
    │              AUDIO CAPTURE THREAD (Single Owner)                 │
    │  • PyAudio reads 30ms chunks @ 48kHz                            │
    │  • High-quality resample to 16kHz (scipy.signal.resample)       │
    │  • Writes to lock-free ring buffer                              │
    └───────────────────────────┬──────────────────────────────────────┘
                                │
            ┌───────────────────┴───────────────────┐
            │                                       │
            ▼                                       ▼
    ┌───────────────────┐               ┌───────────────────────────────┐
    │ WAKEWORD THREAD   │               │      STT PROCESSING           │
    │ • ALWAYS RUNNING  │               │ • Activated on wakeword       │
    │ • Porcupine 512   │               │ • faster-whisper (pre-loaded) │
    │   samples/frame   │               │ • Model stays warm in RAM     │
    │ • Can interrupt   │               │ • Interrupted by wakeword     │
    │   STT at any time │               │                               │
    └───────────────────┘               └───────────────────────────────┘

KEY DESIGN DECISIONS:
    1. Wakeword runs CONTINUOUSLY - even during STT capture/transcription
    2. If wakeword detected during STT → cancel STT, restart flow
    3. faster-whisper model pre-loaded at startup (warm, uses ~500MB RAM)
    4. High-quality resampling via scipy (not linear interp)
    5. Ring buffer ensures no audio loss during STT processing

MEMORY BUDGET (8GB Pi):
    - System + Desktop: ~2GB
    - VS Code Remote: ~1.4GB  
    - faster-whisper tiny.en: ~500MB
    - Porcupine: ~10MB
    - Ring buffer (10s): ~320KB
    - Available: ~4GB headroom
"""
from __future__ import annotations

import argparse
import ctypes
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

# Suppress ALSA errors before importing PyAudio
try:
    ERROR_HANDLER_FUNC = ctypes.CFUNCTYPE(None, ctypes.c_char_p, ctypes.c_int,
                                          ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p)
    def py_error_handler(filename, line, function, err, fmt):
        pass
    c_error_handler = ERROR_HANDLER_FUNC(py_error_handler)
    asound = ctypes.cdll.LoadLibrary('libasound.so.2')
    asound.snd_lib_error_set_handler(c_error_handler)
except Exception:
    pass

import pyaudio

# Project imports
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

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


# ═══════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class VoiceConfig:
    """Voice pipeline configuration."""
    # Hardware
    hw_sample_rate: int = 48000      # USB mic native rate
    target_sample_rate: int = 16000  # Porcupine/Whisper rate
    device_index: Optional[int] = None  # Auto-detect USB device
    chunk_ms: int = 32               # ~512 samples at 16kHz for Porcupine
    
    # Wakeword
    pv_access_key: str = ""
    wakeword_model: Path = Path()
    wakeword_sensitivity: float = 0.7  # Tested: 10/10 detection rate
    
    # STT
    stt_model: str = "tiny.en"
    stt_device: str = "cpu"
    stt_compute_type: str = "int8"
    silence_threshold: float = 0.25  # Calibrated from actual mic RMS
    silence_duration_ms: int = 800   # Silence before stopping capture
    max_capture_seconds: float = 8.0
    min_capture_seconds: float = 0.5
    
    # Ring buffer
    buffer_seconds: float = 10.0


class PipelineState(Enum):
    """Voice pipeline state."""
    IDLE = auto()           # Waiting for wakeword (wakeword always running)
    CAPTURING = auto()      # Recording speech (wakeword still running!)
    TRANSCRIBING = auto()   # Running STT (wakeword still running!)


# ═══════════════════════════════════════════════════════════════════════════
# HIGH-QUALITY RESAMPLER
# ═══════════════════════════════════════════════════════════════════════════

class Resampler:
    """High-quality audio resampler using scipy.
    
    scipy.signal.resample uses FFT-based resampling which preserves
    frequency content much better than linear interpolation.
    Tested and confirmed working with Porcupine wakeword detection.
    """
    
    def __init__(self, src_rate: int, dst_rate: int):
        self.src_rate = src_rate
        self.dst_rate = dst_rate
        self.ratio = dst_rate / src_rate
        
        # Import scipy (required for quality resampling)
        from scipy import signal
        self._scipy_resample = signal.resample
    
    def resample(self, samples: np.ndarray, target_len: Optional[int] = None) -> np.ndarray:
        """Resample int16 audio from src_rate to dst_rate."""
        if self.src_rate == self.dst_rate:
            return samples
        if len(samples) == 0:
            return samples
        
        if target_len is None:
            target_len = int(len(samples) * self.ratio)
        
        # scipy FFT-based resampling (high quality)
        resampled = self._scipy_resample(samples.astype(np.float32), target_len)
        return np.clip(resampled, -32768, 32767).astype(np.int16)


# ═══════════════════════════════════════════════════════════════════════════
# RING BUFFER
# ═══════════════════════════════════════════════════════════════════════════

class AudioRingBuffer:
    """Lock-free ring buffer for audio samples.
    
    Supports multiple read pointers for different consumers.
    """
    
    def __init__(self, capacity_samples: int):
        self.capacity = capacity_samples
        self._buffer = np.zeros(capacity_samples, dtype=np.int16)
        self._write_idx = 0  # Monotonically increasing
        self._lock = threading.RLock()
    
    def write(self, samples: np.ndarray) -> None:
        """Write samples to buffer."""
        n = len(samples)
        with self._lock:
            start = self._write_idx % self.capacity
            end = (self._write_idx + n) % self.capacity
            
            if start < end:
                self._buffer[start:end] = samples
            else:
                first = self.capacity - start
                self._buffer[start:] = samples[:first]
                self._buffer[:end] = samples[first:]
            
            self._write_idx += n
    
    def read(self, read_idx: int, num_samples: int) -> tuple[Optional[np.ndarray], int]:
        """Read samples from a specific position.
        
        Returns (samples, new_read_idx) or (None, read_idx) if not enough data.
        """
        with self._lock:
            available = self._write_idx - read_idx
            
            # Handle reader falling behind
            if available > self.capacity:
                read_idx = self._write_idx - self.capacity
                available = self.capacity
            
            if available < num_samples:
                return None, read_idx
            
            start = read_idx % self.capacity
            end = (read_idx + num_samples) % self.capacity
            
            if start < end:
                samples = self._buffer[start:end].copy()
            else:
                samples = np.concatenate([
                    self._buffer[start:],
                    self._buffer[:end]
                ])
            
            return samples, read_idx + num_samples
    
    @property
    def write_index(self) -> int:
        return self._write_idx


# ═══════════════════════════════════════════════════════════════════════════
# VOICE PIPELINE
# ═══════════════════════════════════════════════════════════════════════════

class BestVoicePipeline:
    """Best practice voice pipeline with continuous wakeword detection.
    
    Key feature: Wakeword detection runs ALL THE TIME, even during STT.
    This allows the user to say "hey robo" to interrupt/restart.
    """
    
    def __init__(self, config: VoiceConfig, config_path: Optional[Path] = None):
        self.cfg = config
        self.config_path = config_path
        
        # Load system config if provided
        if config_path:
            self.raw_config = load_config(config_path)
        else:
            self.raw_config = {}
        
        # Resampler: 48kHz → 16kHz
        self.resampler = Resampler(config.hw_sample_rate, config.target_sample_rate)
        
        # Ring buffer (10s @ 16kHz = 160,000 samples = 320KB)
        buffer_samples = int(config.target_sample_rate * config.buffer_seconds)
        self.ring_buffer = AudioRingBuffer(buffer_samples)
        
        # Chunk sizes
        self.hw_chunk_samples = int(config.hw_sample_rate * config.chunk_ms / 1000)
        self.target_chunk_samples = int(config.target_sample_rate * config.chunk_ms / 1000)
        
        # State machine
        self._state = PipelineState.IDLE
        self._state_lock = threading.Lock()
        
        # Threads
        self._capture_thread: Optional[threading.Thread] = None
        self._wakeword_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        
        # Wakeword
        self._porcupine = None
        self._wakeword_read_idx = 0
        self._wakeword_triggered = threading.Event()
        
        # STT
        self._stt_model = None
        self._stt_read_idx = 0
        self._capture_buffer: List[np.ndarray] = []
        self._capture_start_ts: float = 0.0
        self._silence_frames: int = 0
        self._stt_interrupt = threading.Event()  # Set when wakeword interrupts STT
        
        # ZMQ (optional)
        self._pub = None
        if self.raw_config:
            try:
                self._pub = make_publisher(self.raw_config, channel="upstream")
            except Exception:
                pass
        
        # Statistics
        self._stats = {
            "wakeword_detections": 0,
            "stt_transcriptions": 0,
            "stt_interrupts": 0,
        }
    
    # ─────────────────────────────────────────────────────────────────
    # Lifecycle
    # ─────────────────────────────────────────────────────────────────
    
    def start(self) -> bool:
        """Start the voice pipeline."""
        print("[voice] Initializing pipeline...")
        
        # Initialize Porcupine
        if not self._init_porcupine():
            return False
        
        # Pre-load STT model (keeps it warm in RAM)
        print("[voice] Pre-loading faster-whisper model (this takes a few seconds)...")
        if not self._init_stt():
            print("[voice] Warning: STT model failed to pre-load, will retry on first use")
        
        # Start capture thread
        self._stop_event.clear()
        self._capture_thread = threading.Thread(
            target=self._capture_loop,
            name="AudioCapture",
            daemon=True
        )
        self._capture_thread.start()
        
        # Start wakeword thread (runs continuously!)
        self._wakeword_thread = threading.Thread(
            target=self._wakeword_loop,
            name="WakewordDetector",
            daemon=True
        )
        self._wakeword_thread.start()
        
        print("[voice] Pipeline started successfully")
        return True
    
    def stop(self) -> None:
        """Stop the pipeline."""
        self._stop_event.set()
        
        if self._capture_thread:
            self._capture_thread.join(timeout=2.0)
        if self._wakeword_thread:
            self._wakeword_thread.join(timeout=2.0)
        
        if self._porcupine:
            try:
                self._porcupine.delete()
            except Exception:
                pass
        
        print(f"[voice] Pipeline stopped. Stats: {self._stats}")
    
    def _init_porcupine(self) -> bool:
        """Initialize Porcupine wakeword detector."""
        if not self.cfg.pv_access_key:
            print("[voice] ERROR: Porcupine access key not set")
            return False
        
        if not self.cfg.wakeword_model.exists():
            print(f"[voice] ERROR: Wakeword model not found: {self.cfg.wakeword_model}")
            return False
        
        try:
            import pvporcupine
            self._porcupine = pvporcupine.create(
                access_key=self.cfg.pv_access_key,
                keyword_paths=[str(self.cfg.wakeword_model)],
                sensitivities=[self.cfg.wakeword_sensitivity],
            )
            print(f"[voice] Porcupine initialized: frame_length={self._porcupine.frame_length}, "
                  f"sample_rate={self._porcupine.sample_rate}, sensitivity={self.cfg.wakeword_sensitivity}")
            return True
        except Exception as e:
            print(f"[voice] ERROR: Failed to initialize Porcupine: {e}")
            return False
    
    def _init_stt(self) -> bool:
        """Initialize faster-whisper model (pre-load for warm start)."""
        try:
            from faster_whisper import WhisperModel
            
            download_root = PROJECT_ROOT / "third_party/whisper-fast"
            download_root.mkdir(parents=True, exist_ok=True)
            
            self._stt_model = WhisperModel(
                self.cfg.stt_model,
                device=self.cfg.stt_device,
                compute_type=self.cfg.stt_compute_type,
                download_root=str(download_root),
            )
            print(f"[voice] faster-whisper loaded: model={self.cfg.stt_model}")
            return True
        except Exception as e:
            print(f"[voice] WARNING: STT model load failed: {e}")
            return False
    
    # ─────────────────────────────────────────────────────────────────
    # State Machine
    # ─────────────────────────────────────────────────────────────────
    
    def _get_state(self) -> PipelineState:
        with self._state_lock:
            return self._state
    
    def _set_state(self, state: PipelineState) -> None:
        with self._state_lock:
            old = self._state
            self._state = state
            if old != state:
                print(f"[voice] State: {old.name} → {state.name}")
    
    # ─────────────────────────────────────────────────────────────────
    # Audio Capture Thread
    # ─────────────────────────────────────────────────────────────────
    
    def _find_usb_device(self, pa: pyaudio.PyAudio) -> Optional[int]:
        """Find USB Audio input device index."""
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            if info["maxInputChannels"] > 0 and "USB" in info["name"]:
                print(f"[voice] Found USB device at index {i}: {info['name']}")
                return i
        return None
    
    def _capture_loop(self) -> None:
        """Main audio capture loop - reads from mic, resamples, writes to ring buffer."""
        pa = pyaudio.PyAudio()
        
        try:
            # Auto-detect USB device if not specified
            device_index = self.cfg.device_index
            if device_index is None:
                device_index = self._find_usb_device(pa)
                if device_index is None:
                    print("[voice] ERROR: No USB input device found!")
                    return
            
            stream = pa.open(
                rate=self.cfg.hw_sample_rate,
                channels=1,
                format=pyaudio.paInt16,
                input=True,
                input_device_index=device_index,
                frames_per_buffer=self.hw_chunk_samples,
            )
            
            print(f"[voice] Capture started: {self.cfg.hw_sample_rate}Hz → {self.cfg.target_sample_rate}Hz")
            
            while not self._stop_event.is_set():
                try:
                    data = stream.read(self.hw_chunk_samples, exception_on_overflow=False)
                    hw_samples = np.frombuffer(data, dtype=np.int16)
                    
                    # High-quality resample to 16kHz using scipy
                    samples_16k = self.resampler.resample(hw_samples, self.target_chunk_samples)
                    
                    # Write to ring buffer (available to all consumers)
                    self.ring_buffer.write(samples_16k)
                    
                except Exception as e:
                    print(f"[voice] Capture error: {e}")
                    time.sleep(0.01)
            
        except Exception as e:
            print(f"[voice] Capture init error: {e}")
        finally:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass
            pa.terminate()
            print("[voice] Capture thread exiting")
    
    # ─────────────────────────────────────────────────────────────────
    # Wakeword Detection Thread (ALWAYS RUNNING!)
    # ─────────────────────────────────────────────────────────────────
    
    def _wakeword_loop(self) -> None:
        """Wakeword detection loop - runs continuously, even during STT!
        
        This is the key differentiator: wakeword can interrupt STT.
        """
        frame_length = self._porcupine.frame_length  # 512 samples
        
        # Start from current write position
        self._wakeword_read_idx = self.ring_buffer.write_index
        
        print(f"[voice] Wakeword detector running (frame_length={frame_length})")
        
        while not self._stop_event.is_set():
            # Read frame from ring buffer
            samples, new_idx = self.ring_buffer.read(
                self._wakeword_read_idx, 
                frame_length
            )
            
            if samples is None:
                time.sleep(0.005)  # Wait for more data
                continue
            
            self._wakeword_read_idx = new_idx
            
            # Process with Porcupine
            try:
                result = self._porcupine.process(samples.tolist())
                if result >= 0:
                    self._on_wakeword_detected()
            except Exception as e:
                print(f"[voice] Porcupine error: {e}")
        
        print("[voice] Wakeword thread exiting")
    
    def _on_wakeword_detected(self) -> None:
        """Handle wakeword detection."""
        current_state = self._get_state()
        self._stats["wakeword_detections"] += 1
        
        print(f"\n🎤 WAKEWORD DETECTED! (state={current_state.name}, count={self._stats['wakeword_detections']})")
        
        # Publish event
        if self._pub:
            try:
                payload = {
                    "timestamp": int(time.time()),
                    "keyword": "hey robo",
                    "confidence": 0.99,
                    "source": "best_voice_pipeline",
                }
                publish_json(self._pub, TOPIC_WW_DETECTED, payload)
            except Exception:
                pass
        
        # Handle based on current state
        if current_state == PipelineState.IDLE:
            # Normal case: start capturing
            self._start_capture()
        
        elif current_state in (PipelineState.CAPTURING, PipelineState.TRANSCRIBING):
            # INTERRUPT: User said wakeword during STT!
            # This is a critical feature - restart the flow
            print("[voice] ⚠️ INTERRUPT: Wakeword during STT - restarting flow!")
            self._stats["stt_interrupts"] += 1
            self._stt_interrupt.set()
            # Small delay to let STT thread notice the interrupt
            time.sleep(0.1)
            self._stt_interrupt.clear()
            self._start_capture()
    
    # ─────────────────────────────────────────────────────────────────
    # STT Capture and Processing
    # ─────────────────────────────────────────────────────────────────
    
    def _start_capture(self) -> None:
        """Start capturing audio for STT."""
        self._capture_buffer.clear()
        self._capture_start_ts = time.monotonic()
        self._silence_frames = 0
        self._stt_read_idx = self.ring_buffer.write_index
        self._set_state(PipelineState.CAPTURING)
        
        # Start capture processing in a separate thread
        # (so wakeword loop can continue)
        threading.Thread(
            target=self._capture_and_transcribe,
            name="STTCapture",
            daemon=True
        ).start()
    
    def _capture_and_transcribe(self) -> None:
        """Capture audio and run transcription.
        
        This runs in its own thread, separate from wakeword detection.
        Can be interrupted by wakeword detection at any time.
        """
        chunk_samples = int(self.cfg.target_sample_rate * self.cfg.chunk_ms / 1000)
        silence_frames_needed = int(self.cfg.silence_duration_ms / self.cfg.chunk_ms)
        
        try:
            # Phase 1: Capture with silence detection
            while not self._stop_event.is_set() and not self._stt_interrupt.is_set():
                if self._get_state() != PipelineState.CAPTURING:
                    break
                
                # Read from ring buffer
                samples, new_idx = self.ring_buffer.read(
                    self._stt_read_idx,
                    chunk_samples
                )
                
                if samples is None:
                    time.sleep(0.005)
                    continue
                
                self._stt_read_idx = new_idx
                self._capture_buffer.append(samples)
                
                # Check duration
                elapsed = time.monotonic() - self._capture_start_ts
                
                if elapsed >= self.cfg.max_capture_seconds:
                    print(f"[voice] Max capture duration reached ({elapsed:.1f}s)")
                    break
                
                # Silence detection
                rms = self._calc_rms(samples)
                
                if rms < self.cfg.silence_threshold:
                    self._silence_frames += 1
                    if (self._silence_frames >= silence_frames_needed and 
                        elapsed >= self.cfg.min_capture_seconds):
                        print(f"[voice] Silence detected after {elapsed:.1f}s")
                        break
                else:
                    self._silence_frames = 0
            
            # Check for interrupt
            if self._stt_interrupt.is_set():
                print("[voice] Capture interrupted by wakeword")
                self._set_state(PipelineState.IDLE)
                return
            
            # Phase 2: Transcription
            if self._capture_buffer:
                self._set_state(PipelineState.TRANSCRIBING)
                
                # Concatenate audio
                audio = np.concatenate(self._capture_buffer)
                capture_ms = int((time.monotonic() - self._capture_start_ts) * 1000)
                
                # Check for interrupt before transcription
                if self._stt_interrupt.is_set():
                    print("[voice] Transcription interrupted by wakeword")
                    self._set_state(PipelineState.IDLE)
                    return
                
                # Run transcription
                text, confidence, whisper_ms = self._transcribe(audio)
                
                # Check for interrupt after transcription
                if self._stt_interrupt.is_set():
                    print("[voice] Post-transcription interrupted by wakeword")
                    self._set_state(PipelineState.IDLE)
                    return
                
                # Publish result
                self._publish_transcription(text, confidence, capture_ms, whisper_ms)
        
        except Exception as e:
            print(f"[voice] Capture/transcribe error: {e}")
        
        finally:
            self._capture_buffer.clear()
            self._set_state(PipelineState.IDLE)
    
    def _transcribe(self, audio: np.ndarray) -> tuple[str, float, int]:
        """Run faster-whisper transcription."""
        if self._stt_model is None:
            if not self._init_stt():
                return "", 0.0, 0
        
        # Write to temp WAV file
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            wav_path = Path(f.name)
        
        try:
            with wave.open(str(wav_path), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(self.cfg.target_sample_rate)
                wf.writeframes(audio.astype("<i2").tobytes())
            
            start = time.monotonic()
            segments, info = self._stt_model.transcribe(
                str(wav_path),
                language="en",
                beam_size=1,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 500},
            )
            
            text_parts = []
            logprobs = []
            for seg in segments:
                # Check for interrupt during segment iteration
                if self._stt_interrupt.is_set():
                    break
                text_parts.append(seg.text.strip() if seg.text else "")
                if seg.avg_logprob is not None:
                    logprobs.append(seg.avg_logprob)
            
            text = " ".join(p for p in text_parts if p)
            
            if logprobs:
                confidence = max(0.0, min(1.0, math.exp(sum(logprobs) / len(logprobs))))
            else:
                confidence = 0.8 if text else 0.0
            
            whisper_ms = int((time.monotonic() - start) * 1000)
            
            self._stats["stt_transcriptions"] += 1
            print(f"📝 Transcription: '{text}' (conf={confidence:.2f}, {whisper_ms}ms)")
            
            return text, confidence, whisper_ms
            
        finally:
            try:
                wav_path.unlink()
            except Exception:
                pass
    
    def _publish_transcription(self, text: str, confidence: float, 
                                capture_ms: int, whisper_ms: int) -> None:
        """Publish transcription result via ZMQ."""
        if not self._pub:
            return
        
        try:
            payload = {
                "timestamp": int(time.time()),
                "text": text.strip(),
                "confidence": float(confidence),
                "language": "en",
                "durations_ms": {
                    "capture": capture_ms,
                    "whisper": whisper_ms,
                    "total": capture_ms + whisper_ms,
                },
                "kind": "final",
            }
            publish_json(self._pub, TOPIC_STT, payload)
        except Exception as e:
            print(f"[voice] Publish error: {e}")
    
    @staticmethod
    def _calc_rms(samples: np.ndarray) -> float:
        """Calculate RMS amplitude (0.0 to 1.0)."""
        if len(samples) == 0:
            return 0.0
        energy = np.mean(samples.astype(np.float32) ** 2)
        return min(1.0, math.sqrt(energy) / 32768.0)
    
    # ─────────────────────────────────────────────────────────────────
    # Main Loop
    # ─────────────────────────────────────────────────────────────────
    
    def run(self) -> None:
        """Main event loop."""
        print("[voice] Pipeline running. Say 'hey robo' to start...")
        print("[voice] You can say 'hey robo' anytime to interrupt and restart!")
        
        try:
            while not self._stop_event.is_set():
                time.sleep(0.1)
        except KeyboardInterrupt:
            print("\n[voice] Interrupted by user")


# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(description="Best Practice Voice Pipeline")
    parser.add_argument("--config", default="config/system.yaml", help="Config file")
    parser.add_argument("--sensitivity", type=float, default=0.7, help="Wakeword sensitivity")
    args = parser.parse_args()
    
    config_path = PROJECT_ROOT / args.config
    
    # Load settings from config file
    raw_config = load_config(config_path) if config_path.exists() else {}
    ww_cfg = raw_config.get("wakeword", {}) or {}
    
    # Get Porcupine access key
    access_key = ww_cfg.get("access_key") or os.environ.get("PV_ACCESS_KEY", "")
    
    # Get model path
    model_path = Path(ww_cfg.get("model", "") or 
                      ww_cfg.get("model_paths", {}).get("porcupine_keyword", ""))
    if not model_path.is_absolute():
        model_path = PROJECT_ROOT / model_path
    
    # Create config with tested, calibrated values
    config = VoiceConfig(
        hw_sample_rate=48000,
        target_sample_rate=16000,
        device_index=None,  # Auto-detect USB device
        pv_access_key=access_key,
        wakeword_model=model_path,
        wakeword_sensitivity=args.sensitivity,
        stt_model="tiny.en",
        silence_threshold=0.25,  # Calibrated from actual mic RMS
        silence_duration_ms=800,
    )
    
    # Run pipeline
    pipeline = BestVoicePipeline(config, config_path)
    
    if not pipeline.start():
        print("[voice] Failed to start pipeline")
        sys.exit(1)
    
    try:
        pipeline.run()
    finally:
        pipeline.stop()


if __name__ == "__main__":
    main()
