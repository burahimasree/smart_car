"""Simulation test for STT wrapper publishing stt.transcription.

Runs the STT wrapper in --sim mode, sends a cmd.listen.start over the
downstream bus, and asserts that a single stt.transcription payload is
received on the upstream bus.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import zmq

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_stt_wrapper_simulation_publishes_transcription() -> None:
    # Use isolated IPC ports to avoid collisions with other tests
    upstream_bind = "tcp://*:6225"
    upstream_connect = "tcp://127.0.0.1:6225"
    downstream_bind = "tcp://*:6226"
    downstream_connect = "tcp://127.0.0.1:6226"

    os.environ["IPC_UPSTREAM"] = upstream_connect
    os.environ["IPC_DOWNSTREAM"] = downstream_connect

    ctx = zmq.Context.instance()

    # Subscriber to upstream (STT events from wrapper)
    sub_stt = ctx.socket(zmq.SUB)
    sub_stt.bind(upstream_bind)
    sub_stt.setsockopt(zmq.SUBSCRIBE, b"stt.transcription")

    # Publisher to downstream (commands to wrapper)
    pub_cmds = ctx.socket(zmq.PUB)
    pub_cmds.bind(downstream_bind)

    # Allow sockets to bind before starting wrapper
    time.sleep(0.5)

    env = os.environ.copy()
    env.setdefault("PYTHONPATH", str(PROJECT_ROOT))

    proc = subprocess.Popen(
        [
            sys.executable,
            "src/stt/stt_wrapper_runner.py",
            "--sim",
        ],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
    )

    try:
        # Give wrapper time to connect to IPC
        time.sleep(0.5)

        # Send a single cmd.listen.start command
        from src.core.ipc import TOPIC_CMD_LISTEN_START  # type: ignore

        payload = {"start": True}
        pub_cmds.send_multipart([TOPIC_CMD_LISTEN_START, json.dumps(payload).encode("utf-8")])

        poller = zmq.Poller()
        poller.register(sub_stt, zmq.POLLIN)
        deadline = time.time() + 10
        received = None

        while time.time() < deadline:
            events = dict(poller.poll(200))
            if sub_stt in events:
                topic, data = sub_stt.recv_multipart()
                assert topic == b"stt.transcription"
                received = json.loads(data)
                break

        assert received is not None, "STT wrapper did not publish transcription in sim mode"
        assert isinstance(received.get("text"), str)
        assert received["text"]
        assert isinstance(received.get("confidence"), float)
        assert isinstance(received.get("timestamp"), int)
        assert isinstance(received.get("language"), str)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
