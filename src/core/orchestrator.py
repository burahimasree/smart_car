"""Event-driven orchestrator wiring wakeword → STT → LLM → NAV → TTS."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional
from dataclasses import dataclass
import zmq

try:  # Import optional typed configs for legacy assistant wrapper
    from src.stt.engine import RecognizerConfig, STTBackend  # type: ignore
    from src.tts.engine import TTSConfig  # type: ignore
    from src.llm.llama_wrapper import LlamaConfig  # type: ignore
    from src.vision.detector import VisionConfig  # type: ignore
    from src.ui.display_driver import DisplayConfig  # type: ignore
except Exception:  # pragma: no cover
    RecognizerConfig = object  # type: ignore
    STTBackend = object  # type: ignore
    TTSConfig = object  # type: ignore
    LlamaConfig = object  # type: ignore
    VisionConfig = object  # type: ignore
    DisplayConfig = object  # type: ignore

from src.core.config_loader import load_config
from src.core.ipc import (
    TOPIC_CMD_PAUSE_VISION,
    TOPIC_CMD_VISN_CAPTURE,
    TOPIC_CMD_LISTEN_START,
    TOPIC_CMD_LISTEN_STOP,
    TOPIC_DISPLAY_STATE,
    TOPIC_ESP,
    TOPIC_LLM_REQ,
    TOPIC_LLM_RESP,
    TOPIC_NAV,
    TOPIC_STT,
    TOPIC_TTS,
    TOPIC_VISN,
    TOPIC_WW_DETECTED,
    make_publisher,
    make_subscriber,
    publish_json,
)
from src.core.logging_setup import get_logger
logger = get_logger("orchestrator", Path("logs"))


class Orchestrator:
    def __init__(self) -> None:
        self.config = load_config(Path("config/system.yaml"))
        # Bind downstream PUB for commands, bind upstream SUB for events
        self.cmd_pub = make_publisher(self.config, channel="downstream", bind=True)
        self.events_sub = make_subscriber(self.config, channel="upstream", bind=True)

        self.state: Dict[str, Any] = {
            "vision_paused": False,
            "stt_active": False,
            "llm_pending": False,
            "tts_pending": False,
            "last_transcript": "",
            "last_visn": None,
            "stt_started_ts": None,
            "vision_capture_pending": None,
            "vision_request_text": "",
            "tracking_target": None,
            "last_nav_direction": "stopped",  # Track for LLM context
            # ESP32 sensor state for collision awareness
            "esp_obstacle": False,
            "esp_warning": False,
            "esp_min_distance": -1,
        }

        # Heartbeat auto-trigger configuration
        orch_cfg = self.config.get("orchestrator", {}) or {}
        self.auto_trigger_enabled = bool(orch_cfg.get("auto_trigger_enabled", True))
        self.auto_trigger_interval = float(orch_cfg.get("auto_trigger_interval", 60.0))
        self.last_interaction_ts = time.time()

    def _send_pause_vision(self, pause: bool) -> None:
        publish_json(self.cmd_pub, TOPIC_CMD_PAUSE_VISION, {"pause": pause})
        self.state["vision_paused"] = pause

    def _send_tts(self, text: str) -> None:
        if text:
            publish_json(self.cmd_pub, TOPIC_TTS, {"text": text})

    def _send_display_state(self, state: str) -> None:
        """Publish high-level UI state for display/LED consumers.

        States are simple strings like "idle", "listening",
        "thinking", "speaking", "error".
        """
        publish_json(self.cmd_pub, TOPIC_DISPLAY_STATE, {"state": state})

    def _send_nav(self, direction: str, *, target: Optional[str] = None) -> None:
        payload: Dict[str, Any] = {"direction": direction}
        if target:
            payload["target"] = target
        publish_json(self.cmd_pub, TOPIC_NAV, payload)
        # Track last direction for context in LLM requests
        self.state["last_nav_direction"] = direction

    def _ipc_upstream(self) -> str:
        return os.environ.get("IPC_UPSTREAM", self.config["ipc"]["upstream"])

    def _start_stt(self) -> None:
        if self.state.get("stt_active"):
            return
        self.state["stt_active"] = True
        self.state["stt_started_ts"] = time.time()
        self.last_interaction_ts = time.time()

    def _stop_stt(self) -> None:
        if not self.state.get("stt_active"):
            return
        self.state["stt_active"] = False
        self.state["stt_started_ts"] = None

    def _reset_interaction(self) -> None:
        self.last_interaction_ts = time.time()

    def _is_idle(self) -> bool:
        return not (self.state["stt_active"] or self.state["llm_pending"] or self.state["tts_pending"])

    def _check_timeouts(self) -> None:
        """Apply simple timeouts for long-running STT sessions.

        When using the STT wrapper, a hung or non-responsive audio
        pipeline should not leave vision paused indefinitely. A
        configurable timeout cancels listening and resumes vision.
        """

        stt_cfg = self.config.get("stt", {}) or {}
        timeout_s = float(stt_cfg.get("timeout_seconds", 0.0) or 0.0)
        if not timeout_s:
            return
        started_ts = self.state.get("stt_started_ts")
        if not self.state.get("stt_active") or not started_ts:
            return
        if time.time() - float(started_ts) < timeout_s:
            return

        logger.warning("STT timeout (%.1fs) reached; cancelling listen session", timeout_s)
        self._stop_stt()
        self._send_pause_vision(False)
        publish_json(self.cmd_pub, TOPIC_CMD_LISTEN_STOP, {"stop": True, "reason": "timeout"})
        self._send_display_state("idle")

    def on_wakeword(self, payload: Dict[str, Any]) -> None:
        logger.info("Wakeword: %s", payload)
        self._trigger_listening()

    def _trigger_listening(self) -> None:
        """Common entry for auto-trigger, manual trigger, or wakeword."""
        self._reset_interaction()
        if not self.state.get("vision_paused"):
            self._send_pause_vision(True)
        publish_json(self.cmd_pub, TOPIC_CMD_LISTEN_START, {"start": True})
        self._start_stt()
        self._send_display_state("listening")

    def on_stt(self, payload: Dict[str, Any]) -> None:
        if not self.state.get("stt_active"):
            return
        self._reset_interaction()
        text = str(payload.get("text", "")).strip()
        confidence = float(payload.get("confidence", 0.0) or 0.0)
        min_conf = float(self.config.get("stt", {}).get("min_confidence", 0.0) or 0.0)
        if not text:
            logger.warning("Empty transcription payload: %s", payload)
            self._stop_stt()
            self._send_pause_vision(False)
            publish_json(self.cmd_pub, TOPIC_CMD_LISTEN_STOP, {"stop": True})
            self._send_display_state("idle")
            return
        if confidence < min_conf:
            logger.info(
                "Discarding low-confidence transcription (%.3f < %.3f): '%s'",
                confidence,
                min_conf,
                text,
            )
            self._stop_stt()
            self._send_pause_vision(False)
            publish_json(self.cmd_pub, TOPIC_CMD_LISTEN_STOP, {"stop": True})
            self._send_display_state("idle")
            return
        logger.info("STT transcription received (%d chars)", len(text))
        self.state["last_transcript"] = text
        self.state["tts_pending"] = False
        vision_requested = self._should_request_vision(text)
        if vision_requested:
            self._request_vision_capture(text)
        else:
            self._publish_llm_request(text)
        # Resume vision after transcription per latest contract
        self._stop_stt()
        publish_json(self.cmd_pub, TOPIC_CMD_LISTEN_STOP, {"stop": True})

    def _extract_nav(self, intent_payload: Dict[str, Any]) -> Optional[str]:
        # Support two shapes: {intent: "navigate", slots:{direction:"forward"}}
        # or legacy {name:"navigate", slots:{direction}}
        if not intent_payload:
            return None
        if isinstance(intent_payload, str) and intent_payload.lower().startswith("navigate"):
            return intent_payload.split(":")[-1].strip().lower() if ":" in intent_payload else "forward"
        if isinstance(intent_payload, dict):
            # Preferred: direct direction field from LLM JSON schema
            direct = intent_payload.get("direction")
            if isinstance(direct, str) and direct.strip():
                return direct.strip().lower()
            if intent_payload.get("intent") == "navigate":
                slots = intent_payload.get("slots", {})
                return str(slots.get("direction", "forward")).lower()
            if intent_payload.get("name") == "navigate":
                slots = intent_payload.get("slots", {})
                return str(slots.get("direction", "forward")).lower()
        return None

    def on_llm(self, payload: Dict[str, Any]) -> None:
        logger.info("LLM response received")
        self.state["llm_pending"] = False
        body = payload.get("json") or {}
        speak = payload.get("text") or body.get("speak") or payload.get("raw", "")
        direction = self._extract_nav(body)
        target = self.state.get("last_visn", {}).get("label") if self.state.get("last_visn") else None
        if direction:
            self._send_nav(direction, target=target)
        if speak:
            self._send_tts(speak)
            self.state["tts_pending"] = True
            self._send_display_state("speaking")
        track_target = body.get("track")
        if track_target:
            self.state["tracking_target"] = str(track_target).lower()
            logger.info("Tracking target set to %s", self.state["tracking_target"])
            self._send_display_state("tracking")

        if not speak and not direction and not track_target:
            self.state["tts_pending"] = False
            self._send_pause_vision(False)
            self._send_display_state("idle")

    def _should_request_vision(self, text: str) -> bool:
        lowered = text.lower()
        keywords = ["what do you see", "what are you seeing", "describe what you see", "what can you see"]
        return any(key in lowered for key in keywords)

    def _request_vision_capture(self, transcript: str) -> None:
        request_id = f"visn-{int(time.time() * 1000)}"
        self.state["vision_capture_pending"] = request_id
        self.state["vision_request_text"] = transcript
        publish_json(self.cmd_pub, TOPIC_CMD_VISN_CAPTURE, {"request_id": request_id})
        self._send_display_state("thinking")

    def _publish_llm_request(self, text: str, *, vision: Optional[Dict[str, Any]] = None) -> None:
        """Publish LLM request with full robot context.
        
        The GeminiRunner's ConversationMemory will use this context to:
        1. Update robot state (direction, tracking, vision)
        2. Build a context-aware prompt with history
        """
        payload: Dict[str, Any] = {"text": text}
        
        # Include vision context if available
        if vision:
            payload["vision"] = vision
        
        # Include current robot state for memory context
        # This helps the LLM understand the current situation
        if self.state.get("tracking_target"):
            payload["track"] = self.state["tracking_target"]
        
        # Include last known direction (from nav commands)
        # Note: We track this in orchestrator for context continuity
        last_direction = self.state.get("last_nav_direction", "stopped")
        payload["direction"] = last_direction
        
        publish_json(self.cmd_pub, TOPIC_LLM_REQ, payload)
        self.state["llm_pending"] = True
        self._send_display_state("thinking")

    def on_tts(self, payload: Dict[str, Any]) -> None:
        # Expect a completion marker; different implementations may vary
        done = payload.get("done") or payload.get("final") or payload.get("completed")
        if done:
            logger.info("TTS completed")
            self.state["tts_pending"] = False
            if self.state.get("tracking_target"):
                self._send_display_state("tracking")
            else:
                self._send_pause_vision(False)
                self._send_display_state("idle")

    def on_visn(self, payload: Dict[str, Any]) -> None:
        self.state["last_visn"] = payload
        if not self.state["vision_paused"]:
            logger.debug("Vision: %s", payload)
        pending = self.state.get("vision_capture_pending")
        request_id = payload.get("request_id")
        if pending and request_id == pending:
            text = self.state.get("vision_request_text", "")
            self.state["vision_capture_pending"] = None
            self.state["vision_request_text"] = ""
            self._publish_llm_request(text or self.state.get("last_transcript", ""), vision=payload)

        # Visual servoing: chase target
        target = self.state.get("tracking_target")
        if target:
            label = str(payload.get("label", "")).lower()
            if target in label:
                bbox = payload.get("bbox", [0, 0, 0, 0])
                try:
                    cx = (float(bbox[0]) + float(bbox[2])) / 2.0
                except Exception:
                    return
                direction = "forward"
                if cx < 200:
                    direction = "left"
                elif cx > 440:
                    direction = "right"
                
                # Safety check: don't move forward if obstacle detected
                if direction == "forward" and (self.state.get("esp_obstacle") or self.state.get("esp_warning")):
                    logger.warning("Visual servoing: forward blocked by obstacle (min_dist=%s)", 
                                   self.state.get("esp_min_distance"))
                    direction = "stop"
                
                logger.info("Visual servoing target=%s cx=%.1f -> %s", label, cx, direction)
                self._send_nav(direction)

    def on_esp(self, payload: Dict[str, Any]) -> None:
        """Handle ESP32 sensor data updates."""
        # Handle parsed sensor data
        data = payload.get("data")
        if data:
            self.state["esp_obstacle"] = bool(data.get("obstacle", False))
            self.state["esp_warning"] = bool(data.get("warning", False))
            self.state["esp_min_distance"] = int(data.get("min_distance", -1))
        
        # Handle collision alerts
        alert = payload.get("alert")
        if alert == "COLLISION":
            alert_data = payload.get("alert_data", "")
            if "EMERGENCY" in alert_data:
                logger.critical("ESP32 EMERGENCY STOP - obstacle collision!")
                self.state["esp_obstacle"] = True
                # Cancel tracking if active
                if self.state.get("tracking_target"):
                    logger.warning("Cancelling tracking due to collision alert")
                    self.state["tracking_target"] = None
                    self._send_display_state("idle")
        
        # Handle command blocked feedback
        if payload.get("blocked"):
            logger.warning("ESP32 blocked command %s: %s", 
                          payload.get("command"), payload.get("reason"))

    def _maybe_auto_trigger(self) -> None:
        if not self.auto_trigger_enabled:
            return
        if not self._is_idle():
            return
        if time.time() - self.last_interaction_ts > self.auto_trigger_interval:
            logger.warning(
                "Auto-trigger: idle for %.1fs; forcing listening", time.time() - self.last_interaction_ts
            )
            self._trigger_listening()

    def run(self) -> None:
        logger.info(
            "Orchestrator running (upstream %s, downstream %s) auto_trigger=%s interval=%.1fs",
            self.config["ipc"]["upstream"],
            self.config["ipc"]["downstream"],
            self.auto_trigger_enabled,
            self.auto_trigger_interval,
        )

        poller = zmq.Poller()
        poller.register(self.events_sub, zmq.POLLIN)

        while True:
            socks = dict(poller.poll(timeout=100))  # 100 ms
            if self.events_sub in socks:
                try:
                    topic, data = self.events_sub.recv_multipart()
                except Exception as exc:  # pragma: no cover
                    logger.error("Recv error: %s", exc)
                    continue
                try:
                    payload = json.loads(data)
                except json.JSONDecodeError:
                    logger.error("Invalid JSON on topic %s", topic)
                    continue

                self._check_timeouts()

                if topic == TOPIC_WW_DETECTED:
                    self.on_wakeword(payload)
                elif topic == TOPIC_STT:
                    self.on_stt(payload)
                elif topic == TOPIC_LLM_RESP:
                    self.on_llm(payload)
                elif topic == TOPIC_VISN:
                    self.on_visn(payload)
                elif topic == TOPIC_TTS:
                    self.on_tts(payload)
                elif topic == TOPIC_ESP:
                    self.on_esp(payload)
                elif topic == TOPIC_CMD_LISTEN_START:
                    # Treat manual trigger same as wake
                    self._trigger_listening()

            self._maybe_auto_trigger()


def main() -> None:
    try:
        Orchestrator().run()
    except Exception as exc:  # pragma: no cover
        logger.error("Fatal error in orchestrator: %s", exc)
        raise


if __name__ == "__main__":
    main()


# ---------------------------------------------------------------------------
# Legacy assistant wrapper expected by older CLI/tests (OfflineAssistant).
# Provides a minimal bootstrap routine performing path existence checks and
# then returning control to caller. Keeps current orchestrator logic untouched.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class OrchestratorConfig:
    stt: RecognizerConfig  # type: ignore[valid-type]
    tts: TTSConfig  # type: ignore[valid-type]
    llm: LlamaConfig  # type: ignore[valid-type]
    vision: VisionConfig  # type: ignore[valid-type]
    display: DisplayConfig  # type: ignore[valid-type]


class OfflineAssistant:
    def __init__(self, config: OrchestratorConfig) -> None:
        self.config = config

    def bootstrap(self) -> None:
        # Minimal validation mirroring expectations of existing stub tests.
        missing: list[str] = []
        try:
            mp = getattr(self.config.stt, "model_path", Path("missing"))
            if (not mp.exists()) or mp.suffix == "":
                missing.append("stt.model_path")
        except Exception:
            pass
        try:
            mp = getattr(self.config.tts, "model_path", Path("missing"))
            if (not mp.exists()) or mp.suffix == "":
                missing.append("tts.model_path")
        except Exception:
            pass
        try:
            mp = getattr(self.config.llm, "model_path", Path("missing"))
            if (not mp.exists()) or mp.suffix == "":
                missing.append("llm.model_path")
        except Exception:
            pass
        try:
            mp = getattr(self.config.vision, "model_path", Path("missing"))
            if (not mp.exists()) or mp.suffix == "":
                missing.append("vision.model_path")
        except Exception:
            pass
        if missing:
            raise FileNotFoundError(f"Missing model assets: {', '.join(missing)}")
