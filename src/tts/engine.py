"""Text-to-speech scaffolding targeting Piper voices."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional


@dataclass(slots=True)
class TTSConfig:
    voice: str
    model_path: Path
    sample_rate: int = 22050
    noise_scale: float = 0.6
    length_scale: float = 1.0


class SpeechSynthesizer:
    """Encapsulates Piper CLI or library execution."""

    def __init__(self, config: TTSConfig) -> None:
        self.config = config
        self._is_ready = False

    def bootstrap(self) -> None:
        if not self.config.model_path.exists():
            raise FileNotFoundError(f"Missing Piper voice: {self.config.model_path}")
        self._is_ready = True

    def speak(self, prompt: str, *, output_path: Optional[Path] = None) -> Path:
        if not self._is_ready:
            raise RuntimeError("SpeechSynthesizer.bootstrap must run first")
        target = output_path or Path("./tts-output.wav")
        # Placeholder: write metadata for downstream inspection.
        target.write_text(
            f"voice={self.config.voice}\nrate={self.config.sample_rate}\nprompt={prompt}\n"
        )
        return target

    @property
    def is_ready(self) -> bool:
        return self._is_ready
