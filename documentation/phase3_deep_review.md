# PHASE-3: Deep System Review, Bug Hunt, and Improvement Analysis

## Document Information

| Attribute | Value |
|-----------|-------|
| Document | phase3_deep_review.md |
| Purpose | Comprehensive system audit with findings and recommendations |
| Scope | Full codebase, architecture, runtime behavior |
| Date | 2026-02-01 |

---

## 1. Executive Summary (Hard Truths)

This system is architecturally sound but operationally brittle. The pub-sub design, service isolation, and systemd supervision are genuinely well-conceived. However, the implementation contains several patterns that will cause production failures:

**Critical Issues:**
1. **Git divergence is unrecoverable without manual intervention.** PC and Pi codebases have diverged substantially. The repository does not represent deployed behavior.
2. **No backpressure handling in ZMQ buses.** High-frequency vision/sensor events can overwhelm slow consumers.
3. **TTS and voice share audio hardware with no arbitration.** Attempting to speak while listening causes undefined behavior.
4. **Systemd restart loops are likely under resource exhaustion.** Services have `Restart=on-failure` with 3s delay but no rate limiting beyond the default.
5. **Log files grow unbounded.** No rotation configured in systemd or application code.

**High-Confidence Bugs:**
- Race condition: Vision mode can change while capture is in progress
- Resource leak: Voice service's PyAudio stream can be orphaned on certain exception paths
- Silent failure: Azure TTS runner returns `done=False` on failure but orchestrator treats any response as success
- State desynchronization: Remote interface can publish session active while orchestrator has already timed it out

**Strengths:**
- Fault isolation through process boundaries is real and valuable
- The FSM in the orchestrator is explicit and well-guarded
- Pi-side safety backup for motor commands is a defense-in-depth win
- The mobile app correctly gates actions based on session state

---

## 2. Confirmed Bugs (High Confidence)

### BUG-001: Vision Mode Race Condition

**Subsystem:** Vision Runner  
**Location:** [src/vision/vision_runner.py](src/vision/vision_runner.py#L408-L450)

**Evidence:**
```python
# Line 408-450: Command check block
if topic == TOPIC_CMD_VISION_MODE:
    mode = _normalize_vision_mode(msg.get("mode", ""))
    vision_mode = mode
    stream_enabled = (mode == "on_with_stream")
    logger.info("Vision mode set to %s", vision_mode)
```

**Problem:** The `vision_mode` variable is read and written without synchronization while the main loop is processing frames. If a mode change arrives mid-inference, `force_capture` logic and `stream_enabled` state become inconsistent. The grabber may continue running after mode is set to "off" until the next iteration.

**When it manifests:** Remote app rapidly toggles vision mode, or orchestrator pauses/unpauses vision during voice capture.

**Impact:** Camera resource held when it should be released; streaming continues after mode set to off; frame published after capture is "complete."

**Fix:** Use a threading.Lock or an Event-based state machine. Mode changes should be processed atomically before the next frame is grabbed.

---

### BUG-002: PyAudio Stream Resource Leak

**Subsystem:** Voice Service  
**Location:** [src/audio/voice_service.py](src/audio/voice_service.py#L331-L350)

**Evidence:**
```python
def stop(self):
    """Clean up resources."""
    self._running = False
    # ...
    if self.stream:
        try:
            self.stream.stop_stream()
            self.stream.close()
        except:
            pass
```

**Problem:** The `stop()` method has bare `except: pass` blocks that silently swallow all errors. If `stop_stream()` raises (common on device disconnection), `close()` is never called, and the stream handle leaks. Additionally, `self.pa.terminate()` can fail if streams are still active, leaking the entire PyAudio context.

**When it manifests:** USB microphone is unplugged while capturing, or service is killed during stream read.

**Impact:** ALSA device handles exhausted; subsequent service restarts fail with "device busy" until system reboot.

**Fix:** Use `finally` blocks and log exceptions instead of ignoring them. Consider a context manager pattern for PyAudio lifecycle.

---

### BUG-003: TTS Completion Not Checked Properly

**Subsystem:** Orchestrator + TTS Runner  
**Location:** [src/core/orchestrator.py](src/core/orchestrator.py#L384-L398), [src/tts/azure_tts_runner.py](src/tts/azure_tts_runner.py#L114)

**Evidence (TTS runner):**
```python
publish_json(pub, TOPIC_TTS, {"done": bool(ok)})
```

**Evidence (Orchestrator):**
```python
def on_tts(self, payload: Dict[str, Any]) -> None:
    # ...
    done = payload.get("done") or payload.get("final") or payload.get("completed")
    # ...
    if not done:
        return
    # Transitions to IDLE
```

**Problem:** The TTS runner publishes `{"done": False}` on synthesis failure, and the orchestrator checks `if not done: return`. This means a failure is not treated as completion—the orchestrator remains in SPEAKING state indefinitely until a timeout (which doesn't exist for SPEAKING phase).

**When it manifests:** Azure TTS API fails (network error, quota exceeded, invalid voice).

**Impact:** Robot freezes in SPEAKING phase with no recovery path. User must wait for manual intervention.

**Fix:** Treat `done=False` as an error completion that triggers return to IDLE with an error notification. Add a SPEAKING phase timeout.

---

### BUG-004: Session State Desynchronization

**Subsystem:** Remote Interface + Orchestrator  
**Location:** [src/remote/remote_interface.py](src/remote/remote_interface.py#L268-L285), [src/core/orchestrator.py](src/core/orchestrator.py#L430-L436)

**Evidence:**
```python
# remote_interface.py - Session watchdog
if active and last_seen and (now - last_seen) > self.session_timeout_s:
    with self.telemetry.lock:
        self.telemetry.remote_session_active = False
    self._publish_session_state()
```

```python
# orchestrator.py - Session timeout check
if (now - self._remote_last_seen) > self.remote_session_timeout_s:
    self._remote_session_active = False
    publish_json(self.cmd_pub, TOPIC_REMOTE_SESSION, {...})
```

**Problem:** Both remote_interface and orchestrator independently track session state with their own timeouts. They can disagree: remote_interface may mark session as inactive and publish that, but orchestrator has a different `last_seen` timestamp and thinks session is still active. When remote_interface later publishes `active=False`, orchestrator may still process intents from a "dead" session.

**When it manifests:** Network hiccup causes one party's last_seen to drift; timing-dependent race.

**Impact:** Intent commands accepted after session should be expired, or rejected when session should be active. Leads to confusing UX.

**Fix:** Single source of truth for session state. Either orchestrator exclusively owns session lifecycle (remote_interface only reports heartbeats), or a dedicated session manager mediates.

---

### BUG-005: Silence Detection False Positive on Startup

**Subsystem:** Voice Service  
**Location:** [src/audio/voice_service.py](src/audio/voice_service.py#L660-L680)

**Evidence:**
```python
if rms < self.silence_threshold:
    silence_frames += 1
    if (silence_frames >= silence_frames_needed and 
        elapsed >= MIN_CAPTURE_SECONDS and
        speech_frames >= MIN_SPEECH_FRAMES):
        # End capture
```

**Problem:** The MIN_SPEECH_FRAMES (3) gate is good, but if the environment is quiet when capture starts, the initial frames may be classified as silence before any speech occurs. If there's brief ambient noise above threshold followed by silence, speech_frames accumulates falsely, causing premature capture end.

**When it manifests:** Noisy environment with intermittent sounds (fan cycling, distant traffic).

**Impact:** User's speech truncated; incomplete transcription.

**Fix:** Reset silence_frames counter when speech is detected. Use a more robust voice activity detection algorithm (WebRTC VAD or energy-based with hysteresis).

---

## 3. Probable Bugs / Edge-Case Failures

### PROB-001: UART Partial Write Not Handled

**Subsystem:** Motor Bridge  
**Location:** [src/uart/motor_bridge.py](src/uart/motor_bridge.py#L283-L295)

**Issue:** `self.serial.write(formatted.encode("utf-8"))` assumes the entire string is written. Serial write can return fewer bytes than requested under load or hardware issues. The code doesn't check the return value.

**Likelihood:** Low (115200 baud with small commands)  
**Impact:** Corrupted command to ESP32 causing undefined motor behavior.

**Fix:** Check return value; implement retry with timeout.

---

### PROB-002: ZMQ Socket Exhaustion on Rapid Reconnection

**Subsystem:** IPC Layer  
**Location:** [src/core/ipc.py](src/core/ipc.py#L56-L78)

**Issue:** `make_publisher` and `make_subscriber` use `zmq.Context.instance()` singleton, but sockets are never explicitly closed in most services. If a service crashes and restarts rapidly, stale sockets may accumulate until the context is destroyed.

**Likelihood:** Medium (especially with systemd rapid restarts)  
**Impact:** File descriptor exhaustion; "too many open files" error.

**Fix:** Implement explicit socket cleanup in service shutdown handlers.

---

### PROB-003: Conversation Memory Unbounded Growth

**Subsystem:** LLM Runner  
**Location:** [src/llm/conversation_memory.py](src/llm/conversation_memory.py#L156-L160)

**Issue:** `max_turns * 2` is the message limit (20 by default), but `_summary` can grow to 500 characters indefinitely. If conversations are long and frequent, memory accumulates. More critically, there's no conversation timeout reset—`_state` stays in FOLLOW_UP forever.

**Likelihood:** Medium (long sessions without restart)  
**Impact:** Stale context injected into LLM requests; degraded response quality.

**Fix:** Implement conversation expiration based on `conversation_timeout_s`. Reset state to IDLE after timeout.

---

### PROB-004: Vision Grabber Thread Not Joined on Error

**Subsystem:** Vision Runner  
**Location:** [src/vision/vision_runner.py](src/vision/vision_runner.py#L157-L168)

**Issue:** `stop()` method joins with 2s timeout but doesn't check if thread actually terminated. If the grabber is stuck in `picam2.capture_array()`, the thread becomes orphaned.

**Likelihood:** Low (picamera2 usually responsive)  
**Impact:** Resource leak; camera held by zombie thread.

**Fix:** Use daemon threads (already set) and accept potential leakage, or implement a more robust cancellation mechanism.

---

### PROB-005: ESP32 Alert Handling Race

**Subsystem:** Orchestrator  
**Location:** [src/core/orchestrator.py](src/core/orchestrator.py#L399-L407)

**Issue:** When ESP32 sends `ALERT:COLLISION`, orchestrator publishes stop command, but there's no guarantee motor_bridge has processed the ESP32's internal stop. The command may arrive after ESP32 has already stopped, causing a redundant command, or worse, if ESP32 cleared the alert, the Pi's stop may conflict with a subsequent move.

**Likelihood:** Low (collision is rare and ESP32 handles it immediately)  
**Impact:** Momentary confusion in command state.

**Fix:** Use acknowledgment flow for critical commands.

---

## 4. Architectural Stress Points

### ARCH-001: No Backpressure in ZMQ Pub-Sub

**Issue:** ZMQ PUB socket drops messages if SUB is too slow. Vision publishes frames at 15 FPS (TOPIC_VISN_FRAME), orchestrator forwards them to downstream. If remote_interface is slow (network hiccup), frames are silently dropped.

**Why it matters:** Silent data loss causes inconsistent state. Remote app shows stale frames without knowing why.

**Comparative insight:** ROS uses subscriber queue depths with configurable dropping policy. Industrial systems use explicit flow control or REQ/REP for critical paths.

**Recommendation:** Use high-water mark settings (`ZMQ_SNDHWM`, `ZMQ_RCVHWM`) to control buffer size. Log when dropping occurs. Consider separate sockets for critical vs. streaming data.

---

### ARCH-002: Orchestrator is Single Point of Failure

**Issue:** Orchestrator binds both ZMQ ports. If it crashes, all other services lose communication and must reconnect. Services don't have reconnection logic—they assume sockets stay connected.

**Why it matters:** A bug in orchestrator (even a transient exception) can partition the entire system.

**Comparative insight:** Microservice architectures use message brokers (RabbitMQ, Redis) as the hub, not application code. The broker survives while services restart.

**Recommendation:** For this scale, accept the risk. But add linger settings and explicit socket error handling in all services. Document that orchestrator must be the first service started and last stopped.

---

### ARCH-003: Mixed Responsibility in Remote Interface

**Issue:** Remote interface does three things: (1) serves HTTP, (2) bridges to IPC, (3) maintains telemetry state. This creates coupling—HTTP handler logic has knowledge of ZMQ topics and state machine.

**Why it matters:** Hard to test in isolation. Bug in telemetry tracking affects HTTP responses.

**Recommendation:** Refactor into three components: HTTP server (stateless), telemetry aggregator (subscribes to IPC), intent publisher (translates HTTP to IPC). Can remain in same process but with clear boundaries.

---

### ARCH-004: Configuration Validation is Implicit

**Issue:** Services read `system.yaml` and fail at runtime if values are missing or wrong. No schema validation. Environment variable substitution (e.g., `${ENV:AZURE_SPEECH_KEY}`) happens in config loader with no error if variable is unset (results in empty string).

**Why it matters:** Misconfiguration manifests as cryptic runtime errors deep in service code.

**Comparative insight:** Kubernetes uses admission controllers and CRD validation. Docker Compose has schema validation. Pydantic models can validate config at load time.

**Recommendation:** Add a config validation step at startup (possibly a shared utility). Fail fast with clear error messages.

---

### ARCH-005: Monolithic System Prompt in LLM Memory

**Issue:** The `SYSTEM_PROMPT_TEMPLATE` in conversation_memory.py is 500+ characters of static text, plus robot state, plus conversation summary. Token budget is consumed by scaffolding, leaving less room for actual conversation.

**Why it matters:** Limits conversation depth. Cloud API costs are token-proportional.

**Recommendation:** Use a tiered prompt strategy: minimal system prompt for most turns, full context only when state changes significantly. Cache and reuse prompt prefixes where API supports it.

---

## 5. UX / UI Improvement Opportunities

### UX-001: Intent Feedback Latency

**Issue:** Mobile app sends intent, waits for HTTP 202 (accepted), but doesn't show robot response until next telemetry poll (up to 1s later).

**Why it matters:** User perceives system as sluggish. "Did it work?" uncertainty.

**Evidence:** [AppViewModel.kt](mobile_app/app/src/main/java/com/smartcar/supervision/ui/AppViewModel.kt#L200-L250) shows intent result stored but not immediately reflected in UI.

**Fix:** Optimistic UI update: show "command sent" immediately, then update when confirmed. Or use WebSocket for real-time push.

---

### UX-002: No Indication of Voice Pipeline State

**Issue:** Mobile app shows `mode` (idle, listening, thinking, speaking) but doesn't indicate wakeword detection or capture progress. User can't see that robot heard "Hey Robo" until transition to listening.

**Why it matters:** Voice interaction feels unresponsive.

**Fix:** Publish intermediate states (wakeword_detected, capturing) to telemetry. Display animated indicator during each phase.

---

### UX-003: Sensor Data Presentation is Raw

**Issue:** Sensor card shows `s1`, `s2`, `s3` distances as raw integers. No visualization of robot surroundings, no mapping of sensor positions.

**Why it matters:** Operator can't quickly assess spatial awareness.

**Fix:** Add a simple 2D radar view or arc diagram showing sensor positions and obstacle zones.

---

### UX-004: Error States Are Not Actionable

**Issue:** When connection fails, app shows "offline" or "error: <message>". No retry button, no diagnostic hint.

**Why it matters:** User stuck without knowing what to do.

**Fix:** Add explicit retry action. Show last successful connection time. Suggest checking Tailscale status.

---

### UX-005: Vision Stream Has No Loading State

**Issue:** MJPEG stream viewer shows blank or broken image until first frame arrives. No loading indicator.

**Why it matters:** Confusing—is it broken or just loading?

**Fix:** Show spinner or placeholder until first frame. Display stream metadata (resolution, FPS) when available.

---

## 6. Safety & Reliability Risks

### SAFE-001: Collision Avoidance Depends on ESP32 Firmware

**Issue:** Pi-side safety check in motor_bridge is a backup, but actual enforcement is in ESP32 firmware. That firmware is not in this repository. Behavior is unknown if firmware has bugs.

**Why it matters:** Safety-critical logic is outside version control.

**Evidence:** [motor_bridge.py](src/uart/motor_bridge.py#L212-L235) shows Pi checks, but comments note ESP32 is authoritative.

**Fix:** Either include firmware in repo, or document the protocol exhaustively and version-lock firmware builds.

---

### SAFE-002: No Emergency Stop Hardware Path

**Issue:** Emergency stop goes through software stack: app → HTTP → remote_interface → orchestrator → motor_bridge → UART → ESP32. Any layer failure blocks stop.

**Why it matters:** Uncontrolled robot can cause damage.

**Comparative insight:** Industrial robots have hardware E-stop that physically cuts motor power.

**Recommendation:** For a hobby project, acceptable. For anything more, add a hardware kill switch.

---

### SAFE-003: Gas Sensor Alert Path is Unclear

**Issue:** ESP32 sends MQ2 gas sensor readings, but no code in Pi handles elevated levels as an alert. Only logged, not acted upon.

**Why it matters:** Gas detection without response is false safety.

**Fix:** Add threshold checking and alert escalation (stop robot, notify user, trigger alarm pattern on LED).

---

### SAFE-004: Obstacle Detection While Reversing

**Issue:** Pi-side safety blocks forward when obstacle detected, but backward movement is always allowed. If robot is reversing toward an obstacle (sensors only face forward), it will collide.

**Why it matters:** Collision from behind.

**Evidence:** [motor_bridge.py](src/uart/motor_bridge.py#L217-L225) only checks for forward direction.

**Fix:** Add rear sensor, or implement time-limited backward movement with confirmation.

---

## 7. Observability & Debugging Gaps

### OBS-001: No Structured Logging Format

**Issue:** Log messages are free-form strings. Parsing them for metrics or alerts requires regex.

**Why it matters:** Can't efficiently search or aggregate logs.

**Fix:** Use JSON logging format. Add correlation IDs across IPC boundaries.

---

### OBS-002: No Metrics Endpoint

**Issue:** No Prometheus metrics, no health statistics beyond boolean `/health` check.

**Why it matters:** Can't monitor degradation over time.

**Fix:** Add `/metrics` endpoint with counters (wakewords detected, STT requests, LLM latency, frames processed).

---

### OBS-003: IPC Messages Not Logged Centrally

**Issue:** Each service logs what it sends/receives, but there's no central message trace.

**Why it matters:** Debugging cross-service issues requires correlating multiple log files.

**Fix:** Add optional IPC logger service that subscribes to all topics and writes to a unified trace file.

---

### OBS-004: No Crash Dump Collection

**Issue:** Unhandled exceptions in Python kill the service. Systemd restarts it, but stack trace is only in service log (which may rotate).

**Why it matters:** Transient bugs hard to diagnose.

**Fix:** Use a global exception handler that writes crash dumps to a dedicated directory.

---

## 8. Scalability & Performance Limits

### PERF-001: Single-Threaded Orchestrator

**Issue:** Orchestrator event loop is single-threaded. Long-running operations (like world context serialization for LLM request) block event processing.

**Why it matters:** High event rate can cause increased latency.

**Mitigation:** Current operations are fast enough. Monitor and consider async design if adding more complex logic.

---

### PERF-002: Vision Inference is CPU-Bound

**Issue:** YOLOv8 inference on Pi 4 takes 200-500ms per frame. At 15 FPS target, inference can't keep up—actual rate is 2-5 FPS.

**Why it matters:** Vision response is delayed; tracking can't follow fast motion.

**Fix:** Reduce inference rate, use smaller model (already using nano), or offload to Coral TPU.

---

### PERF-003: Azure STT Latency is Additive

**Issue:** STT requires upload to Azure, processing, return. Round-trip adds 500ms-2s depending on audio length and network.

**Why it matters:** Voice interaction feels slow.

**Comparative insight:** Whisper locally is slower on Pi 4 (5-10s for 15s audio). Azure is the better tradeoff.

**Fix:** Use streaming STT if Azure supports it (they do). Start processing before silence detection completes.

---

### PERF-004: No Connection Pooling for HTTP

**Issue:** Mobile app creates new Retrofit call for each request. No explicit HTTP/2 or keep-alive optimization.

**Why it matters:** Connection setup latency on each poll.

**Fix:** Configure OkHttp client with keep-alive and connection pooling (default behavior, but verify).

---

## 9. Security & Trust Boundary Issues

### SEC-001: API Keys in Environment Variables Logged

**Issue:** Environment variables are read at startup. If logging level is DEBUG and initialization fails, key values may appear in logs.

**Likelihood:** Low (not currently logging env values)  
**Impact:** Key exposure if logs are shared.

**Fix:** Never log env var values. Mask secrets in error messages.

---

### SEC-002: No Authentication on HTTP API

**Issue:** Remote interface validates IP (CIDR allowlist) but no authentication token. Anyone on Tailscale network can control the robot.

**Why it matters:** Shared Tailscale networks mean shared access.

**Fix:** Add bearer token or API key authentication. Store key in app settings.

---

### SEC-003: MJPEG Stream is Unauthenticated

**Issue:** `/stream/mjpeg` endpoint only checks CIDR. Anyone who knows the URL can watch the camera.

**Why it matters:** Privacy concern.

**Fix:** Same as SEC-002—require auth token.

---

### SEC-004: Serial Port Injection

**Issue:** Motor bridge parses ESP32 responses by splitting on `:`. Malformed data from ESP32 (or UART noise) could cause unexpected parsing.

**Likelihood:** Low (ESP32 firmware is trusted)  
**Impact:** Log pollution at worst; no command injection path.

**Fix:** Validate parsed values before use. Log and ignore malformed messages.

---

## 10. Comparative Insights from Similar Systems

### ROS-Based Robots

**What ROS does better:**
- Standardized message types with schema
- Built-in recording and playback (rosbag)
- Sophisticated navigation stack (move_base)
- Transform trees for sensor fusion
- Simulation support (Gazebo)

**What smart_car does better:**
- Dramatically simpler to understand and deploy
- No dependency on ROS ecosystem (which is heavyweight)
- Cloud AI integration is first-class, not bolted on

**What ROS solved that smart_car hasn't:**
- Multi-robot coordination
- SLAM and mapping
- Formal safety certifications

---

### Voice Assistant Stacks (Mycroft/OVOS)

**What Mycroft does better:**
- Plugin architecture for skills
- Fallback handling when STT fails
- Multi-wakeword support
- On-device privacy options

**What smart_car does better:**
- Physical actuation integration
- Vision as part of the loop
- Simpler deployment (no Mycroft Core dependency)

**What Mycroft solved that smart_car hasn't:**
- Skill marketplace / extensibility
- Multi-language support
- User identification

---

### GitHub Smart Car Projects

Reviewed: `donkey_car`, `JetBot`, `aws-robomaker-sample-application-deepracer`

**Common patterns:**
- Separate training and inference modes
- Teleop (teleoperation) mode for data collection
- ROS or simplified custom IPC
- Model serving via ONNX or TensorRT

**What smart_car does differently:**
- Voice is primary interface, not web UI
- Cloud LLM for reasoning (others use RL policies)
- No training loop—pure inference

**What those projects solved:**
- Autonomous path following (donkey_car)
- GPU inference optimization (JetBot)
- CI/CD for robotics (RoboMaker)

---

## 11. High-Impact Improvements (Ranked)

| Priority | Item | Effort | Impact |
|----------|------|--------|--------|
| 1 | **Resolve git divergence** | 1 day | Critical - no reproducibility without this |
| 2 | **Add SPEAKING phase timeout** | 2 hours | Prevents stuck state |
| 3 | **Implement log rotation** | 1 hour | Prevents disk exhaustion |
| 4 | **Add config schema validation** | 4 hours | Prevents misconfiguration errors |
| 5 | **Fix TTS failure handling** | 2 hours | Prevents silent stuck state |
| 6 | **Add session state single source of truth** | 4 hours | Eliminates state desync bugs |
| 7 | **Implement backpressure logging** | 2 hours | Visibility into message dropping |
| 8 | **Add HTTP API authentication** | 4 hours | Security baseline |
| 9 | **Add metrics endpoint** | 8 hours | Operational visibility |
| 10 | **Vision mode thread safety** | 4 hours | Race condition fix |

---

## 12. Low-Effort / High-Return Fixes

1. **Add `RestartSec=5` and `StartLimitBurst=5` to systemd units** (10 min)
   - Prevents restart storms under persistent failure

2. **Configure `StandardOutput=journal` instead of append** (10 min)
   - Leverages journald rotation automatically

3. **Add `finally` blocks in voice service stop()** (15 min)
   - Prevents resource leaks on exception

4. **Add SPEAKING timeout to orchestrator** (30 min)
   - Add timeout check in `_check_timeouts()` for SPEAKING phase

5. **Log ZMQ HWM drops** (30 min)
   - Subscribe to ZMQ monitor sockets for visibility

6. **Add `/ready` endpoint to remote interface** (15 min)
   - Differentiate between healthy and fully initialized

7. **Validate required env vars at startup** (30 min)
   - Fail fast with clear error instead of cryptic API errors

---

## 13. Improvements NOT Recommended (and why)

### NOT: Migrate to ROS2

**Why not:** The system works. ROS2 adds significant complexity (DDS middleware, new build system, different deployment model) without proportional benefit for this scale. Only consider if multi-robot or SLAM becomes required.

### NOT: Replace ZMQ with Redis/RabbitMQ

**Why not:** External broker adds deployment complexity and latency. ZMQ's in-process nature is a feature for embedded systems. The current issues are solvable with configuration, not replacement.

### NOT: Rewrite in Rust for memory safety

**Why not:** The memory issues are minor (leaks, not corruption). Python ecosystem advantages (AI libraries, rapid prototyping) outweigh theoretical safety gains. Fix specific bugs instead.

### NOT: Add full CI/CD pipeline now

**Why not:** The git divergence must be resolved first. CI won't help if the canonical branch doesn't represent deployed code. Fix the human process problem before automating it.

### NOT: Implement WebSocket for mobile app

**Why not:** Polling at 1s is adequate for this UX. WebSocket adds server complexity (state management, connection lifecycle). The mobile app's issues are UX design, not transport protocol.

---

## 14. What NOT To Touch Yet (stability reasons)

### Voice Pipeline

The wakeword + STT flow is documented as "TESTED AND WORKING: 10/10 wakeword detection rate." The silence detection thresholds are calibrated. Changes risk regression.

**What to preserve:** `SENSITIVITY = 0.7`, `SILENCE_THRESHOLD = 0.25`, `SILENCE_DURATION_MS = 1200`

### ESP32 Protocol

The UART protocol is simple and stable. ESP32 firmware is not in repo. Changing the protocol requires coordinated firmware update.

**What to preserve:** Text-based commands with newline framing

### ZMQ Port Assignments

Port 6010/6011 are configured and working. Changing them requires updating all services and systemd units.

**What to preserve:** Port allocations in `system.yaml`

### Virtual Environment Structure

Five venvs are configured and systemd units reference them. Changing venv paths requires updating all service files.

**What to preserve:** `.venvs/stte`, `.venvs/llme`, `.venvs/ttse`, `.venvs/visn-py313`

---

## 15. Final Verdict (Engineering Perspective)

This is a **well-designed system with implementation gaps**. The architecture—separate processes, message passing, systemd supervision, defense-in-depth safety—reflects thoughtful engineering. The codebase is readable, organized, and documented where it matters.

However, the system is not production-ready:

1. **The git divergence is the elephant in the room.** Until PC and Pi are synchronized and committed, no improvement has a reliable baseline.

2. **Edge cases are under-handled.** The happy path works. Failure modes (TTS fails, session expires, camera disconnects) lead to stuck states or silent failures.

3. **Observability is insufficient for operations.** Logs exist but aren't structured. Metrics don't exist. Cross-service tracing requires manual correlation.

4. **Security is embryonic.** IP allowlisting is not authentication. For a personal project, acceptable. For any shared access, inadequate.

**Recommendation:** Spend the next sprint on hardening, not features. Resolve git divergence, add timeouts for all blocking phases, implement log rotation, and add basic auth. Only then consider new capabilities.

**Bottom line:** This robot works. With 2-4 weeks of focused reliability work, it could work reliably.

---

## Appendix: Files Analyzed

| Category | Files |
|----------|-------|
| Core | orchestrator.py, ipc.py, config_loader.py, world_context.py |
| Audio | voice_service.py |
| Vision | vision_runner.py, detector.py, pipeline.py |
| Motor | motor_bridge.py |
| LLM | azure_openai_runner.py, conversation_memory.py |
| TTS | azure_tts_runner.py, piper_runner.py |
| Remote | remote_interface.py |
| Config | system.yaml, logging.yaml |
| Systemd | All 9 service units |
| Mobile | AppViewModel.kt, MainScreen.kt, Models.kt, RobotApi.kt |
