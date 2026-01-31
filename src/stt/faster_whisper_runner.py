"""Capture audio from ALSA, run faster-whisper (CTranslate2), and publish STT over ZeroMQ.

This runner mirrors the CLI and payload shape of whisper_runner.py so the
orchestrator logic and tests remain compatible.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import struct
import sys
import tempfile
import time
import wave
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import zmq  # type: ignore
import subprocess

from src.core.config_loader import load_config
from src.core.ipc import TOPIC_STT, make_publisher, publish_json


def append_setup_log(message: str) -> None:
    log_file = PROJECT_ROOT / "logs" / "setup.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S %Z")
    with log_file.open("a", encoding="utf-8") as fh:
        fh.write(f"{timestamp} [faster_whisper_runner] {message}\n")


def calc_rms(chunk: bytes) -> float:
    if not chunk:
        return 0.0
    sample_count = len(chunk) // 2
    if sample_count == 0:
        return 0.0
    fmt = f"<{sample_count}h"
    samples = struct.unpack(fmt, chunk[: sample_count * 2])
    energy = sum(sample * sample for sample in samples) / sample_count
    return min(1.0, math.sqrt(energy) / 32768.0)


def capture_audio(
    mic: str,
    sample_rate: int,
    silence_threshold: float,
    silence_duration_ms: int,
    max_capture_seconds: int,
) -> bytes:
    chunk_duration = 0.2
    chunk_bytes = int(sample_rate * chunk_duration) * 2
    silence_timeout = silence_duration_ms / 1000.0
    cmd = [
        "arecord",
        "-q",
        "-D",
        mic,
        "-f",
        "S16_LE",
        "-c",
        "1",
        "-r",
        str(sample_rate),
        "-t",
        "raw",
    ]
    append_setup_log(f"Starting arecord on {mic} @ {sample_rate} Hz")
    proc = None
    buffer = bytearray()
    voice_detected = False
    last_voice = None
    start = time.monotonic()

    try:
        proc = os.popen(" ")  # placeholder to satisfy type checkers
    except Exception:
        pass

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)  # type: ignore
    try:
        while True:
            if proc.stdout is None:
                break
            chunk = proc.stdout.read(chunk_bytes)
            if not chunk:
                break
            buffer.extend(chunk)
            rms = calc_rms(chunk)
            now = time.monotonic()
            if rms >= silence_threshold:
                voice_detected = True
                last_voice = now
            if voice_detected and last_voice is not None and now - last_voice >= silence_timeout:
                append_setup_log("Silence detected; stopping capture")
                break
            if now - start >= max_capture_seconds:
                append_setup_log("Max capture window reached")
                break
    finally:
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:  # type: ignore
                proc.kill()

    return bytes(buffer)


def load_simulated_wav(sim_path: Path, sample_rate: int) -> bytes:
    with wave.open(str(sim_path), "rb") as wf:
        if wf.getnchannels() != 1 or wf.getsampwidth() != 2:
            raise ValueError("simulate-wav must be mono 16-bit PCM")
        if wf.getframerate() != sample_rate:
            append_setup_log(
                f"Sim wav sample_rate {wf.getframerate()} != {sample_rate}; resampling not supported"
            )
        frames = wf.readframes(wf.getnframes())
    return frames


def write_wav(frames: bytes, sample_rate: int) -> Path:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
    tmp.close()
    with wave.open(tmp.name, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(frames)
    return Path(tmp.name)


def transcribe_fast(
    model,
    wav_path: Path,
    language: str,
    *,
    beam_size: int = 1,
) -> tuple[str, float]:
    """Transcribe using a pre-loaded `WhisperModel` instance.

    Returns (text, confidence).
    """

    segments, info = model.transcribe(
        str(wav_path),
        language=language,
        beam_size=beam_size,
        vad_filter=False,
    )
    text_parts: list[str] = []
    logprobs: list[float] = []
    for seg in segments:
        t = getattr(seg, "text", "") or ""
        text_parts.append(t.strip())
        lp = getattr(seg, "avg_logprob", None)
        if isinstance(lp, (int, float)):
            logprobs.append(float(lp))
    text = " ".join(p for p in text_parts if p).strip()
    if logprobs:
        conf = max(0.0, min(1.0, math.exp(sum(logprobs) / len(logprobs))))
    else:
        # Heuristic fallback when avg_logprob is unavailable
        conf = 0.8 if text else 0.0
    return text, conf


def load_fast_model(
    model_name_or_dir: str,
    *,
    device: str = "cpu",
    compute_type: str = "int8",
    download_root: Path | None = None,
):
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except Exception as e:  # pragma: no cover
        raise RuntimeError(
            "faster-whisper is not installed in STT venv; please add it to requirements-stte.txt"
        ) from e

    download_root_str = str(download_root) if download_root else None
    return WhisperModel(
        model_name_or_dir,
        device=device,
        compute_type=compute_type,
        download_root=download_root_str,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline faster-whisper STT runner")
    parser.add_argument("--mic", help="ALSA hardware identifier (plughw:X,Y)")
    parser.add_argument("--fast-model", help="faster-whisper model name or CTranslate2 dir")
    parser.add_argument("--ipc", help="Override IPC upstream address")
    parser.add_argument("--sample-rate", type=int)
    parser.add_argument("--silence-threshold", type=float)
    parser.add_argument("--silence-duration-ms", type=int)
    parser.add_argument("--language", default="en")
    parser.add_argument("--max-capture-seconds", type=int)
    parser.add_argument("--compute-type", default=None, help="int8 | int8_float16 | float16 | float32")
    parser.add_argument("--device", default=None, help="cpu | cuda | auto")
    parser.add_argument("--beam-size", type=int, default=None)
    parser.add_argument("--download-root", default=None, help="Where to store downloaded models")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--simulate-wav", help="Testing: reuse an existing WAV instead of recording")
    parser.add_argument("--mock-fast", action="store_true", help="Testing: bypass model and emit stub text")
    parser.add_argument("--continuous", action="store_true", help="Stay alive and keep listening until interrupted")
    args = parser.parse_args()

    cfg = load_config(Path("config/system.yaml"))
    stt_cfg = cfg.get("stt", {})
    fw_cfg = stt_cfg.get("fast_whisper", {})

    mic = args.mic or stt_cfg.get("mic_hw")
    sample_rate = args.sample_rate or int(stt_cfg.get("sample_rate", 16000))
    silence_threshold = args.silence_threshold or float(stt_cfg.get("silence_threshold", 0.35))
    silence_duration_ms = args.silence_duration_ms or int(stt_cfg.get("silence_duration_ms", 900))
    max_capture_seconds = args.max_capture_seconds or int(stt_cfg.get("max_capture_seconds", 15))
    language = args.language or stt_cfg.get("language", "en")

    fast_model = args.fast_model or fw_cfg.get("model") or "tiny.en"
    compute_type = args.compute_type or fw_cfg.get("compute_type", "int8")
    device = args.device or fw_cfg.get("device", "cpu")
    beam_size = args.beam_size or int(fw_cfg.get("beam_size", 1))
    download_root = Path(args.download_root or fw_cfg.get("download_root", PROJECT_ROOT / "third_party/whisper-fast"))

    if not mic and not simulate_wav:
        raise ValueError("Microphone device (--mic) is required unless --simulate-wav is used")
    if args.ipc:
        os.environ["IPC_UPSTREAM"] = args.ipc

    pub = make_publisher(cfg, channel="upstream")

    simulate_wav = Path(args.simulate_wav) if args.simulate_wav else None
    if simulate_wav and not simulate_wav.exists():
        raise FileNotFoundError(simulate_wav)

    if not simulate_wav and shutil.which("arecord") is None:
        raise FileNotFoundError("arecord binary not found in PATH")

    # Pre-load faster-whisper model to avoid per-utterance load overhead
    fw_model = None
    if not args.mock_fast:
        append_setup_log(f"Loading faster-whisper model: {fast_model} device={device} compute={compute_type}")
        fw_model = load_fast_model(
            fast_model,
            device=device,
            compute_type=compute_type,
            download_root=download_root,
        )

    loop_count = 0
    try:
        while True:
            loop_count += 1
            loop_wall_start = time.time()
            append_setup_log(f"Starting capture loop {loop_count}")
            if simulate_wav:
                frames = load_simulated_wav(simulate_wav, sample_rate)
            else:
                capture_start = time.time()
                frames = capture_audio(
                    mic,
                    sample_rate,
                    silence_threshold,
                    silence_duration_ms,
                    max_capture_seconds,
                )
                capture_ms = int((time.time() - capture_start) * 1000)
            if simulate_wav:
                capture_ms = 0

            if not frames:
                append_setup_log("No audio captured in loop; waiting")
                if not args.continuous:
                    break
                time.sleep(0.1)
                continue

            wav_path = write_wav(frames, sample_rate)
            try:
                if args.mock_fast:
                    append_setup_log("Mock fast-whisper enabled; emitting canned transcription")
                    text = "simulated command"
                    confidence = 0.95
                    stt_ms = 0
                else:
                    stt_start = time.time()
                    text, confidence = transcribe_fast(
                        fw_model,
                        wav_path,
                        language,
                        beam_size=beam_size,
                    )
                    stt_ms = int((time.time() - stt_start) * 1000)

                payload = {
                    "timestamp": int(time.time()),
                    "text": text,
                    "confidence": round(float(confidence or 0.0), 3),
                    "language": language,
                    "durations_ms": {
                        "capture": capture_ms,
                        "whisper": stt_ms,
                        "total": int((time.time() - loop_wall_start) * 1000),
                    },
                    "source": "stt.faster_whisper",
                }
                publish_json(pub, TOPIC_STT, payload)
                append_setup_log(f"Published transcription: {payload}")
                if args.debug:
                    print(json.dumps(payload, indent=2))
            finally:
                try:
                    wav_path.unlink()
                except OSError:
                    pass

            if not args.continuous:
                break
    except KeyboardInterrupt:
        append_setup_log("Continuous mode interrupted by user")


if __name__ == "__main__":
    # Local import to avoid top-level dependency when only sim/mocked runs occur
    import subprocess  # noqa: E402
    main()
