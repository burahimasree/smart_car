"""Central AudioManager service (skeleton).

This module provides a minimal AudioManager implementation with two modes:

- Simulation mode (no ALSA):
  - Maintains in-memory sessions and publishes periodic audio.health.
  - Can be used by wakeword/STT wrappers to exercise IPC paths.

- Real mode (future work):
  - Will open ALSA/PortAudio once, run at hardware rate (44.1/48 kHz),
    maintain a ring buffer, and resample to client-requested rates.

The goal of this skeleton is to establish a clear IPC contract and
non-invasive scaffolding that can be enabled via config.audio.use_audio_manager.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple

import numpy as np
import zmq

from src.core.config_loader import load_config
from src.core.ipc import make_publisher, publish_json
from src.core.logging_setup import get_logger


@dataclass(slots=True)
class AudioSession:
    session_id: str
    mode: str  # "wakeword" | "stt"
    target_rate: int
    channels: int
    max_duration_s: float
    priority: int
    started_ts: float
    # Monotonic index in hardware sample domain (int16 frames) from
    # which the next read will begin. This is compared against the
    # global write index maintained by the capture thread.
    read_index_hw: int


class AudioManager:
    """Owns microphone hardware and provides audio sessions via IPC.

    Current implementation is intentionally conservative: it only
    skeletonizes session management and health publishing so that
    wakeword/STT wrappers can exercise the control plane without
    depending on real audio hardware.
    """

    def __init__(self, config_path: Path) -> None:
        self.config = load_config(config_path)
        self.log_dir = Path(self.config.get("logs", {}).get("directory", "logs"))
        if not self.log_dir.is_absolute():
            root = Path(os.environ.get("PROJECT_ROOT", ".")).resolve()
            self.log_dir = root / self.log_dir
        self.logger = get_logger("audio.manager", self.log_dir)

        audio_cfg = self.config.get("audio", {}) or {}
        self.control_endpoint = audio_cfg.get("control_endpoint", "tcp://127.0.0.1:6020")

        # Hardware / ring-buffer configuration
        self.hw_rate = int(audio_cfg.get("hw_sample_rate", 48000))
        self.buffer_seconds = float(audio_cfg.get("hw_buffer_seconds", 5.0))
        self._ring_capacity = max(1, int(self.hw_rate * self.buffer_seconds))
        self._ring = np.zeros(self._ring_capacity, dtype=np.int16)
        self._ring_lock = threading.Lock()
        # Monotonic total number of int16 frames written to ring.
        self._write_index: int = 0
        self._hw_rate_actual: int = self.hw_rate
        self._hw_error: Optional[str] = None
        self._preferred_device_substring = str(audio_cfg.get("preferred_device_substring", "") or "")
        self._hw_buffer_ms = int(audio_cfg.get("hw_buffer_ms", 20))

        # Capture thread lifecycle
        self._capture_thread: Optional[threading.Thread] = None
        self._capture_running = threading.Event()

        # ZeroMQ context and sockets
        self.ctx = zmq.Context.instance()
        self.health_pub = make_publisher(self.config, channel="upstream")
        self.control_sock = self.ctx.socket(zmq.REP)
        try:
            self.control_sock.bind(self.control_endpoint)
        except Exception as exc:  # pragma: no cover - depends on runtime state
            # If another AudioManager instance is already bound (e.g. a
            # systemd service in the background), fall back to an
            # inproc-only endpoint so unit tests and local tools can
            # still exercise the control plane without failing on an
            # address-in-use error.
            self.logger.warning(
                "AudioManager control endpoint %s in use (%s); falling back to inproc://",
                self.control_endpoint,
                exc,
            )
            self.control_endpoint = f"inproc://audio-manager-{os.getpid()}"
            self.control_sock.bind(self.control_endpoint)

        self.sessions: Dict[str, AudioSession] = {}
        self.sessions_lock = threading.Lock()
        self._stop = threading.Event()

    # ------------------------ health publishing ------------------------

    def _publish_health(self, *, ok: bool = True, detail: Optional[str] = None) -> None:
        payload = {
            "timestamp": int(time.time()),
            "component": "audio-manager",
            "ok": bool(ok),
        }
        if detail:
            payload["detail"] = str(detail)
        # Reuse the generic system health topic until a dedicated topic
        # is introduced in src.core.ipc.
        publish_json(self.health_pub, b"system.health", payload)

    # ------------------------ session API ------------------------

    def _ensure_capture_started(self) -> None:
        """Start the hardware capture thread on first use.

        If PyAudio or the hardware device is unavailable, this logs an
        error and leaves the manager in a "degraded" state where
        read_chunk still returns silence. This keeps unit tests from
        failing on machines without audio while enabling full capture
        on the Pi.
        """

        if self._capture_thread and self._capture_thread.is_alive():
            return

        try:
            import pyaudio  # type: ignore
        except Exception as exc:  # pragma: no cover - environment dependent
            self._hw_error = f"PyAudio import failed: {exc}"
            self.logger.error("AudioManager: cannot start capture (PyAudio missing): %s", exc)
            return

        def _capture_loop() -> None:
            pa = None
            stream = None
            try:
                pa = pyaudio.PyAudio()
                device_index = self._select_input_device(pa)
                dev_info = pa.get_device_info_by_index(device_index)
                self._hw_rate_actual = int(dev_info.get("defaultSampleRate", self.hw_rate))
                frames_per_buffer = max(
                    1,
                    int(self._hw_rate_actual * self._hw_buffer_ms / 1000.0),
                )
                stream = pa.open(
                    rate=self._hw_rate_actual,
                    channels=1,
                    format=pyaudio.paInt16,
                    input=True,
                    frames_per_buffer=frames_per_buffer,
                    input_device_index=device_index,
                )
                self.logger.info(
                    "AudioManager capture started (device_index=%s, hw_rate=%s, frames_per_buffer=%s)",
                    device_index,
                    self._hw_rate_actual,
                    frames_per_buffer,
                )
                self._capture_running.set()
                while self._capture_running.is_set():
                    try:
                        data = stream.read(frames_per_buffer, exception_on_overflow=False)
                    except Exception as exc_read:  # pragma: no cover
                        self._hw_error = f"stream.read failed: {exc_read}"
                        self.logger.error("AudioManager capture read error: %s", exc_read)
                        break
                    if not data:
                        continue
                    samples = np.frombuffer(data, dtype=np.int16)
                    self._write_samples(samples)
            except Exception as exc:  # pragma: no cover - depends on hardware
                self._hw_error = str(exc)
                self.logger.error("AudioManager capture initialization failed: %s", exc)
            finally:
                self._capture_running.clear()
                if stream is not None:
                    try:
                        stream.stop_stream()
                        stream.close()
                    except Exception:
                        pass
                if pa is not None:
                    try:
                        pa.terminate()
                    except Exception:
                        pass
                self.logger.info("AudioManager capture thread exiting")

        self._capture_thread = threading.Thread(target=_capture_loop, name="AudioCapture", daemon=True)
        self._capture_thread.start()

    def _select_input_device(self, pa: "pyaudio.PyAudio") -> int:
        """Pick an input device index, preferring the configured substring.

        Falls back to the default input device if the preferred one is
        not found.
        """

        try:
            preferred = self._preferred_device_substring.lower()
            if preferred:
                for idx in range(pa.get_device_count()):
                    info = pa.get_device_info_by_index(idx)
                    if not info.get("maxInputChannels"):
                        continue
                    name = str(info.get("name", "")).lower()
                    if preferred in name:
                        return idx
            default_idx = pa.get_default_input_device_info()["index"]
            return int(default_idx)
        except Exception:  # pragma: no cover
            # Last-resort fallback
            return 0

    def _write_samples(self, samples: np.ndarray) -> None:
        if samples.size == 0:
            return
        with self._ring_lock:
            n = int(samples.size)
            capacity = self._ring_capacity
            start = self._write_index % capacity
            end = (self._write_index + n) % capacity
            if n >= capacity:
                # Only keep the newest window if we overflow in one go.
                self._ring[:] = samples[-capacity:]
                self._write_index += n
                return
            if start < end:
                self._ring[start:end] = samples
            else:
                first = capacity - start
                self._ring[start:] = samples[:first]
                self._ring[: end] = samples[first:]
            self._write_index += n

    def _current_buffer_depth_ms(self) -> float:
        with self._ring_lock:
            depth_frames = min(self._write_index, self._ring_capacity)
            if self._hw_rate_actual <= 0:
                return 0.0
            return depth_frames * 1000.0 / float(self._hw_rate_actual)

    def _resample_to_rate(self, samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
        """Resample int16 mono PCM to dst_rate.

        Tries to use `resampy` for high-quality polyphase resampling if
        available; falls back to a lightweight numpy-based linear
        interpolator when resampy is not installed.
        """

        if src_rate == dst_rate or samples.size == 0:
            return samples
        try:
            import resampy  # type: ignore

            x = samples.astype(np.float32) / 32768.0
            y = resampy.resample(x, src_rate, dst_rate)
            return np.clip(y * 32768.0, -32768, 32767).astype(np.int16)
        except Exception:  # pragma: no cover - optional dependency
            # Fallback: linear interpolation similar to wakeword runner.
            n_src = samples.size
            duration = n_src / float(src_rate)
            n_dst = int(duration * dst_rate)
            if n_dst <= 0:
                return np.zeros(0, dtype=np.int16)
            x_src = np.linspace(0.0, 1.0, num=n_src, endpoint=False)
            x_dst = np.linspace(0.0, 1.0, num=n_dst, endpoint=False)
            return np.interp(x_dst, x_src, samples.astype(np.float32)).astype(np.int16)

    def _read_for_session(self, session: AudioSession, frames_ms: int) -> np.ndarray:
        """Read a window from the ring buffer for a single session.

        The session's read_index_hw is advanced in the hardware sample
        domain. The returned samples are resampled to the session's
        target_rate.
        """

        if frames_ms <= 0:
            return np.zeros(0, dtype=np.int16)

        with self._ring_lock:
            write_index = self._write_index
            capacity = self._ring_capacity
            hw_rate = self._hw_rate_actual or self.hw_rate

            if write_index <= 0 or hw_rate <= 0:
                return np.zeros(0, dtype=np.int16)

            # If the reader has fallen more than the buffer capacity
            # behind the writer, drop old samples and snap to the oldest
            # available frame in the ring.
            if write_index - session.read_index_hw > capacity:
                session.read_index_hw = write_index - capacity

            # Number of hardware samples corresponding to frames_ms
            src_needed = int(hw_rate * frames_ms / 1000.0)
            if src_needed <= 0:
                return np.zeros(0, dtype=np.int16)

            available = write_index - session.read_index_hw
            if available <= 0:
                return np.zeros(0, dtype=np.int16)

            src_count = min(src_needed, int(available))
            start = session.read_index_hw
            end = start + src_count
            session.read_index_hw = end

            # Map from monotonic indices into circular buffer indices.
            start_idx = start % capacity
            end_idx = end % capacity
            if start_idx < end_idx:
                hw_slice = self._ring[start_idx:end_idx].copy()
            else:
                first = self._ring[start_idx:]
                second = self._ring[:end_idx]
                hw_slice = np.concatenate([first, second])

        return self._resample_to_rate(hw_slice, src_rate=hw_rate, dst_rate=session.target_rate)

    def start_session(
        self,
        session_id: str,
        *,
        mode: str,
        target_rate: int,
        channels: int,
        max_duration_s: float,
        priority: int,
    ) -> Tuple[bool, str]:
        """Register a new logical audio session and ensure capture is running."""

        if not session_id:
            return False, "missing_session_id"

        # Do not fail session creation if capture cannot be started; the
        # caller can still observe this via health metrics and receive
        # silence from read_chunk on development machines.
        self._ensure_capture_started()

        with self.sessions_lock:
            if session_id in self.sessions:
                return False, "session_exists"
            self.sessions[session_id] = AudioSession(
                session_id=session_id,
                mode=str(mode or "stt"),
                target_rate=int(target_rate or 16000),
                channels=int(channels or 1),
                max_duration_s=float(max_duration_s or 15.0),
                priority=int(priority or 10),
                started_ts=time.time(),
                read_index_hw=self._write_index,
            )
        self.logger.info(
            "Started audio session id=%s mode=%s rate=%s ch=%s max_s=%.1f priority=%s",
            session_id,
            mode,
            target_rate,
            channels,
            max_duration_s,
            priority,
        )
        return True, "ok"

    def stop_session(self, session_id: str) -> Tuple[bool, str]:
        with self.sessions_lock:
            if session_id not in self.sessions:
                return False, "not_found"
            self.sessions.pop(session_id, None)
        self.logger.info("Stopped audio session id=%s", session_id)
        return True, "ok"

    def read_chunk(self, session_id: str, frames_ms: int) -> Tuple[bool, bytes | None, str]:
        """Read a framed chunk of audio for a given session.

        The return value is a tuple (ok, pcm_bytes, reason). pcm_bytes
        is mono int16 PCM at the session's target sample rate.
        """

        if frames_ms <= 0:
            return False, None, "invalid_frames_ms"

        with self.sessions_lock:
            session = self.sessions.get(session_id)
        if session is None:
            return False, None, "not_found"

        samples = self._read_for_session(session, frames_ms)
        if samples.size == 0:
            return True, b"", "no_data"
        return True, samples.astype("<i2").tobytes(), "ok"

    # ------------------------ control plane ------------------------

    def _handle_request(self, req: Dict[str, object]) -> Dict[str, object]:
        action = str(req.get("action") or "").lower()
        if action == "start_session":
            ok, reason = self.start_session(
                str(req.get("session_id") or ""),
                mode=str(req.get("mode") or "stt"),
                target_rate=int(req.get("target_rate") or 16000),
                channels=int(req.get("channels") or 1),
                max_duration_s=float(req.get("max_duration_s") or 15.0),
                priority=int(req.get("priority") or 10),
            )
            return {"ok": ok, "reason": reason}
        if action == "stop_session":
            ok, reason = self.stop_session(str(req.get("session_id") or ""))
            return {"ok": ok, "reason": reason}
        if action == "read_chunk":
            session_id = str(req.get("session_id") or "")
            frames_ms = int(req.get("frames_ms") or 20)
            ok, data, reason = self.read_chunk(session_id, frames_ms)
            if not ok or data is None:
                return {"ok": ok, "reason": reason, "data": None}
            return {
                "ok": True,
                "reason": reason,
                # Base64-encoded mono int16 PCM for portability in JSON
                "data_b64": base64.b64encode(data).decode("ascii"),
            }
        if action == "health":
            with self.sessions_lock:
                count = len(self.sessions)
            depth_ms = self._current_buffer_depth_ms()
            return {
                "ok": self._hw_error is None,
                "sessions": count,
                "buffer_depth_ms": depth_ms,
                "hw_rate": self._hw_rate_actual,
                "error": self._hw_error,
            }
        return {"ok": False, "reason": "unknown_action"}

    def run(self) -> None:
        self.logger.info("AudioManager starting on %s", self.control_endpoint)
        last_health = 0.0
        while not self._stop.is_set():
            now = time.time()
            if now - last_health >= 10.0:
                with self.sessions_lock:
                    sess_count = len(self.sessions)
                self._publish_health(ok=True, detail=f"sessions={sess_count}")
                last_health = now

            try:
                if self.control_sock.poll(100):  # 100 ms
                    msg = self.control_sock.recv()
                    try:
                        req = json.loads(msg.decode("utf-8"))
                    except Exception:
                        resp = {"ok": False, "reason": "invalid_json"}
                    else:
                        resp = self._handle_request(req)
                    self.control_sock.send(json.dumps(resp).encode("utf-8"))
            except KeyboardInterrupt:
                self.logger.info("AudioManager interrupted; shutting down")
                self._stop.set()
                break
            except Exception as exc:  # pragma: no cover
                self.logger.error("AudioManager loop error: %s", exc)
                self._publish_health(ok=False, detail=str(exc))

        self.logger.info("AudioManager stopped")


def main() -> None:
    ap = argparse.ArgumentParser(description="AudioManager service (skeleton)")
    ap.add_argument("--config", default="config/system.yaml", help="Path to system config YAML")
    ap.add_argument("--sim", action="store_true", help="Run in simulation mode (no ALSA)")
    args = ap.parse_args()

    # For now, --sim and real mode behave the same; sim is a hint for
    # future hardware binding.
    mgr = AudioManager(Path(args.config))
    mgr.run()


if __name__ == "__main__":
    main()
