"""Test orchestrator event flow (wakeword -> STT -> LLM -> TTS completion).

Simulates the message sequence over ZeroMQ and validates orchestrator
publishes expected command/control topics.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import zmq

from src.core.orchestrator import Orchestrator
from src.core.ipc import (
    TOPIC_WW_DETECTED,
    TOPIC_STT,
    TOPIC_LLM_REQ,
    TOPIC_LLM_RESP,
    TOPIC_TTS,
    TOPIC_CMD_PAUSE_VISION,
    TOPIC_CMD_LISTEN_START,
    TOPIC_CMD_LISTEN_STOP,
)


def run_orchestrator():
    orch = Orchestrator()
    # Run limited iterations: after a timeout thread will stop context
    try:
        orch.run()
    except Exception:
        pass


def test_orchestrator_sequence():
    # Use isolated ports
    upstream = "tcp://127.0.0.1:6210"
    downstream = "tcp://127.0.0.1:6211"
    os.environ["IPC_UPSTREAM"] = upstream
    os.environ["IPC_DOWNSTREAM"] = downstream
    os.environ["STT_ENGINE_DISABLED"] = "1"

    ctx = zmq.Context.instance()
    # Publisher to upstream (events to orchestrator)
    pub_events = ctx.socket(zmq.PUB)
    pub_events.connect(upstream)
    # Subscriber to downstream (commands from orchestrator)
    sub_cmds = ctx.socket(zmq.SUB)
    sub_cmds.connect(downstream)
    for t in [
        TOPIC_CMD_PAUSE_VISION,
        TOPIC_CMD_LISTEN_START,
        TOPIC_CMD_LISTEN_STOP,
        TOPIC_LLM_RESP,
        TOPIC_LLM_REQ,
        TOPIC_TTS,
    ]:
        sub_cmds.setsockopt(zmq.SUBSCRIBE, t)

    # Start orchestrator thread (binds sockets)
    th = threading.Thread(target=run_orchestrator, daemon=True)
    th.start()
    time.sleep(0.4)  # allow bind/setup

    def send(topic: bytes, payload: dict):
        pub_events.send_multipart([topic, json.dumps(payload).encode("utf-8")])

    # 1. Wakeword event
    send(TOPIC_WW_DETECTED, {"timestamp": int(time.time()), "keyword": "genny", "variant": "genny", "confidence": 0.99, "source": "porcupine"})
    pause_seen = listen_start_seen = False

    poller = zmq.Poller()
    poller.register(sub_cmds, zmq.POLLIN)
    deadline = time.time() + 5
    while time.time() < deadline and not (pause_seen and listen_start_seen):
        events = dict(poller.poll(200))
        if sub_cmds in events:
            topic, data = sub_cmds.recv_multipart()
            payload = json.loads(data)
            if topic == TOPIC_CMD_PAUSE_VISION and payload.get("pause") is True:
                pause_seen = True
            if topic == TOPIC_CMD_LISTEN_START and payload.get("start") is True:
                listen_start_seen = True
    assert pause_seen and listen_start_seen, "Did not see pause vision and listen start after wakeword"

    # 2. STT transcription event
    send(TOPIC_STT, {"timestamp": int(time.time()), "text": "Move forward", "confidence": 0.91, "language": "en"})

    llm_req_seen = False
    listen_stop_seen = False
    resume_detected = False
    while time.time() < deadline and not (llm_req_seen and listen_stop_seen):
        events = dict(poller.poll(200))
        if sub_cmds in events:
            topic, data = sub_cmds.recv_multipart()
            payload = json.loads(data)
            if topic == TOPIC_LLM_REQ and payload.get("text") == "Move forward":
                llm_req_seen = True
            if topic == TOPIC_CMD_LISTEN_STOP and payload.get("stop") is True:
                listen_stop_seen = True
            if topic == TOPIC_CMD_PAUSE_VISION and payload.get("pause") is False:
                resume_detected = True
    assert llm_req_seen and listen_stop_seen, "Missing llm.request or listen stop after STT"
    assert not resume_detected, "Vision resumed before LLM/TTS finished"

    # 3. LLM response (simulate intent)
    send(
        TOPIC_LLM_RESP,
        {"timestamp": int(time.time()), "text": "Moving forward", "tokens": 5, "latency_ms": 1200},
    )

    tts_request_seen = False
    while time.time() < deadline and not tts_request_seen:
        events = dict(poller.poll(200))
        if sub_cmds in events:
            topic, data = sub_cmds.recv_multipart()
            payload = json.loads(data)
            if topic == TOPIC_TTS and payload.get("text") == "Moving forward":
                tts_request_seen = True
            if topic == TOPIC_CMD_PAUSE_VISION and payload.get("pause") is False:
                resume_detected = True
    assert tts_request_seen, "Missing TTS request"
    assert not resume_detected, "Vision resumed before TTS completion"

    # 4. TTS completion does not change pause state but should be accepted without errors
    send(TOPIC_TTS, {"done": True})

    resumed_after_tts = False
    while time.time() < deadline and not resumed_after_tts:
        events = dict(poller.poll(200))
        if sub_cmds in events:
            topic, data = sub_cmds.recv_multipart()
            payload = json.loads(data)
            if topic == TOPIC_CMD_PAUSE_VISION and payload.get("pause") is False:
                resumed_after_tts = True
    assert resumed_after_tts, "Vision did not resume after TTS completion"
