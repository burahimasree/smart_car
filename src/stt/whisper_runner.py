"""Capture audio from ALSA, run whisper.cpp, and publish STT over ZeroMQ."""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import zmq

from src.core.config_loader import load_config
from src.core.ipc import TOPIC_STT, make_publisher, publish_json


def append_setup_log(message: str) -> None:
    log_file = PROJECT_ROOT / "logs" / "setup.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S %Z")
    with log_file.open("a", encoding="utf-8") as fh:
        fh.write(f"{timestamp} [whisper_runner] {message}\n")


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
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    buffer = bytearray()
    voice_detected = False
    last_voice = None
    start = time.monotonic()

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
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
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


def run_whisper(bin_path: Path, model_path: Path, wav_path: Path, language: str) -> dict:
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir) / "whisper"
        cmd = [
            str(bin_path),
            "-m",
            str(model_path),
            "-f",
            str(wav_path),
            "-l",
            language,
            "-oj",
            "-otxt",
            "-of",
            str(base),
            "--no-prints",
        ]
        append_setup_log(f"Running whisper.cpp: {' '.join(cmd)}")
        try:
            completed = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=180,
            )
        except subprocess.TimeoutExpired:
            append_setup_log("whisper.cpp timeout after 180s")
            raise RuntimeError("whisper.cpp transcription timeout")
        if completed.returncode != 0:
            append_setup_log(f"whisper.cpp failed: {completed.stderr.strip()}")
            raise RuntimeError("whisper.cpp transcription failed")
        json_path = Path(f"{base}.json")
        if not json_path.exists():
            append_setup_log("whisper.cpp did not produce JSON output")
            raise RuntimeError("whisper.cpp transcription failed")
        return json.loads(json_path.read_text())


def extract_text(payload: dict) -> str:
    text = payload.get("text")
    if text:
        return text.strip()
    segments = payload.get("segments", [])
    joined = " ".join(seg.get("text", "").strip() for seg in segments)
    if joined.strip():
        return " ".join(joined.split())
    transcription = payload.get("transcription", [])
    joined = " ".join(item.get("text", "").strip() for item in transcription)
    return " ".join(joined.split())


def extract_confidence(payload: dict) -> float:
    avg_logprob = payload.get("avg_logprob")
    if isinstance(avg_logprob, (int, float)):
        conf = math.exp(avg_logprob)
        return max(0.0, min(1.0, conf))
    segments = payload.get("segments", [])
    if segments:
        scores = [seg.get("avg_logprob", -1.0) for seg in segments if isinstance(seg.get("avg_logprob"), (int, float))]
        if scores:
            return max(0.0, min(1.0, math.exp(sum(scores) / len(scores))))
    transcription = payload.get("transcription", [])
    scores = [item.get("avg_logprob", -1.0) for item in transcription if isinstance(item.get("avg_logprob"), (int, float))]
    if scores:
        return max(0.0, min(1.0, math.exp(sum(scores) / len(scores))))
    return 0.6


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline Whisper.cpp STT runner")
    parser.add_argument("--mic", help="ALSA hardware identifier (plughw:X,Y)")
    parser.add_argument("--model", help="Path to whisper GGUF model")
    parser.add_argument("--ipc", help="Override IPC upstream address")
    parser.add_argument("--sample-rate", type=int)
    parser.add_argument("--silence-threshold", type=float)
    parser.add_argument("--silence-duration-ms", type=int)
    parser.add_argument("--language", default="en")
    parser.add_argument("--max-capture-seconds", type=int)
    parser.add_argument("--bin-path", help="Path to whisper.cpp main binary")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--simulate-wav", help="Testing: reuse an existing WAV instead of recording")
    parser.add_argument("--mock-whisper", action="store_true", help="Testing: bypass whisper binary and emit stub text")
    parser.add_argument("--continuous", action="store_true", help="Stay alive and keep listening until interrupted")
    args = parser.parse_args()

    cfg = load_config(Path("config/system.yaml"))
    stt_cfg = cfg.get("stt", {})
    mic = args.mic or stt_cfg.get("mic_hw")
    model = Path(args.model or stt_cfg.get("model_path"))
    sample_rate = args.sample_rate or int(stt_cfg.get("sample_rate", 16000))
    silence_threshold = args.silence_threshold or float(stt_cfg.get("silence_threshold", 0.35))
    silence_duration_ms = args.silence_duration_ms or int(stt_cfg.get("silence_duration_ms", 900))
    max_capture_seconds = args.max_capture_seconds or int(stt_cfg.get("max_capture_seconds", 15))
    language = args.language or stt_cfg.get("language", "en")
    bin_path = Path(args.bin_path or stt_cfg.get("bin_path", PROJECT_ROOT / "third_party/whisper.cpp/main"))

    if not mic:
        raise ValueError("Microphone device (--mic) is required")
    if args.ipc:
        os.environ["IPC_UPSTREAM"] = args.ipc

    pub = make_publisher(cfg, channel="upstream")

    if not model.exists():
        raise FileNotFoundError(model)
    if not bin_path.exists() and not args.mock_whisper:
        raise FileNotFoundError(bin_path)

    simulate_wav = Path(args.simulate_wav) if args.simulate_wav else None

    if simulate_wav and not simulate_wav.exists():
        raise FileNotFoundError(simulate_wav)

    if not simulate_wav and shutil.which("arecord") is None:
        raise FileNotFoundError("arecord binary not found in PATH")

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
                if args.mock_whisper:
                    append_setup_log("Mock whisper enabled; emitting canned transcription")
                    text = "simulated command"
                    confidence = 0.95
                    whisper_ms = 0
                else:
                    whisper_start = time.time()
                    result = run_whisper(bin_path, model, wav_path, language)
                    whisper_ms = int((time.time() - whisper_start) * 1000)
                    text = extract_text(result)
                    confidence = extract_confidence(result)

                payload = {
                    "timestamp": int(time.time()),
                    "text": text,
                    "confidence": round(confidence, 3),
                    "language": language,
                    "durations_ms": {
                        "capture": capture_ms,
                        "whisper": whisper_ms,
                        "total": int((time.time() - loop_wall_start) * 1000),
                    },
                }
                publish_json(pub, TOPIC_STT, payload)
                append_setup_log(f"Published transcription: {payload}")
                if args.debug:
                    print(json.dumps(payload, indent=2))
                # ZMQ PUB sockets may drop messages if the process exits
                # immediately after send; give it a moment to flush.
                if not args.continuous:
                    time.sleep(0.25)
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
    main()
