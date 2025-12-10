"""LLM server using llama.cpp binary; JSON-only intent extraction.

Listens: TOPIC_LLM_REQ {"text": str, "context": dict}
Publishes: TOPIC_LLM_RESP {"ok": bool, "json": dict, "raw": str}
"""
from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from src.core.ipc import make_subscriber, make_publisher, publish_json, TOPIC_LLM_REQ, TOPIC_LLM_RESP
from src.core.logging_setup import get_logger
from src.core.config_loader import load_config


SYSTEM_PROMPT = (
    "You are an intent engine. Respond ONLY with strict JSON, no prose. "
    "Schema: {intent: string, slots: object, speak: string}. If unclear, set intent='clarify' and ask concise question in 'speak'."
)


@dataclass
class LlamaCfg:
    bin_path: Path
    model_path: Path
    threads: int
    ctx: int


def build_prompt(user_text: str) -> str:
    return (
        f"[INST] <<SYS>>{SYSTEM_PROMPT} <</SYS>>\n"
        f"User: {user_text}\n"
        f"Reply with JSON only. [/INST]"
    )


def extract_json(s: str) -> dict | None:
    m = re.search(r"\{.*\}", s, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def run() -> None:
    cfg = load_config(Path("config/system.yaml"))
    llm = cfg["llm"]
    lc = LlamaCfg(
        bin_path=Path(llm.get("bin_path", "./third_party/llama.cpp/main")),
        model_path=Path(llm["model_path"]),
        threads=int(llm.get("threads", 4)),
        ctx=int(llm.get("context_tokens", 2048)),
    )
    logger = get_logger("llm.server", Path(cfg.get("logs", {}).get("directory", "logs")))
    sub = make_subscriber(cfg, topic=TOPIC_LLM_REQ, channel="downstream")
    pub = make_publisher(cfg, channel="upstream")

    if not lc.bin_path.exists() or not lc.model_path.exists():
        logger.error("llama.cpp binary or model missing: %s, %s", lc.bin_path, lc.model_path)
        sys.exit(1)

    logger.info("LLM server ready on %s", TOPIC_LLM_REQ)
    while True:
        topic, payload = sub.recv_multipart()
        try:
            msg = json.loads(payload)
            text = str(msg.get("text", "")).strip()
            if not text:
                continue
        except Exception as e:  # noqa: BLE001
            logger.error("Bad LLM_REQ payload: %s", e)
            continue

        prompt = build_prompt(text)
        cmd = [
            str(lc.bin_path),
            "-m",
            str(lc.model_path),
            "-t",
            str(lc.threads),
            "-c",
            str(lc.ctx),
            "-n",
            "256",
            "-p",
            prompt,
        ]
        logger.info("Running llama.cpp for prompt len=%d", len(prompt))
        timeout_sec = int(llm.get("timeout", 180))
        try:
            completed = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout_sec,
            )
            out = completed.stdout
        except subprocess.TimeoutExpired as te:
            logger.error("llama.cpp timed out after %ds", timeout_sec)
            publish_json(pub, TOPIC_LLM_RESP, {"ok": False, "json": {}, "raw": f"TIMEOUT: {te}"})
            continue
        js = extract_json(out)
        ok = js is not None
        resp = {"ok": ok, "json": js or {}, "raw": out}
        publish_json(pub, TOPIC_LLM_RESP, resp)
        logger.info("Published LLM response ok=%s", ok)


if __name__ == "__main__":
    run()
