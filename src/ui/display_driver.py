"""Placeholder Waveshare 3.5" driver hooks."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple


@dataclass(slots=True)
class DisplayConfig:
    resolution: Tuple[int, int] = (480, 320)
    rotation: int = 90
    spi_bus: int = 0
    spi_device: int = 0


class WaveshareDisplay:
    def __init__(self, config: DisplayConfig) -> None:
        self.config = config
        self._initialized = False

    def initialize(self) -> None:
        # Real implementation would talk to /dev/spidev*
        self._initialized = True

    def draw_frame(self, buffer: bytes) -> None:
        if not self._initialized:
            raise RuntimeError("display initialize() not called")
        # Placeholder: nothing is drawn yet.
        _ = buffer
