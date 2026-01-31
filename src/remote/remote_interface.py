#!/usr/bin/env python3
"""Remote supervision interface (read-only status + intent commands).

Exposes HTTP endpoints bound to a private network address and translates
remote intents into internal IPC topics for the orchestrator.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address, ip_network
from pathlib import Path
from typing import Any, Dict, List, Optional

import zmq

from src.core.config_loader import load_config
from src.core.ipc import (
    TOPIC_CMD_VISION_MODE,
    TOPIC_CMD_PAUSE_VISION,
    TOPIC_DISPLAY_STATE,
    TOPIC_DISPLAY_TEXT,
    TOPIC_ESP,
    TOPIC_HEALTH,
    TOPIC_REMOTE_EVENT,
    TOPIC_REMOTE_INTENT,
    TOPIC_REMOTE_SESSION,
    TOPIC_VISN,
    TOPIC_VISN_CAPTURED,
    TOPIC_VISN_FRAME,
    make_publisher,
    make_subscriber,
    publish_json,
)
from src.core.logging_setup import get_logger


class TelemetryState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.display_state: str = "unknown"
        self.display_text: str = ""
        self.vision_mode: str = "off"
        self.vision_paused: bool = False
        self.last_detection: Optional[Dict[str, Any]] = None
        self.detection_history: List[Dict[str, Any]] = []
        self.last_esp: Optional[Dict[str, Any]] = None
        self.last_alert: Optional[str] = None
        self.last_health: Optional[Dict[str, Any]] = None
        self.remote_session_active: bool = False
        self.remote_last_seen: float = 0.0
        self.last_remote_event: Optional[Dict[str, Any]] = None
        self.last_capture: Optional[Dict[str, Any]] = None

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            safety_stop = False
            motor_enabled: Optional[bool] = None
            min_distance: Optional[int] = None
            obstacle = False
            warning = False
            if self.last_esp and "data" in self.last_esp:
                data = self.last_esp["data"] or {}
                obstacle = bool(data.get("obstacle", False))
                warning = bool(data.get("warning", False))
                min_distance = data.get("min_distance")
                is_safe = data.get("is_safe")
                if isinstance(is_safe, bool):
                    motor_enabled = is_safe
            if obstacle or warning or (self.last_alert is not None):
                safety_stop = True

            stream_url = "/stream/mjpeg" if self.vision_mode == "on_with_stream" else None
            return {
                "remote_session_active": self.remote_session_active,
                "remote_last_seen": int(self.remote_last_seen) if self.remote_last_seen else None,
                "mode": self.display_state,
                "display_text": self.display_text,
                "vision_mode": self.vision_mode,
                "stream_url": stream_url,
                "vision_active": self.vision_mode != "off",
                "vision_paused": self.vision_paused,
                "motor_enabled": motor_enabled,
                "safety_stop": safety_stop,
                "safety_alert": self.last_alert,
                "sensor": self.last_esp.get("data") if self.last_esp else None,
                "vision_last_detection": self.last_detection,
                "detection_history": list(self.detection_history),
                "last_capture": self.last_capture,
                "health": self.last_health,
                "remote_event": self.last_remote_event,
            }


class RemoteSupervisor:
    def __init__(self, config_path: Path) -> None:
        self.config = load_config(config_path)
        log_dir = Path(self.config.get("logs", {}).get("directory", "logs"))
        if not log_dir.is_absolute():
            log_dir = Path.cwd() / log_dir
        self.logger = get_logger("remote.interface", log_dir)

        remote_cfg = self.config.get("remote_interface", {}) or {}
        self.bind_host = remote_cfg.get("bind_host", "127.0.0.1")
        self.bind_port = int(remote_cfg.get("port", 8770))
        self.allowed_cidrs = self._parse_cidrs(remote_cfg.get("allowed_cidrs", ["100.64.0.0/10"]))
        self.session_timeout_s = float(remote_cfg.get("session_timeout_s", 15.0))
        self._detection_history_max = int(remote_cfg.get("detection_history_max", 200))

        self.telemetry = TelemetryState()
        self._ctx = zmq.Context.instance()
        self._pub = make_publisher(self.config, channel="upstream")
        self._sub_up = make_subscriber(self.config, channel="upstream")
        self._sub_down = make_subscriber(self.config, channel="downstream")

        # Subscribe to key telemetry topics
        for topic in [
            TOPIC_ESP,
            TOPIC_VISN,
            TOPIC_VISN_FRAME,
            TOPIC_VISN_CAPTURED,
            TOPIC_HEALTH,
            TOPIC_REMOTE_EVENT,
        ]:
            self._sub_up.setsockopt(zmq.SUBSCRIBE, topic)

        for topic in [
            TOPIC_DISPLAY_STATE,
            TOPIC_DISPLAY_TEXT,
            TOPIC_CMD_VISION_MODE,
            TOPIC_CMD_PAUSE_VISION,
        ]:
            self._sub_down.setsockopt(zmq.SUBSCRIBE, topic)

        self._poller = zmq.Poller()
        self._poller.register(self._sub_up, zmq.POLLIN)
        self._poller.register(self._sub_down, zmq.POLLIN)

        self._running = True
        self._last_session_emit = False
        self._stream_lock = threading.Condition()
        self._latest_frame: Optional[bytes] = None
        self._latest_frame_ts: float = 0.0

    @staticmethod
    def _parse_cidrs(raw: Any) -> List[Any]:
        cidrs = raw if isinstance(raw, list) else [raw]
        nets = []
        for entry in cidrs:
            if not entry:
                continue
            try:
                nets.append(ip_network(str(entry)))
            except ValueError:
                continue
        return nets

    def _client_allowed(self, client_ip: str) -> bool:
        if not self.allowed_cidrs:
            return True
        try:
            addr = ip_address(client_ip)
        except ValueError:
            return False
        return any(addr in net for net in self.allowed_cidrs)

    def _touch_session(self) -> None:
        now = time.time()
        with self.telemetry.lock:
            self.telemetry.remote_last_seen = now
            self.telemetry.remote_session_active = True

    def _publish_session_state(self) -> None:
        with self.telemetry.lock:
            active = self.telemetry.remote_session_active
            last_seen = self.telemetry.remote_last_seen
        publish_json(
            self._pub,
            TOPIC_REMOTE_SESSION,
            {
                "active": bool(active),
                "last_seen": int(last_seen) if last_seen else None,
                "source": "remote_app",
            },
        )

    def _session_watchdog(self) -> None:
        while self._running:
            now = time.time()
            with self.telemetry.lock:
                active = self.telemetry.remote_session_active
                last_seen = self.telemetry.remote_last_seen
            if active and last_seen and (now - last_seen) > self.session_timeout_s:
                with self.telemetry.lock:
                    self.telemetry.remote_session_active = False
                self._publish_session_state()
            time.sleep(1.0)

    def _telemetry_loop(self) -> None:
        while self._running:
            events = dict(self._poller.poll(timeout=200))
            if self._sub_up in events:
                self._drain_socket(self._sub_up)
            if self._sub_down in events:
                self._drain_socket(self._sub_down)

    def _drain_socket(self, sock: zmq.Socket) -> None:
        while True:
            try:
                topic, raw = sock.recv_multipart(flags=zmq.NOBLOCK)
            except zmq.Again:
                break
            if topic == TOPIC_VISN_FRAME:
                with self._stream_lock:
                    self._latest_frame = raw
                    self._latest_frame_ts = time.time()
                    self._stream_lock.notify_all()
                continue

            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue

            with self.telemetry.lock:
                if topic == TOPIC_DISPLAY_STATE:
                    self.telemetry.display_state = str(payload.get("state", "unknown"))
                elif topic == TOPIC_DISPLAY_TEXT:
                    self.telemetry.display_text = str(payload.get("text", ""))
                elif topic == TOPIC_CMD_VISION_MODE:
                    self.telemetry.vision_mode = str(payload.get("mode", "off"))
                elif topic == TOPIC_CMD_PAUSE_VISION:
                    self.telemetry.vision_paused = bool(payload.get("pause", False))
                elif topic == TOPIC_VISN:
                    self.telemetry.last_detection = payload
                    label = str(payload.get("label", ""))
                    if label and label != "none":
                        entry = {
                            "label": label,
                            "bbox": payload.get("bbox"),
                            "confidence": payload.get("confidence"),
                            "ts": payload.get("ts"),
                        }
                        self.telemetry.detection_history.append(entry)
                        if len(self.telemetry.detection_history) > self._detection_history_max:
                            self.telemetry.detection_history = self.telemetry.detection_history[-self._detection_history_max:]
                elif topic == TOPIC_ESP:
                    self.telemetry.last_esp = payload
                    alert = payload.get("alert")
                    if alert:
                        self.telemetry.last_alert = str(alert)
                    if payload.get("blocked"):
                        self.telemetry.last_alert = str(payload.get("reason", "blocked"))
                elif topic == TOPIC_HEALTH:
                    self.telemetry.last_health = payload
                elif topic == TOPIC_REMOTE_EVENT:
                    self.telemetry.last_remote_event = payload
                elif topic == TOPIC_VISN_CAPTURED:
                    self.telemetry.last_capture = payload

    def serve(self) -> None:
        threading.Thread(target=self._telemetry_loop, daemon=True).start()
        threading.Thread(target=self._session_watchdog, daemon=True).start()

        handler = self._make_handler()
        server = ThreadingHTTPServer((self.bind_host, self.bind_port), handler)
        self.logger.info("Remote interface listening on %s:%s", self.bind_host, self.bind_port)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            self._running = False
            server.server_close()

    def _make_handler(self):
        supervisor = self

        class Handler(BaseHTTPRequestHandler):
            def _send_json(self, code: int, payload: Dict[str, Any]) -> None:
                data = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _read_json(self) -> Dict[str, Any]:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0:
                    return {}
                raw = self.rfile.read(length)
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    supervisor.logger.warning("Remote interface received invalid JSON")
                    return {}

            def _allowed(self) -> bool:
                client_ip = self.client_address[0]
                if supervisor._client_allowed(client_ip):
                    return True
                supervisor.logger.warning("Remote interface rejected IP %s", client_ip)
                return False

            def do_GET(self) -> None:
                if not self._allowed():
                    self._send_json(403, {"error": "forbidden"})
                    return
                if self.path == "/stream/mjpeg":
                    with supervisor.telemetry.lock:
                        vision_mode = supervisor.telemetry.vision_mode
                    if vision_mode != "on_with_stream":
                        self._send_json(409, {"error": "stream_disabled"})
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    try:
                        while True:
                            with supervisor.telemetry.lock:
                                if supervisor.telemetry.vision_mode != "on_with_stream":
                                    break
                            with supervisor._stream_lock:
                                if supervisor._latest_frame is None:
                                    supervisor._stream_lock.wait(timeout=1.0)
                                frame = supervisor._latest_frame
                            if frame is None:
                                continue
                            self.wfile.write(b"--frame\r\n")
                            self.wfile.write(b"Content-Type: image/jpeg\r\n")
                            self.wfile.write(f"Content-Length: {len(frame)}\r\n\r\n".encode("utf-8"))
                            self.wfile.write(frame)
                            self.wfile.write(b"\r\n")
                            self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        return
                    except Exception as exc:  # pragma: no cover - defensive
                        supervisor.logger.info("Stream closed: %s", exc)
                    return
                if self.path in {"/status", "/telemetry"}:
                    supervisor._touch_session()
                    supervisor._publish_session_state()
                    self._send_json(200, supervisor.telemetry.snapshot())
                    return
                if self.path == "/health":
                    self._send_json(200, {"ok": True, "timestamp": int(time.time())})
                    return
                self._send_json(404, {"error": "not_found"})

            def do_POST(self) -> None:
                if not self._allowed():
                    self._send_json(403, {"error": "forbidden"})
                    return

                if self.path == "/intent":
                    payload = self._read_json()
                    intent = str(payload.get("intent", "")).strip()
                    if not intent:
                        self._send_json(400, {"error": "missing_intent"})
                        return

                    supervisor._touch_session()
                    supervisor._publish_session_state()

                    intent_payload = {
                        **payload,
                        "intent": intent,
                        "source": "remote_app",
                        "timestamp": int(time.time()),
                    }
                    publish_json(supervisor._pub, TOPIC_REMOTE_INTENT, intent_payload)
                    self._send_json(202, {"accepted": True, "intent": intent})
                    return

                self._send_json(404, {"error": "not_found"})

            def log_message(self, format: str, *args) -> None:
                supervisor.logger.info("remote.http %s - %s", self.client_address[0], format % args)

        return Handler


def main() -> None:
    supervisor = RemoteSupervisor(Path("config/system.yaml"))
    supervisor.serve()


if __name__ == "__main__":
    main()
