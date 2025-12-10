"""STT wrapper runner (skeleton).

Listens for cmd.listen.start/stop on the downstream IPC bus and, in
simulation mode, publishes a deterministic STT transcription. This
scaffolding is intended to exercise the control plane and AudioManager
integration without touching the existing STTEngine/orchestrator
behaviour.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

import numpy as np
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


class AudioManagerClient:
    """Synchronous client for the AudioManager control API (STT use)."""

    def __init__(self, cfg: dict, logger) -> None:
        self._logger = logger
        audio_cfg = cfg.get("audio", {}) or {}
        endpoint = audio_cfg.get("control_endpoint", "tcp://127.0.0.1:6020")
        self._ctx = zmq.Context.instance()
        self._sock = self._ctx.socket(zmq.REQ)
        self._sock.connect(endpoint)
        self._endpoint = endpoint
        self._logger.info("STT wrapper AudioManager client connected to %s", endpoint)

    def _rpc(self, payload: dict) -> dict:
        self._sock.send_json(payload)
        return self._sock.recv_json()

    def start_session(self, session_id: str, target_rate: int, max_seconds: float) -> bool:
        try:
            resp = self._rpc(
                {
                    "action": "start_session",
                    "session_id": session_id,
                    "mode": "stt",
                    "target_rate": int(target_rate),
                    "channels": 1,
                    "max_duration_s": float(max_seconds),
                    "priority": 10,
                }
            )
        except Exception as exc:  # pragma: no cover
            self._logger.error("AudioManager start_session failed: %s", exc)
            return False
        ok = bool(resp.get("ok"))
        if not ok:
            self._logger.error("AudioManager start_session rejected: %s", resp)
        return ok

    def read_chunk(self, session_id: str, frames_ms: int) -> bytes:
        try:
            resp = self._rpc(
                {
                    "action": "read_chunk",
                    "session_id": session_id,
                    "frames_ms": int(frames_ms),
                }
            )
        except Exception as exc:  # pragma: no cover
            self._logger.error("AudioManager read_chunk RPC failed: %s", exc)
            return b""
        if not resp.get("ok"):
            return b""
        data_b64 = resp.get("data_b64")
        if not data_b64:
            return b""
        try:
            return base64.b64decode(data_b64)
        except Exception:
            return b""

    def stop_session(self, session_id: str) -> None:
        try:
            self._rpc({"action": "stop_session", "session_id": session_id})
        except Exception:  # pragma: no cover
            return


class STTWrapper:
    def __init__(self, cfg_path: Path, *, sim: bool, force_local: bool, force_cloud: bool) -> None:
        self.config = load_config(cfg_path)
        self.sim = sim
        self.force_local = force_local
        self.force_cloud = force_cloud
        log_dir = Path(self.config.get("logs", {}).get("directory", "logs"))
        if not log_dir.is_absolute():
            root = Path(os.environ.get("PROJECT_ROOT", ".")).resolve()
            log_dir = root / log_dir
        self.logger = get_logger("stt.wrapper", log_dir)

        self.cmd_sub = make_subscriber(self.config, channel="downstream")
        self.up_pub = make_publisher(self.config, channel="upstream")

        self._running = True
        self._active_thread: Optional[threading.Thread] = None
        self._stop_flag = threading.Event()

        # AudioManager client
        self.audio = AudioManagerClient(self.config, self.logger)

        # Faster-whisper model (lazy-loaded)
        self._fw_model = None
        self._fw_language = self.config.get("stt", {}).get("language", "en")

    def _publish_simulated_transcript(self) -> None:
        payload = {
            "timestamp": int(time.time()),
            "text": "simulated transcription",
            "confidence": 0.95,
            "language": self._fw_language,
            "durations_ms": {"capture": 0, "whisper": 0, "total": 0},
            "kind": "final",
        }
        self.logger.info("STT wrapper (sim) publishing transcription: %s", payload["text"])
        publish_json(self.up_pub, TOPIC_STT, payload)

    # ------------------------ STT backend ------------------------

    def _ensure_fast_model(self) -> None:
        if self._fw_model is not None:
            return
        stt_cfg = self.config.get("stt", {})
        fw_cfg = stt_cfg.get("engines", {}).get("faster_whisper", {}) or {}
        fast_model = fw_cfg.get("model") or fw_cfg.get("model_path") or "tiny.en"
        compute_type = fw_cfg.get("compute_type", "int8")
        device = fw_cfg.get("device", "cpu")
        download_root = fw_cfg.get("download_root")
        if download_root:
            download_root = Path(download_root)
        else:
            download_root = Path(os.environ.get("PROJECT_ROOT", ".")).resolve() / "third_party/whisper-fast"
        self.logger.info(
            "Loading faster-whisper model in STT wrapper: %s device=%s compute=%s",
            fast_model,
            device,
            compute_type,
        )
        self._fw_model = load_fast_model(
            fast_model,
            device=device,
            compute_type=compute_type,
            download_root=download_root,
        )

    def _run_stt_session(self, session_id: str) -> None:
        stt_cfg = self.config.get("stt", {})
        audio_cfg = self.config.get("audio", {}) or {}
        max_seconds = float(stt_cfg.get("max_capture_seconds", 8))
        chunk_ms = int(audio_cfg.get("stt_chunk_ms", 500))

        capture_start = time.time()
        if not self.audio.start_session(session_id, target_rate=16000, max_seconds=max_seconds):
            self.logger.error("STT wrapper: failed to start AudioManager session; aborting")
            return

        self.logger.info(
            "STT wrapper: started AudioManager session id=%s (max_seconds=%.1f, chunk_ms=%s)",
            session_id,
            max_seconds,
            chunk_ms,
        )

        try:
            pcm_chunks: list[np.ndarray] = []
            while not self._stop_flag.is_set():
                now = time.time()
                if now - capture_start >= max_seconds:
                    self.logger.info("STT wrapper: capture window reached max_seconds")
                    break
                data = self.audio.read_chunk(session_id, chunk_ms)
                if not data:
                    time.sleep(chunk_ms / 1000.0)
                    continue
                samples = np.frombuffer(data, dtype=np.int16)
                if samples.size == 0:
                    continue
                pcm_chunks.append(samples)

            if not pcm_chunks:
                self.logger.warning("STT wrapper: no audio captured; emitting low-confidence empty transcript")
                payload = {
                    "timestamp": int(time.time()),
                    "text": "",
                    "confidence": 0.0,
                    "language": self._fw_language,
                    "durations_ms": {"capture": int((time.time() - capture_start) * 1000), "whisper": 0, "total": int((time.time() - capture_start) * 1000)},
                    "kind": "final",
                }
                publish_json(self.up_pub, TOPIC_STT, payload)
                return

            pcm_all = np.concatenate(pcm_chunks)
            capture_ms = int((time.time() - capture_start) * 1000)

            # Write to a temporary WAV file for reuse of transcribe_fast
            with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp:
                import wave

                with wave.open(tmp.name, "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(16000)
                    wf.writeframes(pcm_all.astype("<i2").tobytes())
                wav_path = Path(tmp.name)

            whisper_start = time.time()
            try:
                self._ensure_fast_model()
                text, conf = transcribe_fast(self._fw_model, wav_path, self._fw_language)
            finally:
                try:
                    wav_path.unlink(missing_ok=True)
                except Exception:
                    pass

            whisper_ms = int((time.time() - whisper_start) * 1000)
            total_ms = int((time.time() - capture_start) * 1000)

            payload = {
                "timestamp": int(time.time()),
                "text": text.strip(),
                "confidence": float(conf),
                "language": self._fw_language,
                "durations_ms": {"capture": capture_ms, "whisper": whisper_ms, "total": total_ms},
                "kind": "final",
            }
            self.logger.info(
                "STT wrapper: publishing transcription (len=%s, conf=%.3f)",
                len(payload["text"]),
                payload["confidence"],
            )
            publish_json(self.up_pub, TOPIC_STT, payload)
        finally:
            self.audio.stop_session(session_id)

    def run(self) -> None:
        self.logger.info("STT wrapper running (sim=%s)", self.sim)
        while self._running:
            topic, data = self.cmd_sub.recv_multipart()
            try:
                payload: Any = json.loads(data)
            except Exception:
                self.logger.error("Invalid JSON on topic %s", topic)
                continue

            if topic == TOPIC_CMD_LISTEN_START:
                self.logger.info("Received cmd.listen.start: %s", payload)
                if self.sim:
                    self._publish_simulated_transcript()
                    continue
                if self._active_thread and self._active_thread.is_alive():
                    self.logger.info("STT wrapper: session already active; ignoring extra start")
                    continue
                self._stop_flag.clear()
                session_id = f"stt-{int(time.time() * 1000)}"
                self._active_thread = threading.Thread(
                    target=self._run_stt_session,
                    args=(session_id,),
                    name="STTSession",
                    daemon=True,
                )
                self._active_thread.start()
            elif topic == TOPIC_CMD_LISTEN_STOP:
                self.logger.info("Received cmd.listen.stop: %s", payload)
                self._stop_flag.set()


def main() -> None:
    ap = argparse.ArgumentParser(description="STT wrapper runner (skeleton)")
    ap.add_argument("--config", default="config/system.yaml", help="Path to system config YAML")
    ap.add_argument("--sim", action="store_true", help="Run in simulation mode (no real audio)")
    ap.add_argument("--force-local", action="store_true", help="Reserved: force local STT backend")
    ap.add_argument("--force-cloud", action="store_true", help="Reserved: force cloud STT backend")
    args = ap.parse_args()

    # For now we primarily exercise simulation and local faster-whisper
    # via the AudioManager; the force_* flags are wired for future
    # policy routing between local and cloud engines.
    wrapper = STTWrapper(
        Path(args.config),
        sim=bool(args.sim),
        force_local=bool(args.force_local),
        force_cloud=bool(args.force_cloud),
    )
    wrapper.run()


if __name__ == "__main__":
    main()
