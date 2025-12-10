#!/usr/bin/env python3
"""Quick integration test for display and motor bridge IPC communication.

Simulates orchestrator sending state changes and verifies services respond.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.config_loader import load_config
from src.core.ipc import (
    TOPIC_CMD_LISTEN_START,
    TOPIC_CMD_LISTEN_STOP,
    TOPIC_LLM_RESP,
    TOPIC_NAV,
    TOPIC_TTS,
    TOPIC_WW_DETECTED,
    make_publisher,
    publish_json,
)


def main():
    cfg = load_config(Path("config/system.yaml"))
    
    # Publisher on downstream channel (like orchestrator)
    pub = make_publisher(cfg, channel="downstream", bind=True)
    time.sleep(0.5)  # Let socket bind
    
    print("IPC Integration Test - Simulating Orchestrator Events")
    print("=" * 50)
    
    # Simulate flow: wakeword → listening → thinking → speaking → nav
    events = [
        (TOPIC_WW_DETECTED, {"keyword": "hey genny", "confidence": 0.95}),
        (TOPIC_CMD_LISTEN_START, {"start": True}),
        (TOPIC_CMD_LISTEN_STOP, {"stop": True}),
        (TOPIC_LLM_RESP, {"ok": True, "json": {"intent": "navigate", "slots": {"direction": "forward"}, "speak": "Moving forward"}}),
        (TOPIC_TTS, {"text": "Moving forward"}),
        (TOPIC_NAV, {"direction": "forward", "speed": 80}),
        (TOPIC_TTS, {"done": True}),
    ]
    
    for topic, payload in events:
        print(f"  → {topic.decode()}: {json.dumps(payload)[:60]}...")
        publish_json(pub, topic, payload)
        time.sleep(0.3)
    
    print("=" * 50)
    print("All events published. Check display_runner and motor_bridge logs.")
    print("Services should show state transitions:")
    print("  Display: IDLE → LISTENING → THINKING → SPEAKING → NAVIGATING → IDLE")
    print("  Motor:   Received NAV command: FWD:80")


if __name__ == "__main__":
    main()
