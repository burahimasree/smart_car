#!/usr/bin/env python3
"""Local LLM runner using llama.cpp for offline inference.

PLAN B: Use this when Gemini API hits rate limits.

- Subscribes to `llm.request` on the downstream bus.
- Calls local TinyLlama 1.1B via llama.cpp subprocess.
- Publishes `llm.response` with parsed JSON body plus raw text.

DESIGNED FOR RASPBERRY PI 4:
- Uses TinyLlama 1.1B Q4_K_M (~638MB) - fits in RAM
- No GPU required (CPU only)
- Response time: ~10-20 seconds on Pi4
- Falls back to simple keyword responses if LLM is slow/unavailable
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

import zmq

from src.core.config_loader import load_config
from src.core.ipc import (
    TOPIC_LLM_REQ,
    TOPIC_LLM_RESP,
    make_publisher,
    make_subscriber,
    publish_json,
)
from src.core.logging_setup import get_logger


# Simple system prompt for TinyLlama
SYSTEM_PROMPT = "You are ROBO, a smart assistant for a robotic car. Keep answers to 1-2 sentences."


@dataclass
class LocalLLMConfig:
    model_path: str
    llama_simple_bin: str
    max_tokens: int
    timeout_seconds: int


class LocalLLMRunner:
    def __init__(self) -> None:
        self.config = load_config(Path("config/system.yaml"))
        logs_cfg = self.config.get("logs", {}) or {}
        log_dir = Path(logs_cfg.get("directory", "logs"))
        self.logger = get_logger("llm.local", log_dir)

        llm_cfg = self.config.get("llm", {}) or {}
        
        # Model path - use TinyLlama by default
        project_root = os.environ.get("PROJECT_ROOT", str(Path.cwd()))
        default_model = f"{project_root}/models/llm/tinyllama-1.1b-chat.Q4_K_M.gguf"
        model_path = str(llm_cfg.get("local_model_path", default_model))
        
        # llama.cpp llama-simple binary (single-shot inference)
        default_bin = f"{project_root}/third_party/llama.cpp/build/bin/llama-simple"
        llama_bin = str(llm_cfg.get("llama_simple_bin", default_bin))
        
        # Inference settings optimized for Pi4
        max_tokens = int(llm_cfg.get("local_max_tokens", 60))  # Short responses
        timeout_s = int(llm_cfg.get("local_timeout_seconds", 45))  # Allow up to 45s
        
        self.cfg = LocalLLMConfig(
            model_path=model_path,
            llama_simple_bin=llama_bin,
            max_tokens=max_tokens,
            timeout_seconds=timeout_s,
        )
        
        # Verify paths exist
        if not Path(self.cfg.model_path).exists():
            raise RuntimeError(f"Model not found: {self.cfg.model_path}")
        if not Path(self.cfg.llama_simple_bin).exists():
            raise RuntimeError(f"llama-simple not found: {self.cfg.llama_simple_bin}")
        
        # Set LD_LIBRARY_PATH for llama.cpp libs
        lib_path = str(Path(self.cfg.llama_simple_bin).parent)
        current_ld = os.environ.get("LD_LIBRARY_PATH", "")
        os.environ["LD_LIBRARY_PATH"] = f"{lib_path}:{current_ld}"

        self.ctx = zmq.Context.instance()
        self.sub = make_subscriber(self.config, topic=TOPIC_LLM_REQ, channel="downstream")
        self.pub = make_publisher(self.config, channel="upstream")
        self._running = True

        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        self.logger.info(
            "LocalLLMRunner initialized (model=%s, max_tok=%d, timeout=%ds)",
            Path(self.cfg.model_path).name, self.cfg.max_tokens, self.cfg.timeout_seconds
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
        self.logger.info("LocalLLMRunner shutting down")

    def _build_prompt(self, user_text: str) -> str:
        """Build chat prompt with TinyLlama ChatML template."""
        return f"<|system|>\n{SYSTEM_PROMPT}</s>\n<|user|>\n{user_text}</s>\n<|assistant|>\n"

    def _call_llama(self, prompt: str) -> tuple[str, int]:
        """Call llama-simple subprocess and get response."""
        cmd = [
            self.cfg.llama_simple_bin,
            "-m", self.cfg.model_path,
            "-p", prompt,
            "-n", str(self.cfg.max_tokens),
        ]
        
        # Prepare environment with library path
        env = os.environ.copy()
        lib_path = str(Path(self.cfg.llama_simple_bin).parent)
        env["LD_LIBRARY_PATH"] = f"{lib_path}:{env.get('LD_LIBRARY_PATH', '')}"
        
        start = time.time()
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.cfg.timeout_seconds,
                env=env,
            )
            latency_ms = int((time.time() - start) * 1000)
            
            if result.returncode != 0:
                self.logger.error("llama-simple failed: %s", result.stderr[:200])
                return "", latency_ms
            
            # Extract response text from output (skip debug lines)
            output = result.stdout
            
            # llama-simple outputs the generated text after the prompt
            # Look for the assistant response after </s><|assistant|>
            if "<|assistant|>" in output:
                response = output.split("<|assistant|>")[-1]
            else:
                # Filter out debug lines (starting with llama_, graph_, ~llama_)
                lines = output.split("\n")
                response_lines = []
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    # Skip llama.cpp debug output
                    if any(line.startswith(prefix) for prefix in [
                        "llama_", "graph_", "~llama_", "<s>", "main:", "Loading"
                    ]):
                        continue
                    response_lines.append(line)
                response = " ".join(response_lines)
            
            # Clean up response
            response = response.replace("</s>", "").strip()
            # Remove any remaining prompt echoes
            if prompt in response:
                response = response.replace(prompt, "").strip()
            
            return response, latency_ms
            
        except subprocess.TimeoutExpired:
            latency_ms = int((time.time() - start) * 1000)
            self.logger.warning("llama-simple timeout after %dms", latency_ms)
            return "", latency_ms
        except Exception as e:
            latency_ms = int((time.time() - start) * 1000)
            self.logger.error("llama-simple error: %s", e)
            return "", latency_ms

    def _fallback_response(self, user_text: str) -> Dict[str, Any]:
        """Generate simple fallback response if LLM fails."""
        user_lower = user_text.lower()
        
        # Simple keyword matching for basic responses
        if any(w in user_lower for w in ["hello", "hi ", "hey "]):
            speak = "Hello! I'm ROBO, your robot assistant. How can I help?"
        elif any(w in user_lower for w in ["how are you", "how're you"]):
            speak = "I'm doing great! Ready to help you."
        elif any(w in user_lower for w in ["forward", "go ahead", "move forward"]):
            return {"speak": "Moving forward!", "direction": "forward", "track": ""}
        elif any(w in user_lower for w in ["back", "reverse", "backward"]):
            return {"speak": "Moving backward!", "direction": "backward", "track": ""}
        elif "left" in user_lower:
            return {"speak": "Turning left!", "direction": "left", "track": ""}
        elif "right" in user_lower:
            return {"speak": "Turning right!", "direction": "right", "track": ""}
        elif any(w in user_lower for w in ["stop", "halt", "wait"]):
            return {"speak": "Stopping now!", "direction": "stop", "track": ""}
        elif any(w in user_lower for w in ["who are you", "your name", "what are you"]):
            speak = "I'm ROBO, a smart car robot assistant!"
        elif any(w in user_lower for w in ["thank", "thanks"]):
            speak = "You're welcome!"
        elif any(w in user_lower for w in ["bye", "goodbye"]):
            speak = "Goodbye! Have a great day!"
        elif any(w in user_lower for w in ["time", "what time"]):
            import datetime
            now = datetime.datetime.now().strftime("%I:%M %p")
            speak = f"The time is {now}."
        else:
            speak = "I heard you! Is there something specific you'd like me to do?"
        
        return {"speak": speak, "direction": "stop", "track": ""}

    def run(self) -> None:
        self.logger.info("LocalLLMRunner listening on %s", TOPIC_LLM_REQ)
        print(f"🤖 Local LLM ready! Model: {Path(self.cfg.model_path).name}", flush=True)
        print(f"   Max tokens: {self.cfg.max_tokens}, Timeout: {self.cfg.timeout_seconds}s", flush=True)
        
        while self._running:
            try:
                topic, payload = self.sub.recv_multipart()
            except Exception as exc:
                self.logger.error("ZMQ recv failed: %s", exc)
                time.sleep(0.5)
                continue

            try:
                msg = json.loads(payload)
            except Exception as exc:
                self.logger.error("Invalid llm.request payload: %s", exc)
                continue

            user_text = str(msg.get("text", "")).strip()
            if not user_text:
                self.logger.warning("Empty user text in llm.request; skipping")
                continue

            self.logger.info("Processing: %s", user_text[:100])
            print(f"🧠 User: {user_text[:60]}...", flush=True)

            # Try local LLM
            prompt = self._build_prompt(user_text)
            raw_response, latency_ms = self._call_llama(prompt)
            
            if raw_response:
                self.logger.info("LLM response (%dms): %s", latency_ms, raw_response[:100])
                parsed = {
                    "speak": raw_response[:300],  # Limit length
                    "direction": "stop",
                    "track": "",
                }
                ok = True
            else:
                # Fallback to simple responses
                self.logger.warning("Using fallback response (LLM timeout/error)")
                parsed = self._fallback_response(user_text)
                ok = True
                raw_response = parsed["speak"]

            resp_payload = {
                "ok": ok,
                "json": parsed,
                "raw": raw_response,
                "latency_ms": latency_ms,
                "local": True,  # Flag indicating local LLM was used
            }
            publish_json(self.pub, TOPIC_LLM_RESP, resp_payload)
            
            speak = parsed.get("speak", "")[:60]
            print(f"💬 ROBO: {speak}", flush=True)
            self.logger.info("Published llm.response ok=%s", ok)


def main() -> None:
    try:
        runner = LocalLLMRunner()
    except Exception as exc:
        print(f"[llm.local] Fatal startup error: {exc}", file=sys.stderr)
        sys.exit(1)
    try:
        runner.run()
    except KeyboardInterrupt:
        runner.shutdown()
    except Exception as exc:
        runner.logger.error("Unhandled exception in LocalLLMRunner: %s", exc)
        runner.shutdown()
        sys.exit(1)


if __name__ == "__main__":
    main()
