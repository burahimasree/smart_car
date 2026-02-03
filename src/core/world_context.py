from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

import zmq

from src.core.ipc import (
    TOPIC_CMD_VISION_MODE,
    TOPIC_DISPLAY_STATE,
    TOPIC_ESP,
    TOPIC_NAV,
    TOPIC_REMOTE_EVENT,
    TOPIC_VISN,
    make_subscriber,
)
from src.core.logging_setup import get_logger

logger = get_logger("world.context", Path("logs"))


@dataclass
class _TimedValue:
    value: Optional[Dict[str, Any]] = None
    received_ts: Optional[float] = None

    def update(self, value: Dict[str, Any], ts: Optional[float] = None) -> None:
        self.value = value
        self.received_ts = ts if ts is not None else time.time()


@dataclass
class WorldSnapshot:
    vision: Dict[str, Any] = field(default_factory=dict)
    sensors: Dict[str, Any] = field(default_factory=dict)
    robot_state: Dict[str, Any] = field(default_factory=dict)
    generated_at: int = 0


class WorldContextAggregator:
    def __init__(self, config: Dict[str, Any]) -> None:
        self.config = config
        self._vision = _TimedValue()
        self._sensors = _TimedValue()
        self._robot = _TimedValue()
        self._scan_summary = _TimedValue()
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None

        self._sub_up = make_subscriber(config, channel="upstream")
        self._sub_up.setsockopt(zmq.SUBSCRIBE, TOPIC_VISN)
        self._sub_up.setsockopt(zmq.SUBSCRIBE, TOPIC_ESP)
        self._sub_up.setsockopt(zmq.SUBSCRIBE, TOPIC_REMOTE_EVENT)

        self._sub_down = make_subscriber(config, channel="downstream")
        self._sub_down.setsockopt(zmq.SUBSCRIBE, TOPIC_DISPLAY_STATE)
        self._sub_down.setsockopt(zmq.SUBSCRIBE, TOPIC_NAV)
        self._sub_down.setsockopt(zmq.SUBSCRIBE, TOPIC_CMD_VISION_MODE)

        self._poller = zmq.Poller()
        self._poller.register(self._sub_up, zmq.POLLIN)
        self._poller.register(self._sub_down, zmq.POLLIN)

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="WorldContext")
        self._thread.start()
        logger.info("World context aggregator started")

    def stop(self) -> None:
        self._running = False

    def _loop(self) -> None:
        while self._running:
            events = dict(self._poller.poll(timeout=200))
            if self._sub_up in events:
                self._drain(self._sub_up)
            if self._sub_down in events:
                self._drain(self._sub_down)

    def _drain(self, sock: zmq.Socket) -> None:
        while True:
            try:
                topic, raw = sock.recv_multipart(flags=zmq.NOBLOCK)
            except zmq.Again:
                break
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue

            now = time.time()
            with self._lock:
                if topic == TOPIC_VISN:
                    vision = {
                        "label": payload.get("label"),
                        "bbox": payload.get("bbox"),
                        "confidence": payload.get("confidence"),
                        "ts": payload.get("ts"),
                        "request_id": payload.get("request_id"),
                    }
                    self._vision.update(vision, ts=now)
                elif topic == TOPIC_ESP:
                    sensors = {
                        "data": payload.get("data"),
                        "alert": payload.get("alert"),
                        "blocked": payload.get("blocked"),
                        "reason": payload.get("reason"),
                    }
                    self._sensors.update(sensors, ts=now)
                elif topic == TOPIC_DISPLAY_STATE:
                    robot = self._robot.value or {}
                    robot = {**robot, "mode": payload.get("state")}
                    self._robot.update(robot, ts=now)
                elif topic == TOPIC_NAV:
                    robot = self._robot.value or {}
                    robot = {**robot, "motion": payload.get("direction")}
                    self._robot.update(robot, ts=now)
                elif topic == TOPIC_CMD_VISION_MODE:
                    robot = self._robot.value or {}
                    robot = {**robot, "vision_mode": payload.get("mode")}
                    self._robot.update(robot, ts=now)
                elif topic == TOPIC_REMOTE_EVENT:
                    if payload.get("event") == "scan_complete":
                        summary = payload.get("summary")
                        if summary:
                            self._scan_summary.update({"summary": summary}, ts=now)

    def get_snapshot(self) -> Dict[str, Any]:
        now = time.time()
        with self._lock:
            vision_age = self._age_ms(self._vision.received_ts, now)
            sensors_age = self._age_ms(self._sensors.received_ts, now)
            robot_age = self._age_ms(self._robot.received_ts, now)
            scan_age = self._age_ms(self._scan_summary.received_ts, now)

            sensors_data = (self._sensors.value or {}).get("data") or {}
            gas_level = sensors_data.get("mq2")
            try:
                gas_level = int(gas_level) if gas_level is not None else None
            except Exception:
                gas_level = None
            gas_threshold = int(self.config.get("orchestrator", {}).get("gas_threshold", 800))
            gas_warning = gas_level is not None and gas_level >= gas_threshold

            lmotor = sensors_data.get("lmotor")
            rmotor = sensors_data.get("rmotor")
            motor_active = False
            try:
                motor_active = (lmotor is not None and int(lmotor) != 0) or (rmotor is not None and int(rmotor) != 0)
            except Exception:
                motor_active = False

            safety_status = "clear"
            if gas_warning:
                safety_status = "gas_warning"
            elif sensors_data.get("obstacle"):
                safety_status = "obstacle"
            elif sensors_data.get("warning"):
                safety_status = "warning"
            elif (self._sensors.value or {}).get("alert"):
                safety_status = "alert"

            vision_active = vision_age is not None and vision_age <= 5000
            snapshot = WorldSnapshot(
                vision={
                    "last_known": self._vision.value,
                    "age_ms": vision_age,
                    "stale": self._is_stale(vision_age),
                    "active": vision_active,
                    "last_received_ts": int(self._vision.received_ts) if self._vision.received_ts else None,
                },
                sensors={
                    "last_known": self._sensors.value,
                    "age_ms": sensors_age,
                    "stale": self._is_stale(sensors_age),
                },
                robot_state={
                    "last_known": self._robot.value,
                    "age_ms": robot_age,
                    "stale": self._is_stale(robot_age),
                },
                generated_at=int(now),
            )
            return {
                "vision": snapshot.vision,
                "sensors": snapshot.sensors,
                "robot_state": snapshot.robot_state,
                "last_scan_summary": self._scan_summary.value.get("summary") if self._scan_summary.value else None,
                "last_scan_age_ms": scan_age,
                "gas_level": gas_level,
                "gas_warning": gas_warning,
                "safety_status": safety_status,
                "motor_active": motor_active,
                "generated_at": snapshot.generated_at,
                "context_type": "last_known_state",
            }

    @staticmethod
    def _age_ms(ts: Optional[float], now: float) -> Optional[int]:
        if ts is None:
            return None
        return int((now - ts) * 1000)

    @staticmethod
    def _is_stale(age_ms: Optional[int], threshold_ms: int = 5000) -> Optional[bool]:
        if age_ms is None:
            return None
        return age_ms > threshold_ms
