"""Simulation test for wakeword runner publishing ww.detected.

Runs the wakeword runner in --sim mode and subscribes to upstream
to assert that a single ww.detected payload is received matching the schema.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import yaml
import zmq

CONFIG = Path("config/system.yaml")


def expected_keywords():
    cfg = yaml.safe_load(CONFIG.read_text())
    ww = cfg.get("wakeword", {})
    keywords = ww.get("keywords") or []
    primary = (
        ww.get("payload_keyword")
        or ww.get("primary_keyword")
        or (keywords[0] if keywords else "genny")
    )
    variant = ww.get("payload_variant") or ww.get("variant_keyword") or primary
    return primary, variant


def test_wakeword_simulation():
    # Override IPC ports to avoid collisions
    bind_addr = "tcp://*:6205"
    connect_addr = "tcp://127.0.0.1:6205"
    os.environ["IPC_UPSTREAM"] = connect_addr
    ctx = zmq.Context.instance()
    sub = ctx.socket(zmq.SUB)
    sub.bind(bind_addr)
    sub.setsockopt(zmq.SUBSCRIBE, b"ww.detected")
    # Allow subscriber to bind before starting the wakeword runner
    time.sleep(0.5)

    env = os.environ.copy()
    env.setdefault("PYTHONPATH", str(Path(__file__).resolve().parents[2]))
    proc = subprocess.Popen(
        [
            sys.executable,
            "src/wakeword/porcupine_runner.py",
            "--sim",
            "--after",
            "0.2",
            "--ipc",
            connect_addr,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
        cwd=Path(__file__).resolve().parents[2],
    )

    poller = zmq.Poller()
    poller.register(sub, zmq.POLLIN)
    deadline = time.time() + 10
    received = None
    while time.time() < deadline:
        events = dict(poller.poll(200))
        if sub in events:
            topic, data = sub.recv_multipart()
            payload = json.loads(data)
            received = payload
            break
    proc.wait(timeout=5)
    assert received is not None, "Did not receive ww.detected in sim mode"
    expected_keyword, expected_variant = expected_keywords()
    assert received["keyword"] == expected_keyword
    assert received["variant"] == expected_variant
    assert isinstance(received["confidence"], float)
    assert received["source"] in {"porcupine", "sim", "fallback"}
