"""UI module for Waveshare 3.5-inch display rendering.

Components:
- display_driver.py: Low-level Waveshare TFT driver (placeholder)
- display_runner.py: IPC-integrated display service with state rendering
- tft_smiley_test.py: Hardware test script
"""
from src.ui.display_driver import DisplayConfig, WaveshareDisplay

__all__ = ["DisplayConfig", "WaveshareDisplay"]
