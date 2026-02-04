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
        self._events = _TimedValue()
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None

        orch_cfg = self.config.get("orchestrator", {}) or {}
        self._gas_warning_threshold = int(orch_cfg.get("gas_warning_threshold", 1000))
        self._gas_danger_threshold = int(orch_cfg.get("gas_danger_threshold", 1000))

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
                elif topic == TOPIC_REMOTE_EVENT:
                    event = str(payload.get("event", ""))
                    current = self._events.value or {}
                    if event == "scan_complete":
                        current = {**current, "last_scan_summary": payload.get("summary")}
                    elif event == "gas_warning":
                        current = {**current, "gas_warning": True, "gas_severity": "warning"}
                    elif event == "gas_danger":
                        current = {**current, "gas_warning": True, "gas_severity": "danger"}
                    elif event == "gas_clear":
                        current = {**current, "gas_warning": False, "gas_severity": "clear"}
                    self._events.update(current, ts=now)
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

    def get_snapshot(self) -> Dict[str, Any]:
        now = time.time()
        with self._lock:
            vision_age = self._age_ms(self._vision.received_ts, now)
            sensors_age = self._age_ms(self._sensors.received_ts, now)
            robot_age = self._age_ms(self._robot.received_ts, now)
            events_age = self._age_ms(self._events.received_ts, now)

            sensor_data = (self._sensors.value or {}).get("data") or {}
            gas_level = sensor_data.get("mq2")
            gas_warning = self._events.value.get("gas_warning") if self._events.value else None
            gas_severity = self._events.value.get("gas_severity") if self._events.value else None
            if gas_severity is None and gas_level is not None:
                gas_value = int(gas_level)
                if gas_value >= self._gas_danger_threshold:
                    gas_severity = "danger"
                elif gas_value >= self._gas_warning_threshold:
                    gas_severity = "warning"
                else:
                    gas_severity = "clear"
            if gas_warning is None and gas_severity is not None:
                gas_warning = gas_severity in {"warning", "danger"}

            motor_left = sensor_data.get("lmotor")
            motor_right = sensor_data.get("rmotor")
            motor_active = False
            if motor_left is not None or motor_right is not None:
                motor_active = bool((motor_left or 0) != 0 or (motor_right or 0) != 0)

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
                "motor_activity": {
                    "left": motor_left,
                    "right": motor_right,
                    "active": motor_active,
                },
                "safety": {
                    "obstacle": sensor_data.get("obstacle"),
                    "warning": sensor_data.get("warning"),
                    "is_safe": sensor_data.get("is_safe"),
                },
                "gas_level": gas_level,
                "gas_warning": gas_warning,
                "gas_severity": gas_severity,
                "last_scan_summary": (self._events.value or {}).get("last_scan_summary"),
                "events_age_ms": events_age,
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
