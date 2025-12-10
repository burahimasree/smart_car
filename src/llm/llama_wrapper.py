"""Local LLM wrapper delegating to llama.cpp binaries."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List


@dataclass(slots=True)
class LlamaConfig:
    model_path: Path
    context_tokens: int = 2048
    gpu_layers: int = 0
    threads: int = 4


class LlamaRunner:
    """Minimal llama.cpp process wrapper (no python bindings yet)."""

    def __init__(self, config: LlamaConfig) -> None:
        self.config = config
        self._llama_binary = Path("./llama.cpp/main")

    def ensure_ready(self) -> None:
        if not self.config.model_path.exists():
            raise FileNotFoundError(f"LLM checkpoint missing: {self.config.model_path}")
        if not self._llama_binary.exists():
            raise FileNotFoundError("Compile llama.cpp and place binaries under ./llama.cpp")

    def generate(self, prompt: str, *, max_tokens: int = 256) -> str:
        self.ensure_ready()
        # Stubbed response; real implementation would spawn subprocess
        # with --ctx_size, --n_predict, etc.
        return (
            f"[llama.cpp simulated output]\nprompt: {prompt[:60]}...\n"
            f"ctx={self.config.context_tokens} max={max_tokens}"
        )
