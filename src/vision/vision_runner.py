from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import cv2
import numpy as np
import zmq

from src.core.config_loader import load_config
from src.core.ipc import TOPIC_CMD_PAUSE_VISION, TOPIC_VISN, make_publisher, make_subscriber, publish_json
from src.core.logging_setup import get_logger
from src.vision.detector import Detection, VisionConfig
from src.vision.pipeline import VisionPipeline


logger = get_logger("vision.runner", Path("logs"))


class MockDetector:
    """Deterministic detector used when running tests without a real model."""

    def load(self) -> None:  # pragma: no cover - trivial
        return

    def detect(self, frame: np.ndarray) -> list[Detection]:
        height, width = frame.shape[:2]
        bbox = (width // 4, height // 4, width * 3 // 4, height * 3 // 4)
        return [Detection(label="mock-object", confidence=0.99, bbox=bbox)]


def draw_detections(frame: np.ndarray, detections: list[Detection]) -> np.ndarray:
    for det in detections:
        x1, y1, x2, y2 = det.bbox
        label = f"{det.label}:{det.confidence:.2f}"
        cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
        cv2.putText(frame, label, (x1, max(12, y1 - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
    return frame


def publish_detections(pub_sock, detections: list[Detection], ts: float) -> None:
    for det in detections:
        x1, y1, x2, y2 = det.bbox
        payload = {"label": det.label, "bbox": [x1, y1, x2, y2], "confidence": det.confidence, "ts": ts}
        publish_json(pub_sock, TOPIC_VISN, payload)


def _coerce_path(path_value: str | None) -> Path | None:
    if not path_value:
        return None
    return Path(path_value).expanduser()


def build_vision_config(raw_cfg: dict) -> VisionConfig:
    model_path = _coerce_path(raw_cfg.get("model_path_onnx")) or Path("models/vision/yolo11n.onnx")
    return VisionConfig(
        model_path=model_path,
        backend=raw_cfg.get("backend", "onnx"),
        input_size=tuple(raw_cfg.get("input_size", [640, 640])),
        confidence=float(raw_cfg.get("confidence", 0.25)),
        iou=float(raw_cfg.get("iou", 0.45)),
        label_path=_coerce_path(raw_cfg.get("label_path"))
        or Path(raw_cfg.get("label_map", ""))
        if raw_cfg.get("label_map")
        else None,
    )


def _generate_test_frame(width: int = 640, height: int = 480) -> np.ndarray:
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    cv2.rectangle(frame, (50, 50), (width - 50, height - 50), (0, 128, 255), 3)
    cv2.putText(frame, "vision test", (60, height // 2), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2)
    return frame


def run():
    parser = argparse.ArgumentParser(description="YOLO-based vision runner")
    parser.add_argument("--config", default="config/system.yaml")
    parser.add_argument("--camera-index", type=int, default=None)
    parser.add_argument("--show", action="store_true", help="Display detections locally")
    parser.add_argument("--image", type=str, help="Run inference on a single image path", default=None)
    parser.add_argument("--test", action="store_true", help="Run a synthetic frame test instead of opening the camera")
    parser.add_argument("--max-frames", type=int, default=None, help="Exit after processing N frames")
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    vis_cfg = cfg.get("vision", {})
    camera_index = args.camera_index if args.camera_index is not None else int(vis_cfg.get("camera_index", 0))

    vision_config = build_vision_config(vis_cfg)
    pipeline = VisionPipeline(vision_config)
    mock_mode = False
    try:
        pipeline.bootstrap()
    except FileNotFoundError as exc:
        if args.test or args.image:
            logger.warning("Vision model missing (%s). Using mock detector for test mode.", exc)
            pipeline.detector = MockDetector()
            mock_mode = True
        else:
            logger.error("Vision bootstrap failed: %s", exc)
            return
    except Exception as exc:  # pragma: no cover - defensive logging
        if args.test or args.image:
            logger.warning("Vision bootstrap failed (%s). Using mock detector for test mode.", exc)
            pipeline.detector = MockDetector()
            mock_mode = True
        else:
            logger.exception("Vision bootstrap failed")
            return

    if args.test or args.image:
        frame = _generate_test_frame()
        if args.image:
            img_path = Path(args.image)
            if not img_path.exists():
                logger.error("Image %s not found", img_path)
                return
            frame = cv2.imread(str(img_path))
            if frame is None:
                logger.error("Failed to read image %s", img_path)
                return
        detections = pipeline.infer_once(frame)
        summary = [f"{d.label}:{d.confidence:.2f}" for d in detections]
        logger.info("Test detections (%s mode): %s", "mock" if mock_mode else "yolo", summary)
        if args.show:
            cv2.imshow("vision-test", draw_detections(frame.copy(), detections))
            cv2.waitKey(0)
            cv2.destroyAllWindows()
        return

    pub = make_publisher(cfg, channel="upstream")
    ctrl_sub = make_subscriber(cfg, topic=TOPIC_CMD_PAUSE_VISION, channel="downstream")
    poller = zmq.Poller()
    poller.register(ctrl_sub, zmq.POLLIN)

    paused = False
    frame_counter = 0
    cap = cv2.VideoCapture(camera_index)
    if not cap.isOpened():
        logger.error("Cannot open camera index %s", camera_index)
        return

    try:
        while True:
            if args.max_frames is not None and frame_counter >= args.max_frames:
                logger.info("Reached max frame budget (%s), exiting", args.max_frames)
                break

            socks = dict(poller.poll(timeout=10))
            if ctrl_sub in socks:
                try:
                    _topic, raw = ctrl_sub.recv_multipart(zmq.NOBLOCK)
                    msg = json.loads(raw)
                    paused = bool(msg.get("pause", False))
                    logger.info("Vision paused=%s", paused)
                except zmq.Again:
                    pass
                except json.JSONDecodeError as exc:
                    logger.error("Pause command decode error: %s", exc)

            if paused:
                time.sleep(0.05)
                continue

            ok, frame = cap.read()
            if not ok:
                logger.warning("Failed to read frame from camera")
                time.sleep(0.05)
                continue

            detections = pipeline.infer_once(frame)
            ts = time.time()
            publish_detections(pub, detections, ts)
            frame_counter += 1

            if args.show:
                vis = draw_detections(frame.copy(), detections)
                cv2.imshow("vision", vis)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break
    finally:
        cap.release()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    run()
