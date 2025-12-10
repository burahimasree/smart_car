"""Ensure llm_runner publishes completions by mocking llama-server with a stub HTTP endpoint."""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import zmq

from src.llm import llm_runner
from src.core.ipc import TOPIC_LLM_REQ, TOPIC_LLM_RESP


class _StubHandler(BaseHTTPRequestHandler):
    response_text = "The answer is 4."

    def do_POST(self):  # noqa: N802
        if self.path != "/completion":
            self.send_response(404)
            self.end_headers()
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length:
            self.rfile.read(length)
        payload = {
            "content": self.response_text,
            "tokens_predicted": 4,
        }
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args):  # noqa: A003, D401
        return  # silence default HTTPServer logging


def _start_stub_http_server(host: str, port: int) -> HTTPServer:
    server = HTTPServer((host, port), _StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


class DummyServerManager:
    def __init__(self, **_: object) -> None:
        self.started = False

    def ensure(self) -> None:
        self.started = True

    def restart(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False


def test_llm_runner_publishes_stub_response(monkeypatch, tmp_path):
    upstream = "tcp://127.0.0.1:6520"
    downstream = "tcp://127.0.0.1:6521"
    host = "127.0.0.1"
    port = 18090

    server = _start_stub_http_server(host, port)

    tmp_log_dir = tmp_path / "logs"
    tmp_log_dir.mkdir()
    dummy_model = tmp_path / "model.gguf"
    dummy_model.write_bytes(b"stub")

    config = {
        "ipc": {"upstream": upstream, "downstream": downstream},
        "logs": {"directory": str(tmp_log_dir)},
        "llm": {
            "engine": "llama.cpp",
            "model_path": str(dummy_model),
            "host": host,
            "port": port,
            "max_context": 128,
            "threads": 1,
            "gpu_layers": 0,
        },
    }

    monkeypatch.setattr(llm_runner, "load_config", lambda _: config)
    monkeypatch.setattr(llm_runner, "LlamaServerManager", DummyServerManager)

    ctx = zmq.Context.instance()
    req_pub = ctx.socket(zmq.PUB)
    req_pub.bind(downstream)
    resp_sub = ctx.socket(zmq.SUB)
    resp_sub.bind(upstream)
    resp_sub.setsockopt(zmq.SUBSCRIBE, TOPIC_LLM_RESP)

    args = SimpleNamespace(model=None, ipc=None, host=None, port=None, debug=False)
    runner = llm_runner.LLMRunner(args)

    thread = threading.Thread(target=runner.run, daemon=True)
    thread.start()
    time.sleep(0.2)

    req_pub.send_multipart([TOPIC_LLM_REQ, json.dumps({"text": "What is 2+2?"}).encode("utf-8")])

    poller = zmq.Poller()
    poller.register(resp_sub, zmq.POLLIN)
    result = None
    deadline = time.time() + 5
    while time.time() < deadline:
        events = dict(poller.poll(200))
        if resp_sub in events:
            topic, data = resp_sub.recv_multipart()
            if topic == TOPIC_LLM_RESP:
                result = json.loads(data)
                break
    assert result is not None, "Did not receive llm.response payload"
    assert "4" in result.get("text", "")
    assert result.get("tokens") >= 1

    runner.shutdown()
    thread.join(timeout=2)
    resp_sub.close(0)
    req_pub.close(0)
    server.shutdown()