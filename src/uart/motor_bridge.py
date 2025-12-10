#!/usr/bin/env python3
"""Enhanced UART bridge with bidirectional communication and ESP32 feedback.

Supports:
- Sending navigation commands to ESP32 motor controller
- Receiving status/sensor feedback from ESP32
- Speed control and duration parameters
- Emergency stop handling
"""
from __future__ import annotations

import argparse
import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from queue import Queue
from typing import Any, Dict, Optional

from src.core.config_loader import load_config
from src.core.ipc import (
    TOPIC_ESP,
    TOPIC_NAV,
    make_publisher,
    make_subscriber,
    publish_json,
)
from src.core.logging_setup import get_logger

try:
    import serial
    SERIAL_AVAILABLE = True
except ImportError:
    serial = None
    SERIAL_AVAILABLE = False

logger = get_logger("uart.motor_bridge", Path("logs"))


@dataclass
class MotorCommand:
    """Motor control command structure."""
    direction: str  # forward, backward, left, right, stop
    speed: int = 100  # 0-100 percentage
    duration_ms: int = 0  # 0 = continuous until next command
    target: Optional[str] = None  # optional vision target


class UARTMotorBridge:
    """Bidirectional UART bridge for ESP32 motor control."""

    # Default command mapping (can be overridden from config)
    # Align with ESP32 sketch command tokens in esp-code.ino
    DEFAULT_COMMANDS = {
        "forward": "FORWARD",
        "backward": "BACKWARD",
        "left": "LEFT",
        "right": "RIGHT",
        "stop": "STOP",
        "status": "STATUS",
        "reset": "RESET",
    }

    def __init__(self, config: Dict[str, Any], sim: bool = False):
        self.config = config
        self.sim = sim
        nav_cfg = config.get("nav", {})

        self.device = nav_cfg.get("uart_device", "/dev/ttyAMA0")
        self.baud = int(nav_cfg.get("baud_rate", 115200))
        self.timeout = float(nav_cfg.get("timeout", 1.0))
        self.commands = {**self.DEFAULT_COMMANDS, **nav_cfg.get("commands", {})}

        self.serial: Optional[serial.Serial] = None
        self._running = False
        self._rx_queue: Queue = Queue()
        self._rx_thread: Optional[threading.Thread] = None

        # IPC sockets
        self.sub: Optional[Any] = None
        self.pub: Optional[Any] = None

    def _open_serial(self) -> bool:
        """Open serial port connection."""
        if self.sim:
            logger.info("UART simulation mode - no serial port opened")
            return True

        if not SERIAL_AVAILABLE:
            logger.error("pyserial not installed")
            return False

        try:
            self.serial = serial.Serial(
                port=self.device,
                baudrate=self.baud,
                timeout=self.timeout,
                write_timeout=self.timeout,
            )
            logger.info("UART opened: %s @ %d baud", self.device, self.baud)
            return True
        except Exception as e:
            logger.error("Failed to open UART %s: %s", self.device, e)
            return False

    def _close_serial(self) -> None:
        """Close serial port."""
        if self.serial:
            try:
                self.serial.close()
            except Exception:
                pass
            self.serial = None

    def _format_command(self, cmd: MotorCommand) -> str:
        """Format command for ESP32 protocol (esp-code.ino).

        Supported tokens:
          - FORWARD, BACKWARD, LEFT, RIGHT, STOP, STATUS, RESET
          - SERVO:<angle> (when target field is used as angle)

        Notes:
          - The current .ino ignores speed/duration for basic moves; commands
            are digital full-speed. We emit plain tokens accordingly.
        """
        direction = (cmd.direction or "").lower()
        if direction == "servo":
            # Use cmd.target to pass the angle when requested
            try:
                angle = int(cmd.target) if cmd.target is not None else 90
            except Exception:
                angle = 90
            return f"SERVO:{angle}\n"

        base_cmd = self.commands.get(direction, None)
        if not base_cmd:
            # Fallback to STOP if unknown
            base_cmd = "STOP"
        return f"{base_cmd}\n"

    def _send_command(self, cmd: MotorCommand) -> bool:
        """Send command to ESP32."""
        formatted = self._format_command(cmd)

        if self.sim:
            logger.info("[SIM] UART TX: %s", formatted.strip())
            return True

        if not self.serial or not self.serial.is_open:
            logger.error("Serial port not open")
            return False

        try:
            self.serial.write(formatted.encode("utf-8"))
            self.serial.flush()
            logger.info("UART TX: %s", formatted.strip())
            return True
        except Exception as e:
            logger.error("UART write failed: %s", e)
            return False

    def _rx_loop(self) -> None:
        """Background thread for reading ESP32 responses."""
        while self._running:
            if self.sim:
                time.sleep(0.1)
                continue

            if not self.serial or not self.serial.is_open:
                time.sleep(0.1)
                continue

            try:
                if self.serial.in_waiting > 0:
                    line = self.serial.readline().decode("utf-8", errors="replace").strip()
                    if line:
                        self._rx_queue.put(line)
                        logger.debug("UART RX: %s", line)
            except Exception as e:
                logger.warning("UART read error: %s", e)
                time.sleep(0.1)

    def _process_rx(self) -> None:
        """Process received data from ESP32 and publish to IPC."""
        while not self._rx_queue.empty():
            try:
                line = self._rx_queue.get_nowait()
                # Parse ESP32 feedback (expected format: STATUS:value or JSON)
                if line.startswith("{"):
                    # JSON response (future extension)
                    payload = json.loads(line)
                else:
                    # Parse ACK/STATUS and sensor data formats from esp-code.ino
                    # Examples:
                    #   ACK:<CMD>:<STATUS>
                    #   STATUS:SERVO:<angle>,LMOTOR:<ls>,RMOTOR:<rs>
                    #   DATA:S1:<d1>,S2:<d2>,S3:<d3>,MQ2:<v>,SERVO:<a>,LMOTOR:<ls>,RMOTOR:<rs>
                    parts = line.split(":")
                    if not parts:
                        payload = {"raw": line}
                    elif parts[0] == "ACK" and len(parts) >= 3:
                        payload = {"ack": parts[1], "status": ":".join(parts[2:])}
                    elif parts[0] == "STATUS":
                        payload = {"status_raw": ":".join(parts[1:])}
                    elif parts[0] == "DATA":
                        payload = {"data_raw": ":".join(parts[1:])}
                    else:
                        payload = {"type": parts[0], "value": ":".join(parts[1:])}

                if self.pub:
                    publish_json(self.pub, TOPIC_ESP, payload)
                    logger.debug("Published ESP32 feedback: %s", payload)

            except Exception as e:
                logger.warning("Failed to process RX: %s", e)

    def _parse_nav_command(self, payload: Dict[str, Any]) -> MotorCommand:
        """Parse IPC navigation payload into MotorCommand."""
        direction = payload.get("direction", "stop")
        speed = int(payload.get("speed", 100))
        duration = int(payload.get("duration_ms", 0))
        target = payload.get("target")

        return MotorCommand(
            direction=direction,
            speed=max(0, min(100, speed)),
            duration_ms=max(0, duration),
            target=target,
        )

    def run(self) -> None:
        """Main bridge loop."""
        if not self._open_serial():
            if not self.sim:
                logger.error("Cannot run bridge without serial port (use --sim for simulation)")
                return

        # Setup IPC
        self.sub = make_subscriber(self.config, topic=TOPIC_NAV, channel="downstream")
        self.pub = make_publisher(self.config, channel="upstream")

        # Start RX thread
        self._running = True
        if not self.sim:
            self._rx_thread = threading.Thread(target=self._rx_loop, daemon=True)
            self._rx_thread.start()

        logger.info("UART Motor Bridge running (device=%s, sim=%s)", self.device, self.sim)

        try:
            while self._running:
                # Check for incoming nav commands
                try:
                    topic, data = self.sub.recv_multipart()
                    payload = json.loads(data)
                    cmd = self._parse_nav_command(payload)
                    self._send_command(cmd)
                except Exception as e:
                    logger.error("Failed to process nav command: %s", e)

                # Process any ESP32 feedback
                self._process_rx()

        except KeyboardInterrupt:
            logger.info("Bridge interrupted")
        finally:
            self._running = False
            self._close_serial()
            logger.info("UART Motor Bridge stopped")

    def stop(self) -> None:
        """Stop the bridge."""
        self._running = False
        # Send emergency stop
        if self.serial and self.serial.is_open:
            self._send_command(MotorCommand(direction="stop"))


def run() -> None:
    """Legacy entry point for compatibility."""
    cfg = load_config(Path("config/system.yaml"))
    bridge = UARTMotorBridge(cfg)
    bridge.run()


def main() -> None:
    parser = argparse.ArgumentParser(description="UART Motor Bridge for ESP32")
    parser.add_argument("--sim", action="store_true", help="Run in simulation mode (no serial)")
    parser.add_argument("--device", default=None, help="Override UART device path")
    parser.add_argument("--baud", type=int, default=None, help="Override baud rate")
    args = parser.parse_args()

    cfg = load_config(Path("config/system.yaml"))

    # Apply CLI overrides
    if args.device:
        cfg.setdefault("nav", {})["uart_device"] = args.device
    if args.baud:
        cfg.setdefault("nav", {})["baud_rate"] = args.baud

    bridge = UARTMotorBridge(cfg, sim=args.sim)
    bridge.run()


if __name__ == "__main__":
    main()
