# Industrial Grade Flaw Audit Report

**Date**: 2026-01-18
**Auditor**: Expert Embedded Systems Agent
**Scope**: Core Architecture, Reliability, Security, Performance.

## Executive Summary
The `smart_car` repository demonstrates **advanced** architectural patterns (Event-Driven FSM, Threaded I/O, Rotating Logging) rare in prototype code. However, to meet **Industrial Grade** standards (IEC 61508 / ISO 26262 principles), specific "Fail-Safe" and "Determinism" gaps must be addressed.

## Critical Findings (Priority 1)

### 1. CPU Spin Risk in Orchestrator
**Location**: `src/core/orchestrator.py:333`
**Issue**: The main event loop catches `Exception` but immediately `continue`s.
**Risk**: If a persistent error occurs (e.g., ZMQ driver failure), the loop will spin at 100% CPU, overheating the Pi and potentially causing voltage sag that resets the microcontroller.
**Recommendation**:
```python
except Exception as exc:
    logger.error("Recv/parse error: %s", exc)
    time.sleep(0.1)  # Prevent CPU spin
    continue
```

### 2. Missing Hardware Watchdog Keeping
**Location**: `src/core/orchestrator.py`
**Issue**: The system relies on software logging to report health. If the Python interpreter hangs (GIL deadlock), `systemd` might not know.
**Risk**: Silent failure where the car stops responding but appears "on".
**Recommendation**: Implement `systemd-notify` watchdog pings or a dedicated GPIO heartbeat to the ESP32.

### 3. ZMQ Socket Linger
**Location**: `src/core/ipc.py`
**Issue**: Sockets are created without `setsockopt(zmq.LINGER, 0)`.
**Risk**: On service restart/shutdown, the context may hang indefinitely trying to flush messages to dead peers, preventing clean restarts.

## Moderate Findings (Priority 2)

### 1. Hardcoded Model Paths
**Location**: `src/vision/vision_runner.py:153`, `src/core/config_loader.py`
**Issue**: Fallback paths like `models/vision/yolo11n.onnx` are hardcoded.
**Risk**: Deployment to a different directory structure (e.g., `/opt/smart_car`) will break the system.
**Recommendation**: Always use `${PROJECT_ROOT}` relative paths in `config/system.yaml` and remove code-level hardcoding.

### 2. Lock Contention in Vision
**Location**: `src/vision/vision_runner.py:88`
**Issue**: `self._latest_frame.copy()` runs inside the lock.
**Risk**: For high-res frames (4K), memory copy time increases, potentially blocking the capture thread.
**Recommendation**: This is acceptable for 640x480 (VGA), but for industrial scalability, double-buffering or zero-copy shared memory is preferred.

## Security & Reliability Affirmations
*   ✅ **Secrets Management**: Excellent use of `${ENV:VAR}` in `config_loader.py`. No secrets verified in code.
*   ✅ **Disk Safety**: `RotatingFileHandler` prevents log files from filling the SD card.
*   ✅ **Thread Safety**: `LowestFrameGrabber` correctly manages access to the shared frame buffer checking valid `isOpened()` state.

## Final Verdict
**Current Grade**: A- (Prototyping Excellence)
**Target Grade**: A+ (Industrial Robustness)
**Action Plan**: Apply the 3 Critical Fixes to ensure 24/7 reliability.
