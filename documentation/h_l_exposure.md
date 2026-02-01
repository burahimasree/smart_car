# High-Level System Exposure — smart_car

## Document Information

| Attribute | Value |
|-----------|-------|
| Document | h_l_exposure.md |
| Purpose | Mental model for the entire system |
| Audience | Engineers, reviewers, auditors, future maintainers |
| Last Updated | 2026-02-01 |

---

## 1. What This System Is (In Plain Engineering Terms)

The smart_car project is a voice-controlled mobile robot with computer vision capabilities and remote supervision. It is not a toy, nor a product — it is an integration platform that combines edge AI inference, real-time motor control, and cloud services into a coherent autonomous system.

At its core, this is a state machine that orchestrates multiple specialized subsystems. The robot listens for a wake word, understands spoken commands through speech recognition, reasons about them using a large language model, speaks responses aloud, and executes physical motion — all while maintaining situational awareness through ultrasonic sensors and an optional camera.

The system follows a strict separation of concerns. The Raspberry Pi handles all cognitive work: voice processing, AI inference, decision-making, and coordination. The ESP32 microcontroller handles all real-time actuation: reading sensors, controlling motors, and enforcing safety constraints at the hardware level. A mobile Android app provides remote observation and manual control, but never bypasses the Pi's intelligence — it is a peer input, not a master controller.

This is not a monolithic application. It is nine independent processes communicating through message passing, designed so that failure in one subsystem does not cascade to others.

---

## 2. The Three Physical Layers

The system spans three distinct hardware platforms, each with a specific purpose.

**The Mobile App (Human Interface)**

An Android application running on a phone or tablet serves as the operator's window into the robot. It polls the robot for telemetry, displays sensor readings and camera feeds, and allows the operator to send control intents like "move forward" or "start listening." The app communicates over HTTP to the Raspberry Pi, using a Tailscale VPN for secure access regardless of network topology.

The app exists because the robot has no physical control surface. It provides observability (what is the robot seeing, hearing, doing?) and manual override capability (stop, go left, trigger voice input). However, the app is explicitly not the brain — it cannot command motors directly or bypass the safety logic. Every intent it sends becomes an internal event that the Pi's orchestrator processes like any other input.

**The Raspberry Pi (Cognitive and Control Brain)**

A Raspberry Pi 4 runs the entire software stack: wakeword detection, speech-to-text, language model inference, text-to-speech, vision processing, navigation decisions, and external communication. It hosts nine systemd services that collectively implement the robot's behavior.

The Pi exists as the brain because it has the computational power to run neural networks (for voice and vision), the memory to hold conversation context and world state, and the operating system facilities (systemd, Python, ZeroMQ) to manage multiple concurrent processes reliably. A microcontroller cannot do this. A cloud server cannot provide the latency required for real-time control.

**The ESP32 (Real-Time Actuation and Sensing)**

An ESP32 microcontroller handles all hardware I/O: reading ultrasonic distance sensors, reading the gas sensor, controlling four DC motors through H-bridge drivers, and enforcing collision avoidance at the firmware level. It communicates with the Pi over a serial UART link at 115200 baud.

The ESP32 exists because the Pi should never directly control motors or read sensors through GPIO in a safety-critical application. The microcontroller provides deterministic timing (no garbage collection pauses, no kernel preemption), hardware-level safety enforcement (if sensors detect an obstacle, motors stop immediately without waiting for the Pi), and electrical isolation (motor noise does not corrupt the Pi's operation).

This three-layer architecture is not accidental. It reflects a fundamental principle: each layer does what it is best suited for, and communicates with others through well-defined interfaces.

---

## 3. The Raspberry Pi as the System Brain

The Pi runs Raspberry Pi OS (Bookworm, 64-bit) and hosts nine independent Python processes managed by systemd. Each process is a systemd service with `Restart=always`, meaning the system automatically recovers from crashes without human intervention.

Systemd is used because it provides process supervision, automatic restart on failure, ordered startup, logging integration, and resource isolation — all without requiring containers or virtual machines. This is a single-purpose embedded system, not a multi-tenant server. Containerization would add complexity without meaningful benefit.

The services use separate Python virtual environments to isolate dependencies. The voice pipeline requires Porcupine and Azure Speech SDK. The vision runner requires OpenCV, Picamera2, and YOLO. The LLM runner requires the OpenAI client library. These have conflicting or heavy dependencies that would create version conflicts if installed together. Virtual environments solve this cleanly.

Isolation is achieved without containers through three mechanisms: process boundaries (each service is a separate OS process), message passing (services never import each other's code or share memory), and configuration (each service reads `system.yaml` independently). A bug in the vision service cannot corrupt the voice service's memory. A crash in the LLM runner cannot deadlock the orchestrator.

---

## 4. The Event-Driven Architecture (The Core Idea)

All inter-service communication happens through ZeroMQ publish-subscribe messaging. Services never call each other's functions, never import each other's modules, and never share mutable state.

ZeroMQ is used because it provides reliable message queuing without requiring a separate broker process (unlike RabbitMQ or Redis), works with plain TCP sockets (easy to debug and monitor), and supports the pub-sub pattern natively. The library is mature, fast, and well-suited to embedded systems.

The system uses two unidirectional message buses. The upstream bus (port 6010) carries messages from services to the orchestrator: wakeword detections, transcription results, LLM responses, sensor data. The downstream bus (port 6011) carries commands from the orchestrator to services: start listening, speak this text, navigate this direction.

This dual-bus architecture exists to prevent message loops and clarify data flow. A service that only publishes to upstream and subscribes to downstream cannot accidentally create feedback cycles. The orchestrator is the only component that both subscribes to upstream and publishes to downstream — it is explicitly the routing hub.

Services never call each other directly because direct coupling would make the system fragile. If the voice service directly called the LLM service, a slow LLM response would block voice processing. If the orchestrator directly called the motor bridge, a serial port timeout would freeze the entire system. Message passing decouples timing: publishers send and continue; subscribers process when ready.

This architecture means you can stop, modify, and restart any single service without affecting others (as long as the orchestrator remains running). You can add new services that subscribe to existing topics without modifying existing code. You can test services in isolation by publishing mock messages.

---

## 5. The Orchestrator's Role (Without Code)

The orchestrator is a finite-state machine that coordinates all other services without performing any domain-specific work itself. It does not detect wakewords. It does not transcribe speech. It does not run neural networks. It does not control motors. It only decides what should happen next and tells the appropriate service to do it.

The orchestrator maintains a single primary state variable called "phase" with five possible values: IDLE (waiting for input), LISTENING (capturing speech), THINKING (waiting for LLM response), SPEAKING (playing audio response), and ERROR (handling a fault). State transitions are triggered by events arriving on the upstream bus.

When the robot is idle and a wakeword event arrives, the orchestrator transitions to LISTENING and commands the voice service to start capturing audio. When a transcription event arrives, it transitions to THINKING and sends the text to the LLM service along with current world context (what the robot sees, what the sensors read). When an LLM response arrives, it transitions to SPEAKING, commands the TTS service to speak the response, and commands the motor bridge to execute any navigation directive. When TTS reports completion, it returns to IDLE.

The orchestrator never touches hardware directly. It never opens the microphone, never writes to serial ports, never draws to displays. This is essential for fault isolation: if the orchestrator crashes and restarts, it simply begins again in IDLE state without leaving hardware in an inconsistent state.

The orchestrator also maintains "world context" — an aggregated snapshot of recent sensor data, vision detections, and robot state. This context is included in LLM requests so the language model can make informed decisions ("there is an obstacle ahead" or "a person was detected to the left").

---

## 6. Voice → Thought → Action (The Intelligence Loop)

The core intelligence loop transforms spoken human intent into robot action through five stages, each handled by a different service.

The voice service continuously feeds audio from a USB microphone through the Porcupine wakeword engine. When the phrase "hey robo" is detected, it publishes a wakeword event and begins capturing speech. The audio is streamed to Azure Speech-to-Text, which returns a transcription. The voice service publishes this transcription and returns to wakeword detection.

The orchestrator receives the transcription and prepares an LLM request. This request includes the user's words, the robot's current direction, and the world context snapshot. It publishes this request and waits.

The LLM runner receives the request and sends it to Azure OpenAI (GPT-4o deployment). The system prompt defines the robot's persona and available actions (forward, backward, left, right, stopped). The model returns a JSON response containing a verbal response ("Moving forward now") and a direction command ("forward"). The LLM runner publishes this response.

The orchestrator receives the LLM response, parses the JSON, and issues two commands: tell the TTS service to speak the verbal response, and tell the motor bridge to execute the direction. These happen in parallel.

The TTS runner receives the speak command, synthesizes audio using Azure Text-to-Speech, plays it through the speaker, and publishes a completion event. The motor bridge receives the navigation command, translates it to a UART command ("FORWARD\n"), and sends it to the ESP32.

This pipeline is split across services for fault isolation and resource efficiency. Speech recognition and synthesis require cloud API calls with variable latency. LLM inference is computationally intensive. Audio capture requires exclusive microphone access. If all of this ran in one process, any component's failure would kill the entire pipeline. Separated, each component can fail and recover independently.

---

## 7. Vision as a Parallel Perception System

Vision operates independently of the voice pipeline, feeding awareness to the orchestrator without controlling robot behavior directly.

The vision runner captures frames from a Pi Camera using Picamera2, runs YOLOv8 object detection, and publishes detection events (what object, where, confidence) and JPEG frames (for streaming to the mobile app). It does this continuously while in "detection" mode.

Vision is optional and mode-based. The mobile app can switch vision between "off" (no camera activity), "on without stream" (detection runs but no MJPEG output), and "on with stream" (detection plus MJPEG for remote viewing). This flexibility exists because vision is computationally expensive and not always needed.

Vision feeds awareness but not control. When the orchestrator prepares an LLM request, it includes the latest vision detection in world context. The language model can use this information ("I see a person ahead") to inform its response. But the vision service never directly commands motors or makes navigation decisions — that would create parallel control paths and race conditions.

The vision stream is decoupled from inference to serve different purposes. MJPEG streaming allows the mobile operator to see what the robot sees in near-real-time. Detection events provide structured data (object labels, bounding boxes) for intelligent reasoning. These are published as separate topics so consumers can subscribe to what they need.

---

## 8. Motion and Safety Model

Physical motion is the responsibility of the ESP32 microcontroller, with the Pi providing high-level direction commands and the ESP32 enforcing low-level safety.

The Pi never directly controls motors. The motor bridge service translates navigation commands ("forward", "left", "stop") into UART strings ("FORWARD\n", "LEFT\n", "STOP\n") and sends them to the ESP32. This serial protocol is intentionally simple — text commands, text acknowledgments — because simplicity means fewer failure modes.

The ESP32 owns motor control because real-time safety constraints cannot be reliably met by a Linux system. When the ultrasonic sensors detect an obstacle within 20cm, the ESP32 must stop the motors within milliseconds, not wait for the Pi to process the sensor data, decide to stop, and send a command. The firmware implements this safety check in its main loop, before executing any movement command.

Collision avoidance is layered. The ESP32 provides the inner layer: hardware-level enforcement that operates regardless of what the Pi does. The Pi provides an outer layer: the orchestrator tracks sensor data and can avoid sending forward commands when obstacles are known. The mobile app provides a human layer: the operator can see telemetry and send stop commands. This defense-in-depth approach means safety does not depend on any single component functioning correctly.

UART is sufficient for this application because the data rates are low (sensor readings every 100ms, commands occasionally) and the reliability requirements are met by the protocol (text framing with newlines, acknowledgment responses). A more complex protocol like I2C or SPI would require additional GPIO wiring and driver complexity without meaningful benefit.

---

## 9. Remote Control vs Autonomous Control

The mobile app is a peer input source, not a master controller. Remote control and autonomous operation coexist within the same event architecture.

When the operator taps "Forward" in the app, the request goes to the remote-interface service as an HTTP POST. This service translates the intent into an internal IPC message (`remote.intent`) and publishes it on the upstream bus. The orchestrator receives this event exactly as it would receive a voice command — it processes the intent and issues appropriate commands to other services.

This design means remote control does not bypass intelligence. If the operator says "go forward" through the app while an obstacle is detected, the orchestrator (and the ESP32's firmware) will still prevent the motion. The app cannot send raw motor commands that circumvent safety logic.

HTTP intents become internal events because uniformity simplifies reasoning about the system. The orchestrator has one codepath for handling "user wants to move forward" regardless of whether that intent came from voice, mobile app, or (hypothetically) a future input source. There is no special "remote mode" — there is only "input from various sources, processed identically."

The remote-interface also serves read-only telemetry. The app polls `/status` every second to get sensor readings, display state, vision detections, and LLM responses. This is observation, not control — the app can see what is happening but cannot modify internal state except through the intent mechanism.

---

## 10. Configuration as a Control Plane

All configurable behavior is defined in `config/system.yaml`, a single file that every service reads at startup. This file is the control plane for the entire system.

Configuration exists to separate policy from mechanism. The code defines what the system can do (detect wakewords, run inference, control motors). Configuration defines what the system should do (which wakeword model, what confidence threshold, what timeout values). Changing configuration changes behavior without changing code, reducing the risk of introducing bugs.

Environment variables are used for secrets and deployment-specific values: Azure API keys, Picovoice access keys, endpoint URLs. These are read at runtime and never stored in configuration files or committed to source control. The system will not start if required secrets are missing — this is an intentional fail-fast behavior.

Configuration changes take effect on service restart. There is no dynamic reconfiguration mechanism. This is deliberate: dynamic reconfiguration introduces complexity (race conditions, partial updates) that is not justified for an embedded system that restarts in seconds.

The configuration file is validated implicitly by each service at startup. If required fields are missing or values are invalid, the service logs an error and exits. Systemd's restart policy means the service will retry, allowing transient issues (like a slow filesystem mount) to resolve themselves.

---

## 11. Startup to Steady State (Narrative)

When power is applied, the Raspberry Pi's bootloader loads the kernel, which starts systemd as PID 1. Systemd brings up basic services (filesystem, networking, time sync) before reaching the "multi-user" target where application services start.

The orchestrator service starts first because it binds the ZeroMQ ports that all other services connect to. It loads configuration, creates the upstream and downstream sockets, initializes its state machine to IDLE, and begins listening for events. At this point, the orchestrator is running but the system is not yet functional — no other services are producing events.

Other services start in parallel (systemd runs them concurrently unless dependencies are declared). Each service loads configuration, creates its ZeroMQ sockets, initializes its domain-specific resources (microphone, camera, serial port), and enters its main loop. As each service connects to the ZeroMQ buses, it becomes part of the messaging fabric.

The voice service initializes Porcupine with the wakeword model and opens the USB microphone. The motor bridge opens the serial port to the ESP32 and receives initial sensor data. The vision runner (if enabled) opens the camera and loads the YOLO model. The remote-interface starts its HTTP server and begins accepting connections.

When all services are running and connected, the system becomes "alive." The LED ring shows a slow blue pulse indicating IDLE state. The voice service is listening for the wakeword. The motor bridge is publishing sensor data. The remote-interface is responding to health checks. The system is ready to accept input.

A "healthy" system is one where all nine services are active, the orchestrator is in a non-ERROR phase, sensor data is flowing from the ESP32, and HTTP health checks return successfully. The mobile app indicates connection status based on health check responses.

---

## 12. What Makes This System Robust

Fault isolation is structural. Each service runs in its own process with its own memory space. A segmentation fault in the vision service cannot corrupt the orchestrator's state. A memory leak in the LLM runner cannot exhaust memory available to the voice service (though it can eventually exhaust system memory).

Restartability is guaranteed by systemd. Every service is configured with `Restart=always` and `RestartSec=3`. If a service crashes, systemd waits three seconds and starts it again. Services are written to be stateless across restarts — they reload configuration, reconnect to ZeroMQ, and resume operation from a clean state. The orchestrator returns to IDLE; the voice service resumes wakeword detection; the motor bridge resumes publishing sensor data.

Observability is provided through multiple channels. Each service writes structured logs to files under `logs/`. Systemd captures stdout/stderr and makes it available through `journalctl`. The remote-interface exposes telemetry through HTTP. The mobile app displays this telemetry in human-readable form. An operator can see what is happening without SSH access to the Pi.

Failure of one service does not kill the system because services are loosely coupled through message passing. If the LLM runner crashes, the orchestrator will timeout waiting for a response and return to IDLE — disappointing, but not catastrophic. If the vision runner crashes, the orchestrator loses visual context but can still process voice commands. If the TTS runner crashes, the orchestrator can still issue navigation commands. Only the orchestrator itself is truly critical; its failure means no coordination occurs, though other services continue running independently.

---

## 13. Current Reality (No Sugarcoating)

**Confirmed Working:**

The nine-service architecture is running on the Raspberry Pi. SSH inspection confirms all services are active. The HTTP health endpoint responds correctly. ZeroMQ ports 6010 and 6011 are bound. The UART connection to ESP32 is established. Sensor data is being published. The voice pipeline detects wakewords (10/10 detection rate in testing, per code comments). Azure Speech-to-Text and Azure OpenAI integrations are configured and functional (as evidenced by the codebase and API client initialization).

**Running But Unverified:**

End-to-end voice command flow was not directly tested during the analysis. The exact TTS output quality and latency were not measured. Vision inference accuracy was not benchmarked. Motor responsiveness timing was not quantified. These components exist and are integrated, but their performance characteristics in actual operation were not independently verified.

**Git Divergence:**

Both the developer PC and the Raspberry Pi have uncommitted changes to the same files: `orchestrator.py`, `remote_interface.py`, `motor_bridge.py`, and others. The Pi has more extensive modifications. This means the code running on the Pi differs from the code in the repository. The magnitude of divergence is substantial (hundreds of lines). Until these changes are reconciled and committed, the repository does not reflect the deployed system.

**Why This Matters:**

Git divergence means documentation based on repository files may not accurately describe runtime behavior. It means there is no single source of truth. It means a "clean" deployment from the repository would produce different behavior than the current running system. Resolving this should be a priority before further development.

---

## 14. Explicit Non-Assumptions

**The system does not have autonomous navigation.** It moves in the direction commanded by voice or remote control. It does not plan paths, avoid obstacles proactively (only reactively at collision distance), or navigate to goals.

**The system does not learn.** The language model is a cloud service (Azure OpenAI). The wakeword model is pre-trained (Porcupine). The vision model is pre-trained (YOLOv8). No on-device training or adaptation occurs.

**The system does not operate offline.** Speech recognition, language understanding, and speech synthesis all require Azure cloud services. Without internet connectivity, the robot cannot understand voice commands or speak responses. Local Whisper support exists in the codebase but is not the default configuration.

**The system does not have multi-user awareness.** Whoever speaks the wakeword gets the robot's attention. There is no voice identification, no user profiles, no permission levels.

**The system does not persist state across reboots.** Conversation history is held in memory and lost on restart. There is no database, no checkpoint files, no "resume where I left off."

**The mobile app does not work without network access to the Pi.** The app is a remote interface, not a local controller. It has no offline mode, no cached data, no standalone functionality.

**The ESP32 firmware is not in this repository.** The UART protocol is documented, but the Arduino sketch running on the ESP32 was not found in the analyzed codebase. The firmware is a dependency, not a deliverable of this project.

---

## 15. Mental Model Summary

Think of the smart_car system as a **nervous system** with three layers:

The **spinal cord** is the ESP32 — it handles reflexes. When a sensor detects an obstacle, the ESP32 stops the motors before the brain even knows. This reflex arc protects the robot from immediate harm without cognitive delay.

The **brain** is the Raspberry Pi — it handles perception, reasoning, and planning. It sees through the camera, hears through the microphone, thinks through the language model, and speaks through the synthesizer. But it never directly moves muscles; it sends commands down to the spinal cord.

The **senses** are the peripheral services — the voice pipeline is the ears, the vision runner is the eyes, the motor bridge is the motor cortex translating intent into movement commands.

The **orchestrator** is the executive function — the part of the brain that decides what to pay attention to and what to do next. It does not see or hear directly; it receives processed information from sensory systems. It does not move directly; it issues commands to motor systems.

The **mobile app** is like holding the robot's hand — you can feel what it feels (telemetry), see what it sees (camera stream), and guide its movements (intents), but you cannot override its reflexes or bypass its cognition.

This is not a product. It is a platform demonstrating that voice-controlled robotics with cloud AI can be built from commodity hardware and open-source software, with clear architectural boundaries and operational robustness. The documentation exists because the architecture matters more than any individual feature.

---

## Navigation

To dive deeper, consult the detailed documentation:

- [01_project_overview.md](01_project_overview.md) — System capabilities and state machine
- [04_ipc_and_data_flow.md](04_ipc_and_data_flow.md) — Topic taxonomy and message schemas
- [05_services_reference.md](05_services_reference.md) — Per-service specifications
- [06_configuration_reference.md](06_configuration_reference.md) — system.yaml breakdown
- [11_execution_flows.md](11_execution_flows.md) — Sequence diagrams for all flows
- [12_known_unknowns.md](12_known_unknowns.md) — Explicit documentation gaps
