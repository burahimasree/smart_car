"""Phase-driven LED strip controller: LED = f(Phase).

ARCHITECTURE:
    LED state is derived SOLELY from TOPIC_DISPLAY_STATE.
    No internal flags. No inference from other topics.
    The orchestrator publishes phase, we render it.

LED STATES (user-specified color scheme):
    idle (wakeword listening) - Dim cyan breathing (waiting, ready)
    wakeword_detected        - Bright GREEN flash (acknowledged!)
    listening (STT capture)  - Bright BLUE sweep (capturing audio)
    transcribing             - PURPLE pulse (STT processing)
    thinking                 - PINK pulse (LLM processing)
    tts_processing           - ORANGE pulse (generating speech)
    speaking                 - Dark GREEN chase pattern (playing audio)
    scanning                 - Hazard-style sweep
    gas_warning              - Strong amber alert
    gas_danger               - Aggressive red attention
    error                    - Solid red
    sleep                    - Dim blue/purple fade

DESIGN PRINCIPLE:
    If a human cannot tell what the system is doing
    by looking at the LEDs alone, the design is WRONG.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import time
from pathlib import Path
from typing import Optional

import zmq

try:  # Hardware modules are only present on the Pi
    import board  # type: ignore
    import neopixel  # type: ignore
except Exception:  # pragma: no cover - desktop devs
    board = None  # type: ignore
    neopixel = None  # type: ignore

from src.core.config_loader import load_config
from src.core.ipc import TOPIC_DISPLAY_STATE, TOPIC_HEALTH, make_subscriber
from src.core.logging_setup import get_logger


RGB = tuple[int, int, int]


class LedRingHardware:
    """Thin wrapper around the NeoPixel strip to centralize error handling."""

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
    """Generates color frames for each phase state.

    LED = f(Phase). Each phase has a distinct, human-recognizable pattern.

    COLOR SCHEME (user-specified):
        idle (wakeword listening) - Dim cool breathing glow
        wakeword_detected        - Bright pulse sweeping left -> right
        listening                - Same color, slower sweep
        thinking                 - 3-LED frame moving left <-> right
        speaking                 - Calm centered glow
        scanning                 - Hazard-style sweep (~70% brightness)
        gas_warning              - Strong amber alert
        gas_danger               - Aggressive red attention pattern
        error                    - Solid red
        sleep                    - Dim blue/purple fade
        forward                  - All LEDs white, full brightness
        left                     - LEDs 0-1 yellow
        right                    - LEDs 7-9 yellow
    """

    def __init__(self, hardware: LedRingHardware) -> None:
        self.hw = hardware
        self.current_state = "idle"
        self._last_render = 0.0
        self._state_entered = 0.0

    def set_state(self, state: str) -> None:
        """Set LED state. Only changes on actual state change."""
        if state != self.current_state:
            self.current_state = state
            self._last_render = 0.0  # Force immediate refresh
            self._state_entered = time.time()

    def step(self, now: float) -> None:
        """Render current state at 60fps."""
        if now - self._last_render < 1.0 / 60.0:
            return
        renderer = getattr(self, f"_render_{self.current_state}", self._render_idle)
        renderer(now)
        self._last_render = now

    def _sweep(self, now: float, color: RGB, speed: float) -> None:
        pos = (now * speed) % max(1, self.hw.pixel_count)
        colors: list[RGB] = []
        for idx in range(self.hw.pixel_count):
            delta = abs(idx - pos)
            fade = max(0.0, 1.0 - (delta / 2.5))
            r, g, b = color
            colors.append((int(r * fade), int(g * fade), int(b * fade)))
        self.hw.show(colors)

    def _ping_pong_pos(self, now: float, speed: float) -> int:
        if self.hw.pixel_count <= 1:
            return 0
        span = (self.hw.pixel_count - 1) * 2
        step = int(now * speed) % span
        return step if step < self.hw.pixel_count else span - step

    def _render_idle(self, now: float) -> None:
        """IDLE: Dim cool breathing glow."""
        phase = 0.5 + 0.5 * math.sin(now * 1.0)  # Slow breathing
        level = int(5 + 25 * phase)  # Dim: 5-30 range
        self.hw.fill((0, level, level + 3))  # Dim cyan

    def _render_wakeword_detected(self, now: float) -> None:
        """WAKEWORD: Bright pulse sweeping left -> right."""
        self._sweep(now, (0, 220, 200), speed=10.0)

    def _render_listening(self, now: float) -> None:
        """LISTENING: Same color, slower sweep."""
        self._sweep(now, (0, 220, 200), speed=4.0)

    def _render_transcribing(self, now: float) -> None:
        """TRANSCRIBING: PURPLE pulse - STT processing."""
        phase = 0.5 + 0.5 * math.sin(now * 3)  # Medium speed pulse
        r = int(100 + 80 * phase)   # Purple: R
        g = int(0 + 20 * phase)     # Minimal green
        b = int(150 + 100 * phase)  # Purple: B dominant
        self.hw.fill((r, g, b))

    def _render_thinking(self, now: float) -> None:
        """THINKING: 3-LED frame moving left <-> right."""
        pos = self._ping_pong_pos(now, speed=6.0)
        colors: list[RGB] = []
        for idx in range(self.hw.pixel_count):
            if idx in {pos - 1, pos, pos + 1}:
                colors.append((200, 80, 160))
            else:
                colors.append((0, 0, 0))
        self.hw.show(colors)

    def _render_tts_processing(self, now: float) -> None:
        """TTS PROCESSING: ORANGE pulse - generating speech."""
        phase = 0.5 + 0.5 * math.sin(now * 3)
        r = int(200 + 55 * phase)   # Orange: strong R
        g = int(80 + 60 * phase)    # Orange: medium G
        b = int(0)                   # No blue for orange
        self.hw.fill((r, g, b))

    def _render_speaking(self, now: float) -> None:
        """SPEAKING: Calm centered glow."""
        colors: list[RGB] = [(0, 0, 0)] * self.hw.pixel_count
        center_left = max(0, (self.hw.pixel_count // 2) - 1)
        center_right = min(self.hw.pixel_count - 1, center_left + 1)
        for idx in {center_left, center_right}:
            colors[idx] = (120, 120, 140)
        self.hw.show(colors)

    def _render_scanning(self, now: float) -> None:
        """SCANNING: Hazard-style sweep (~70% brightness)."""
        pos = int((now * 6) % max(1, self.hw.pixel_count))
        colors: list[RGB] = []
        for idx in range(self.hw.pixel_count):
            if idx in {pos, pos + 1}:
                colors.append((180, 140, 0))
            else:
                colors.append((10, 8, 0))
        self.hw.show(colors)

    def _render_gas_warning(self, now: float) -> None:
        """GAS WARNING: Strong amber alert."""
        phase = 0.5 + 0.5 * math.sin(now * 4)
        level = int(120 + 120 * phase)
        self.hw.fill((level, int(level * 0.7), 0))

    def _render_gas_danger(self, now: float) -> None:
        """GAS DANGER: Aggressive red attention pattern."""
        phase = 0.5 + 0.5 * math.sin(now * 8)
        level = int(140 + 115 * phase)
        self.hw.fill((level, 0, 0))

    def _render_error(self, now: float) -> None:
        """ERROR: Solid red."""
        self.hw.fill((200, 0, 0))

    def _render_sleep(self, now: float) -> None:
        """SLEEP: Dim blue/purple fade."""
        phase = 0.5 + 0.5 * math.sin(now * 0.6)
        r = int(10 + 15 * phase)
        g = int(0 + 5 * phase)
        b = int(25 + 25 * phase)
        self.hw.fill((r, g, b))

    def _render_forward(self, now: float) -> None:
        """FORWARD: All LEDs white, full brightness."""
        self.hw.fill((255, 255, 255))

    def _render_left(self, now: float) -> None:
        """LEFT: LEDs 0-1 yellow."""
        colors = [(0, 0, 0)] * self.hw.pixel_count
        for idx in range(min(2, self.hw.pixel_count)):
            colors[idx] = (255, 200, 0)
        self.hw.show(colors)

    def _render_right(self, now: float) -> None:
        """RIGHT: LEDs 7-9 yellow."""
        colors = [(0, 0, 0)] * self.hw.pixel_count
        start = max(0, self.hw.pixel_count - 3)
        for idx in range(start, self.hw.pixel_count):
            colors[idx] = (255, 200, 0)
        self.hw.show(colors)


class LedRingService:
    """Phase-driven LED service: LED = f(Phase).
    
    SINGLE SOURCE OF TRUTH: TOPIC_DISPLAY_STATE
    
    This service subscribes ONLY to display state updates from the orchestrator.
    NO internal flags. NO inference from other topics.
    The orchestrator tells us the phase, we render it. Period.
    """

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

        # SINGLE SUBSCRIBER: display state from orchestrator
        self.ctx = zmq.Context.instance()
        self.sub = make_subscriber(self.config, channel="downstream", topic=TOPIC_DISPLAY_STATE)
        # Also subscribe to health for system errors
        self.sub.setsockopt(zmq.SUBSCRIBE, TOPIC_HEALTH)
        
        self.poller = zmq.Poller()
        self.poller.register(self.sub, zmq.POLLIN)
        
        self._in_error = False

    def _handle_display_state(self, payload: dict) -> None:
        """LED = f(Phase). This is THE ONLY state source."""
        if self._in_error:
            return  # Error state takes priority
        state = payload.get("state", "idle")
        self.logger.info("Display state received: %s", state)
        self.animator.set_state(state)

    def _handle_health(self, payload: dict) -> None:
        """Health errors override display state."""
        ok = payload.get("ok", True)
        if not ok and not self._in_error:
            self._in_error = True
            self.logger.error("Health error: %s", payload)
            self.animator.set_state("error")
        elif ok and self._in_error:
            self._in_error = False
            self.logger.info("Health restored")
            # Return to idle; orchestrator will send correct state
            self.animator.set_state("idle")

    def _drain(self) -> None:
        """Process all pending messages."""
        while True:
            try:
                topic, data = self.sub.recv_multipart(flags=zmq.NOBLOCK)
            except zmq.Again:
                break
            try:
                payload = json.loads(data)
            except json.JSONDecodeError:
                self.logger.error("Invalid JSON on topic %s", topic)
                continue
            
            if topic == TOPIC_DISPLAY_STATE:
                self._handle_display_state(payload)
            elif topic == TOPIC_HEALTH:
                self._handle_health(payload)

    def run(self) -> None:
        """Main loop: poll for state updates, render animations."""
        self.logger.info("LED ring service running (phase-driven mode)")
        try:
            while True:
                events = dict(self.poller.poll(50))
                if self.sub in events:
                    self._drain()
                self.animator.step(time.time())
        except KeyboardInterrupt:
            self.logger.info("LED ring service interrupted")
        finally:
            self.hardware.clear()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the orchestrator LED ring service")
    parser.add_argument("--config", default="config/system.yaml", help="Path to system config")
    parser.add_argument("--pin", default="D12", help="Board pin attribute for the NeoPixel ring")
    parser.add_argument("--pixels", type=int, default=10, help="Number of LEDs on the strip")
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
