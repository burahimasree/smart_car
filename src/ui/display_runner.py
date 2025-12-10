#!/usr/bin/env python3
"""Display service with IPC integration for Waveshare 3.5" TFT.

Subscribes to orchestrator events and renders status on the display:
- Idle: Blue background with "Ready" text
- Listening: Green pulsing ear icon
- Thinking: Yellow spinning gear
- Speaking: Orange sound wave animation
- Navigation: Shows direction arrow
- Vision: Shows detection label if present

Uses pygame + framebuffer for rendering (proven working in display_smiley.py).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

# Set SDL to use framebuffer before importing pygame
os.environ.setdefault("SDL_VIDEODRIVER", "fbcon")
os.environ.setdefault("SDL_FBDEV", "/dev/fb1")

try:
    import pygame
    PYGAME_AVAILABLE = True
except ImportError:
    PYGAME_AVAILABLE = False
    pygame = None  # type: ignore

from src.core.config_loader import load_config
from src.core.ipc import (
    TOPIC_CMD_LISTEN_START,
    TOPIC_CMD_LISTEN_STOP,
    TOPIC_CMD_PAUSE_VISION,
    TOPIC_LLM_REQ,
    TOPIC_LLM_RESP,
    TOPIC_NAV,
    TOPIC_STT,
    TOPIC_TTS,
    TOPIC_VISN,
    TOPIC_WW_DETECTED,
    make_subscriber,
)
from src.core.logging_setup import get_logger

logger = get_logger("ui.display_runner", Path("logs"))


class DisplayState(Enum):
    """Current display mode."""
    IDLE = auto()
    LISTENING = auto()
    THINKING = auto()
    SPEAKING = auto()
    NAVIGATING = auto()


@dataclass
class DisplayStatus:
    """Current status to render."""
    state: DisplayState = DisplayState.IDLE
    text: str = "Ready"
    direction: Optional[str] = None
    vision_label: Optional[str] = None
    vision_paused: bool = False
    last_update: float = 0.0


# Colors (RGB)
COLORS = {
    "bg_idle": (20, 30, 60),
    "bg_listening": (20, 80, 40),
    "bg_thinking": (80, 70, 20),
    "bg_speaking": (100, 50, 20),
    "bg_navigating": (30, 30, 80),
    "text_primary": (255, 255, 255),
    "text_secondary": (180, 180, 180),
    "accent": (0, 200, 255),
    "icon_ear": (100, 255, 100),
    "icon_gear": (255, 220, 50),
    "icon_speaker": (255, 150, 50),
    "arrow": (100, 200, 255),
    "face_outline": (240, 240, 255),
    "face_fill": (10, 15, 30),
    "eye_fill": (240, 240, 255),
    "eye_pupil": (10, 10, 20),
    "mouth": (240, 160, 120),
    # Soft pink blush for cheeks
    "blush": (255, 192, 203),
}


class DisplayRenderer:
    """Renders status to the TFT display using pygame."""

    def __init__(self, width: int = 480, height: int = 320, fb_device: str = "/dev/fb1"):
        self.width = width
        self.height = height
        self.fb_device = fb_device
        self.screen: Optional[pygame.Surface] = None
        self.font_large: Optional[pygame.font.Font] = None
        self.font_medium: Optional[pygame.font.Font] = None
        self.font_small: Optional[pygame.font.Font] = None
        self.animation_frame = 0
        self._initialized = False

    def initialize(self) -> bool:
        """Initialize pygame and display."""
        if not PYGAME_AVAILABLE:
            logger.error("pygame not installed")
            return False

        try:
            os.environ["SDL_FBDEV"] = self.fb_device
            pygame.init()
            pygame.mouse.set_visible(False)

            # Try framebuffer first, fallback to dummy for testing
            try:
                self.screen = pygame.display.set_mode((self.width, self.height), pygame.FULLSCREEN)
            except pygame.error:
                logger.warning("Framebuffer not available, using dummy display")
                os.environ["SDL_VIDEODRIVER"] = "dummy"
                pygame.display.quit()
                pygame.display.init()
                self.screen = pygame.display.set_mode((self.width, self.height))

            pygame.font.init()
            self.font_large = pygame.font.Font(None, 64)
            self.font_medium = pygame.font.Font(None, 42)
            self.font_small = pygame.font.Font(None, 28)

            self._initialized = True
            logger.info("Display initialized: %dx%d on %s", self.width, self.height, self.fb_device)
            return True

        except Exception as e:
            logger.error("Failed to initialize display: %s", e)
            return False

    def _get_background_color(self, state: DisplayState) -> Tuple[int, int, int]:
        """Get background color for state."""
        return {
            DisplayState.IDLE: COLORS["bg_idle"],
            DisplayState.LISTENING: COLORS["bg_listening"],
            DisplayState.THINKING: COLORS["bg_thinking"],
            DisplayState.SPEAKING: COLORS["bg_speaking"],
            DisplayState.NAVIGATING: COLORS["bg_navigating"],
        }.get(state, COLORS["bg_idle"])

    def _draw_listening_icon(self, cx: int, cy: int, frame: int) -> None:
        """Draw animated ear/microphone icon."""
        if not self.screen:
            return
        # Pulsing circles representing sound waves
        pulse = abs(math.sin(frame * 0.15)) * 20
        for i, radius in enumerate([30, 50, 70]):
            alpha = 255 - i * 60
            r = int(radius + pulse * (i + 1) * 0.3)
            color = (*COLORS["icon_ear"][:3],)
            pygame.draw.circle(self.screen, color, (cx, cy), r, 3)
        # Central microphone shape
        pygame.draw.rect(self.screen, COLORS["text_primary"], (cx - 15, cy - 30, 30, 50), border_radius=15)
        pygame.draw.rect(self.screen, COLORS["text_primary"], (cx - 25, cy + 20, 50, 8))
        pygame.draw.rect(self.screen, COLORS["text_primary"], (cx - 3, cy + 28, 6, 20))

    def _draw_thinking_icon(self, cx: int, cy: int, frame: int) -> None:
        """Draw animated gear/brain icon."""
        if not self.screen:
            return
        # Spinning gear
        angle = frame * 5
        radius = 40
        teeth = 8
        for i in range(teeth):
            a = math.radians(angle + i * (360 / teeth))
            x1 = cx + int(radius * math.cos(a))
            y1 = cy + int(radius * math.sin(a))
            x2 = cx + int((radius + 15) * math.cos(a))
            y2 = cy + int((radius + 15) * math.sin(a))
            pygame.draw.line(self.screen, COLORS["icon_gear"], (x1, y1), (x2, y2), 8)
        pygame.draw.circle(self.screen, COLORS["icon_gear"], (cx, cy), radius, 6)
        pygame.draw.circle(self.screen, self._get_background_color(DisplayState.THINKING), (cx, cy), 20)

    def _draw_speaking_icon(self, cx: int, cy: int, frame: int) -> None:
        """Draw animated speaker icon."""
        if not self.screen:
            return
        # Speaker cone
        points = [(cx - 30, cy - 20), (cx - 30, cy + 20), (cx, cy + 35), (cx, cy - 35)]
        pygame.draw.polygon(self.screen, COLORS["icon_speaker"], points)
        # Sound waves
        for i in range(3):
            wave_offset = (frame * 3 + i * 20) % 60
            alpha = max(0, 255 - wave_offset * 4)
            arc_rect = pygame.Rect(cx + 10 + wave_offset, cy - 40, 30, 80)
            pygame.draw.arc(self.screen, COLORS["icon_speaker"], arc_rect, -0.5, 0.5, 4)

    def _draw_direction_arrow(self, cx: int, cy: int, direction: str) -> None:
        """Draw navigation direction arrow."""
        if not self.screen:
            return
        size = 60
        arrows = {
            "forward": [(cx, cy - size), (cx - size // 2, cy + size // 2), (cx + size // 2, cy + size // 2)],
            "backward": [(cx, cy + size), (cx - size // 2, cy - size // 2), (cx + size // 2, cy - size // 2)],
            "left": [(cx - size, cy), (cx + size // 2, cy - size // 2), (cx + size // 2, cy + size // 2)],
            "right": [(cx + size, cy), (cx - size // 2, cy - size // 2), (cx - size // 2, cy + size // 2)],
            "stop": [],  # Draw X for stop
        }
        points = arrows.get(direction.lower(), [])
        if points:
            pygame.draw.polygon(self.screen, COLORS["arrow"], points)
        elif direction.lower() == "stop":
            pygame.draw.line(self.screen, (255, 80, 80), (cx - 40, cy - 40), (cx + 40, cy + 40), 8)
            pygame.draw.line(self.screen, (255, 80, 80), (cx - 40, cy + 40), (cx + 40, cy - 40), 8)

    def _draw_face(self, status: DisplayStatus) -> None:
        """Draw a simple expressive face in the center of the screen."""
        if not self.screen:
            return

        cx, cy = self.width // 2, self.height // 2 - 10
        radius = min(self.width, self.height) // 3

        # Face background circle
        pygame.draw.circle(self.screen, COLORS["face_outline"], (cx, cy), radius + 4, width=4)
        pygame.draw.circle(self.screen, COLORS["face_fill"], (cx, cy), radius)

        # Eye positions
        eye_dx = radius // 2
        eye_dy = radius // 3
        eye_r = max(6, radius // 7)

        left_eye_center = (cx - eye_dx, cy - eye_dy)
        right_eye_center = (cx + eye_dx, cy - eye_dy)

        # Base eye fill
        pygame.draw.circle(self.screen, COLORS["eye_fill"], left_eye_center, eye_r)
        pygame.draw.circle(self.screen, COLORS["eye_fill"], right_eye_center, eye_r)

        # Expression-specific tweaks
        state = status.state
        if state == DisplayState.LISTENING:
            # Wider, more alert eyes
            pupil_offset_y = -eye_r // 5
            pupil_r = eye_r // 2
            for (ex, ey) in (left_eye_center, right_eye_center):
                pygame.draw.circle(
                    self.screen,
                    COLORS["eye_pupil"],
                    (ex, ey + pupil_offset_y),
                    pupil_r,
                )
        elif state == DisplayState.THINKING:
            # Slight squint / eyebrow
            brow_w = eye_r * 2
            brow_offset = eye_r
            pygame.draw.line(
                self.screen,
                COLORS["eye_pupil"],
                (left_eye_center[0] - brow_w // 2, left_eye_center[1] - brow_offset),
                (left_eye_center[0] + brow_w // 2, left_eye_center[1] - brow_offset // 2),
                3,
            )
            pygame.draw.line(
                self.screen,
                COLORS["eye_pupil"],
                (right_eye_center[0] - brow_w // 2, right_eye_center[1] - brow_offset // 2),
                (right_eye_center[0] + brow_w // 2, right_eye_center[1] - brow_offset),
                3,
            )
        elif state == DisplayState.SPEAKING:
            # Simple open mouth talking look: pupils centered
            pupil_r = eye_r // 2
            for (ex, ey) in (left_eye_center, right_eye_center):
                pygame.draw.circle(self.screen, COLORS["eye_pupil"], (ex, ey), pupil_r)
        else:
            # Neutral/idle: soft pupils
            pupil_r = eye_r // 3
            for (ex, ey) in (left_eye_center, right_eye_center):
                pygame.draw.circle(self.screen, COLORS["eye_pupil"], (ex, ey), pupil_r)

        # Mouth
        mouth_w = radius
        mouth_h = radius // 2
        mouth_rect = pygame.Rect(
            cx - mouth_w // 2,
            cy + radius // 4,
            mouth_w,
            mouth_h,
        )

        if state == DisplayState.SPEAKING:
            # Open mouth rectangle
            open_h = max(8, mouth_h // 2)
            open_rect = pygame.Rect(mouth_rect.x, mouth_rect.y, mouth_rect.w, open_h)
            pygame.draw.rect(self.screen, COLORS["mouth"], open_rect, border_radius=8)
        elif state == DisplayState.THINKING:
            # Slight flat mouth
            y = mouth_rect.centery
            pygame.draw.line(
                self.screen,
                COLORS["mouth"],
                (mouth_rect.left, y),
                (mouth_rect.right, y),
                4,
            )
        else:
            # Gentle smile
            start = math.pi / 8
            end = math.pi - math.pi / 8
            pygame.draw.arc(self.screen, COLORS["mouth"], mouth_rect, start, end, 4)

        # Pink blush on cheeks
        blush_r = max(4, radius // 7)
        blush_offset_x = radius * 2 // 3
        blush_offset_y = radius // 5
        left_blush = (cx - blush_offset_x, cy + blush_offset_y)
        right_blush = (cx + blush_offset_x, cy + blush_offset_y)
        pygame.draw.circle(self.screen, COLORS["blush"], left_blush, blush_r)
        pygame.draw.circle(self.screen, COLORS["blush"], right_blush, blush_r)

    def render(self, status: DisplayStatus) -> None:
        """Render current status to display."""
        if not self._initialized or not self.screen:
            return

        self.animation_frame += 1
        bg_color = self._get_background_color(status.state)
        self.screen.fill(bg_color)

        cx, cy = self.width // 2, self.height // 2 - 30

        # Draw expressive face in center
        self._draw_face(status)

        # Optional overlay icon for navigation
        if status.state == DisplayState.NAVIGATING and status.direction:
            self._draw_direction_arrow(cx, cy, status.direction)

        # Draw status text
        state_labels = {
            DisplayState.IDLE: "Ready",
            DisplayState.LISTENING: "Listening...",
            DisplayState.THINKING: "Thinking...",
            DisplayState.SPEAKING: "Speaking...",
            DisplayState.NAVIGATING: f"Moving: {status.direction or 'forward'}",
        }
        label = state_labels.get(status.state, "Ready")
        if self.font_large:
            text_surface = self.font_large.render(label, True, COLORS["text_primary"])
            text_rect = text_surface.get_rect(center=(cx, cy + 90))
            self.screen.blit(text_surface, text_rect)

        # Draw secondary info (vision label, transcript preview)
        if status.text and status.text != "Ready" and self.font_small:
            # Truncate long text
            display_text = status.text[:50] + "..." if len(status.text) > 50 else status.text
            text_surface = self.font_small.render(display_text, True, COLORS["text_secondary"])
            text_rect = text_surface.get_rect(center=(cx, cy + 130))
            self.screen.blit(text_surface, text_rect)

        # Vision status indicator (top-right)
        if self.font_small:
            vision_text = "👁 PAUSED" if status.vision_paused else "👁 ACTIVE"
            vision_color = (150, 150, 150) if status.vision_paused else (100, 255, 100)
            text_surface = self.font_small.render(vision_text, True, vision_color)
            self.screen.blit(text_surface, (self.width - 120, 10))

        # Detection label (bottom)
        if status.vision_label and not status.vision_paused and self.font_medium:
            det_text = f"Detected: {status.vision_label}"
            text_surface = self.font_medium.render(det_text, True, COLORS["accent"])
            text_rect = text_surface.get_rect(center=(cx, self.height - 30))
            self.screen.blit(text_surface, text_rect)

        pygame.display.flip()

    def cleanup(self) -> None:
        """Cleanup pygame resources."""
        if PYGAME_AVAILABLE:
            pygame.quit()
        self._initialized = False


class DisplayService:
    """IPC-integrated display service."""

    def __init__(self, fb_device: str = "/dev/fb1", sim: bool = False):
        self.config = load_config(Path("config/system.yaml"))
        self.status = DisplayStatus()
        self.renderer = DisplayRenderer(fb_device=fb_device)
        self.sim = sim
        self._running = False

    def _update_state_from_topic(self, topic: bytes, payload: Dict[str, Any]) -> bool:
        """Update display status based on IPC message. Returns True if display should update."""
        changed = False

        if topic == TOPIC_WW_DETECTED:
            self.status.state = DisplayState.LISTENING
            self.status.text = payload.get("keyword", "Wake word detected")
            changed = True

        elif topic == TOPIC_CMD_LISTEN_START:
            self.status.state = DisplayState.LISTENING
            self.status.text = "Listening..."
            changed = True

        elif topic == TOPIC_CMD_LISTEN_STOP:
            # Transition to thinking if we were listening
            if self.status.state == DisplayState.LISTENING:
                self.status.state = DisplayState.THINKING
                self.status.text = "Processing..."
            changed = True

        elif topic == TOPIC_STT:
            self.status.state = DisplayState.THINKING
            self.status.text = payload.get("text", "")[:100]
            changed = True

        elif topic == TOPIC_LLM_REQ:
            self.status.state = DisplayState.THINKING
            self.status.text = payload.get("text", "")[:100]
            changed = True

        elif topic == TOPIC_LLM_RESP:
            # Extract speak text from LLM response
            body = payload.get("json", {})
            speak = payload.get("text") or body.get("speak", "")
            if speak:
                self.status.state = DisplayState.SPEAKING
                self.status.text = speak[:100]
            changed = True

        elif topic == TOPIC_TTS:
            done = payload.get("done") or payload.get("final") or payload.get("completed")
            if done:
                self.status.state = DisplayState.IDLE
                self.status.text = "Ready"
            else:
                self.status.state = DisplayState.SPEAKING
                self.status.text = payload.get("text", "")[:100]
            changed = True

        elif topic == TOPIC_NAV:
            self.status.state = DisplayState.NAVIGATING
            self.status.direction = payload.get("direction", "forward")
            self.status.text = f"Moving {self.status.direction}"
            changed = True

        elif topic == TOPIC_CMD_PAUSE_VISION:
            self.status.vision_paused = payload.get("pause", False)
            changed = True

        elif topic == TOPIC_VISN:
            if not self.status.vision_paused:
                label = payload.get("label") or payload.get("class")
                if label:
                    self.status.vision_label = label
                    changed = True

        if changed:
            self.status.last_update = time.time()

        return changed

    def run(self) -> None:
        """Main display service loop."""
        if not self.renderer.initialize():
            if not self.sim:
                logger.error("Display init failed and not in sim mode, exiting")
                return
            logger.warning("Running in simulation mode without display")

        # Subscribe to all relevant topics on both channels
        sub_upstream = make_subscriber(self.config, topic=b"", channel="upstream")
        sub_downstream = make_subscriber(self.config, topic=b"", channel="downstream")

        # Use poller to listen on both sockets
        import zmq
        poller = zmq.Poller()
        poller.register(sub_upstream, zmq.POLLIN)
        poller.register(sub_downstream, zmq.POLLIN)

        self._running = True
        logger.info("Display service running")

        # Initial render
        self.renderer.render(self.status)

        frame_interval = 1.0 / 30  # 30 FPS for animations
        last_frame = time.time()

        try:
            while self._running:
                # Poll with short timeout for animation updates
                socks = dict(poller.poll(timeout=33))  # ~30fps

                for sock in [sub_upstream, sub_downstream]:
                    if sock in socks:
                        try:
                            topic, data = sock.recv_multipart(zmq.NOBLOCK)
                            payload = json.loads(data)
                            if self._update_state_from_topic(topic, payload):
                                logger.debug("Display update: %s -> %s", topic, self.status.state)
                        except zmq.Again:
                            pass
                        except json.JSONDecodeError as e:
                            logger.warning("Invalid JSON: %s", e)

                # Update display at frame rate
                now = time.time()
                if now - last_frame >= frame_interval:
                    self.renderer.render(self.status)
                    last_frame = now

                # Auto-return to idle after 10s of no activity
                if self.status.state != DisplayState.IDLE:
                    if now - self.status.last_update > 10.0:
                        self.status.state = DisplayState.IDLE
                        self.status.text = "Ready"
                        self.status.direction = None

                # Handle pygame events (for graceful exit)
                if PYGAME_AVAILABLE:
                    for event in pygame.event.get():
                        if event.type == pygame.QUIT:
                            self._running = False

        except KeyboardInterrupt:
            logger.info("Display service interrupted")
        finally:
            self.renderer.cleanup()
            logger.info("Display service stopped")


def main() -> None:
    parser = argparse.ArgumentParser(description="Display service for orchestrator")
    parser.add_argument("--fb", default="/dev/fb1", help="Framebuffer device (default: /dev/fb1)")
    parser.add_argument("--sim", action="store_true", help="Run in simulation mode (no display required)")
    args = parser.parse_args()

    service = DisplayService(fb_device=args.fb, sim=args.sim)
    service.run()


if __name__ == "__main__":
    main()
