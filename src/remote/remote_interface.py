#!/usr/bin/env python3
"""Remote supervision interface (read-only status + intent commands).

Exposes HTTP endpoints bound to a private network address and translates
remote intents into internal IPC topics for the orchestrator.
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address, ip_network
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, parse_qs

import zmq
import yaml

from src.core.config_loader import load_config
from src.core.ipc import (
    TOPIC_CMD_VISION_MODE,
    TOPIC_CMD_PAUSE_VISION,
    TOPIC_CMD_CAMERA_SETTINGS,
    TOPIC_DISPLAY_STATE,
    TOPIC_DISPLAY_TEXT,
    TOPIC_ESP,
    TOPIC_HEALTH,
    TOPIC_LLM_RESP,
    TOPIC_REMOTE_EVENT,
    TOPIC_REMOTE_INTENT,
    TOPIC_REMOTE_SESSION,
    TOPIC_TTS,
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
        self.last_llm_response: Optional[str] = None
        self.last_llm_ts: Optional[float] = None
        self.last_tts_text: Optional[str] = None
        self.last_tts_status: Optional[str] = None
        self.last_tts_ts: Optional[float] = None
        self.last_scan_summary: Optional[str] = None
        self.gas_level: Optional[int] = None
        self.gas_warning: Optional[bool] = None
        self.gas_severity: Optional[str] = None

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            safety_stop = False
            motor_enabled: Optional[bool] = None
            motor: Optional[Dict[str, Any]] = None
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
                lmotor = data.get("lmotor")
                rmotor = data.get("rmotor")
                if lmotor is not None or rmotor is not None:
                    motor = {
                        "left": lmotor,
                        "right": rmotor,
                        "ts": self.last_esp.get("data_ts"),
                    }
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
                "motor": motor,
                "safety_stop": safety_stop,
                "safety_alert": self.last_alert,
                "sensor": self.last_esp.get("data") if self.last_esp else None,
                "sensor_ts": self.last_esp.get("data_ts") if self.last_esp else None,
                "sensor_buffer": self.last_esp.get("buffer") if self.last_esp else None,
                "vision_last_detection": self.last_detection,
                "detection_history": list(self.detection_history),
                "last_capture": self.last_capture,
                "last_scan_summary": self.last_scan_summary,
                "gas_level": self.gas_level,
                "gas_warning": self.gas_warning,
                "gas_severity": self.gas_severity,
                "last_llm_response": self.last_llm_response,
                "last_llm_ts": int(self.last_llm_ts) if self.last_llm_ts else None,
                "last_tts_text": self.last_tts_text,
                "last_tts_status": self.last_tts_status,
                "last_tts_ts": int(self.last_tts_ts) if self.last_tts_ts else None,
                "health": self.last_health,
                "remote_event": self.last_remote_event,
            }


class RemoteSupervisor:
    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path
        self.config = load_config(config_path)
        log_dir = Path(self.config.get("logs", {}).get("directory", "logs"))
        if not log_dir.is_absolute():
            log_dir = Path.cwd() / log_dir
        self.log_dir = log_dir
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
        self._pub_down = make_publisher(self.config, channel="downstream")
        self._sub_up = make_subscriber(self.config, channel="upstream")
        self._sub_down = make_subscriber(self.config, channel="downstream")

        # Subscribe to key telemetry topics
        for topic in [
            TOPIC_ESP,
            TOPIC_VISN,
            TOPIC_VISN_FRAME,
            TOPIC_VISN_CAPTURED,
            TOPIC_HEALTH,
            TOPIC_LLM_RESP,
            TOPIC_TTS,
            TOPIC_REMOTE_EVENT,
        ]:
            self._sub_up.setsockopt(zmq.SUBSCRIBE, topic)

        for topic in [
            TOPIC_DISPLAY_STATE,
            TOPIC_DISPLAY_TEXT,
            TOPIC_CMD_VISION_MODE,
            TOPIC_CMD_PAUSE_VISION,
            TOPIC_VISN,
            TOPIC_VISN_FRAME,
            TOPIC_VISN_CAPTURED,
            TOPIC_TTS,
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
        self._log_services = {
            "remote_interface": ["remote.interface.log", "remote-interface.log"],
            "orchestrator": ["orchestrator.log"],
            "uart": ["uart.motor_bridge.log", "uart.bridge.log", "uart.log"],
            "vision": ["vision.log", "vision.runner.log"],
            "llm_tts": [
                "llm.azure_openai.log",
                "llm.gemini.log",
                "llm.local.log",
                "tts.azure.log",
                "tts.piper.log",
                "tts.log",
            ],
        }

    def _tail_lines(self, path: Path, max_lines: int) -> List[str]:
        if max_lines <= 0:
            return []
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as handle:
                buffer = deque(handle, maxlen=max_lines)
            return [line.rstrip("\n") for line in buffer]
        except FileNotFoundError:
            return []
        except OSError:
            return []

    def _fetch_logs(self, service: str, max_lines: int) -> Optional[Dict[str, Any]]:
        filenames = self._log_services.get(service)
        if not filenames:
            return None
        files = []
        for name in filenames:
            path = self.log_dir / name
            if path.exists():
                files.append(path)
        if not files:
            return {
                "service": service,
                "lines": [],
                "sources": [str(self.log_dir / name) for name in filenames],
                "ts": int(time.time()),
                "error": "log_files_missing",
            }
        merged: List[str] = []
        sources: List[str] = []
        for path in files:
            sources.append(str(path))
            lines = self._tail_lines(path, max_lines)
            if len(files) > 1:
                merged.extend([f"[{path.name}] {line}" for line in lines])
            else:
                merged.extend(lines)
        return {
            "service": service,
            "lines": merged[-max_lines:],
            "sources": sources,
            "ts": int(time.time()),
        }

    def _load_raw_config(self) -> Dict[str, Any]:
        return yaml.safe_load(self.config_path.read_text()) or {}

    def _save_raw_config(self, data: Dict[str, Any]) -> None:
        self.config_path.write_text(yaml.safe_dump(data, sort_keys=False))

    @staticmethod
    def _stringify_controls(controls: Dict[str, Any]) -> Dict[str, str]:
        output: Dict[str, str] = {}
        for key, value in controls.items():
            if isinstance(value, (list, tuple)):
                output[key] = ",".join(str(item) for item in value)
            else:
                output[key] = str(value)
        return output

    @staticmethod
    def _parse_control_value(value: Any) -> Any:
        if isinstance(value, (bool, int, float, list, tuple)):
            return value
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        lowered = text.lower()
        if lowered in {"true", "false"}:
            return lowered == "true"
        if "," in text or (text.startswith("[") and text.endswith("]")):
            stripped = text.strip("[]")
            items = [item.strip() for item in stripped.split(",") if item.strip()]
            parsed_items = []
            for item in items:
                try:
                    parsed_items.append(float(item))
                except ValueError:
                    parsed_items.append(item)
            return parsed_items
        try:
            if "." in text:
                return float(text)
            return int(text)
        except ValueError:
            return text

    def _camera_settings_payload(self, vision_cfg: Dict[str, Any]) -> Dict[str, Any]:
        controls = vision_cfg.get("picam2_controls") or {}
        if not isinstance(controls, dict):
            controls = {}
        awb_enabled = controls.get("AwbEnable")
        gains = controls.get("ColourGains")
        awb_locked = False
        if awb_enabled is False and isinstance(gains, (list, tuple)):
            try:
                awb_locked = bool(float(gains[0]) != 0.0 or float(gains[1]) != 0.0)
            except (TypeError, ValueError, IndexError):
                awb_locked = False
        return {
            "stream_gamma": vision_cfg.get("stream_gamma", 1.0),
            "picam2_width": vision_cfg.get("picam2_width"),
            "picam2_height": vision_cfg.get("picam2_height"),
            "picam2_fps": vision_cfg.get("picam2_fps"),
            "picam2_controls": self._stringify_controls(controls),
            "awb_locked": awb_locked,
        }

    def _update_camera_settings(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        raw = self._load_raw_config()
        vision_cfg = raw.get("vision") if isinstance(raw.get("vision"), dict) else {}
        if not isinstance(vision_cfg, dict):
            vision_cfg = {}
        requires_restart = False
        updated = False
        update_payload: Dict[str, Any] = {}
        awb_action: Optional[str] = None

        if "picam2_controls" in payload and isinstance(payload.get("picam2_controls"), dict):
            controls = vision_cfg.get("picam2_controls") or {}
            if not isinstance(controls, dict):
                controls = {}
            incoming_controls = payload.get("picam2_controls") or {}
            parsed_controls: Dict[str, Any] = {}
            for key, value in incoming_controls.items():
                parsed_value = self._parse_control_value(value)
                if parsed_value is None:
                    continue
                parsed_controls[str(key)] = parsed_value
            if parsed_controls:
                controls.update(parsed_controls)
                vision_cfg["picam2_controls"] = controls
                update_payload["picam2_controls"] = controls
                updated = True

        if "stream_gamma" in payload:
            try:
                gamma_value = float(payload.get("stream_gamma"))
                vision_cfg["stream_gamma"] = gamma_value
                update_payload["stream_gamma"] = gamma_value
                updated = True
            except (TypeError, ValueError):
                pass

        if "picam2_fps" in payload:
            try:
                fps_value = int(payload.get("picam2_fps"))
                vision_cfg["picam2_fps"] = fps_value
                update_payload["picam2_fps"] = fps_value
                updated = True
            except (TypeError, ValueError):
                pass

        if "picam2_width" in payload:
            try:
                width_value = int(payload.get("picam2_width"))
                if vision_cfg.get("picam2_width") != width_value:
                    requires_restart = True
                vision_cfg["picam2_width"] = width_value
                updated = True
            except (TypeError, ValueError):
                pass

        if "picam2_height" in payload:
            try:
                height_value = int(payload.get("picam2_height"))
                if vision_cfg.get("picam2_height") != height_value:
                    requires_restart = True
                vision_cfg["picam2_height"] = height_value
                updated = True
            except (TypeError, ValueError):
                pass

        if "awb_lock" in payload:
            awb_action = "lock_awb" if payload.get("awb_lock") else "unlock_awb"
            update_payload["action"] = awb_action
            if awb_action == "unlock_awb":
                controls = vision_cfg.get("picam2_controls") or {}
                if not isinstance(controls, dict):
                    controls = {}
                controls["AwbEnable"] = True
                controls["ColourGains"] = [0.0, 0.0]
                vision_cfg["picam2_controls"] = controls
                updated = True

        if updated:
            raw["vision"] = vision_cfg
            self._save_raw_config(raw)
            self.config = load_config(self.config_path)
            if update_payload:
                publish_json(self._pub_down, TOPIC_CMD_CAMERA_SETTINGS, update_payload)
        elif update_payload:
            publish_json(self._pub_down, TOPIC_CMD_CAMERA_SETTINGS, update_payload)

        return {
            "ok": True,
            "requires_restart": requires_restart,
            "settings": self._camera_settings_payload(vision_cfg),
        }

    def _restart_service(self, service: str) -> Dict[str, Any]:
        if service != "vision":
            return {"ok": False, "service": service, "error": "unsupported_service"}
        try:
            result = subprocess.run(
                ["sudo", "systemctl", "restart", service],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
        except Exception as exc:
            return {"ok": False, "service": service, "error": str(exc)}
        if result.returncode != 0:
            return {
                "ok": False,
                "service": service,
                "error": (result.stderr or result.stdout or "restart_failed").strip(),
            }
        return {"ok": True, "service": service}

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
                    data = payload.get("data") or {}
                    if "mq2" in data:
                        try:
                            self.telemetry.gas_level = int(data.get("mq2"))
                        except (TypeError, ValueError):
                            self.telemetry.gas_level = None
                        if self.telemetry.gas_level is not None and self.telemetry.gas_severity is None:
                            self.telemetry.gas_severity = "unknown"
                elif topic == TOPIC_HEALTH:
                    self.telemetry.last_health = payload
                elif topic == TOPIC_REMOTE_EVENT:
                    self.telemetry.last_remote_event = payload
                    event = payload.get("event")
                    if event == "scan_complete":
                        self.telemetry.last_scan_summary = payload.get("summary")
                    elif event == "awb_locked":
                        gains = payload.get("gains") or []
                        if isinstance(gains, list) and len(gains) >= 2:
                            raw = self._load_raw_config()
                            vision_cfg = raw.get("vision") if isinstance(raw.get("vision"), dict) else {}
                            if not isinstance(vision_cfg, dict):
                                vision_cfg = {}
                            controls = vision_cfg.get("picam2_controls") or {}
                            if not isinstance(controls, dict):
                                controls = {}
                            controls["AwbEnable"] = False
                            controls["ColourGains"] = [float(gains[0]), float(gains[1])]
                            vision_cfg["picam2_controls"] = controls
                            raw["vision"] = vision_cfg
                            self._save_raw_config(raw)
                            self.config = load_config(self.config_path)
                    elif event == "awb_unlocked":
                        raw = self._load_raw_config()
                        vision_cfg = raw.get("vision") if isinstance(raw.get("vision"), dict) else {}
                        if not isinstance(vision_cfg, dict):
                            vision_cfg = {}
                        controls = vision_cfg.get("picam2_controls") or {}
                        if not isinstance(controls, dict):
                            controls = {}
                        controls["AwbEnable"] = True
                        controls["ColourGains"] = [0.0, 0.0]
                        vision_cfg["picam2_controls"] = controls
                        raw["vision"] = vision_cfg
                        self._save_raw_config(raw)
                        self.config = load_config(self.config_path)
                    elif event == "gas_warning":
                        self.telemetry.gas_warning = True
                        self.telemetry.gas_severity = "warning"
                    elif event == "gas_danger":
                        self.telemetry.gas_warning = True
                        self.telemetry.gas_severity = "danger"
                    elif event == "gas_clear":
                        self.telemetry.gas_warning = False
                        self.telemetry.gas_severity = "clear"
                elif topic == TOPIC_VISN_CAPTURED:
                    self.telemetry.last_capture = payload
                elif topic == TOPIC_LLM_RESP:
                    body = payload.get("json") or {}
                    speak = body.get("speak") or payload.get("text") or payload.get("raw")
                    if speak:
                        self.telemetry.last_llm_response = str(speak)[:240]
                        self.telemetry.last_llm_ts = time.time()
                elif topic == TOPIC_TTS:
                    if payload.get("text"):
                        self.telemetry.last_tts_text = str(payload.get("text"))[:240]
                        self.telemetry.last_tts_status = "queued"
                        self.telemetry.last_tts_ts = time.time()
                    if payload.get("started"):
                        self.telemetry.last_tts_status = "started"
                        self.telemetry.last_tts_ts = time.time()
                    if payload.get("done") or payload.get("final") or payload.get("completed"):
                        self.telemetry.last_tts_status = "done"
                        self.telemetry.last_tts_ts = time.time()

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
                parsed = urlparse(self.path)
                if parsed.path == "/stream/mjpeg":
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
                if parsed.path in {"/status", "/telemetry"}:
                    supervisor._touch_session()
                    supervisor._publish_session_state()
                    self._send_json(200, supervisor.telemetry.snapshot())
                    return
                if parsed.path == "/health":
                    self._send_json(200, {"ok": True, "timestamp": int(time.time())})
                    return
                if parsed.path == "/settings/camera":
                    raw = supervisor._load_raw_config()
                    vision_cfg = raw.get("vision") if isinstance(raw.get("vision"), dict) else {}
                    if not isinstance(vision_cfg, dict):
                        vision_cfg = {}
                    self._send_json(200, supervisor._camera_settings_payload(vision_cfg))
                    return
                if parsed.path == "/logs":
                    qs = parse_qs(parsed.query or "")
                    service = (qs.get("service") or qs.get("name") or [None])[0]
                    try:
                        requested = int((qs.get("lines") or ["100"])[0])
                    except ValueError:
                        requested = 100
                    max_lines = max(10, min(500, requested))
                    if not service:
                        self._send_json(200, {"services": list(supervisor._log_services.keys())})
                        return
                    payload = supervisor._fetch_logs(str(service), max_lines)
                    if payload is None:
                        self._send_json(404, {"error": "unknown_service"})
                        return
                    self._send_json(200, payload)
                    return
                log = getattr(supervisor, "logger", None)
                if log:
                    log.warning("/intent rejected reason=bad_path path=%s ts=%s", self.path, time.time())
                self._send_json(404, {"error": "not_found"})

            def do_POST(self) -> None:
                if not self._allowed():
                    log = getattr(supervisor, "logger", None)
                    if log:
                        log.warning("/intent rejected reason=forbidden ip=%s ts=%s", self.client_address[0], time.time())
                    self._send_json(403, {"error": "forbidden"})
                    return

                if self.path == "/intent":
                    payload = self._read_json()
                    intent = str(payload.get("intent", "")).strip()
                    if not intent:
                        log = getattr(supervisor, "logger", None)
                        if log:
                            log.warning("/intent rejected reason=missing_intent ts=%s payload=%s", time.time(), payload)
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
                    log = getattr(supervisor, "logger", None)
                    if log:
                        log.info("/intent received ts=%s payload=%s", time.time(), intent_payload)
                        log.info("ipc publish topic=%s ts=%s", TOPIC_REMOTE_INTENT, time.time())
                    publish_json(supervisor._pub, TOPIC_REMOTE_INTENT, intent_payload)
                    self._send_json(202, {"accepted": True, "intent": intent})
                    return

                if self.path == "/settings/camera":
                    payload = self._read_json()
                    result = supervisor._update_camera_settings(payload)
                    self._send_json(200, result)
                    return

                if self.path == "/service/restart":
                    payload = self._read_json()
                    service = str(payload.get("service", "")).strip().lower()
                    result = supervisor._restart_service(service)
                    self._send_json(200, result)
                    return

                log = getattr(supervisor, "logger", None)
                if log:
                    log.warning("/intent rejected reason=bad_path path=%s ts=%s", self.path, time.time())
                self._send_json(404, {"error": "not_found"})

            def log_message(self, format: str, *args) -> None:
                supervisor.logger.info("remote.http %s - %s", self.client_address[0], format % args)

        return Handler


def main() -> None:
    supervisor = RemoteSupervisor(Path("config/system.yaml"))
    supervisor.serve()


if __name__ == "__main__":
    main()
