#!/usr/bin/env python3
from __future__ import annotations

import os
from pathlib import Path
import sys

# Ensure project_root is on sys.path so `src.*` imports work from venv
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Use a dummy video driver so this can run headless or over SSH
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import pygame  # type: ignore

from src.ui.display_runner import DisplayRenderer, DisplayStatus, DisplayState


def main() -> None:
    # Match your TFT resolution
    renderer = DisplayRenderer(width=480, height=320, fb_device="/dev/fb0")
    if not renderer.initialize():
        raise SystemExit("Failed to initialize DisplayRenderer for preview")

    # Pick a representative state for the preview (Listening face)
    status = DisplayStatus(
        state=DisplayState.LISTENING,
        text="Hi, I'm GENNY!",
    )

    renderer.render(status)

    # Save current screen surface as PNG in the current directory
    out_path = Path.cwd() / "face_preview.png"
    if renderer.screen is not None:
        pygame.image.save(renderer.screen, str(out_path))
        print(f"Saved face preview to {out_path}")
    else:
        raise SystemExit("Renderer has no screen surface; cannot save preview")

    pygame.display.quit()
    pygame.quit()


if __name__ == "__main__":
    main()
