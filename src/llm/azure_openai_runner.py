#!/usr/bin/env python3
"""Azure OpenAI LLM runner bridging ZMQ llm.request to llm.response.

- Subscribes to `llm.request` on the downstream bus.
- Calls Azure OpenAI Responses API (tested in third_party/azure-openai/testing.py).
- Publishes `llm.response` with parsed JSON body plus raw text.

Environment variables (no API keys in code):
- AZURE_OPENAI_API_KEY
- AZURE_OPENAI_ENDPOINT
- AZURE_OPENAI_DEPLOYMENT
- AZURE_OPENAI_API_VERSION (optional, default: 2025-03-01-preview)
"""
from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Dict

import zmq
from openai import AzureOpenAI

from src.core.config_loader import load_config
from src.core.ipc import (
    TOPIC_LLM_REQ,
    TOPIC_LLM_RESP,
    make_publisher,
    make_subscriber,
    publish_json,
)
from src.core.logging_setup import get_logger


class AzureOpenAIRunner:
    def __init__(self) -> None:
        self.config = load_config(Path("config/system.yaml"))
        logs_cfg = self.config.get("logs", {}) or {}
        log_dir = Path(logs_cfg.get("directory", "logs"))
        self.logger = get_logger("llm.azure_openai", log_dir)

        llm_cfg = self.config.get("llm", {}) or {}
        engine = str(llm_cfg.get("engine", "")).lower()
        if engine not in {"azure_openai", "azure-openai", "azure"}:
            self.logger.warning("LLM engine not set to azure_openai (engine=%s)", engine)

        api_key = os.environ.get("AZURE_OPENAI_API_KEY", "").strip()
        endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "").strip()
        deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "").strip()
        api_version = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-12-01-preview").strip()

        if not api_key:
            raise RuntimeError("AZURE_OPENAI_API_KEY not configured")
        if not endpoint:
            raise RuntimeError("AZURE_OPENAI_ENDPOINT not configured")
        if not deployment:
            raise RuntimeError("AZURE_OPENAI_DEPLOYMENT not configured")

        self.client = AzureOpenAI(
            api_version=api_version,
            azure_endpoint=endpoint,
            api_key=api_key,
        )
        self.deployment = deployment

        self.ctx = zmq.Context.instance()
        self.sub = make_subscriber(self.config, topic=TOPIC_LLM_REQ, channel="downstream")
        self.pub = make_publisher(self.config, channel="upstream")
        self._running = True

        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        self.logger.info(
            "AzureOpenAIRunner initialized (deployment=%s, api_version=%s)",
            self.deployment,
            api_version,
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
        self.logger.info("AzureOpenAIRunner shutting down")

    @staticmethod
    def _extract_json(raw: str) -> Dict[str, Any]:
        raw = raw.strip()
        if not raw:
            return {}
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

    def _call_azure(self, text: str) -> tuple[Dict[str, Any], str]:
        if not text:
            return {}, ""
        system_prompt = (
            "You are ROBO, a smart assistant for a robotic car. "
            "Reply with JSON only: {\"speak\": string, \"direction\": "
            "'forward'|'backward'|'left'|'right'|'stop', \"track\": string}. "
            "If no movement, use direction 'stop' and empty track."
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text[:300]},
        ]

        try:
            resp = self.client.chat.completions.create(
                model=self.deployment,
                messages=messages,
                max_completion_tokens=160,
                response_format={"type": "json_object"},
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("Azure OpenAI JSON request failed, retrying without response_format: %s", exc)
            try:
                resp = self.client.chat.completions.create(
                    model=self.deployment,
                    messages=messages,
                    max_completion_tokens=160,
                )
            except Exception as exc2:  # noqa: BLE001
                self.logger.error("Azure OpenAI request failed: %s", exc2)
                raise

        content = ""
        try:
            msg = resp.choices[0].message
            raw_content = msg.content
            if isinstance(raw_content, str):
                content = raw_content.strip()
            elif isinstance(raw_content, list):
                parts: list[str] = []
                for item in raw_content:
                    if isinstance(item, dict):
                        text_part = item.get("text") or ""
                        if text_part:
                            parts.append(str(text_part))
                    elif isinstance(item, str):
                        parts.append(item)
                content = "".join(parts).strip()
            elif raw_content is not None:
                content = str(raw_content).strip()
        except Exception:
            content = ""

        if not content:
            self.logger.warning("Azure OpenAI returned empty content")

        parsed = self._extract_json(content)
        return parsed, content

    def run(self) -> None:
        self.logger.info("AzureOpenAIRunner listening on %s", TOPIC_LLM_REQ)
        while self._running:
            try:
                _, payload = self.sub.recv_multipart()
            except Exception as exc:  # noqa: BLE001
                self.logger.error("ZMQ recv failed: %s", exc)
                time.sleep(0.5)
                continue

            try:
                msg = json.loads(payload)
            except Exception as exc:  # noqa: BLE001
                self.logger.error("Invalid llm.request payload: %s", exc)
                continue

            user_text = str(msg.get("text", "")).strip()
            if not user_text:
                self.logger.warning("Empty user text in llm.request; skipping")
                continue

            try:
                parsed, raw = self._call_azure(user_text)
                ok = bool(parsed) or bool(raw.strip())
            except Exception as exc:  # noqa: BLE001
                ok = False
                raw = f"AZURE_OPENAI_ERROR: {exc}"
                parsed = {}

            if not isinstance(parsed, dict):
                parsed = {}
            parsed.setdefault("speak", raw.strip()[:300] if raw else "")
            parsed.setdefault("direction", "stop")
            parsed.setdefault("track", "")

            resp_payload = {
                "ok": ok,
                "json": parsed,
                "raw": raw,
                "azure": True,
            }
            publish_json(self.pub, TOPIC_LLM_RESP, resp_payload)
            self.logger.info("Published llm.response ok=%s", ok)


def main() -> None:
    try:
        runner = AzureOpenAIRunner()
    except Exception as exc:  # noqa: BLE001
        print(f"[llm.azure_openai] Fatal startup error: {exc}", file=sys.stderr)
        sys.exit(1)
    try:
        runner.run()
    except KeyboardInterrupt:
        runner.shutdown()
    except Exception as exc:  # noqa: BLE001
        runner.logger.error("Unhandled exception in AzureOpenAIRunner: %s", exc)
        runner.shutdown()
        sys.exit(1)


if __name__ == "__main__":
    main()
