"""Smoke-test orchestrator wiring using fake file paths."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.core.orchestrator import OfflineAssistant, OrchestratorConfig
from src.llm.llama_wrapper import LlamaConfig
from src.stt.engine import RecognizerConfig, STTBackend
from src.tts.engine import TTSConfig
from src.ui.display_driver import DisplayConfig
from src.vision.detector import VisionConfig


def test_offline_assistant_bootstrap(tmp_path: Path) -> None:
    dummy = tmp_path / "dummy"
    dummy.write_text("mock")
    config = OrchestratorConfig(
        stt=RecognizerConfig(STTBackend.WHISPER_CPP, dummy),
        tts=TTSConfig("demo", dummy),
        llm=LlamaConfig(dummy),
        vision=VisionConfig(dummy),
        display=DisplayConfig(),
    )
    assistant = OfflineAssistant(config)
    with pytest.raises(FileNotFoundError):
        assistant.bootstrap()
