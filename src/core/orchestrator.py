"""Phase-driven orchestrator: single source of truth state machine.

LED COLOR SCHEME (granular feedback):
    idle                - Dim cyan breathing (waiting for wakeword)
    wakeword_detected   - Bright GREEN flash (acknowledged!)
    listening           - Bright BLUE sweep (capturing audio)
    transcribing        - PURPLE pulse (STT processing)
    thinking            - PINK pulse (LLM processing)
    tts_processing      - ORANGE pulse (generating speech)
    speaking            - Dark GREEN chase (playing audio)
    error               - RED blink (system error)
"""
from __future__ import annotations

import json
import random
import time
from enum import Enum, auto
from pathlib import Path
from typing import Any, Dict, Optional

import zmq

from src.core.config_loader import load_config
from src.core.ipc import (
    TOPIC_CMD_LISTEN_START,
    TOPIC_CMD_LISTEN_STOP,
    TOPIC_CMD_PAUSE_VISION,
    TOPIC_CMD_VISN_CAPTURE,
    TOPIC_CMD_VISION_MODE,
    TOPIC_DISPLAY_STATE,
    TOPIC_DISPLAY_TEXT,
    TOPIC_ESP,
    TOPIC_HEALTH,
    TOPIC_LLM_REQ,
    TOPIC_LLM_RESP,
    TOPIC_NAV,
    TOPIC_REMOTE_EVENT,
    TOPIC_REMOTE_INTENT,
    TOPIC_REMOTE_SESSION,
    TOPIC_STT,
    TOPIC_TTS,
    TOPIC_VISN,
    TOPIC_VISN_CAPTURED,
    TOPIC_VISN_FRAME,
    TOPIC_WW_DETECTED,
    make_publisher,
    make_subscriber,
    publish_json,
)
from src.core.logging_setup import get_logger
from src.core.world_context import WorldContextAggregator

logger = get_logger("orchestrator", Path("logs"))


class Phase(Enum):
    IDLE = auto()
    LISTENING = auto()
    THINKING = auto()
    SPEAKING = auto()
    ERROR = auto()


class VisionMode(Enum):
    OFF = "off"
    ON_NO_STREAM = "on_no_stream"
    ON_WITH_STREAM = "on_with_stream"


class Orchestrator:
    TRANSITIONS = {
        (Phase.IDLE, "wakeword"): Phase.LISTENING,
        (Phase.IDLE, "auto_trigger"): Phase.LISTENING,
        (Phase.IDLE, "manual_trigger"): Phase.LISTENING,
        (Phase.LISTENING, "stt_valid"): Phase.THINKING,
        (Phase.LISTENING, "stt_invalid"): Phase.IDLE,
        (Phase.LISTENING, "stt_timeout"): Phase.IDLE,
        (Phase.THINKING, "llm_with_speech"): Phase.SPEAKING,
        (Phase.THINKING, "llm_no_speech"): Phase.IDLE,
        (Phase.SPEAKING, "tts_done"): Phase.IDLE,
        (Phase.IDLE, "health_error"): Phase.ERROR,
        (Phase.LISTENING, "health_error"): Phase.ERROR,
        (Phase.THINKING, "health_error"): Phase.ERROR,
        (Phase.SPEAKING, "health_error"): Phase.ERROR,
        (Phase.ERROR, "health_ok"): Phase.IDLE,
        (Phase.ERROR, "error_timeout"): Phase.IDLE,
    }

    def __init__(self) -> None:
        self.config = load_config(Path("config/system.yaml"))
        self.cmd_pub = make_publisher(self.config, channel="downstream", bind=True)
        self.events_sub = make_subscriber(self.config, channel="upstream", bind=True)
        self._world_context = WorldContextAggregator(self.config)
        self._world_context.start()
        self._phase = Phase.IDLE
        self._phase_entered_ts = time.time()
        self._last_interaction_ts = time.time()
        self._last_transcript = ""
        self._last_vision: Optional[Dict[str, Any]] = None
        self._last_nav_direction = "stopped"
        self._vision_capture_pending: Optional[str] = None
        self._vision_capture_requested_ts: Optional[float] = None
        self._esp_obstacle = False
        self._esp_min_distance = -1
        self._obstacle_latched = False

        self._remote_session_active = False
        self._remote_last_seen = 0.0
        
        orch_cfg = self.config.get("orchestrator", {}) or {}
        self.auto_trigger_enabled = bool(orch_cfg.get("auto_trigger_enabled", True))
        self.auto_trigger_interval = float(orch_cfg.get("auto_trigger_interval", 60.0))
        
        stt_cfg = self.config.get("stt", {}) or {}
        self.stt_timeout_s = float(stt_cfg.get("timeout_seconds", 30.0))
        self.stt_min_confidence = float(stt_cfg.get("min_confidence", 0.3))
        self.error_recovery_s = 2.0

        remote_cfg = self.config.get("remote_interface", {}) or {}
        self.remote_session_timeout_s = float(remote_cfg.get("session_timeout_s", 15.0))

        vision_cfg = self.config.get("vision", {}) or {}
        default_mode = str(vision_cfg.get("default_mode", "off")).lower()
        self.vision_mode = self._coerce_vision_mode(default_mode)

    def _publish_led_state(self, state: str) -> None:
        publish_json(self.cmd_pub, TOPIC_DISPLAY_STATE, {
            "state": state,
            "phase": self._phase.name,
            "timestamp": int(time.time()),
            "source": "orchestrator",
        })
        logger.debug("LED: %s", state)

    def _publish_display_text(self, text: str) -> None:
        publish_json(self.cmd_pub, TOPIC_DISPLAY_TEXT, {
            "text": text,
            "timestamp": int(time.time()),
            "source": "orchestrator",
        })

    @property
    def phase(self) -> Phase:
        return self._phase

    def _transition(self, event_type: str) -> bool:
        key = (self._phase, event_type)
        next_phase = self.TRANSITIONS.get(key)
        if next_phase is None:
            logger.debug("IGNORED: event '%s' illegal in phase %s", event_type, self._phase.name)
            return False
        if next_phase == self._phase:
            return False
        old_phase = self._phase
        self._phase = next_phase
        self._phase_entered_ts = time.time()
        logger.info("PHASE: %s -> %s (event: %s)", old_phase.name, next_phase.name, event_type)
        return True

    @staticmethod
    def _normalize_direction(direction: Optional[str]) -> str:
        allowed = {"forward", "backward", "left", "right", "stop", "scan"}
        if not direction:
            return "stop"
        value = str(direction).strip().lower()
        return value if value in allowed else "stop"

    def _enter_listening(self, from_wakeword: bool = False) -> None:
        self._last_interaction_ts = time.time()
        if from_wakeword:
            self._publish_led_state("wakeword_detected")
            self._publish_display_text("Wakeword detected")
        else:
            self._publish_led_state("listening")
            self._publish_display_text("Listening...")
        if self.vision_mode != VisionMode.OFF:
            publish_json(self.cmd_pub, TOPIC_CMD_PAUSE_VISION, {"pause": True, "source": "orchestrator"})
        publish_json(self.cmd_pub, TOPIC_CMD_LISTEN_START, {"start": True, "source": "orchestrator"})

    def _exit_listening(self, reason: str) -> None:
        publish_json(self.cmd_pub, TOPIC_CMD_LISTEN_STOP, {"stop": True, "reason": reason, "source": "orchestrator"})
        if self.vision_mode != VisionMode.OFF:
            publish_json(self.cmd_pub, TOPIC_CMD_PAUSE_VISION, {"pause": False, "source": "orchestrator"})

    def _enter_thinking(self, text: str, vision: Optional[Dict[str, Any]] = None) -> None:
        self._publish_led_state("thinking")
        self._publish_display_text(f"Heard: {text[:120]}")
        payload: Dict[str, Any] = {"text": text}
        if vision:
            payload["vision"] = vision
        payload["direction"] = self._last_nav_direction
        payload["world_context"] = self._world_context.get_snapshot()
        payload["context_note"] = "system_observation_only_last_known_state"
        payload["source"] = "orchestrator"
        publish_json(self.cmd_pub, TOPIC_LLM_REQ, payload)
        logger.info("LLM request text: %s", text[:120])

    def _enter_speaking(self, text: str, direction: Optional[str] = None) -> None:
        self._publish_led_state("tts_processing")
        self._publish_display_text(f"Saying: {text[:120]}")
        normalized = self._normalize_direction(direction)
        if normalized != "stop":
            if self._esp_obstacle and normalized == "forward":
                logger.warning("Blocked forward command due to obstacle")
                publish_json(
                    self.cmd_pub,
                    TOPIC_NAV,
                    {"direction": "stop", "reason": "obstacle", "source": "orchestrator"},
                )
                self._last_nav_direction = "stop"
            else:
                self._last_nav_direction = normalized
                publish_json(self.cmd_pub, TOPIC_NAV, {"direction": normalized, "source": "orchestrator"})
        publish_json(self.cmd_pub, TOPIC_TTS, {"text": text, "source": "orchestrator"})

    def _enter_idle(self) -> None:
        self._publish_led_state("idle")
        self._publish_display_text("Idle")
        # Vision lifecycle is managed explicitly; do not auto-resume here.

    def _notify_stt_failure(self, reason: str) -> None:
        feedback_messages = {
            "timeout": [
                "I didn't catch anything. Try again?",
                "I lost you there. Say it once more.",
                "I waited but heard nothing. Please try again.",
            ],
            "empty": [
                "I couldn't make that out. Please speak clearly.",
                "That came through empty. Try a bit louder.",
                "I missed that. Please repeat.",
            ],
            "low_confidence": [
                "I'm not sure I got that. Please repeat.",
                "That was unclear. Say it again for me.",
                "I didn't get enough confidence. Try again.",
            ],
        }
        choices = feedback_messages.get(reason)
        if choices:
            message = random.choice(choices)
        else:
            message = "Something went wrong. Please try again."
        publish_json(self.cmd_pub, TOPIC_TTS, {"text": message, "notification": True, "source": "orchestrator"})
        logger.info("STT failure feedback: %s", reason)

    def on_wakeword(self, payload: Dict[str, Any]) -> None:
        if self._phase != Phase.IDLE:
            logger.debug("Wakeword ignored: not in IDLE (current: %s)", self._phase.name)
            return
        logger.info("Wakeword detected: %s", payload.get("keyword", "unknown"))
        if self._transition("wakeword"):
            self._enter_listening(from_wakeword=True)

    def on_manual_trigger(self, payload: Dict[str, Any]) -> None:
        if self._phase != Phase.IDLE:
            logger.debug("Manual trigger ignored: not in IDLE (current: %s)", self._phase.name)
            return
        logger.info("Manual trigger received")
        if self._transition("manual_trigger"):
            self._enter_listening(from_wakeword=False)

    def on_stt(self, payload: Dict[str, Any]) -> None:
        if self._phase != Phase.LISTENING:
            logger.debug("STT result ignored: not in LISTENING (current: %s)", self._phase.name)
            return
        text = str(payload.get("text", "")).strip()
        confidence = float(payload.get("confidence", 0.0) or 0.0)
        logger.info("STT payload: text='%s' conf=%.2f", text[:120], confidence)
        
        if not text:
            logger.warning("Empty transcription received")
            self._exit_listening("empty")
            self._notify_stt_failure("empty")
            self._transition("stt_invalid")
            self._enter_idle()
            return
        
        if confidence < self.stt_min_confidence:
            logger.info("Low confidence (%.3f < %.3f): '%s'", confidence, self.stt_min_confidence, text[:50])
            self._exit_listening("low_confidence")
            self._notify_stt_failure("low_confidence")
            self._transition("stt_invalid")
            self._enter_idle()
            return
        
        logger.info("STT valid (%d chars, conf=%.2f)", len(text), confidence)
        self._last_transcript = text
        self._exit_listening("success")
        
        if self._transition("stt_valid"):
            if self._should_request_vision(text):
                self._request_vision_capture(text)
            else:
                self._enter_thinking(text)

    def _should_request_vision(self, text: str) -> bool:
        keywords = ["what do you see", "what are you seeing", "describe", "look at"]
        return any(k in text.lower() for k in keywords)

    def _request_vision_capture(self, text: str) -> None:
        if self.vision_mode == VisionMode.OFF:
            self._set_vision_mode(VisionMode.ON_NO_STREAM, source="internal")
        request_id = f"visn-{int(time.time() * 1000)}"
        self._vision_capture_pending = request_id
        self._vision_capture_requested_ts = time.time()
        self._last_transcript = text
        publish_json(self.cmd_pub, TOPIC_CMD_VISN_CAPTURE, {"request_id": request_id, "source": "orchestrator"})

    def _request_frame_capture(self, source: str) -> str:
        if self.vision_mode == VisionMode.OFF:
            self._set_vision_mode(VisionMode.ON_NO_STREAM, source=source)
        request_id = f"capture-{int(time.time() * 1000)}"
        publish_json(
            self.cmd_pub,
            TOPIC_CMD_VISN_CAPTURE,
            {"request_id": request_id, "source": source, "save": True, "purpose": "capture_frame"},
        )
        return request_id

    def on_vision(self, payload: Dict[str, Any]) -> None:
        self._last_vision = payload
        if self._vision_capture_pending:
            request_id = payload.get("request_id")
            if request_id == self._vision_capture_pending:
                self._vision_capture_pending = None
                self._vision_capture_requested_ts = None
                if self._phase == Phase.THINKING:
                    self._enter_thinking(self._last_transcript, vision=payload)

    def on_llm(self, payload: Dict[str, Any]) -> None:
        if self._phase != Phase.THINKING:
            logger.debug("LLM response ignored: not in THINKING (current: %s)", self._phase.name)
            return
        logger.info("LLM response received")
        body = payload.get("json") or {}
        speak = body.get("speak") or payload.get("text", "")
        direction = self._normalize_direction(body.get("direction"))
        if self._esp_obstacle and direction == "forward":
            direction = "stop"
            if speak:
                speak = f"{speak} Obstacle ahead, stopping."
        logger.info("LLM response speak: %s", (speak or "")[:120])
        
        if speak:
            if self._transition("llm_with_speech"):
                self._enter_speaking(speak, direction)
        else:
            if direction and direction != "stop":
                publish_json(self.cmd_pub, TOPIC_NAV, {"direction": direction, "source": "orchestrator"})
                self._last_nav_direction = direction
            self._transition("llm_no_speech")
            self._enter_idle()

    def on_tts(self, payload: Dict[str, Any]) -> None:
        if payload.get("started"):
            self._publish_led_state("speaking")
            return
        done = payload.get("done") or payload.get("final") or payload.get("completed")
        if payload.get("notification"):
            return
        if not done:
            return
        if self._phase != Phase.SPEAKING:
            logger.debug("TTS done ignored: not in SPEAKING (current: %s)", self._phase.name)
            return
        logger.info("TTS completed")
        if self._transition("tts_done"):
            self._enter_idle()

    def on_esp(self, payload: Dict[str, Any]) -> None:
        data = payload.get("data")
        if data:
            self._esp_obstacle = bool(data.get("obstacle", False)) or (data.get("is_safe") is False)
            self._esp_min_distance = int(data.get("min_distance", -1))
            if self._esp_obstacle and not self._obstacle_latched:
                self._obstacle_latched = True
                logger.warning("Obstacle detected by ESP32; forcing stop")
                publish_json(self.cmd_pub, TOPIC_NAV, {"direction": "stop", "reason": "obstacle"})
                self._last_nav_direction = "stop"
                self._publish_display_text("Obstacle detected - stopping")
            elif not self._esp_obstacle and self._obstacle_latched:
                self._obstacle_latched = False
                logger.info("Obstacle cleared by ESP32")
        alert = payload.get("alert")
        if alert == "COLLISION":
            logger.critical("ESP32 collision alert!")
            publish_json(self.cmd_pub, TOPIC_NAV, {"direction": "stop", "reason": "collision"})
            self._last_nav_direction = "stop"

    def on_health(self, payload: Dict[str, Any]) -> None:
        ok = bool(payload.get("ok", True))
        if not ok and self._phase != Phase.ERROR:
            logger.error("Health error: %s", payload)
            self._publish_led_state("error")
            self._transition("health_error")
        elif ok and self._phase == Phase.ERROR:
            logger.info("Health restored")
            self._transition("health_ok")
            self._enter_idle()

    def _check_timeouts(self) -> None:
        now = time.time()
        elapsed = now - self._phase_entered_ts
        if self._phase == Phase.LISTENING and elapsed > self.stt_timeout_s:
            logger.warning("STT timeout (%.1fs)", self.stt_timeout_s)
            self._exit_listening("timeout")
            self._notify_stt_failure("timeout")
            self._transition("stt_timeout")
            self._enter_idle()
        elif self._phase == Phase.ERROR and elapsed > self.error_recovery_s:
            logger.info("Error auto-recovery after %.1fs", self.error_recovery_s)
            self._transition("error_timeout")
            self._publish_display_text("Recovered. Ready.")
            self._enter_idle()

        if self._vision_capture_pending and self._vision_capture_requested_ts:
            if (now - self._vision_capture_requested_ts) > 3.0:
                logger.warning("Vision capture timeout; proceeding without vision")
                self._vision_capture_pending = None
                self._vision_capture_requested_ts = None
                if self._phase == Phase.THINKING:
                    self._enter_thinking(self._last_transcript)

        if self._remote_session_active and self._remote_last_seen:
            if (now - self._remote_last_seen) > self.remote_session_timeout_s:
                self._remote_session_active = False
                publish_json(self.cmd_pub, TOPIC_REMOTE_SESSION, {
                    "active": False,
                    "last_seen": int(self._remote_last_seen) if self._remote_last_seen else None,
                    "source": "orchestrator",
                })

    def _check_auto_trigger(self) -> None:
        if not self.auto_trigger_enabled:
            return
        if self._phase != Phase.IDLE:
            return
        idle_time = time.time() - self._last_interaction_ts
        if idle_time > self.auto_trigger_interval:
            logger.info("Auto-trigger after %.1fs idle", idle_time)
            if self._transition("auto_trigger"):
                self._enter_listening(from_wakeword=False)

    def run(self) -> None:
        logger.info(
            "Orchestrator running (Phase FSM) auto_trigger=%s interval=%.1fs stt_timeout=%.1fs",
            self.auto_trigger_enabled,
            self.auto_trigger_interval,
            self.stt_timeout_s,
        )
        logger.info("Initial phase: %s", self._phase.name)
        self._publish_led_state("idle")
        self._set_vision_mode(self.vision_mode, source="internal")

        poller = zmq.Poller()
        poller.register(self.events_sub, zmq.POLLIN)

        while True:
            socks = dict(poller.poll(timeout=100))
            if self.events_sub in socks:
                try:
                    topic, data = self.events_sub.recv_multipart()
                    if topic == TOPIC_VISN_FRAME:
                        self.cmd_pub.send_multipart([topic, data])
                        continue
                    payload = json.loads(data)
                except Exception as exc:
                    logger.error("Recv/parse error: %s", exc)
                    continue

                if topic == TOPIC_WW_DETECTED:
                    self.on_wakeword(payload)
                elif topic == TOPIC_CMD_LISTEN_START:
                    self.on_manual_trigger(payload)
                elif topic == TOPIC_STT:
                    self.on_stt(payload)
                elif topic == TOPIC_LLM_RESP:
                    self.on_llm(payload)
                elif topic == TOPIC_TTS:
                    self.on_tts(payload)
                elif topic == TOPIC_VISN:
                    self.on_vision(payload)
                    publish_json(self.cmd_pub, TOPIC_VISN, payload)
                elif topic == TOPIC_VISN_CAPTURED:
                    publish_json(self.cmd_pub, TOPIC_VISN_CAPTURED, payload)
                elif topic == TOPIC_ESP:
                    self.on_esp(payload)
                elif topic == TOPIC_HEALTH:
                    self.on_health(payload)
                elif topic == TOPIC_REMOTE_SESSION:
                    self.on_remote_session(payload)
                elif topic == TOPIC_REMOTE_INTENT:
                    self.on_remote_intent(payload)

            self._check_timeouts()
            self._check_auto_trigger()

    def _coerce_vision_mode(self, raw: str) -> VisionMode:
        raw = (raw or "").lower().strip()
        if raw in {"off", "disabled", "false", "0"}:
            return VisionMode.OFF
        if raw in {"on_with_stream", "with_stream", "stream"}:
            return VisionMode.ON_WITH_STREAM
        return VisionMode.ON_NO_STREAM

    def _set_vision_mode(self, mode: VisionMode, *, source: str) -> None:
        if mode == self.vision_mode:
            return
        self.vision_mode = mode
        publish_json(
            self.cmd_pub,
            TOPIC_CMD_VISION_MODE,
            {"mode": mode.value, "timestamp": int(time.time()), "source": source},
        )

    def _publish_remote_event(self, event: str, payload: Dict[str, Any]) -> None:
        message = {"event": event, "timestamp": int(time.time()), **payload}
        publish_json(self.cmd_pub, TOPIC_REMOTE_EVENT, message)

    def on_remote_session(self, payload: Dict[str, Any]) -> None:
        active = bool(payload.get("active", False))
        self._remote_session_active = active
        if active:
            self._remote_last_seen = time.time()

    def on_remote_intent(self, payload: Dict[str, Any]) -> None:
        source = payload.get("source", "unknown")
        if source != "remote_app":
            self._publish_remote_event("rejected", {"reason": "invalid_source", "payload": payload})
            return

        if not self._remote_session_active:
            self._publish_remote_event("rejected", {"reason": "no_active_session", "payload": payload})
            return

        intent = str(payload.get("intent", "")).strip().lower()
        if not intent:
            self._publish_remote_event("rejected", {"reason": "missing_intent", "payload": payload})
            return

        if intent in {"enable_vision", "enable_perception"}:
            self._set_vision_mode(VisionMode.ON_NO_STREAM, source="remote_app")
            self._publish_remote_event("accepted", {"intent": intent})
            return
        if intent in {"disable_vision", "disable_perception"}:
            self._set_vision_mode(VisionMode.OFF, source="remote_app")
            self._publish_remote_event("accepted", {"intent": intent})
            return
        if intent in {"enable_stream"}:
            self._set_vision_mode(VisionMode.ON_WITH_STREAM, source="remote_app")
            self._publish_remote_event("accepted", {"intent": intent})
            return
        if intent in {"disable_stream"}:
            self._set_vision_mode(VisionMode.ON_NO_STREAM, source="remote_app")
            self._publish_remote_event("accepted", {"intent": intent})
            return
        if intent in {"capture_frame"}:
            request_id = self._request_frame_capture("remote_app")
            self._publish_remote_event("accepted", {"intent": intent, "request_id": request_id})
            return
        if intent in {"scan", "start_scan"}:
            publish_json(self.cmd_pub, TOPIC_NAV, {"direction": "scan", "source": "remote_app"})
            self._publish_remote_event("accepted", {"intent": intent})
            return
        if intent in {"stop", "stop_motion"}:
            publish_json(self.cmd_pub, TOPIC_NAV, {"direction": "stop", "source": "remote_app"})
            self._publish_remote_event("accepted", {"intent": intent})
            return
        if intent in {"rotate", "rotate_left", "rotate_right", "start_motion", "start"}:
            direction = str(payload.get("direction", "")).strip().lower()
            if not direction:
                if intent == "rotate_left":
                    direction = "left"
                elif intent == "rotate_right":
                    direction = "right"
                elif intent in {"start_motion", "start"}:
                    direction = "forward"

            if direction not in {"forward", "backward", "left", "right"}:
                self._publish_remote_event("rejected", {"reason": "invalid_direction", "payload": payload})
                return
            publish_json(self.cmd_pub, TOPIC_NAV, {"direction": direction, "source": "remote_app"})
            self._publish_remote_event("accepted", {"intent": intent, "direction": direction})
            return

        self._publish_remote_event("rejected", {"reason": "unsupported_intent", "payload": payload})


def main() -> None:
    try:
        Orchestrator().run()
    except Exception as exc:
        logger.error("Fatal error: %s", exc)
        raise


if __name__ == "__main__":
    main()
