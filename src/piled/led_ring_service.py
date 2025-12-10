"""Orchestrator-aware LED ring controller for the 8-pixel NeoPixel ring.

This service listens to upstream (wakeword/STT/LLM/TTS/health) and
 downstream (command) ZeroMQ topics so the hardware reflects which stage
 of the voice pipeline is currently active. It replaces the legacy
 `src.ui.led_status_runner` implementation and is designed to run under
 the `.venvs/visn` virtual environment where the NeoPixel stack is pre-
 installed.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import zmq

try:  # Hardware modules are only present on the Pi
    import board  # type: ignore
    import neopixel  # type: ignore
except Exception:  # pragma: no cover - desktop devs
    board = None  # type: ignore
    neopixel = None  # type: ignore

from src.core.config_loader import load_config
from src.core.ipc import (
    TOPIC_CMD_LISTEN_START,
    TOPIC_CMD_LISTEN_STOP,
    TOPIC_HEALTH,
    TOPIC_LLM_REQ,
    TOPIC_LLM_RESP,
    TOPIC_STT,
    TOPIC_TTS,
    TOPIC_WW_DETECTED,
    make_subscriber,
)
from src.core.logging_setup import get_logger


RGB = tuple[int, int, int]


class LedRingHardware:
    """Thin wrapper around the NeoPixel ring to centralize error handling."""

    def __init__(
        self,
        *,
        pixel_pin_attr: str,
        pixel_count: int,
        brightness: float,
        dry_run: bool,
        logger,
    ) -> None:
        self.logger = logger
        self.pixel_count = pixel_count
        self._pixels = None
        self._dry_run = dry_run
        if dry_run:
            self.logger.warning("LED ring running in dry-run mode (no hardware)")
            return
        if board is None or neopixel is None:
            self.logger.error("Board/NeoPixel modules unavailable; forcing dry-run mode")
            self._dry_run = True
            return
        try:
            pixel_pin = getattr(board, pixel_pin_attr)
            order = getattr(neopixel, "GRB", neopixel.GRB)
            self._pixels = neopixel.NeoPixel(
                pixel_pin,
                pixel_count,
                brightness=brightness,
                auto_write=False,
                pixel_order=order,
            )
            self.logger.info(
                "LED ring initialized (%s pixels on %s, brightness %.2f)",
                pixel_count,
                pixel_pin_attr,
                brightness,
            )
        except Exception as exc:  # pragma: no cover - hardware failures
            self.logger.error("Failed to init NeoPixel ring: %s", exc)
            self._dry_run = True
            self._pixels = None

    def ready(self) -> bool:
        return (self._pixels is not None) and (not self._dry_run)

    def show(self, colors: list[RGB]) -> None:
        if not self.ready():
            return
        if not colors:
            return
        for idx in range(min(len(colors), self.pixel_count)):
            r, g, b = colors[idx]
            self._pixels[idx] = (
                max(0, min(255, int(r))),
                max(0, min(255, int(g))),
                max(0, min(255, int(b))),
            )
        self._pixels.show()

    def fill(self, color: RGB) -> None:
        if not self.ready():
            return
        self._pixels.fill(tuple(max(0, min(255, int(c))) for c in color))
        self._pixels.show()

    def clear(self) -> None:
        if not self.ready():
            return
        self._pixels.fill((0, 0, 0))
        self._pixels.show()


class LedAnimator:
    """Generates color frames for each orchestrator state."""

    def __init__(self, hardware: LedRingHardware) -> None:
        self.hw = hardware
        self.current_state = "idle"
        self.desired_state = "idle"
        self._fallback_deadline: Optional[float] = None
        self._fallback_state: str = "idle"
        self._last_render = 0.0

    def set_state(self, state: str, *, hold: Optional[float] = None, fallback: Optional[str] = None) -> None:
        self.desired_state = state
        if state == self.current_state and hold is None:
            return
        self.current_state = state
        if hold:
            self._fallback_deadline = time.time() + hold
            self._fallback_state = fallback or "idle"
        else:
            self._fallback_deadline = None
        self._last_render = 0.0  # force immediate refresh

    def _maybe_fallback(self, now: float) -> None:
        if self._fallback_deadline and now >= self._fallback_deadline:
            self._fallback_deadline = None
            self.set_state(self._fallback_state)

    def step(self, now: float) -> None:
        if now - self._last_render < 1.0 / 60.0:
            return
        self._maybe_fallback(now)
        renderer = getattr(self, f"_render_{self.current_state}", self._render_idle)
        renderer(now)
        self._last_render = now

    def _render_idle(self, now: float) -> None:
        phase = 0.5 + 0.5 * math.sin(now * 1.5)
        level = int(8 + 40 * phase)
        self.hw.fill((0, level, level + 5))

    def _render_wakeword(self, now: float) -> None:
        on = int(now * 8) % 2 == 0
        color = (120, 70, 0) if on else (0, 0, 0)
        self.hw.fill(color)

    def _render_listening(self, now: float) -> None:
        pos = (now * 6) % self.hw.pixel_count
        colors: list[RGB] = []
        for idx in range(self.hw.pixel_count):
            delta = min((idx - pos) % self.hw.pixel_count, (pos - idx) % self.hw.pixel_count)
            fade = max(0.0, 1.0 - delta / 2.5)
            value = int(25 + 120 * fade)
            colors.append((0, 0, value))
        self.hw.show(colors)

    def _render_llm(self, now: float) -> None:
        colors: list[RGB] = []
        for idx in range(self.hw.pixel_count):
            phase = math.sin(now * 2 + idx)
            colors.append((int(50 + 40 * phase), int(5 + 15 * phase), int(90 + 60 * (1 - phase))))
        self.hw.show(colors)

    def _render_tts_queue(self, now: float) -> None:
        # Short loader before audio starts
        sweep = (now * 5) % self.hw.pixel_count
        colors: list[RGB] = []
        for idx in range(self.hw.pixel_count):
            hit = 1.0 - min(abs(idx - sweep), self.hw.pixel_count - abs(idx - sweep)) / self.hw.pixel_count
            colors.append((int(20 + 80 * hit), int(40 + 80 * hit), 0))
        self.hw.show(colors)

    def _render_speaking(self, now: float) -> None:
        phase = 0.5 + 0.5 * math.sin(now * 4)
        level = int(40 + 150 * phase)
        self.hw.fill((0, level, 10))

    def _render_error(self, now: float) -> None:
        on = int(now * 4) % 2 == 0
        self.hw.fill((150, 0, 0) if on else (0, 0, 0))


class LedRingService:
    def __init__(
        self,
        *,
        cfg_path: Path,
        pixel_pin: str,
        pixels: int,
        brightness: float,
        dry_run: bool = False,
    ) -> None:
        self.config = load_config(cfg_path)
        root = Path(os.environ.get("PROJECT_ROOT", Path.cwd()))
        log_dir = self.config.get("logs", {}).get("directory", "logs")
        log_path = root / log_dir if not Path(log_dir).is_absolute() else Path(log_dir)
        log_path.mkdir(parents=True, exist_ok=True)
        self.logger = get_logger("piled.led", log_path)

        self.hardware = LedRingHardware(
            pixel_pin_attr=pixel_pin,
            pixel_count=pixels,
            brightness=brightness,
            dry_run=dry_run,
            logger=self.logger,
        )
        self.animator = LedAnimator(self.hardware)
        self.animator.set_state("idle")
        self.desired_state = "idle"
        self.flags: Dict[str, bool] = {
            "stt_active": False,
            "llm_active": False,
            "tts_active": False,
            "error": False,
        }

        self.ctx = zmq.Context.instance()
        self.sub_upstream = self._make_subscriber(
            "upstream",
            [TOPIC_WW_DETECTED, TOPIC_STT, TOPIC_LLM_RESP, TOPIC_TTS, TOPIC_HEALTH],
        )
        self.sub_downstream = self._make_subscriber(
            "downstream",
            [TOPIC_CMD_LISTEN_START, TOPIC_CMD_LISTEN_STOP, TOPIC_LLM_REQ, TOPIC_TTS],
        )
        self.poller = zmq.Poller()
        self.poller.register(self.sub_upstream, zmq.POLLIN)
        self.poller.register(self.sub_downstream, zmq.POLLIN)

    def _make_subscriber(self, channel: str, topics: Iterable[bytes]) -> zmq.Socket:
        topics = list(topics)
        first = topics[0] if topics else b""
        sock = make_subscriber(self.config, channel=channel, topic=first)
        for topic in topics[1:]:
            sock.setsockopt(zmq.SUBSCRIBE, topic)
        return sock

    def _set_state(self, state: str, *, hold: Optional[float] = None, fallback: Optional[str] = None) -> None:
        self.desired_state = state
        if self.flags.get("error"):
            return
        self.animator.set_state(state, hold=hold, fallback=fallback)

    def _enter_error(self, reason: Dict[str, Any]) -> None:
        if self.flags["error"]:
            return
        self.flags["error"] = True
        self.logger.error("Health error: %s", reason)
        self.animator.set_state("error")

    def _clear_error(self) -> None:
        if not self.flags["error"]:
            return
        self.flags["error"] = False
        self.logger.info("Health restored; returning to %s", self.desired_state)
        self.animator.set_state(self.desired_state)

    def _handle_upstream(self, topic: bytes, payload: Dict[str, Any]) -> None:
        if topic == TOPIC_WW_DETECTED:
            self.logger.info("Wakeword detected")
            self._set_state("wakeword", hold=1.2, fallback="listening")
        elif topic == TOPIC_STT:
            # STT result implies LLM will run next
            self.flags["llm_active"] = True
            self._set_state("llm")
        elif topic == TOPIC_LLM_RESP:
            self.flags["llm_active"] = False
            if self.flags["tts_active"]:
                self._set_state("speaking")
            else:
                self._set_state("tts_queue", hold=1.0)
        elif topic == TOPIC_TTS:
            if payload.get("done"):
                self.flags["tts_active"] = False
                self._set_state("idle")
        elif topic == TOPIC_HEALTH:
            if not bool(payload.get("ok", True)):
                self._enter_error(payload)
            else:
                self._clear_error()

    def _handle_downstream(self, topic: bytes, payload: Dict[str, Any]) -> None:
        if topic == TOPIC_CMD_LISTEN_START:
            self.flags["stt_active"] = True
            self._set_state("listening")
        elif topic == TOPIC_CMD_LISTEN_STOP:
            self.flags["stt_active"] = False
            if not self.flags["llm_active"] and not self.flags["tts_active"]:
                self._set_state("idle")
        elif topic == TOPIC_LLM_REQ:
            self.flags["llm_active"] = True
            self._set_state("llm")
        elif topic == TOPIC_TTS:
            if payload.get("text"):
                self.flags["tts_active"] = True
                self._set_state("tts_queue", hold=0.5, fallback="speaking")

    def _drain(self, sock: zmq.Socket, *, upstream: bool) -> None:
        while True:
            try:
                topic, data = sock.recv_multipart(flags=zmq.NOBLOCK)
            except zmq.Again:
                break
            try:
                payload = json.loads(data)
            except json.JSONDecodeError:
                self.logger.error("Invalid JSON on topic %s", topic)
                continue
            if upstream:
                self._handle_upstream(topic, payload)
            else:
                self._handle_downstream(topic, payload)

    def run(self) -> None:
        self.logger.info("LED ring service running")
        try:
            while True:
                events = dict(self.poller.poll(50))
                if self.sub_upstream in events:
                    self._drain(self.sub_upstream, upstream=True)
                if self.sub_downstream in events:
                    self._drain(self.sub_downstream, upstream=False)
                self.animator.step(time.time())
        except KeyboardInterrupt:
            self.logger.info("LED ring service interrupted")
        finally:
            self.hardware.clear()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the orchestrator LED ring service")
    parser.add_argument("--config", default="config/system.yaml", help="Path to system config")
    parser.add_argument("--pin", default="D12", help="Board pin attribute for the NeoPixel ring")
    parser.add_argument("--pixels", type=int, default=8, help="Number of LEDs on the ring")
    parser.add_argument("--brightness", type=float, default=0.25, help="LED brightness (0-1)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Force dry-run mode (useful on dev machines without hardware)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    service = LedRingService(
        cfg_path=Path(args.config),
        pixel_pin=args.pin,
        pixels=args.pixels,
        brightness=args.brightness,
        dry_run=args.dry_run,
    )
    service.run()


if __name__ == "__main__":
    main()
