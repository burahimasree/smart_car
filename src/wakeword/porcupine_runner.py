"""Porcupine wakeword runner.

Requirements:
    - pvporcupine==3.x installed in the STT/wakeword venv
    - Access key provided via environment variable PV_ACCESS_KEY or config wakeword.access_key
    - Keyword model file (.ppn) at path specified in config or provided via --model

Publishes ZeroMQ topic `ww.detected` with payload:
    {"timestamp": int, "keyword": "genny", "variant": "genny", "confidence": float, "source": "porcupine"}

Flags:
    --sim                Publish a single simulated event and exit
    --after SECS         Delay before simulated publish (default 1.5)
    --sensitivity FLOAT  Porcupine sensitivity override (default from config)
    --model PATH         Override keyword .ppn file path
    --ipc ADDR           Override upstream IPC address
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import wave
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import zmq

from src.core.config_loader import load_config
from src.core.ipc import TOPIC_WW_DETECTED, make_publisher, publish_json
from src.core.logging_setup import get_logger


@dataclass
class WakeConfig:
    sensitivity: float
    model_path: Optional[Path]
    access_key: Optional[str]
    sim_mode: bool
    keyword: str
    variant: str


class AudioManagerClient:
    """Minimal client for the AudioManager control API.

    This client is intentionally lightweight and synchronous: it uses a
    single REQ socket and is only invoked from the wakeword thread.
    """

    def __init__(self, cfg: dict, logger) -> None:
        self._logger = logger
        audio_cfg = cfg.get("audio", {}) or {}
        endpoint = audio_cfg.get("control_endpoint", "tcp://127.0.0.1:6020")
        self._ctx = zmq.Context.instance()
        self._sock = self._ctx.socket(zmq.REQ)
        self._sock.connect(endpoint)
        self._endpoint = endpoint
        self._logger.info("Wakeword AudioManager client connected to %s", endpoint)

    def _rpc(self, payload: dict) -> dict:
        self._sock.send_json(payload)
        return self._sock.recv_json()

    def start_session(self, session_id: str, target_rate: int, max_seconds: float) -> Tuple[bool, str]:
        try:
            resp = self._rpc(
                {
                    "action": "start_session",
                    "session_id": session_id,
                    "mode": "wakeword",
                    "target_rate": int(target_rate),
                    "channels": 1,
                    "max_duration_s": float(max_seconds),
                    "priority": 5,
                }
            )
        except Exception as exc:  # pragma: no cover - runtime dependent
            self._logger.error("AudioManager start_session failed: %s", exc)
            return False, str(exc)
        return bool(resp.get("ok")), str(resp.get("reason") or "ok")

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
        except Exception:  # pragma: no cover
            return b""

    def stop_session(self, session_id: str) -> None:
        try:
            self._rpc({"action": "stop_session", "session_id": session_id})
        except Exception:  # pragma: no cover
            return


def load_wake_config(cfg_path: Path) -> WakeConfig:
    cfg = load_config(cfg_path)
    ww = cfg.get("wakeword", {})
    model_path = ww.get("model") or ww.get("model_path") or ww.get("model_paths", {}).get("porcupine_keyword")
    access_key = ww.get("access_key") or os.environ.get("PV_ACCESS_KEY")
    keywords = ww.get("keywords") or []
    primary_keyword = (
        ww.get("payload_keyword")
        or ww.get("primary_keyword")
        or (keywords[0] if keywords else "genny")
    )
    variant_keyword = ww.get("payload_variant") or ww.get("variant_keyword") or primary_keyword
    return WakeConfig(
        sensitivity=float(ww.get("sensitivity", 0.6)),
        model_path=Path(model_path) if model_path else None,
        access_key=access_key,
        sim_mode=bool(ww.get("sim_mode", False)),
        keyword=primary_keyword,
        variant=variant_keyword,
    )


def publish_detected(pub: zmq.Socket, score: float, source: str, keyword: str, variant: str) -> None:
    payload = {
        "timestamp": int(time.time()),
        "keyword": keyword,
        "variant": variant,
        "confidence": float(score),
        "source": source,
    }
    # Publish over IPC and also emit a console line so tests and
    # operators can easily confirm detection.
    publish_json(pub, TOPIC_WW_DETECTED, payload)
    print("[wakeword]", json.dumps(payload))


def dump_ring_buffer(ring_buffer: AudioRingBuffer | None, dump_path: Optional[Path], logger) -> None:
    if not ring_buffer or dump_path is None:
        return
    try:
        saved = ring_buffer.dump(dump_path)
        if saved:
            logger.info("Dumped ~%.2fs of wakeword audio to %s", ring_buffer.duration_seconds, saved)
    except Exception as exc:  # pragma: no cover - debug helper
        logger.error("Failed to dump wakeword audio: %s", exc)


def run_sim(pub: zmq.Socket, after: float, keyword: str, variant: str) -> None:
    time.sleep(after)
    publish_detected(pub, 0.98, "sim", keyword, variant)


def try_import_porcupine() -> Optional[Any]:
    try:
        import pvporcupine
        return pvporcupine
    except Exception:
        return None


def _resample_int16(samples: "np.ndarray", src_rate: int, dst_rate: int, dst_len: int) -> "np.ndarray":
    """Lightweight linear resampler to feed Porcupine when mic rate != 16 kHz."""
    import numpy as np  # local import to avoid hard dependency when unused

    if src_rate == dst_rate:
        return samples
    x_src = np.linspace(0, 1, num=len(samples), endpoint=False)
    x_dst = np.linspace(0, 1, num=dst_len, endpoint=False)
    return np.interp(x_dst, x_src, samples).astype(np.int16)


class AudioRingBuffer:
    """Fixed-duration PCM buffer for debugging wakeword audio."""

    def __init__(self, seconds: float, sample_rate: int) -> None:
        self.sample_rate = int(sample_rate)
        self.max_bytes = max(1, int(max(seconds, 0.5) * self.sample_rate * 2))
        self.queue: deque[bytes] = deque()
        self.size = 0

    def push(self, pcm: bytes) -> None:
        if not pcm:
            return
        self.queue.append(pcm)
        self.size += len(pcm)
        while self.size > self.max_bytes and self.queue:
            removed = self.queue.popleft()
            self.size -= len(removed)

    @property
    def duration_seconds(self) -> float:
        return self.size / float(2 * self.sample_rate) if self.size else 0.0

    def dump(self, path: Path) -> Path | None:
        if self.size == 0:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(self.sample_rate)
            for chunk in self.queue:
                wf.writeframes(chunk)
        return path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/system.yaml", help="Path to system config YAML")
    ap.add_argument("--sim", action="store_true", help="Run in simulation mode and emit a single ww.detected")
    ap.add_argument("--after", type=float, default=1.5, help="Delay before sim event")
    ap.add_argument("--sensitivity", type=float, default=0.6)
    ap.add_argument("--model", type=str, default=None)
    ap.add_argument("--ipc", type=str, default=None, help="Override IPC upstream address (tcp://host:port)")
    ap.add_argument("--device-index", type=int, default=None, help="PyAudio input device index (see list in logs)")
    ap.add_argument("--use-audio-manager", action="store_true", help="Prefer AudioManager for audio input when available")
    ap.add_argument("--wav-path", type=str, default=None, help="Testing: run Porcupine on a WAV file instead of microphone")
    ap.add_argument("--dump-audio", action="store_true", help="Write the last N seconds of PCM to a WAV file when detections occur")
    ap.add_argument("--dump-audio-seconds", type=float, default=5.0, help="Seconds of audio to keep in the debug ring buffer")
    ap.add_argument("--dump-audio-path", type=str, default="logs/wakeword_last5s.wav", help="Destination WAV path for audio dumps")
    args = ap.parse_args()

    try:
        cfg = load_config(Path(args.config))
        if args.ipc:
            os.environ["IPC_UPSTREAM"] = args.ipc
        pub = make_publisher(cfg, channel="upstream")
        log_dir = Path(cfg.get("logs", {}).get("directory", "logs"))
        if not log_dir.is_absolute():
            log_dir = PROJECT_ROOT / log_dir
        logger = get_logger("wakeword", log_dir)
    except Exception as exc:  # pragma: no cover
        print(f"[wakeword] Fatal startup error: {exc}", file=sys.stderr)
        sys.exit(1)

    dump_audio_enabled = bool(args.dump_audio)
    dump_path: Optional[Path] = None
    if dump_audio_enabled:
        dump_path = Path(args.dump_audio_path)
        if not dump_path.is_absolute():
            dump_path = PROJECT_ROOT / dump_path

    # load config-derived wake settings
    wcfg = load_wake_config(Path(args.config))

    # Decide whether to route audio via the AudioManager service. For now
    # this is a non-invasive hint: in sim mode we behave exactly as
    # before, and in real mode we still open ALSA directly until the
    # AudioManager acquires exclusive hardware ownership.
    audio_cfg = cfg.get("audio", {}) or {}
    use_audio_manager = bool(args.use_audio_manager or audio_cfg.get("use_audio_manager"))
    if use_audio_manager:
        logger.info("AudioManager preference enabled; wakeword will consume audio frames from AudioManager when available")
    if args.sim:
        run_sim(pub, args.after, wcfg.keyword, wcfg.variant)
        return

    sensitivity = args.sensitivity if args.sensitivity else wcfg.sensitivity
    model_path = Path(args.model) if args.model else wcfg.model_path
    device_index_env = os.environ.get("PA_INPUT_INDEX")
    device_index = args.device_index if args.device_index is not None else (
        int(device_index_env) if device_index_env and device_index_env.isdigit() else None
    )
    if device_index is not None:
        logger.info("Using input device index %s", device_index)
    pvporcupine = try_import_porcupine()
    detector = None
    ring_buffer: AudioRingBuffer | None = None
    if pvporcupine and model_path and model_path.exists():
        if not wcfg.access_key:
            logger.error("Porcupine access key not provided (PV_ACCESS_KEY). Use .env or config.")
        else:
            try:
                detector = pvporcupine.create(access_key=wcfg.access_key, keyword_paths=[str(model_path)], sensitivities=[sensitivity])
                logger.info("Porcupine initialized model=%s sensitivity=%.2f", model_path, sensitivity)
                if dump_audio_enabled:
                    ring_buffer = AudioRingBuffer(args.dump_audio_seconds, detector.sample_rate)
            except Exception as exc:  # pragma: no cover
                logger.error("Failed to create Porcupine instance: %s", exc)

    # Offline testing mode: run Porcupine on a WAV file and exit
    if args.wav_path:
        if detector is None:
            logger.error("Porcupine detector not initialized; cannot run WAV test")
            sys.exit(1)
        wav_path = Path(args.wav_path)
        if not wav_path.exists():
            logger.error("WAV path does not exist: %s", wav_path)
            sys.exit(1)
        import wave
        import numpy as np

        with wave.open(str(wav_path), "rb") as wf:
            nch = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            rate = wf.getframerate()
            nframes = wf.getnframes()
            if nch != 1 or sampwidth != 2:
                logger.error("WAV must be mono 16-bit PCM; channels=%s sampwidth=%s", nch, sampwidth)
                sys.exit(1)
            if rate != detector.sample_rate:
                logger.error("WAV sample_rate %s != Porcupine sample_rate %s; please resample the file", rate, detector.sample_rate)
                sys.exit(1)
            raw = wf.readframes(nframes)

        samples = np.frombuffer(raw, dtype=np.int16)
        frame_len = detector.frame_length
        detected = False
        for i in range(0, len(samples) - frame_len + 1, frame_len):
            frame = samples[i : i + frame_len]
            if ring_buffer:
                ring_buffer.push(frame.tobytes())
            res = detector.process(frame.tolist())
            if res >= 0:
                publish_detected(pub, 0.99, "porcupine-wav", wcfg.keyword, wcfg.variant)
                dump_ring_buffer(ring_buffer, dump_path, logger)
                logger.info("Wakeword detected in WAV at frame index %s", i)
                detected = True
                break
        if not detected:
            logger.info("No wakeword detected in WAV %s", wav_path)
        return

    if detector is not None and use_audio_manager:
        import numpy as np

        # Use AudioManager as the **only** mic owner.
        # When this path is enabled we do **not** fall back to
        # opening ALSA directly; if AudioManager is unhealthy the
        # service should fail fast so the operator can fix capture.
        frames_ms = int(audio_cfg.get("wakeword_frame_ms", 30))
        session_id = f"wakeword-{os.getpid()}"
        am_client = AudioManagerClient(cfg, logger)
        ok, reason = am_client.start_session(
            session_id,
            target_rate=detector.sample_rate,
            max_seconds=86400.0,
        )
        if not ok:
            logger.error("Failed to start AudioManager wakeword session (%s); exiting", reason)
            sys.exit(1)

        logger.info(
            "Listening for wakeword via AudioManager (session=%s, frame_ms=%s, rate=%s)",
            session_id,
            frames_ms,
            detector.sample_rate,
        )
        try:
            while True:
                pcm = am_client.read_chunk(session_id, frames_ms)
                if not pcm:
                    # Avoid tight spin if no data yet
                    time.sleep(frames_ms / 1000.0)
                    continue
                samples = np.frombuffer(pcm, dtype=np.int16)
                if samples.size < detector.frame_length:
                    continue
                # Porcupine expects fixed-size frames
                frame = samples[: detector.frame_length]
                if ring_buffer:
                    ring_buffer.push(frame.tobytes())
                result = detector.process(frame.tolist())
                if result >= 0:
                    logger.info("Wakeword detected via AudioManager (source=porcupine, frame_len=%s)", detector.frame_length)
                    publish_detected(pub, 0.99, "porcupine", wcfg.keyword, wcfg.variant)
                    dump_ring_buffer(ring_buffer, dump_path, logger)
        except KeyboardInterrupt:
            logger.info("Interrupted, closing porcupine (AudioManager mode)")
        finally:
            am_client.stop_session(session_id)
            detector.delete()
        return

    if detector is not None:
        import pyaudio
        pa = pyaudio.PyAudio()
        rate = int(pa.get_device_info_by_index(device_index)["defaultSampleRate"]) if device_index is not None else detector.sample_rate
        frame_length = detector.frame_length
        frames_per_buffer = max(int(frame_length * rate / detector.sample_rate), frame_length)
        stream = pa.open(
            rate=rate,
            channels=1,
            format=pyaudio.paInt16,
            input=True,
            frames_per_buffer=frames_per_buffer,
            input_device_index=device_index,
        )
        logger.info("Listening for wakeword (porcupine, direct ALSA)")
        try:
            while True:
                pcm = stream.read(frames_per_buffer, exception_on_overflow=False)
                import numpy as np

                samples = np.frombuffer(pcm, dtype=np.int16)
                resampled = _resample_int16(samples, rate, detector.sample_rate, frame_length)
                if ring_buffer:
                    ring_buffer.push(resampled.tobytes())
                result = detector.process(resampled.tolist())
                if result >= 0:
                    logger.info("Wakeword detected (direct ALSA, source=porcupine)")
                    publish_detected(pub, 0.99, "porcupine", wcfg.keyword, wcfg.variant)
                    dump_ring_buffer(ring_buffer, dump_path, logger)
        except KeyboardInterrupt:
            logger.info("Interrupted, closing porcupine")
        finally:
            stream.stop_stream()
            stream.close()
            pa.terminate()
            detector.delete()
        return

    # Fallback: energy-based spike detection
    logger.warning("Porcupine unavailable; using energy-threshold fallback")
    try:
        import pyaudio
        pa = pyaudio.PyAudio()
        rate = int(pa.get_device_info_by_index(device_index)["defaultSampleRate"]) if device_index is not None else 16000
        frames = 1024
        stream = pa.open(
            rate=rate,
            channels=1,
            format=pyaudio.paInt16,
            input=True,
            frames_per_buffer=frames,
            input_device_index=device_index,
        )
        logger.info("Listening for wakeword (fallback) - energy threshold")
        threshold = 2000
        fb_ring: AudioRingBuffer | None = None
        if dump_audio_enabled:
            fb_ring = AudioRingBuffer(args.dump_audio_seconds, rate)
        try:
            while True:
                data = stream.read(frames, exception_on_overflow=False)
                if fb_ring:
                    fb_ring.push(data)
                volume = max(abs(int.from_bytes(data[i:i+2], "little", signed=True)) for i in range(0, len(data), 2))
                if volume > threshold:
                    logger.info("Wakeword-like spike detected (fallback energy threshold, volume=%s)", volume)
                    publish_detected(pub, min(volume / 32768.0, 1.0), "fallback", wcfg.keyword, wcfg.variant)
                    dump_ring_buffer(fb_ring, dump_path, logger)
                    time.sleep(1.5)
        except KeyboardInterrupt:
            logger.info("Interrupted, closing fallback listener")
        finally:
            stream.stop_stream()
            stream.close()
            pa.terminate()
    except Exception:
        logger.error("Audio input not available; exiting")
        sys.exit(1)
