"""Piper TTS runner: subscribes to TOPIC_TTS and plays audio via ALSA (aplay)."""
from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

import time

from src.core.ipc import make_subscriber, make_publisher, publish_json, TOPIC_TTS
from src.core.logging_setup import get_logger
from src.core.config_loader import load_config


def run() -> None:
    cfg = load_config(Path("config/system.yaml"))
    tts = cfg["tts"]
    bin_path = Path(tts.get("bin_path", "/usr/local/bin/piper"))
    model_path = Path(tts["model_path"])
    playback = tts.get("playback", "aplay")
    logger = get_logger("tts.piper", Path(cfg.get("logs", {}).get("directory", "logs")))

    if not bin_path.exists():
        logger.error("piper binary not found at %s", bin_path)
        sys.exit(1)
    if not model_path.exists():
        logger.error("piper model not found at %s", model_path)
        sys.exit(1)

    sub = make_subscriber(cfg, topic=TOPIC_TTS, channel="downstream")
    pub = make_publisher(cfg, channel="upstream")
    logger.info("Piper TTS listening for messages on %s", TOPIC_TTS)
    while True:
        topic, data = sub.recv_multipart()
        try:
            import json

            msg = json.loads(data)
            text = msg["text"].strip()
        except Exception as e:  # noqa: BLE001
            logger.error("Invalid TTS payload: %s", e)
            continue
        if not text:
            continue
        # Run piper and pipe to aplay
        cmd = f"{shlex.quote(str(bin_path))} -m {shlex.quote(str(model_path))} -f -"
        logger.info("Speaking %d chars", len(text))
        publish_json(pub, TOPIC_TTS, {"started": True})
        # Use plughw:0,0 (BCM headphone jack) to avoid USB device conflict with mic capture
        playback_device = tts.get("playback_device", "plughw:0,0")
        with subprocess.Popen(
            shlex.split(cmd), stdin=subprocess.PIPE, stdout=subprocess.PIPE
        ) as p_piper:
            assert p_piper.stdin is not None and p_piper.stdout is not None
            p_piper.stdin.write(text.encode("utf-8"))
            p_piper.stdin.close()
            with subprocess.Popen([playback, "-q", "-D", playback_device], stdin=p_piper.stdout):
                p_piper.wait()
        publish_json(pub, TOPIC_TTS, {"done": True, "timestamp": int(time.time())})


if __name__ == "__main__":
    run()
