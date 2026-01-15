"""UI module for Waveshare 3.5-inch display rendering.

Components:
- display_runner.py: IPC-integrated display service with state rendering
- face_fb.py: Framebuffer face renderer
- tft_smiley_test.py: Hardware test script
"""
from dataclasses import dataclass
from typing import Tuple


@dataclass(slots=True)
class DisplayConfig:
    """Placeholder display config (moved from deleted display_driver.py)."""
    resolution: Tuple[int, int] = (480, 320)
    rotation: int = 90
    spi_bus: int = 0
    spi_device: int = 0


# WaveshareDisplay was a placeholder stub; removed during cleanup
__all__ = ["DisplayConfig"]
