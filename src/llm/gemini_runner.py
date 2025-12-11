#!/usr/bin/env python3
"""Gemini-based LLM runner bridging ZMQ llm.request to llm.response.

- Subscribes to `llm.request` on the downstream bus.
- Calls Google Gemini (cloud) with conversation memory and context.
- Publishes `llm.response` with a parsed JSON body plus raw text.

ARCHITECTURE NOTE (Comparison with OVOS/Rhasspy/Wyoming):
Unlike local LLMs, cloud APIs like Gemini are STATELESS - each call is independent.
This runner uses ConversationMemory to:
1. Maintain conversation history across turns (like OVOS ConverseService)
2. Inject robot state for context (similar to Rhasspy's context_input)
3. Manage context window limits (like Wyoming's session management)

Flow: llm.request → [Memory Context Injection] → Gemini → [Memory Update] → llm.response
"""
from __future__ import annotations

import json
import os
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import zmq

try:  # Optional at import time; we handle missing library at runtime.
    import google.generativeai as genai  # type: ignore
except Exception:  # pragma: no cover - handled in main
    genai = None  # type: ignore

from src.core.config_loader import load_config
from src.core.ipc import (
    TOPIC_LLM_REQ,
    TOPIC_LLM_RESP,
    make_publisher,
    make_subscriber,
    publish_json,
)
from src.core.logging_setup import get_logger
from src.llm.conversation_memory import ConversationMemory


# Legacy system prompt (kept for reference) - now handled by ConversationMemory
_LEGACY_SYSTEM_PROMPT = (
    "You are a robot assistant controlling a physical robot. "
    "You MUST reply with STRICT JSON only, no extra text or comments. "
    "The JSON schema is: {"
    "'speak': string, 'direction': 'forward'|'backward'|'left'|'right'|'stop', 'track': string}. "
    "If you do not want to move, use 'direction': 'stop'. "
    "If there is nothing to track, use an empty string for 'track'. "
    "Never include any natural language outside the JSON object."
)


@dataclass
class GeminiConfig:
    api_key: str
    model: str
    temperature: float
    top_p: float


class GeminiRunner:
    def __init__(self) -> None:
        self.config = load_config(Path("config/system.yaml"))
        logs_cfg = self.config.get("logs", {}) or {}
        log_dir = Path(logs_cfg.get("directory", "logs"))
        self.logger = get_logger("llm.gemini", log_dir)

        llm_cfg = self.config.get("llm", {}) or {}
        engine = str(llm_cfg.get("engine", "")).lower()
        if engine not in {"gemini", "google-gemini", "gemini_flash"}:
            self.logger.warning("LLM engine not set to gemini (engine=%s)", engine)

        api_key = (
            str(llm_cfg.get("gemini_api_key") or "")
            or os.environ.get("GEMINI_API_KEY", "")
        )
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY not configured (llm.gemini_api_key or env)")

        model = str(llm_cfg.get("gemini_model", "gemini-1.5-flash"))
        temperature = float(llm_cfg.get("temperature", 0.2))
        top_p = float(llm_cfg.get("top_p", 0.9))
        self.gcfg = GeminiConfig(api_key=api_key, model=model, temperature=temperature, top_p=top_p)

        if genai is None:
            raise RuntimeError(
                "google-generativeai is not installed. Run scripts/install_gemini.sh in the llme venv."
            )

        genai.configure(api_key=self.gcfg.api_key)
        # Prefer JSON-only responses using response_mime_type if available.
        generation_config: Dict[str, Any] = {
            "temperature": self.gcfg.temperature,
            "top_p": self.gcfg.top_p,
        }
        try:
            generation_config["response_mime_type"] = "application/json"
        except Exception:
            pass

        # Initialize WITHOUT system_instruction - we inject context per-request
        # This enables multi-turn conversations with memory
        self.model = genai.GenerativeModel(
            self.gcfg.model,
            generation_config=generation_config,
        )
        
        # Initialize conversation memory
        # Max turns and timeout can be configured in system.yaml under llm.memory_*
        max_turns = int(llm_cfg.get("memory_max_turns", 10))
        timeout_s = float(llm_cfg.get("conversation_timeout_s", 120.0))
        self.memory = ConversationMemory(
            max_turns=max_turns,
            conversation_timeout_s=timeout_s,
        )

        self.ctx = zmq.Context.instance()
        self.sub = make_subscriber(self.config, topic=TOPIC_LLM_REQ, channel="downstream")
        self.pub = make_publisher(self.config, channel="upstream")
        self._running = True

        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        self.logger.info(
            "GeminiRunner initialized (model=%s, memory_turns=%d, timeout=%ds)",
            self.gcfg.model, max_turns, timeout_s
        )

    def _handle_signal(self, *_: int) -> None:
        self.shutdown()
        sys.exit(0)

    def shutdown(self) -> None:
        self._running = False
        try:
            self.sub.close(0)
        except Exception:
            pass
        try:
            self.pub.close(0)
        except Exception:
            pass
        self.logger.info("GeminiRunner shutting down")

    @staticmethod
    def _build_user_prompt(msg: Dict[str, Any]) -> str:
        """Extract user text from message (vision context is handled by memory)."""
        text = str(msg.get("text", "")).strip()
        return text

    def _update_memory_from_message(self, msg: Dict[str, Any]) -> None:
        """Update conversation memory with message context."""
        # Update robot state if vision data is present
        vision = msg.get("vision")
        if vision:
            self.memory.update_robot_state(vision=vision)
        
        # Update direction if provided
        direction = msg.get("direction")
        if direction:
            self.memory.update_robot_state(direction=direction)
        
        # Update tracking target if provided
        track = msg.get("track")
        if track is not None:
            self.memory.update_robot_state(tracking_target=track if track else None)

    @staticmethod
    def _extract_json(raw: str) -> Dict[str, Any]:
        raw = raw.strip()
        if not raw:
            return {}
        # Best-effort: prefer full-string JSON; fallback to first {...} block.
        try:
            if raw[0] == "{" and raw[-1] == "}":
                return json.loads(raw)
        except Exception:
            pass
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(raw[start : end + 1])
            except Exception:
                return {}
        return {}

    def _call_gemini(self, prompt: str) -> tuple[Dict[str, Any], str]:
        """Call Gemini API with full context prompt."""
        if not prompt:
            return {}, ""
        start = time.time()
        try:
            resp = self.model.generate_content(prompt)
        except Exception as exc:  # noqa: BLE001
            self.logger.error("Gemini generate_content failed: %s", exc)
            raise
        latency_ms = int((time.time() - start) * 1000)
        try:
            raw_text = resp.text or ""
        except Exception:
            raw_text = ""
        self.logger.info("Gemini response latency=%dms len=%d", latency_ms, len(raw_text))
        parsed = self._extract_json(raw_text)
        return parsed, raw_text

    def run(self) -> None:
        self.logger.info("GeminiRunner listening on %s (with conversation memory)", TOPIC_LLM_REQ)
        while self._running:
            try:
                topic, payload = self.sub.recv_multipart()
            except Exception as exc:  # noqa: BLE001
                self.logger.error("ZMQ recv failed: %s", exc)
                time.sleep(0.5)
                continue

            try:
                msg = json.loads(payload)
            except Exception as exc:  # noqa: BLE001
                self.logger.error("Invalid llm.request payload: %s", exc)
                continue

            user_text = self._build_user_prompt(msg)
            if not user_text:
                self.logger.warning("Empty user text in llm.request; skipping")
                continue

            # Update memory with any context from the message
            self._update_memory_from_message(msg)
            
            # Add user message to memory
            self.memory.add_user_message(user_text)
            
            # Build full context prompt with memory, robot state, and conversation history
            full_prompt = self.memory.build_context(current_query=user_text)
            self.logger.debug(
                "Memory state: %s, prompt_len=%d",
                self.memory.get_state().name,
                len(full_prompt),
            )

            try:
                parsed, raw = self._call_gemini(full_prompt)
                ok = bool(parsed)
            except Exception as exc:  # noqa: BLE001
                ok = False
                raw = f"GEMINI_ERROR: {exc}"
                parsed = {}

            # Ensure schema keys exist even if Gemini omitted them.
            if not isinstance(parsed, dict):
                parsed = {}
            parsed.setdefault("speak", "")
            parsed.setdefault("direction", "stop")
            parsed.setdefault("track", "")

            # Store assistant response in memory for context continuity
            speak_text = parsed.get("speak", "")
            if speak_text:
                self.memory.add_assistant_message(speak_text)
            
            # Update robot state from response
            if parsed.get("direction"):
                self.memory.update_robot_state(direction=parsed["direction"])
            if parsed.get("track"):
                self.memory.update_robot_state(tracking_target=parsed["track"])

            resp_payload = {
                "ok": ok,
                "json": parsed,
                "raw": raw,
                "memory_state": self.memory.get_state().name,  # For debugging
            }
            publish_json(self.pub, TOPIC_LLM_RESP, resp_payload)
            self.logger.info(
                "Published llm.response ok=%s memory=%s",
                ok, self.memory.get_state().name
            )


def main() -> None:
    try:
        runner = GeminiRunner()
    except Exception as exc:  # noqa: BLE001
        print(f"[llm.gemini] Fatal startup error: {exc}", file=sys.stderr)
        sys.exit(1)
    try:
        runner.run()
    except KeyboardInterrupt:
        runner.shutdown()
    except Exception as exc:  # noqa: BLE001
        runner.logger.error("Unhandled exception in GeminiRunner: %s", exc)
        runner.shutdown()
        sys.exit(1)


if __name__ == "__main__":
    main()
