"""Offline llama.cpp server orchestrator bridging ZMQ requests to HTTP completions."""
from __future__ import annotations

import argparse
import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import zmq

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.config_loader import load_config
from src.core.ipc import (
    TOPIC_LLM_REQ,
    TOPIC_LLM_RESP,
    make_publisher,
    make_subscriber,
    publish_json,
)
from src.core.logging_setup import get_logger


class LlamaServerManager:
    """Supervise llama-server process and restart if it crashes."""

    def __init__(
        self,
        *,
        bin_path: Path,
        model_path: Path,
        host: str,
        port: int,
        ctx_size: int,
        log_dir: Path,
        gpu_layers: int = 0,
        threads: int = 4,
    ) -> None:
        self.bin_path = bin_path
        self.model_path = model_path
        self.host = host
        self.port = port
        self.ctx_size = ctx_size
        self.gpu_layers = gpu_layers
        self.threads = threads
        self.log_dir = log_dir
        self.proc: Optional[subprocess.Popen[bytes]] = None
        self.log_file_path = log_dir / "llama-server.log"
        self._log_handle: Optional[Any] = None

    def start(self) -> None:
        if not self.bin_path.exists():
            raise FileNotFoundError(f"llama-server binary missing: {self.bin_path}")
        if not self.model_path.exists():
            raise FileNotFoundError(f"LLM model missing: {self.model_path}")
        self.stop()
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._log_handle = self.log_file_path.open("ab", buffering=0)
        cmd = [
            str(self.bin_path),
            "--model",
            str(self.model_path),
            "--ctx-size",
            str(self.ctx_size),
            "--n-gpu-layers",
            str(self.gpu_layers),
            "--threads",
            str(self.threads),
            "--host",
            self.host,
            "--port",
            str(self.port),
            "--temp",
            "0.7",
        ]
        self.proc = subprocess.Popen(
            cmd,
            stdout=self._log_handle,
            stderr=subprocess.STDOUT,
            cwd=str(self.bin_path.parent),
        )
        self._wait_until_ready()

    def ensure(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            self.start()

    def restart(self) -> None:
        self.stop()
        time.sleep(1.0)
        self.start()

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        if self._log_handle:
            self._log_handle.close()
            self._log_handle = None

    def _wait_until_ready(self, timeout: float = 30.0) -> None:
        # Wait until the server accepts TCP and returns a successful /completion response.
        # Server may accept TCP early but still return HTTP 503 while loading the model,
        # so we probe the HTTP endpoint until it returns 200 or the timeout expires.
        deadline = time.time() + timeout
        backoff = 0.5
        while time.time() < deadline:
            if self.proc and self.proc.poll() is not None:
                raise RuntimeError("llama-server exited during startup")
            # First ensure TCP connect succeeds
            try:
                with socket.create_connection((self.host, self.port), timeout=1.0):
                    pass
            except OSError:
                time.sleep(backoff)
                backoff = min(backoff * 1.5, 5.0)
                continue

            # Then attempt an HTTP probe to /completion. If it returns 200 we are ready.
            try:
                conn = http.client.HTTPConnection(self.host, self.port, timeout=10)
                probe_body = json.dumps({"prompt": "", "n_predict": 1, "stream": False, "cache_prompt": False})
                conn.request("POST", "/completion", body=probe_body, headers={"Content-Type": "application/json"})
                resp = conn.getresponse()
                resp.read()
                conn.close()
                if resp.status == 200:
                    return
                # If server is still loading model it may return 503; wait and retry.
                self._log_handle and self._log_handle.write(f"llama-server probe status {resp.status}\n".encode())
            except Exception:
                # ignore and retry until timeout
                pass

            time.sleep(backoff)
            backoff = min(backoff * 1.5, 5.0)

        raise TimeoutError("llama-server did not become ready before timeout")


class LLMRunner:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.config = load_config(Path("config/system.yaml"))
        log_dir = Path(self.config.get("logs", {}).get("directory", "logs"))
        self.logger = get_logger("llm.runner", log_dir)

        llm_cfg = self.config.get("llm", {})
        self.host = args.host or llm_cfg.get("host", "127.0.0.1")
        self.port = int(args.port or llm_cfg.get("port", 8080))
        self.max_context = int(llm_cfg.get("max_context", 2048))
        self.max_predict = int(llm_cfg.get("max_predict", 256))
        self.model_path = Path(args.model or llm_cfg.get("model_path", "models/llm/model.gguf"))
        self.bin_path = Path(llm_cfg.get("server_bin", PROJECT_ROOT / "third_party/llama.cpp/bin/llama-server"))
        self.gpu_layers = int(llm_cfg.get("gpu_layers", 0))
        self.threads = int(llm_cfg.get("threads", 4))

        self.server = LlamaServerManager(
            bin_path=self.bin_path,
            model_path=self.model_path,
            host=self.host,
            port=self.port,
            ctx_size=self.max_context,
            gpu_layers=self.gpu_layers,
            threads=self.threads,
            log_dir=log_dir,
        )
        self._running = True
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        if args.ipc:
            os.environ["IPC_UPSTREAM"] = args.ipc

        self.pub = make_publisher(self.config, channel="upstream")
        self.sub = make_subscriber(self.config, topic=TOPIC_LLM_REQ, channel="downstream")

    def _handle_signal(self, *_: int) -> None:
        self.shutdown()
        sys.exit(0)

    def shutdown(self) -> None:
        self.logger.info("Shutting down LLM runner")
        self.server.stop()
        self._running = False
        try:
            self.sub.close(0)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.pub.close(0)
        except Exception:  # noqa: BLE001
            pass

    def truncate_prompt(self, prompt: str) -> str:
        if len(prompt) <= self.max_context:
            return prompt
        return prompt[-self.max_context :]

    def _format_prompt(self, user_prompt: str) -> str:
        """Wrap user text in a short system instruction to encourage a direct reply.

        Some GGUF chat models expect an instruction-style prompt (with SYS/INST markers)
        or a system message. Wrapping improves chance of getting a plain-text answer
        instead of an empty content payload from the http server.
        """
        sys_instr = (
            "You are an offline assistant. Answer concisely and directly. "
            "If a short factual answer exists, return only that answer."
        )
        # Use the [INST] chat wrapper which llama.cpp recognizes for many chat models.
        return f"[INST] <<SYS>>{sys_instr} <</SYS>>\nUser: {user_prompt}\n[/INST]"

    def request_completion(self, prompt: str) -> Tuple[str, int, int]:
        self.server.ensure()
        attempts = 0
        last_err: Optional[Exception] = None
        max_attempts = 5
        backoff = 0.5
        while attempts < max_attempts:
            attempts += 1
            start = time.time()
            try:
                text, tokens = self._call_llama_server(prompt)
                latency_ms = int((time.time() - start) * 1000)
                return text, tokens, latency_ms
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                msg = str(exc)
                # If the server is still loading the model it may return HTTP 503.
                # In that case, wait and retry without force-restarting the server.
                if "HTTP 503" in msg or "Loading model" in msg:
                    self.logger.warning("LLM not ready yet (attempt %d/%d): %s", attempts, max_attempts, msg)
                    time.sleep(backoff)
                    backoff = min(backoff * 1.8, 5.0)
                    continue

                self.logger.error("LLM request failed (attempt %d/%d): %s", attempts, max_attempts, exc)
                # For other errors, try restarting the server and retry.
                try:
                    self.server.restart()
                except Exception:
                    pass
                time.sleep(backoff)
                backoff = min(backoff * 1.8, 5.0)
        raise RuntimeError(f"llama-server failed after retries: {last_err}")

    def _call_llama_server(self, prompt: str) -> Tuple[str, int]:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=120)
        body = json.dumps(
            {
                "prompt": prompt,
                "n_predict": self.max_predict,
                "stream": False,
                "cache_prompt": True,
            }
        )
        conn.request("POST", "/completion", body=body, headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        payload = response.read()
        conn.close()
        if response.status != 200:
            raise RuntimeError(f"llama-server HTTP {response.status}: {payload.decode('utf-8', 'ignore')}")
        data = json.loads(payload)
        text = self._extract_text(data)
        tokens = self._extract_tokens(data)
        return text.strip(), tokens

    @staticmethod
    def _extract_text(data: Dict[str, Any]) -> str:
        content = data.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join([chunk.get("text", "") for chunk in content])
        return str(content or "")

    @staticmethod
    def _extract_tokens(data: Dict[str, Any]) -> int:
        if isinstance(data.get("tokens_predicted"), int):
            return int(data["tokens_predicted"])
        if isinstance(data.get("token_ids"), list):
            return len(data["token_ids"])
        token_usage = data.get("token_usage") or {}
        if isinstance(token_usage, dict) and isinstance(token_usage.get("completion_tokens"), int):
            return int(token_usage["completion_tokens"])
        content = data.get("content")
        if isinstance(content, str):
            return max(1, len(content.split()))
        return 0

    def run(self) -> None:
        self.logger.info("LLM runner listening for requests")
        while self._running:
            try:
                topic, raw = self.sub.recv_multipart()
            except zmq.ZMQError:
                if not self._running:
                    break
                raise
            if topic != TOPIC_LLM_REQ:
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                self.logger.error("Invalid JSON payload: %s", raw)
                continue
            prompt = str(payload.get("text", "")).strip()
            if not prompt:
                continue
            truncated = self.truncate_prompt(prompt)
            formatted = self._format_prompt(truncated)
            try:
                text, tokens, latency_ms = self.request_completion(formatted)
            except Exception as exc:  # noqa: BLE001
                self.logger.exception("Failed to service LLM request: %s", exc)
                continue
            response_payload = {
                "timestamp": int(time.time()),
                "text": text,
                "tokens": tokens,
                "latency_ms": latency_ms,
            }
            publish_json(self.pub, TOPIC_LLM_RESP, response_payload)
            if self.args.debug:
                print(json.dumps(response_payload, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline LLM runner controlling llama.cpp server")
    parser.add_argument("--model", help="Override model path")
    parser.add_argument("--ipc", help="Override upstream IPC address")
    parser.add_argument("--host", help="HTTP host for llama-server")
    parser.add_argument("--port", type=int, help="HTTP port for llama-server")
    parser.add_argument("--debug", action="store_true", help="Print responses to stdout")
    args = parser.parse_args()

    runner = LLMRunner(args)
    runner.run()


if __name__ == "__main__":
    main()
