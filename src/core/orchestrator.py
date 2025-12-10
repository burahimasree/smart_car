"""Event-driven orchestrator wiring wakeword → STT → LLM → NAV → TTS."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional
from dataclasses import dataclass

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
    TOPIC_CMD_LISTEN_START,
    TOPIC_CMD_LISTEN_STOP,
    TOPIC_DISPLAY_STATE,
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
from src.stt.engine import STTEngine


logger = get_logger("orchestrator", Path("logs"))


class Orchestrator:
    def __init__(self) -> None:
        self.config = load_config(Path("config/system.yaml"))
        # Bind downstream PUB for commands, bind upstream SUB for events
        self.cmd_pub = make_publisher(self.config, channel="downstream", bind=True)
        self.events_sub = make_subscriber(self.config, channel="upstream", bind=True)

        # When running with the dedicated STT wrapper + AudioManager
        # architecture, the orchestrator should avoid spawning the
        # legacy STT engine process and instead rely solely on
        # cmd.listen.start/stop and stt.transcription events.
        stt_cfg = self.config.get("stt", {}) or {}
        self.stt_use_wrapper: bool = bool(
            stt_cfg.get("use_wrapper")
            or os.environ.get("STT_USE_WRAPPER") == "1"
        )
        if self.stt_use_wrapper:
            if os.environ.get("STT_ENGINE_DISABLED") is None:
                os.environ["STT_ENGINE_DISABLED"] = "1"
            self.stt_engine: Optional[STTEngine] = None
        else:
            self.stt_engine = STTEngine.from_config(self.config)

        self.state: Dict[str, Any] = {
            "vision_paused": False,
            "stt_active": False,
            "llm_pending": False,
            "tts_pending": False,
            "last_transcript": "",
            "last_visn": None,
            "stt_started_ts": None,
        }

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

    def _ipc_upstream(self) -> str:
        return os.environ.get("IPC_UPSTREAM", self.config["ipc"]["upstream"])

    def _start_stt(self) -> None:
        if self.state.get("stt_active"):
            return
        if self.stt_use_wrapper:
            # Wrapper + AudioManager own the actual capture; we
            # simply track logical state and rely on cmd.listen.start
            # having been published.
            self.state["stt_active"] = True
            self.state["stt_started_ts"] = time.time()
            return

        started = self.stt_engine.start_session(self._ipc_upstream())
        if started:
            self.state["stt_active"] = True
            self.state["stt_started_ts"] = time.time()
        else:
            logger.warning("STT session already running; ignoring")

    def _stop_stt(self) -> None:
        if not self.state.get("stt_active"):
            return
        if not self.stt_use_wrapper:
            self.stt_engine.stop_session()
        self.state["stt_active"] = False
        self.state["stt_started_ts"] = None

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
        # Pause vision immediately
        if not self.state.get("vision_paused"):
            self._send_pause_vision(True)
        publish_json(self.cmd_pub, TOPIC_CMD_LISTEN_START, {"start": True})
        # Start STT listening session (runner publishes on upstream bus)
        self._start_stt()
        self._send_display_state("listening")

    def on_stt(self, payload: Dict[str, Any]) -> None:
        if not self.state.get("stt_active"):
            return
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
        publish_json(self.cmd_pub, TOPIC_LLM_REQ, {"text": text})
        self.state["llm_pending"] = True
        self.state["tts_pending"] = False
        self._send_display_state("thinking")
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
        else:
            self.state["tts_pending"] = False
            self._send_pause_vision(False)
            self._send_display_state("idle")

    def on_tts(self, payload: Dict[str, Any]) -> None:
        # Expect a completion marker; different implementations may vary
        done = payload.get("done") or payload.get("final") or payload.get("completed")
        if done:
            logger.info("TTS completed")
            self.state["tts_pending"] = False
            self._send_pause_vision(False)
            self._send_display_state("idle")

    def on_visn(self, payload: Dict[str, Any]) -> None:
        self.state["last_visn"] = payload
        if not self.state["vision_paused"]:
            logger.debug("Vision: %s", payload)

    def run(self) -> None:
        logger.info("Orchestrator running (upstream %s, downstream %s)", self.config["ipc"]["upstream"], self.config["ipc"]["downstream"])
        while True:
            topic, data = self.events_sub.recv_multipart()
            try:
                payload = json.loads(data)
            except json.JSONDecodeError:
                logger.error("Invalid JSON on topic %s", topic)
                continue

            # Apply any session timeouts before handling the next
            # event so that a hung STT pipeline cannot wedge the
            # system indefinitely.
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
