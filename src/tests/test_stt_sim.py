"""Simulated STT run ensuring whisper_runner publishes transcription quickly."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

import zmq

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _make_silence_wav(path: Path, sample_rate: int = 16000) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x00\x00" * sample_rate)


def test_whisper_runner_simulation_publishes_transcription() -> None:
    upstream_bind = "tcp://*:6230"
    upstream_connect = "tcp://127.0.0.1:6230"

    ctx = zmq.Context.instance()
    sub = ctx.socket(zmq.SUB)
    sub.bind(upstream_bind)
    sub.setsockopt(zmq.SUBSCRIBE, b"stt.transcription")
    # Give SUB socket time to bind before starting the runner
    time.sleep(0.5)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        wav_path = tmp_path / "sim.wav"
        model_path = tmp_path / "tiny.en-q5_1.gguf"
        _make_silence_wav(wav_path)
        model_path.write_bytes(b"placeholder")

        env = os.environ.copy()
        env.setdefault("PYTHONPATH", str(PROJECT_ROOT))
        cmd = [
            sys.executable,
            "src/stt/whisper_runner.py",
            "--mic",
            "plughw:0,0",
            "--model",
            str(model_path),
            "--ipc",
            upstream_connect,
            "--simulate-wav",
            str(wav_path),
            "--mock-whisper",
            "--bin-path",
            str(model_path),
            "--debug",
        ]
        proc = subprocess.Popen(cmd, cwd=PROJECT_ROOT, env=env, text=True)

        poller = zmq.Poller()
        poller.register(sub, zmq.POLLIN)
        deadline = time.time() + 10
        payload = None
        while time.time() < deadline:
            events = dict(poller.poll(200))
            if sub in events:
                topic, data = sub.recv_multipart()
                payload = json.loads(data)
                break
        proc.wait(timeout=5)

    assert payload is not None, "STT runner did not publish transcription"
    assert payload["language"] == "en"
    assert 0 <= float(payload["confidence"]) <= 1
    assert isinstance(payload["timestamp"], int)
    assert isinstance(payload["text"], str) and payload["text"]
