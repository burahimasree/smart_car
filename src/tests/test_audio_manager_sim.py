"""Tests for the AudioManager skeleton in simulation mode.

These tests do not exercise real ALSA hardware; they only validate the
session bookkeeping and basic control-plane contract.
"""
from __future__ import annotations

from pathlib import Path

from src.audio.audio_manager import AudioManager


def test_audio_manager_start_stop_session() -> None:
    mgr = AudioManager(Path("config/system.yaml"))

    ok, reason = mgr.start_session(
        "test-session",
        mode="stt",
        target_rate=16000,
        channels=1,
        max_duration_s=5.0,
        priority=10,
    )
    assert ok, reason

    ok, reason = mgr.stop_session("test-session")
    assert ok, reason
