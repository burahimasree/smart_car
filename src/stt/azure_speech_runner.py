"""Azure Speech Services STT runner.

Captures microphone audio via the Azure SDK and publishes transcripts over the
shared ZeroMQ bus so the orchestrator can treat it like any other STT backend.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

import zmq

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.config_loader import load_config
from src.core.ipc import TOPIC_STT, make_publisher, publish_json
from src.core.logging_setup import get_logger


def _import_speech_sdk():
    try:
        import azure.cognitiveservices.speech as speechsdk  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "azure-cognitiveservices-speech is required for the Azure STT runner; "
            "install it in the STT venv (requirements-stte.txt)"
        ) from exc
    return speechsdk


def _build_recognizer(speechsdk: Any, *, key: str, region: str, endpoint: Optional[str], language: str, mic: Optional[str]):
    speech_config = speechsdk.SpeechConfig(subscription=key, region=region)
    if endpoint:
        speech_config.endpoint = endpoint
    speech_config.speech_recognition_language = language
    if mic and mic.lower() != "default":
        audio_config = speechsdk.audio.AudioConfig(device_name=mic)
    else:
        audio_config = speechsdk.audio.AudioConfig()
    return speechsdk.SpeechRecognizer(speech_config=speech_config, audio_config=audio_config)


def _extract_confidence(speechsdk: Any, result: Any) -> float:
    try:
        json_blob = result.properties.get(speechsdk.PropertyId.SpeechServiceResponse_JsonResult)
        if not json_blob:
            return 0.0
        data = json.loads(json_blob)
        nbest = data.get("NBest") or data.get("nbest")
        if isinstance(nbest, list) and nbest:
            conf = nbest[0].get("Confidence") or nbest[0].get("confidence")
            if conf is not None:
                return max(0.0, min(1.0, float(conf)))
    except Exception:
        return 0.0
    return 0.0


def main() -> None:
    parser = argparse.ArgumentParser(description="Azure Speech Services STT runner")
    parser.add_argument("--config", default="config/system.yaml", help="Path to system config")
    parser.add_argument("--ipc", help="Override IPC upstream address")
    parser.add_argument("--region", help="Azure speech region override")
    parser.add_argument("--endpoint", help="Custom endpoint URL override")
    parser.add_argument("--language", default="en-US", help="Language/locale (e.g. en-US)")
    parser.add_argument("--mic", help="Device name for Azure audio capture")
    parser.add_argument("--min-confidence", type=float, default=0.0)
    parser.add_argument("--continuous", action="store_true", help="Keep listening until interrupted")
    parser.add_argument("--mock", action="store_true", help="Emit a canned transcription without contacting Azure")
    parser.add_argument("--mock-text", default="simulated azure command", help="Text to emit when --mock is set")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    stt_cfg = cfg.get("stt", {})
    engines_cfg = (stt_cfg.get("engines") or {}).get("azure_speech", {})

    min_conf = float(args.min_confidence)
    mic_device = args.mic or engines_cfg.get("mic_device") or engines_cfg.get("mic")
    region = args.region or engines_cfg.get("region") or os.environ.get("AZURE_SPEECH_REGION")
    endpoint = args.endpoint or engines_cfg.get("endpoint") or os.environ.get("AZURE_SPEECH_ENDPOINT")
    language = args.language or engines_cfg.get("language") or stt_cfg.get("language", "en-US")
    speech_key = engines_cfg.get("key") or os.environ.get("AZURE_SPEECH_KEY")

    if not speech_key:
        raise RuntimeError("Azure Speech key not configured (set stt.engines.azure_speech.key or AZURE_SPEECH_KEY)")
    if not region:
        raise RuntimeError("Azure Speech region not configured (set stt.engines.azure_speech.region or AZURE_SPEECH_REGION)")

    if args.ipc:
        os.environ["IPC_UPSTREAM"] = args.ipc
    pub = make_publisher(cfg, channel="upstream")

    log_dir = Path(cfg.get("logs", {}).get("directory", "logs"))
    if not log_dir.is_absolute():
        log_dir = PROJECT_ROOT / log_dir
    logger = get_logger("stt.azure", log_dir)

    if args.mock:
        payload = {
            "timestamp": int(time.time()),
            "text": args.mock_text,
            "confidence": 0.95,
            "language": language,
            "durations_ms": {"capture": 0, "whisper": 0, "total": 0},
        }
        publish_json(pub, TOPIC_STT, payload)
        if args.debug:
            print(json.dumps(payload, indent=2))
        return

    speechsdk = _import_speech_sdk()
    recognizer = _build_recognizer(
        speechsdk,
        key=speech_key,
        region=region,
        endpoint=endpoint,
        language=language,
        mic=mic_device,
    )

    try:
        while True:
            loop_start = time.time()
            try:
                result = recognizer.recognize_once_async().get()
            except Exception as exc:  # pragma: no cover
                logger.error("Azure STT recognize_once failed: %s", exc)
                break

            if result.reason == speechsdk.ResultReason.RecognizedSpeech:
                text = (result.text or "").strip()
                confidence = _extract_confidence(speechsdk, result) or 0.9
                if not text:
                    logger.debug("Azure STT returned empty text; skipping")
                elif confidence < min_conf:
                    logger.debug(
                        "Azure STT confidence %.2f below min %.2f; skipping", confidence, min_conf
                    )
                else:
                    total_ms = int((time.time() - loop_start) * 1000)
                    payload = {
                        "timestamp": int(time.time()),
                        "text": text,
                        "confidence": round(confidence, 3),
                        "language": language,
                        "durations_ms": {
                            "capture": 0,
                            "whisper": total_ms,
                            "total": total_ms,
                        },
                    }
                    publish_json(pub, TOPIC_STT, payload)
                    logger.info("Azure STT published transcription (%s)", text[:60])
                    if args.debug:
                        print(json.dumps(payload, indent=2))
                    if not args.continuous:
                        break
            elif result.reason == speechsdk.ResultReason.NoMatch:
                logger.warning("Azure STT no match: %s", result.no_match_details)
                if not args.continuous:
                    break
            elif result.reason == speechsdk.ResultReason.Canceled:
                cancellation = result.cancellation_details
                logger.error("Azure STT canceled: %s", cancellation.reason)
                if cancellation.error_details:
                    logger.error("Azure STT error: %s", cancellation.error_details)
                break
            else:
                logger.warning("Azure STT unexpected result: %s", result.reason)
                if not args.continuous:
                    break
    finally:
        try:
            recognizer.stop_continuous_recognition_async()
        except Exception:
            pass


if __name__ == "__main__":
    main()
