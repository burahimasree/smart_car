"""Standalone Porcupine wakeword service using ALSA dsnoop input via sounddevice.

Uses sounddevice with device NAME (not index) to properly use ALSA plugin chain
including dsnoop for shared microphone access with STT service.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np
import sounddevice as sd
from scipy import signal
import zmq

from src.core.config_loader import load_config
from src.core.ipc import (
	TOPIC_CMD_LISTEN_START,
	TOPIC_CMD_LISTEN_STOP,
	TOPIC_WW_DETECTED,
	make_publisher,
	make_subscriber,
	publish_json,
)
from src.core.logging_setup import get_logger


# Native sample rate of USB mic - dsnoop runs at this rate
NATIVE_SAMPLE_RATE = 44100
# Porcupine requires 16kHz
TARGET_SAMPLE_RATE = 16000


@dataclass(slots=True)
class WakewordSettings:
	access_key: str
	keyword_path: Path
	sensitivity: float
	keyword: str
	variant: str
	mic_device: str


class WakewordService:
	def __init__(self, config_path: Path) -> None:
		self.config = load_config(config_path)
		log_dir = Path(self.config.get("logs", {}).get("directory", "logs"))
		if not log_dir.is_absolute():
			project_root = Path(os.environ.get("PROJECT_ROOT", Path(__file__).resolve().parents[2]))
			log_dir = project_root / log_dir
		self.logger = get_logger("wake", log_dir)

		self.settings = self._load_settings()
		self.detector = self._init_porcupine()
		self.sample_rate = self.detector.sample_rate
		if self.sample_rate != TARGET_SAMPLE_RATE:
			raise RuntimeError(f"Porcupine sample_rate {self.sample_rate} unsupported; expected {TARGET_SAMPLE_RATE} Hz")
		self.frame_length = self.detector.frame_length

		# Calculate native frame size for resampling
		self.native_frame_length = int(self.frame_length * NATIVE_SAMPLE_RATE / TARGET_SAMPLE_RATE)
		
		# Open sounddevice stream with ALSA device NAME (enables dsnoop sharing)
		self.stream = self._open_stream()
		self.processing_enabled = True
		self.running = True

		self.publisher = make_publisher(self.config, channel="upstream")
		self.cmd_sub = make_subscriber(self.config, channel="downstream", topic=TOPIC_CMD_LISTEN_START)
		self.cmd_sub.setsockopt(zmq.SUBSCRIBE, TOPIC_CMD_LISTEN_STOP)
		self.poller = zmq.Poller()
		self.poller.register(self.cmd_sub, zmq.POLLIN)

	def run(self) -> None:
		self.logger.info("Wakeword service ready (keyword=%s)", self.settings.keyword)
		try:
			while self.running:
				self._drain_commands()
				pcm = self._read_frame()
				if pcm is None or not self.processing_enabled:
					continue
				samples = np.frombuffer(pcm, dtype=np.int16)
				if samples.size != self.frame_length:
					continue
				detected = self.detector.process(samples.tolist())
				if detected >= 0:
					self._publish_detection(detected)
		finally:
			self._shutdown()

	def _drain_commands(self) -> None:
		try:
			socks = dict(self.poller.poll(timeout=0))
		except zmq.ZMQError:
			return
		if self.cmd_sub not in socks:
			return
		while True:
			try:
				topic, raw = self.cmd_sub.recv_multipart(flags=zmq.NOBLOCK)
			except zmq.Again:
				break
			self._handle_command(topic, raw)

	def _handle_command(self, topic: bytes, raw: bytes) -> None:
		try:
			payload: Dict[str, Any] = json.loads(raw)
		except Exception:
			self.logger.error("Invalid downstream payload on topic %s", topic)
			return
		if topic == TOPIC_CMD_LISTEN_START:
			self.processing_enabled = False
			self.logger.info("cmd.listen.start -> pausing detection (draining stream)")
		elif topic == TOPIC_CMD_LISTEN_STOP:
			self.processing_enabled = True
			self.logger.info("cmd.listen.stop -> resuming detection")

	def _read_frame(self) -> Optional[bytes]:
		"""Read frame at native rate, resample to 16kHz for Porcupine."""
		try:
			# Read at native 44100Hz
			data, overflowed = self.stream.read(self.native_frame_length)
			if overflowed:
				self.logger.debug("Audio buffer overflow (non-fatal)")
			
			# Convert to float for resampling
			samples_float = data.flatten().astype(np.float32) / 32768.0
			
			# Resample 44100 -> 16000
			resampled = signal.resample(samples_float, self.frame_length)
			
			# Convert back to int16
			samples_16k = (resampled * 32768.0).astype(np.int16)
			return samples_16k.tobytes()
		except Exception as exc:
			self.logger.error("Microphone read failed: %s", exc)
			time.sleep(0.05)
			return None

	def _publish_detection(self, keyword_index: int) -> None:
		payload = {
			"timestamp": int(time.time()),
			"keyword": self.settings.keyword,
			"variant": self.settings.variant,
			"confidence": 0.99,
			"source": "porcupine",
			"keyword_index": keyword_index,
		}
		publish_json(self.publisher, TOPIC_WW_DETECTED, payload)
		self.processing_enabled = False
		self.logger.info("Wakeword detected -> entering paused state until cmd.listen.stop")

	def _shutdown(self) -> None:
		self.logger.info("Wakeword service shutting down")
		try:
			self.stream.stop()
		except Exception:
			pass
		try:
			self.stream.close()
		except Exception:
			pass
		try:
			self.detector.delete()
		except Exception:
			pass
		try:
			self.publisher.close(0)
		except Exception:
			pass
		try:
			self.cmd_sub.close(0)
		except Exception:
			pass

	def _open_stream(self) -> sd.InputStream:
		"""Open sounddevice stream by device NAME for proper ALSA dsnoop usage."""
		mic_device = self.settings.mic_device
		
		# Use device name directly - this enables ALSA plugin chain (dsnoop)
		# Don't use device index as that bypasses ALSA plugins!
		device = mic_device if mic_device != "default" else None
		
		try:
			stream = sd.InputStream(
				device=device,
				samplerate=NATIVE_SAMPLE_RATE,
				channels=1,
				dtype='int16',
				blocksize=self.native_frame_length,
			)
			stream.start()
			self.logger.info(
				"Opened audio stream: device=%s, rate=%d, blocksize=%d",
				mic_device, NATIVE_SAMPLE_RATE, self.native_frame_length
			)
			return stream
		except Exception as exc:
			self.logger.error("Unable to open ALSA device '%s': %s", mic_device, exc)
			raise RuntimeError("Failed to open microphone") from exc

	def _load_settings(self) -> WakewordSettings:
		cfg = self.config.get("wakeword", {}) or {}
		access_key = (
			cfg.get("access_key")
			or os.environ.get("PV_ACCESS_KEY")
			or self._read_access_key_file(cfg.get("access_key_path"))
		)
		if not access_key:
			raise RuntimeError("PV access key missing; set PV_ACCESS_KEY or wakeword.access_key")
		keyword_path = self._resolve_keyword_path(cfg)
		keywords = cfg.get("keywords") or []
		keyword = cfg.get("payload_keyword") or (keywords[0] if keywords else "wakeword")
		variant = cfg.get("payload_variant") or keyword
		mic_device = str(cfg.get("mic_device") or self.config.get("audio", {}).get("mic_device") or "default").strip() or "default"
		return WakewordSettings(
			access_key=str(access_key),
			keyword_path=keyword_path,
			sensitivity=float(cfg.get("sensitivity", 0.6)),
			keyword=keyword,
			variant=variant,
			mic_device=mic_device,
		)

	def _read_access_key_file(self, path_value: Optional[str]) -> Optional[str]:
		if not path_value:
			return None
		path = Path(path_value).expanduser()
		if not path.exists():
			return None
		return path.read_text().strip()

	def _resolve_keyword_path(self, cfg: Dict[str, Any]) -> Path:
		candidates = [
			cfg.get("model"),
			cfg.get("model_path"),
			(cfg.get("model_paths") or {}).get("porcupine_keyword"),
		]
		for value in candidates:
			if not value:
				continue
			path = Path(value)
			if path.exists():
				return path
		raise RuntimeError("Wakeword keyword model not found; update config")

	def _init_porcupine(self):
		try:
			import pvporcupine
		except ImportError as exc:  # pragma: no cover
			raise RuntimeError("pvporcupine module not available") from exc
		try:
			return pvporcupine.create(
				access_key=self.settings.access_key,
				keyword_paths=[str(self.settings.keyword_path)],
				sensitivities=[self.settings.sensitivity],
			)
		except Exception as exc:  # pragma: no cover
			raise RuntimeError(f"Failed to initialize Porcupine: {exc}") from exc


def main() -> None:
	parser = argparse.ArgumentParser(description="Wakeword service (Porcupine + dsnoop)")
	parser.add_argument("--config", default="config/system.yaml")
	args = parser.parse_args()

	service = WakewordService(Path(args.config))
	try:
		service.run()
	except KeyboardInterrupt:
		pass
	except Exception as exc:
		print(f"[wakeword] Fatal error: {exc}", file=sys.stderr)
		raise


def entrypoint() -> None:
	try:
		main()
	except SystemExit:
		raise
	except Exception as exc:
		print(f"[wakeword] Unhandled exception: {exc}", file=sys.stderr)
		sys.exit(1)


if __name__ == "__main__":
	entrypoint()
