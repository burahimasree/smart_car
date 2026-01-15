"""Phase-driven orchestrator: single source of truth state machine.

ARCHITECTURE:
    Phase is the ONLY state variable. All behavior derives from Phase.
    No boolean flags. No parallel state tracking.

PHASE TRANSITIONS (explicit and exhaustive):
    IDLE       → LISTENING  (wakeword OR auto-trigger OR manual trigger)
    LISTENING  → THINKING   (STT success with valid transcript)
    LISTENING  → IDLE       (STT timeout OR empty/low-confidence OR error)
    THINKING   → SPEAKING   (LLM response with speak text)
    THINKING   → IDLE       (LLM response with no speak text)
    SPEAKING   → IDLE       (TTS done)
    ERROR      → IDLE       (recovery timeout)
    *          → ERROR      (health failure)

ILLEGAL EVENTS:
    Events arriving in wrong phase are logged and IGNORED.
    No state mutation. No side effects.
"""
from __future__ import annotations

import json
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
    TOPIC_DISPLAY_STATE,
    TOPIC_ESP,
    TOPIC_HEALTH,
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


class Phase(Enum):
    """Orchestrator phase - THE SINGLE SOURCE OF TRUTH.
    
    Every decision in the orchestrator derives from this Phase.
    There are no boolean flags. Phase determines:
    - What events are legal
    - What LED state should be shown
    - What timeouts apply
    - What resources are active
    """
    IDLE = auto()       # Waiting for wakeword; vision active
    LISTENING = auto()  # STT capturing; vision paused
    THINKING = auto()   # LLM processing; vision paused
    SPEAKING = auto()   # TTS playing; vision paused
    ERROR = auto()      # System error; recovery pending


# ═══════════════════════════════════════════════════════════════════════════
# PHASE TRANSITION TABLE (exhaustive)
# ═══════════════════════════════════════════════════════════════════════════
# 
# Current Phase │ Event              │ Next Phase │ Action
# ──────────────┼────────────────────┼────────────┼─────────────────────────
# IDLE          │ wakeword           │ LISTENING  │ pause vision, start STT
# IDLE          │ auto_trigger       │ LISTENING  │ pause vision, start STT
# IDLE          │ cmd.listen.start   │ LISTENING  │ pause vision, start STT
# LISTENING     │ stt (valid)        │ THINKING   │ send LLM request
# LISTENING     │ stt (empty/low)    │ IDLE       │ resume vision, notify user
# LISTENING     │ stt_timeout        │ IDLE       │ resume vision, notify user
# THINKING      │ llm.response       │ SPEAKING   │ send TTS (if speak text)
# THINKING      │ llm.response       │ IDLE       │ resume vision (no speak)
# SPEAKING      │ tts.done           │ IDLE       │ resume vision
# *             │ health.error       │ ERROR      │ show error LED
# ERROR         │ health.ok          │ IDLE       │ resume normal
# ERROR         │ error_timeout(30s) │ IDLE       │ auto-recover
# ──────────────┴────────────────────┴────────────┴─────────────────────────
#
# ALL OTHER EVENT+PHASE COMBINATIONS ARE IGNORED (logged as illegal)
# ═══════════════════════════════════════════════════════════════════════════


class Orchestrator:
    """Phase-driven orchestrator with deterministic state machine."""

    # Phase → LED state mapping (LED = f(Phase))
    PHASE_TO_LED = {
        Phase.IDLE: "idle",
        Phase.LISTENING: "listening",
        Phase.THINKING: "thinking",
        Phase.SPEAKING: "speaking",
        Phase.ERROR: "error",
    }

    # Allowed transitions: (current_phase, event_type) → next_phase
    # If not in this table, the event is ILLEGAL for that phase
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
        # Error transitions (any phase can go to ERROR)
        (Phase.IDLE, "health_error"): Phase.ERROR,
        (Phase.LISTENING, "health_error"): Phase.ERROR,
        (Phase.THINKING, "health_error"): Phase.ERROR,
        (Phase.SPEAKING, "health_error"): Phase.ERROR,
        (Phase.ERROR, "health_ok"): Phase.IDLE,
        (Phase.ERROR, "error_timeout"): Phase.IDLE,
    }

    def __init__(self) -> None:
        self.config = load_config(Path("config/system.yaml"))
        
        # ZMQ sockets
        self.cmd_pub = make_publisher(self.config, channel="downstream", bind=True)
        self.events_sub = make_subscriber(self.config, channel="upstream", bind=True)

        # THE SINGLE SOURCE OF TRUTH
        self._phase = Phase.IDLE
        
        # Timestamps for timeouts (derived from phase, not independent state)
        self._phase_entered_ts = time.time()
        self._last_interaction_ts = time.time()
        
        # Context data (NOT state flags - just data for LLM context)
        self._last_transcript = ""
        self._last_vision: Optional[Dict[str, Any]] = None
        self._last_nav_direction = "stopped"
        self._vision_capture_pending: Optional[str] = None
        
        # ESP32 sensor data (read-only context, not state)
        self._esp_obstacle = False
        self._esp_min_distance = -1
        
        # Configuration
        orch_cfg = self.config.get("orchestrator", {}) or {}
        self.auto_trigger_enabled = bool(orch_cfg.get("auto_trigger_enabled", True))
        self.auto_trigger_interval = float(orch_cfg.get("auto_trigger_interval", 60.0))
        
        stt_cfg = self.config.get("stt", {}) or {}
        self.stt_timeout_s = float(stt_cfg.get("timeout_seconds", 15.0))
        self.stt_min_confidence = float(stt_cfg.get("min_confidence", 0.5))
        
        self.error_recovery_s = 30.0  # Auto-recover from ERROR after 30s

    # ═══════════════════════════════════════════════════════════════════════
    # PHASE MANAGEMENT (centralized, logged, deterministic)
    # ═══════════════════════════════════════════════════════════════════════

    @property
    def phase(self) -> Phase:
        """Read-only access to current phase."""
        return self._phase

    def _transition(self, event_type: str) -> bool:
        """Attempt a phase transition. Returns True if transition occurred.
        
        This is THE ONLY place where phase can change.
        All transitions are logged and validated against the transition table.
        """
        key = (self._phase, event_type)
        next_phase = self.TRANSITIONS.get(key)
        
        if next_phase is None:
            logger.debug(
                "IGNORED: event '%s' illegal in phase %s",
                event_type, self._phase.name
            )
            return False
        
        if next_phase == self._phase:
            return False  # No-op transition
            
        old_phase = self._phase
        self._phase = next_phase
        self._phase_entered_ts = time.time()
        
        logger.info("PHASE: %s → %s (event: %s)", old_phase.name, next_phase.name, event_type)
        
        # Publish LED state (LED = f(Phase))
        self._publish_display_state()
        
        return True

    def _publish_display_state(self) -> None:
        """Publish current phase as display/LED state.
        
        This is the ONLY source of LED state. LED services
        should derive their state solely from this topic.
        """
        led_state = self.PHASE_TO_LED[self._phase]
        publish_json(self.cmd_pub, TOPIC_DISPLAY_STATE, {
            "state": led_state,
            "phase": self._phase.name,
            "timestamp": int(time.time()),
        })

    # ═══════════════════════════════════════════════════════════════════════
    # PHASE-SPECIFIC ACTIONS
    # ═══════════════════════════════════════════════════════════════════════

    def _enter_listening(self) -> None:
        """Actions when entering LISTENING phase."""
        self._last_interaction_ts = time.time()
        # Pause vision during listening
        publish_json(self.cmd_pub, TOPIC_CMD_PAUSE_VISION, {"pause": True})
        # Signal voice pipeline to start STT
        publish_json(self.cmd_pub, TOPIC_CMD_LISTEN_START, {"start": True})

    def _exit_listening(self, reason: str) -> None:
        """Actions when exiting LISTENING phase."""
        # Stop STT capture
        publish_json(self.cmd_pub, TOPIC_CMD_LISTEN_STOP, {"stop": True, "reason": reason})
        # Resume vision
        publish_json(self.cmd_pub, TOPIC_CMD_PAUSE_VISION, {"pause": False})

    def _enter_thinking(self, text: str, vision: Optional[Dict[str, Any]] = None) -> None:
        """Actions when entering THINKING phase."""
        payload: Dict[str, Any] = {"text": text}
        if vision:
            payload["vision"] = vision
        payload["direction"] = self._last_nav_direction
        publish_json(self.cmd_pub, TOPIC_LLM_REQ, payload)

    def _enter_speaking(self, text: str, direction: Optional[str] = None) -> None:
        """Actions when entering SPEAKING phase."""
        if direction and direction != "stop":
            self._last_nav_direction = direction
            publish_json(self.cmd_pub, TOPIC_NAV, {"direction": direction})
        publish_json(self.cmd_pub, TOPIC_TTS, {"text": text})

    def _enter_idle(self) -> None:
        """Actions when entering IDLE phase."""
        # Ensure vision is resumed
        publish_json(self.cmd_pub, TOPIC_CMD_PAUSE_VISION, {"pause": False})

    def _notify_stt_failure(self, reason: str) -> None:
        """Deterministic feedback for STT failure.
        
        POLICY: On STT failure (timeout, silence, low confidence),
        the system MUST provide audible feedback so user knows
        what happened. Then return to IDLE cleanly.
        """
        feedback_messages = {
            "timeout": "I didn't hear anything. Please try again.",
            "empty": "I couldn't understand that. Please speak clearly.",
            "low_confidence": "I'm not sure what you said. Please try again.",
        }
        message = feedback_messages.get(reason, "Something went wrong. Please try again.")
        
        # Send TTS feedback (this is NOT a phase transition to SPEAKING,
        # it's a short notification before going to IDLE)
        publish_json(self.cmd_pub, TOPIC_TTS, {"text": message, "notification": True})
        logger.info("STT failure feedback: %s", reason)

    # ═══════════════════════════════════════════════════════════════════════
    # EVENT HANDLERS (validate phase, then act)
    # ═══════════════════════════════════════════════════════════════════════

    def on_wakeword(self, payload: Dict[str, Any]) -> None:
        """Handle wakeword detection."""
        if self._phase != Phase.IDLE:
            logger.debug("Wakeword ignored: not in IDLE (current: %s)", self._phase.name)
            return
            
        logger.info("Wakeword detected: %s", payload.get("keyword", "unknown"))
        if self._transition("wakeword"):
            self._enter_listening()

    def on_manual_trigger(self, payload: Dict[str, Any]) -> None:
        """Handle manual listen command."""
        if self._phase != Phase.IDLE:
            logger.debug("Manual trigger ignored: not in IDLE (current: %s)", self._phase.name)
            return
            
        logger.info("Manual trigger received")
        if self._transition("manual_trigger"):
            self._enter_listening()

    def on_stt(self, payload: Dict[str, Any]) -> None:
        """Handle STT transcription result."""
        if self._phase != Phase.LISTENING:
            logger.debug("STT result ignored: not in LISTENING (current: %s)", self._phase.name)
            return
        
        text = str(payload.get("text", "")).strip()
        confidence = float(payload.get("confidence", 0.0) or 0.0)
        
        # Case 1: Empty transcription
        if not text:
            logger.warning("Empty transcription received")
            self._exit_listening("empty")
            self._notify_stt_failure("empty")
            self._transition("stt_invalid")
            self._enter_idle()
            return
        
        # Case 2: Low confidence
        if confidence < self.stt_min_confidence:
            logger.info(
                "Low confidence (%.3f < %.3f): '%s'",
                confidence, self.stt_min_confidence, text[:50]
            )
            self._exit_listening("low_confidence")
            self._notify_stt_failure("low_confidence")
            self._transition("stt_invalid")
            self._enter_idle()
            return
        
        # Case 3: Valid transcription
        logger.info("STT valid (%d chars, conf=%.2f)", len(text), confidence)
        self._last_transcript = text
        self._exit_listening("success")
        
        if self._transition("stt_valid"):
            # Check if vision context is requested
            if self._should_request_vision(text):
                self._request_vision_capture(text)
            else:
                self._enter_thinking(text)

    def _should_request_vision(self, text: str) -> bool:
        """Check if user is asking about what robot sees."""
        keywords = ["what do you see", "what are you seeing", "describe", "look at"]
        return any(k in text.lower() for k in keywords)

    def _request_vision_capture(self, text: str) -> None:
        """Request one-shot vision capture for LLM context."""
        request_id = f"visn-{int(time.time() * 1000)}"
        self._vision_capture_pending = request_id
        self._last_transcript = text
        publish_json(self.cmd_pub, TOPIC_CMD_VISN_CAPTURE, {"request_id": request_id})

    def on_vision(self, payload: Dict[str, Any]) -> None:
        """Handle vision detection result."""
        self._last_vision = payload
        
        # Check for pending vision capture
        if self._vision_capture_pending:
            request_id = payload.get("request_id")
            if request_id == self._vision_capture_pending:
                self._vision_capture_pending = None
                if self._phase == Phase.THINKING:
                    self._enter_thinking(self._last_transcript, vision=payload)

    def on_llm(self, payload: Dict[str, Any]) -> None:
        """Handle LLM response."""
        if self._phase != Phase.THINKING:
            logger.debug("LLM response ignored: not in THINKING (current: %s)", self._phase.name)
            return
        
        logger.info("LLM response received")
        body = payload.get("json") or {}
        speak = body.get("speak") or payload.get("text", "")
        direction = body.get("direction")
        
        if speak:
            if self._transition("llm_with_speech"):
                self._enter_speaking(speak, direction)
        else:
            # No speech response - go back to idle
            if direction:
                publish_json(self.cmd_pub, TOPIC_NAV, {"direction": direction})
                self._last_nav_direction = direction
            self._transition("llm_no_speech")
            self._enter_idle()

    def on_tts(self, payload: Dict[str, Any]) -> None:
        """Handle TTS completion."""
        done = payload.get("done") or payload.get("final") or payload.get("completed")
        
        # Ignore notification TTS (not associated with SPEAKING phase)
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
        """Handle ESP32 sensor data (context only, not state)."""
        data = payload.get("data")
        if data:
            self._esp_obstacle = bool(data.get("obstacle", False))
            self._esp_min_distance = int(data.get("min_distance", -1))
        
        # Handle collision alerts
        alert = payload.get("alert")
        if alert == "COLLISION":
            logger.critical("ESP32 collision alert!")

    def on_health(self, payload: Dict[str, Any]) -> None:
        """Handle system health status."""
        ok = bool(payload.get("ok", True))
        
        if not ok and self._phase != Phase.ERROR:
            logger.error("Health error: %s", payload)
            self._transition("health_error")
        elif ok and self._phase == Phase.ERROR:
            logger.info("Health restored")
            self._transition("health_ok")
            self._enter_idle()

    # ═══════════════════════════════════════════════════════════════════════
    # TIMEOUT HANDLERS
    # ═══════════════════════════════════════════════════════════════════════

    def _check_timeouts(self) -> None:
        """Check for phase-specific timeouts."""
        now = time.time()
        elapsed = now - self._phase_entered_ts
        
        # STT timeout while LISTENING
        if self._phase == Phase.LISTENING and elapsed > self.stt_timeout_s:
            logger.warning("STT timeout (%.1fs)", self.stt_timeout_s)
            self._exit_listening("timeout")
            self._notify_stt_failure("timeout")
            self._transition("stt_timeout")
            self._enter_idle()
        
        # Error auto-recovery
        elif self._phase == Phase.ERROR and elapsed > self.error_recovery_s:
            logger.info("Error auto-recovery after %.1fs", self.error_recovery_s)
            self._transition("error_timeout")
            self._enter_idle()

    def _check_auto_trigger(self) -> None:
        """Check for auto-trigger condition."""
        if not self.auto_trigger_enabled:
            return
        if self._phase != Phase.IDLE:
            return
        
        idle_time = time.time() - self._last_interaction_ts
        if idle_time > self.auto_trigger_interval:
            logger.info("Auto-trigger after %.1fs idle", idle_time)
            if self._transition("auto_trigger"):
                self._enter_listening()

    # ═══════════════════════════════════════════════════════════════════════
    # MAIN LOOP
    # ═══════════════════════════════════════════════════════════════════════

    def run(self) -> None:
        """Main event loop."""
        logger.info(
            "Orchestrator running (Phase FSM) auto_trigger=%s interval=%.1fs",
            self.auto_trigger_enabled,
            self.auto_trigger_interval,
        )
        logger.info("Initial phase: %s", self._phase.name)
        self._publish_display_state()

        poller = zmq.Poller()
        poller.register(self.events_sub, zmq.POLLIN)

        while True:
            socks = dict(poller.poll(timeout=100))
            
            if self.events_sub in socks:
                try:
                    topic, data = self.events_sub.recv_multipart()
                    payload = json.loads(data)
                except Exception as exc:
                    logger.error("Recv/parse error: %s", exc)
                    continue

                # Route to appropriate handler
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
                elif topic == TOPIC_ESP:
                    self.on_esp(payload)
                elif topic == TOPIC_HEALTH:
                    self.on_health(payload)

            # Check timeouts
            self._check_timeouts()
            self._check_auto_trigger()


def main() -> None:
    try:
        Orchestrator().run()
    except Exception as exc:
        logger.error("Fatal error: %s", exc)
        raise


if __name__ == "__main__":
    main()
