"""Interactive terminal chat that talks to the llama.cpp runner and triggers TTS."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import zmq

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.config_loader import load_config
from src.core.ipc import (
    TOPIC_LLM_REQ,
    TOPIC_LLM_RESP,
    TOPIC_TTS,
    make_publisher,
    make_subscriber,
    publish_json,
)


def prompt_loop(args: argparse.Namespace) -> None:
    cfg = load_config(Path("config/system.yaml"))
    downstream_pub = make_publisher(cfg, channel="downstream")
    upstream_sub = make_subscriber(cfg, topic=TOPIC_LLM_RESP, channel="upstream")
    poller = zmq.Poller()
    poller.register(upstream_sub, zmq.POLLIN)

    print("Type a prompt and press Enter. Use /exit to quit.\n")
    while True:
        try:
            user_text = input("You> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting chat CLI.")
            break
        if not user_text:
            continue
        if user_text in {"/exit", "/quit"}:
            break
        if args.direct:
            # call llama-server HTTP endpoint directly for quick testing
            try:
                import http.client, json
                host = args.host or cfg.get('llm', {}).get('host', '127.0.0.1')
                port = int(args.port or cfg.get('llm', {}).get('port', 8080))
                conn = http.client.HTTPConnection(host, port, timeout=120)
                body = json.dumps({"prompt": user_text, "n_predict": 256, "stream": False, "cache_prompt": False})
                conn.request('POST', '/completion', body=body, headers={'Content-Type': 'application/json'})
                resp = conn.getresponse(); data = resp.read(); conn.close()
                try:
                    parsed = json.loads(data)
                    text = parsed.get('content') or parsed.get('text') or ''
                    if isinstance(text, list):
                        text = ''.join([chunk.get('text','') for chunk in text])
                except Exception:
                    text = data.decode('utf-8', 'ignore')
                print(f"LLM> {str(text).strip() or '<empty response>'}\n")
                publish_json(downstream_pub, TOPIC_TTS, {"text": text})
                continue
            except Exception as e:
                print(f"Direct HTTP call failed: {e}")
                continue
        payload = {"text": user_text}
        publish_json(downstream_pub, TOPIC_LLM_REQ, payload)
        print("…waiting for LLM response…")
        response = _wait_for_response(poller, upstream_sub, timeout=args.timeout)
        if response is None:
            print("LLM did not respond within timeout. Try again.")
            continue
        text = response.get("text", "").strip() or "<empty response>"
        print(f"LLM> {text}\n")
        publish_json(downstream_pub, TOPIC_TTS, {"text": text})
        if args.tts_wait:
            print("TTS event published. Waiting for completion…")
            if _wait_for_tts_done(cfg, text, timeout=args.timeout):
                print("(TTS completed)\n")
            else:
                print("(No TTS completion observed)\n")


def _wait_for_response(poller: zmq.Poller, sock: zmq.Socket, timeout: float) -> dict | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        events = dict(poller.poll(200))
        if sock in events:
            topic, data = sock.recv_multipart()
            if topic != TOPIC_LLM_RESP:
                continue
            try:
                return json.loads(data)
            except json.JSONDecodeError:
                continue
    return None


def _wait_for_tts_done(cfg: dict, text: str, timeout: float) -> bool:
    sub = make_subscriber(cfg, topic=TOPIC_TTS, channel="upstream")
    poller = zmq.Poller()
    poller.register(sub, zmq.POLLIN)
    deadline = time.time() + timeout
    while time.time() < deadline:
        events = dict(poller.poll(200))
        if sub in events:
            topic, data = sub.recv_multipart()
            if topic != TOPIC_TTS:
                continue
            try:
                payload = json.loads(data)
            except json.JSONDecodeError:
                continue
            if payload.get("done"):
                sub.close(0)
                return True
    sub.close(0)
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Manual chat loop via llama.cpp runner")
    parser.add_argument("--timeout", type=float, default=30.0, help="Seconds to wait for responses")
    parser.add_argument(
        "--tts-wait",
        action="store_true",
        help="Subscribe for TTS completion messages before accepting new prompts",
    )
    parser.add_argument("--direct", action="store_true", help="Call llama-server HTTP directly instead of IPC")
    parser.add_argument("--host", help="llama-server host for direct mode")
    parser.add_argument("--port", type=int, help="llama-server port for direct mode")
    parser.add_argument("--prompt", help="If provided, send a single prompt and exit (works with --direct)")
    args = parser.parse_args()
    if args.prompt:
        # single-shot prompt path
        if not args.direct:
            print("--prompt requires --direct (HTTP) mode")
            return
        cfg = load_config(Path("config/system.yaml"))
        try:
            import http.client, json, time
            host = args.host or cfg.get('llm', {}).get('host', '127.0.0.1')
            port = int(args.port or cfg.get('llm', {}).get('port', 8080))
            conn = http.client.HTTPConnection(host, port, timeout=120)
            body = json.dumps({"prompt": args.prompt, "n_predict": 256, "stream": False, "cache_prompt": False})
            t0 = time.time()
            conn.request('POST', '/completion', body=body, headers={'Content-Type': 'application/json'})
            resp = conn.getresponse(); data = resp.read(); conn.close()
            latency_ms = int((time.time() - t0) * 1000)
            try:
                parsed = json.loads(data)
                text = parsed.get('content') or parsed.get('text') or ''
                if isinstance(text, list):
                    text = ''.join([chunk.get('text','') for chunk in text])
            except Exception:
                text = data.decode('utf-8', 'ignore')
            print(f"LLM (latency {latency_ms} ms):\n{text}\n")
        except Exception as e:
            print(f"Direct HTTP call failed: {e}")
        return
    prompt_loop(args)


if __name__ == "__main__":
    main()
