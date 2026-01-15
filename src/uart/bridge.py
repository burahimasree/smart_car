"""UART bridge for navigation commands."""
from __future__ import annotations

import json
from pathlib import Path

from src.core.ipc import make_subscriber, TOPIC_NAV
from src.core.config_loader import load_config
from src.core.logging_setup import get_logger

try:
    import serial  # type: ignore
except Exception:  # pragma: no cover
    serial = None


logger = get_logger("uart.bridge", Path("logs"))


def run() -> None:
    cfg = load_config(Path("config/system.yaml"))
    nav = cfg.get("nav", {})
    device = nav.get("uart_device", "/dev/ttyAMA0")
    baud = int(nav.get("baud_rate", 115200))
    timeout = float(nav.get("timeout", 1.0))
    commands = nav.get("commands", {})

    if serial is None:
        logger.error("pyserial not installed; cannot open UART")
        return
    try:
        ser = serial.Serial(port=device, baudrate=baud, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to open UART %s: %s", device, e)
        return

    sub = make_subscriber(cfg, topic=TOPIC_NAV, channel="downstream")
    logger.info("UART bridge listening for nav commands on %s", TOPIC_NAV)
    try:
        while True:
            _topic, data = sub.recv_multipart()
            try:
                msg = json.loads(data)
                direction = msg.get("direction", "stop")
                cmd = commands.get(direction, "STOP")
                ser.write(f"{cmd}\n".encode())
                logger.info("UART sent: %s", cmd)
            except Exception as e:  # noqa: BLE001
                logger.error("Invalid nav payload: %s", e)
    finally:
        ser.close()


if __name__ == "__main__":
    run()
