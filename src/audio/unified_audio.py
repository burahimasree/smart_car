"""Unified Audio Pipeline: Single microphone owner with multi-consumer fan-out.

This module implements a SINGLE-WRITER / MULTI-READER audio architecture that
solves the microphone resource contention problem on Raspberry Pi.

ARCHITECTURE:
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
              ┌───────────────────────────┼───────────────────────────┐
              │                           │                           │
              ▼                           ▼                           ▼
    ┌─────────────────┐         ┌─────────────────┐         ┌─────────────────┐
    │ WakewordConsumer│         │   STTConsumer   │         │ Future Consumer │
    │ (read_index A)  │         │ (read_index B)  │         │ (read_index C)  │
    │ Porcupine       │         │ faster-whisper  │         │ ...             │
    └─────────────────┘         └─────────────────┘         └─────────────────┘

BENEFITS:
- Single PyAudio/ALSA open - no resource conflicts
- Lock-free ring buffer for low-latency fan-out  
- Each consumer maintains its own read pointer
- Zero-copy for consumers reading same buffer
- Graceful handling of slow consumers (they skip old audio)
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Callable, Dict, Optional, List
import os

import numpy as np

try:
    import pyaudio
except ImportError:
    pyaudio = None  # type: ignore


class AudioState(Enum):
    """Current state of the audio pipeline."""
    IDLE = auto()           # Wakeword listening
    CAPTURING_STT = auto()  # Recording for STT
    PLAYING_TTS = auto()    # TTS playback (mic can still capture)


@dataclass
class AudioConsumer:
    """Represents a consumer of the shared audio stream."""
    consumer_id: str
    read_index: int = 0
    active: bool = True
    priority: int = 10  # Lower = higher priority
    callback: Optional[Callable[[np.ndarray], None]] = None


@dataclass
class AudioConfig:
    """Configuration for the unified audio system."""
    sample_rate: int = 16000
    channels: int = 1
    chunk_ms: int = 30  # ~512 samples at 16kHz for Porcupine
    buffer_seconds: float = 10.0  # Ring buffer duration
    device_keyword: str = ""  # Substring to match device name
    device_index: Optional[int] = None  # Explicit device index


class UnifiedAudioCapture:
    """Single-owner microphone capture with multi-consumer fan-out.
    
    This class owns the microphone exclusively and provides audio data
    to multiple consumers via a shared ring buffer. Each consumer has
    its own read pointer and can consume at its own pace.
    
    Thread-safe for multiple readers, single writer (capture thread).
    """
    
    def __init__(self, config: AudioConfig, logger=None) -> None:
        self.config = config
        self.logger = logger or self._default_logger()
        
        # Ring buffer sizing
        self.chunk_samples = int(config.sample_rate * config.chunk_ms / 1000)
        self.buffer_capacity = int(config.sample_rate * config.buffer_seconds)
        
        # Pre-allocate ring buffer (int16 PCM)
        self._ring = np.zeros(self.buffer_capacity, dtype=np.int16)
        self._ring_lock = threading.RLock()
        self._write_index: int = 0  # Monotonic write position
        
        # Consumer management
        self._consumers: Dict[str, AudioConsumer] = {}
        self._consumers_lock = threading.Lock()
        
        # Pipeline state
        self._state = AudioState.IDLE
        self._state_lock = threading.Lock()
        
        # Capture thread
        self._capture_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._started = threading.Event()
        
        # PyAudio handles
        self._pa: Optional["pyaudio.PyAudio"] = None
        self._stream = None
        self._hw_error: Optional[str] = None
        
    def _default_logger(self):
        """Fallback logger if none provided."""
        import logging
        logger = logging.getLogger("unified_audio")
        if not logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter(
                "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
            ))
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)
        return logger
    
    # ─────────────────────────────────────────────────────────────────
    # Lifecycle Management
    # ─────────────────────────────────────────────────────────────────
    
    def start(self) -> bool:
        """Start the capture thread. Returns True if started successfully."""
        if self._capture_thread and self._capture_thread.is_alive():
            return True
            
        if pyaudio is None:
            self._hw_error = "PyAudio not installed"
            self.logger.error("Cannot start capture: PyAudio not installed")
            return False
            
        self._stop_event.clear()
        self._started.clear()
        self._capture_thread = threading.Thread(
            target=self._capture_loop,
            name="UnifiedAudioCapture",
            daemon=True
        )
        self._capture_thread.start()
        
        # Wait for capture to actually start (or fail)
        if not self._started.wait(timeout=5.0):
            self.logger.error("Capture thread did not start within timeout")
            return False
            
        return self._hw_error is None
    
    def stop(self) -> None:
        """Stop the capture thread and release hardware."""
        self._stop_event.set()
        if self._capture_thread:
            self._capture_thread.join(timeout=2.0)
            self._capture_thread = None
        self._cleanup_pyaudio()
    
    def is_running(self) -> bool:
        """Check if capture is actively running."""
        return (
            self._capture_thread is not None 
            and self._capture_thread.is_alive()
            and self._hw_error is None
        )
    
    # ─────────────────────────────────────────────────────────────────
    # Consumer Registration
    # ─────────────────────────────────────────────────────────────────
    
    def register_consumer(
        self, 
        consumer_id: str, 
        priority: int = 10,
        callback: Optional[Callable[[np.ndarray], None]] = None
    ) -> AudioConsumer:
        """Register a new consumer to receive audio data.
        
        Args:
            consumer_id: Unique identifier for this consumer
            priority: Lower values = higher priority (for conflict resolution)
            callback: Optional callback invoked with each new chunk
            
        Returns:
            AudioConsumer handle for reading audio
        """
        with self._consumers_lock:
            if consumer_id in self._consumers:
                return self._consumers[consumer_id]
                
            consumer = AudioConsumer(
                consumer_id=consumer_id,
                read_index=self._write_index,  # Start from current position
                active=True,
                priority=priority,
                callback=callback
            )
            self._consumers[consumer_id] = consumer
            self.logger.info(f"Registered audio consumer: {consumer_id}")
            return consumer
    
    def unregister_consumer(self, consumer_id: str) -> None:
        """Remove a consumer from the audio stream."""
        with self._consumers_lock:
            if consumer_id in self._consumers:
                del self._consumers[consumer_id]
                self.logger.info(f"Unregistered audio consumer: {consumer_id}")
    
    # ─────────────────────────────────────────────────────────────────
    # Consumer Reading API
    # ─────────────────────────────────────────────────────────────────
    
    def read_chunk(
        self, 
        consumer_id: str, 
        num_samples: Optional[int] = None,
        blocking: bool = True,
        timeout_ms: int = 100
    ) -> Optional[np.ndarray]:
        """Read audio samples for a specific consumer.
        
        Args:
            consumer_id: The consumer requesting audio
            num_samples: Number of samples to read (default: chunk_samples)
            blocking: If True, wait for data; if False, return None immediately
            timeout_ms: Max time to wait for data in blocking mode
            
        Returns:
            numpy array of int16 samples, or None if no data available
        """
        with self._consumers_lock:
            consumer = self._consumers.get(consumer_id)
            if not consumer or not consumer.active:
                return None
        
        if num_samples is None:
            num_samples = self.chunk_samples
            
        deadline = time.monotonic() + timeout_ms / 1000.0
        
        while True:
            with self._ring_lock:
                available = self._write_index - consumer.read_index
                
                # Handle consumer falling too far behind
                if available > self.buffer_capacity:
                    # Skip old audio, snap to oldest available
                    consumer.read_index = self._write_index - self.buffer_capacity
                    available = self.buffer_capacity
                    self.logger.warning(
                        f"Consumer {consumer_id} fell behind; skipping to latest"
                    )
                
                if available >= num_samples:
                    # Extract samples from ring buffer
                    start_idx = consumer.read_index % self.buffer_capacity
                    end_idx = (consumer.read_index + num_samples) % self.buffer_capacity
                    
                    if start_idx < end_idx:
                        samples = self._ring[start_idx:end_idx].copy()
                    else:
                        # Wrap-around read
                        samples = np.concatenate([
                            self._ring[start_idx:],
                            self._ring[:end_idx]
                        ])
                    
                    consumer.read_index += num_samples
                    return samples
            
            # No data available
            if not blocking or time.monotonic() >= deadline:
                return None
                
            time.sleep(0.001)  # 1ms sleep before retry
    
    def get_latest_chunk(self, num_samples: Optional[int] = None) -> Optional[np.ndarray]:
        """Get the most recent audio without tracking consumer position.
        
        Useful for one-off reads or diagnostics.
        """
        if num_samples is None:
            num_samples = self.chunk_samples
            
        with self._ring_lock:
            if self._write_index < num_samples:
                return None
                
            start_idx = (self._write_index - num_samples) % self.buffer_capacity
            end_idx = self._write_index % self.buffer_capacity
            
            if start_idx < end_idx:
                return self._ring[start_idx:end_idx].copy()
            else:
                return np.concatenate([
                    self._ring[start_idx:],
                    self._ring[:end_idx]
                ])
    
    # ─────────────────────────────────────────────────────────────────
    # Pipeline State Management
    # ─────────────────────────────────────────────────────────────────
    
    def set_state(self, state: AudioState) -> None:
        """Update the pipeline state."""
        with self._state_lock:
            old_state = self._state
            self._state = state
            self.logger.info(f"Audio state: {old_state.name} -> {state.name}")
    
    def get_state(self) -> AudioState:
        """Get current pipeline state."""
        with self._state_lock:
            return self._state
    
    # ─────────────────────────────────────────────────────────────────
    # Internal: Capture Thread
    # ─────────────────────────────────────────────────────────────────
    
    def _capture_loop(self) -> None:
        """Main capture thread: reads from mic, writes to ring buffer."""
        try:
            self._pa = pyaudio.PyAudio()
            device_index = self._find_device()
            
            self._stream = self._pa.open(
                rate=self.config.sample_rate,
                channels=self.config.channels,
                format=pyaudio.paInt16,
                input=True,
                input_device_index=device_index,
                frames_per_buffer=self.chunk_samples,
            )
            
            self.logger.info(
                f"Capture started: rate={self.config.sample_rate}, "
                f"chunk={self.chunk_samples}, device={device_index}"
            )
            self._started.set()
            
            while not self._stop_event.is_set():
                try:
                    data = self._stream.read(
                        self.chunk_samples,
                        exception_on_overflow=False
                    )
                except Exception as e:
                    self.logger.error(f"Capture read error: {e}")
                    time.sleep(0.01)
                    continue
                    
                samples = np.frombuffer(data, dtype=np.int16)
                self._write_samples(samples)
                self._invoke_callbacks(samples)
                
        except Exception as e:
            self._hw_error = str(e)
            self.logger.error(f"Capture initialization failed: {e}")
        finally:
            self._started.set()  # Unblock waiters even on failure
            self._cleanup_pyaudio()
            self.logger.info("Capture thread exiting")
    
    def _write_samples(self, samples: np.ndarray) -> None:
        """Write samples to the ring buffer (thread-safe)."""
        n = len(samples)
        with self._ring_lock:
            start_idx = self._write_index % self.buffer_capacity
            end_idx = (self._write_index + n) % self.buffer_capacity
            
            if start_idx < end_idx:
                self._ring[start_idx:end_idx] = samples
            else:
                # Wrap-around write
                first_part = self.buffer_capacity - start_idx
                self._ring[start_idx:] = samples[:first_part]
                self._ring[:end_idx] = samples[first_part:]
                
            self._write_index += n
    
    def _invoke_callbacks(self, samples: np.ndarray) -> None:
        """Invoke registered consumer callbacks."""
        with self._consumers_lock:
            for consumer in self._consumers.values():
                if consumer.callback and consumer.active:
                    try:
                        consumer.callback(samples)
                    except Exception as e:
                        self.logger.error(
                            f"Consumer callback error ({consumer.consumer_id}): {e}"
                        )
    
    def _find_device(self) -> Optional[int]:
        """Find the appropriate input device."""
        if self.config.device_index is not None:
            return self.config.device_index
            
        if not self.config.device_keyword:
            return None  # Use default
            
        keyword = self.config.device_keyword.lower()
        for i in range(self._pa.get_device_count()):
            info = self._pa.get_device_info_by_index(i)
            if info.get("maxInputChannels", 0) <= 0:
                continue
            name = str(info.get("name", "")).lower()
            if keyword in name:
                self.logger.info(f"Found device matching '{keyword}': {info['name']}")
                return i
        return None
    
    def _cleanup_pyaudio(self) -> None:
        """Release PyAudio resources."""
        if self._stream:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
            
        if self._pa:
            try:
                self._pa.terminate()
            except Exception:
                pass
            self._pa = None


# ═══════════════════════════════════════════════════════════════════════════
# Singleton Access for System-Wide Audio
# ═══════════════════════════════════════════════════════════════════════════

_global_capture: Optional[UnifiedAudioCapture] = None
_global_lock = threading.Lock()


def get_unified_audio(config: Optional[AudioConfig] = None, logger=None) -> UnifiedAudioCapture:
    """Get or create the singleton UnifiedAudioCapture instance.
    
    This ensures only ONE process owns the microphone system-wide.
    """
    global _global_capture
    
    with _global_lock:
        if _global_capture is None:
            if config is None:
                config = AudioConfig()
            _global_capture = UnifiedAudioCapture(config, logger)
        return _global_capture


def shutdown_unified_audio() -> None:
    """Shut down the global audio capture."""
    global _global_capture
    
    with _global_lock:
        if _global_capture is not None:
            _global_capture.stop()
            _global_capture = None
