# Smart Car Intelligence Enhancement Plan

## Document Purpose

This document serves as a **comprehensive specification and gap analysis** for evolving the smart_car project from a voice-controlled robot into a **truly intelligent autonomous assistant**. It is written to be used as a prompt input to AI models for implementation guidance, or as a development roadmap for human engineers.

---

## PART 1: THE WORKING SOFTWARE FLOW (As It Exists Today)

### 1.1 The Core Pipeline: Wakeword → STT → LLM → TTS

The current system implements a working voice interaction loop:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         WORKING PIPELINE (VERIFIED)                         │
└─────────────────────────────────────────────────────────────────────────────┘

[USER] ──"Hey Robo"──▶ [WAKEWORD] ──ww.detected──▶ [ORCHESTRATOR]
                        (Porcupine)                      │
                        10/10 detection                  │
                        48kHz→16kHz resample             │
                                                         ▼
                                                 ┌───────────────┐
                                                 │ Phase: IDLE   │
                                                 │   → LISTENING │
                                                 └───────┬───────┘
                                                         │
[USER SPEAKS] ◀────cmd.listen.start────────────────────┘
       │
       ▼
┌─────────────────┐
│ VOICE SERVICE   │──── Capture until silence (1.2s) or timeout (15s)
│ STT (Azure)     │──── Transcription via Azure Speech
└────────┬────────┘
         │
         │ stt.transcription (text, confidence)
         ▼
┌─────────────────────────────────────────────────────────────────┐
│                        ORCHESTRATOR                             │
│  Phase: LISTENING → THINKING                                    │
│                                                                 │
│  Prepares LLM Request:                                         │
│  {                                                              │
│    "text": "user's transcribed words",                         │
│    "direction": "current_movement_direction",                  │
│    "world_context": {                                          │
│      "vision": { "label": "...", "confidence": ..., "age_ms" },│
│      "sensors": { "s1": ..., "s2": ..., "obstacle": ... },     │
│      "robot_state": { "mode": "idle", "motion": "stopped" }    │
│    }                                                            │
│  }                                                              │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             │ llm.request
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                     LLM RUNNER (Azure OpenAI)                   │
│                                                                 │
│  ConversationMemory builds context:                            │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │ SYSTEM PROMPT (hardcoded in conversation_memory.py):    │   │
│  │                                                          │   │
│  │ You are ROBO, a smart assistant for a physical robot    │   │
│  │ car with camera and motors.                              │   │
│  │                                                          │   │
│  │ YOUR CAPABILITIES:                                       │   │
│  │ - Move: forward, backward, left, right, stop             │   │
│  │ - See: camera with object detection (YOLO)               │   │
│  │ - Track: follow a detected object visually               │   │
│  │ - Speak: respond via text-to-speech                      │   │
│  │ - Scan: do a 360° scan to map surroundings              │   │
│  │                                                          │   │
│  │ RESPONSE FORMAT (STRICT JSON):                           │   │
│  │ {                                                        │   │
│  │   "speak": "Your spoken response",                       │   │
│  │   "direction": "forward|backward|left|right|stop|scan",  │   │
│  │   "track": "" | "person" | "object_label"                │   │
│  │ }                                                        │   │
│  │                                                          │   │
│  │ CURRENT ROBOT STATE:                                     │   │
│  │ {robot_state from RobotState dataclass}                  │   │
│  │                                                          │   │
│  │ CONVERSATION CONTEXT:                                    │   │
│  │ {last N turns of conversation}                           │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                 │
│  + world_context injected as system context                    │
│                                                                 │
│  Azure OpenAI API Call → Response JSON                         │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             │ llm.response
                             │ { "json": {"speak": "...", "direction": "...", "track": "..."} }
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                        ORCHESTRATOR                             │
│  Phase: THINKING → SPEAKING                                     │
│                                                                 │
│  Parses response:                                              │
│  - speak → Send to TTS                                         │
│  - direction → Send to Motor Bridge (nav.command)              │
│  - track → (partially implemented, updates memory)              │
│                                                                 │
│  Obstacle Safety Check:                                        │
│  - If direction="forward" AND obstacle detected → force STOP    │
└────────┬───────────────────────────────────────┬────────────────┘
         │                                       │
         │ tts.speak                             │ nav.command
         ▼                                       ▼
┌─────────────────┐                     ┌─────────────────┐
│   TTS RUNNER    │                     │  MOTOR BRIDGE   │
│ (Azure Speech)  │                     │    (UART)       │
│                 │                     │                 │
│ Synthesize &    │                     │ FORWARD\n       │
│ Play Audio      │                     │ LEFT\n          │
└────────┬────────┘                     │ STOP\n          │
         │                              │ SCAN\n          │
         │ tts.speak (done=true)        └────────┬────────┘
         ▼                                       │
┌─────────────────┐                              ▼
│  ORCHESTRATOR   │                     ┌─────────────────┐
│ Phase: SPEAKING │                     │     ESP32       │
│   → IDLE        │                     │   Motor Control │
│                 │                     │   Sensors       │
│ LED: idle       │                     │   Safety        │
│ Ready for next  │                     └─────────────────┘
│ wakeword        │
└─────────────────┘
```

### 1.2 What Data the LLM Currently Receives

**From ConversationMemory.robot_state:**
```python
@dataclass
class RobotState:
    direction: str = "stopped"          # Current movement direction
    tracking_target: Optional[str] = None  # What we're tracking (if any)
    last_detection: Optional[Dict] = None  # {label, confidence, bbox}
    detection_timestamp: float = 0.0       # When detection occurred
    battery_level: Optional[float] = None  # (Future - not populated)
    obstacle_detected: bool = False        # (Future - not populated)
```

**From WorldContextAggregator.get_snapshot():**
```python
{
    "vision": {
        "last_known": {"label": "person", "bbox": [...], "confidence": 0.87},
        "age_ms": 450,
        "stale": false
    },
    "sensors": {
        "last_known": {
            "data": {"s1": 45, "s2": 38, "s3": 52, "mq2": 0, "obstacle": false, "warning": false},
            "alert": null,
            "blocked": false
        },
        "age_ms": 100,
        "stale": false
    },
    "robot_state": {
        "last_known": {"mode": "idle", "motion": "stopped", "vision_mode": "off"},
        "age_ms": 50,
        "stale": false
    },
    "generated_at": 1706745600,
    "context_type": "last_known_state"
}
```

### 1.3 What the LLM Can Currently Output

The LLM is constrained to output this JSON structure:

```json
{
    "speak": "What I say to the user (TTS)",
    "direction": "forward | backward | left | right | stop | scan",
    "track": "" | "person" | "object_label"
}
```

**Limitations of current output:**
- Only ONE action at a time (cannot chain: "scan, then move to door")
- No duration control ("move forward for 3 seconds")
- No speed control ("move slowly")
- No waypoint navigation ("go to the red box")
- No feedback loop ("stop when you see X")
- `track` field exists but tracking behavior is NOT implemented

---

## PART 2: THE VISION - What a "Smart Car" Should Be Able To Do

### 2.1 Natural Language Commands That Should Work

**Spatial Navigation:**
```
"Go to the door"
"Move towards the boxes"
"Navigate to the person"
"Go to the red chair"
"Move to my left side"
```

**Environmental Awareness:**
```
"Do a 360 scan and tell me what you see"
"What's around you?"
"Describe your surroundings"
"Is there anything in front of you?"
"How far is the nearest obstacle?"
```

**Task-Oriented Commands:**
```
"Find the nearest person and stop in front of them"
"Patrol this room"
"Follow the ball"
"Explore until you find a chair"
"Go forward until you see a wall, then turn right"
```

**Status and Feedback:**
```
"Where are you now?"
"What did you just see?"
"Are you stuck?"
"Why did you stop?"
"What's your battery level?"
```

**Compound Commands:**
```
"Scan around, then go to the clearest path"
"Move forward slowly, and tell me if you see anything"
"Turn left, move a bit, then scan"
```

### 2.2 Expected Robot Behaviors

**During Navigation:**
- Announce when starting: "Moving towards the door"
- Announce progress: "I see the door, getting closer"
- Announce obstacles: "Obstacle detected, adjusting path"
- Announce arrival: "I've reached the door"
- Announce failure: "I can't find a path to the door"

**During Scanning:**
- Rotate 360 degrees while capturing vision at each angle
- Build a mental map: "At 0° I see a wall, at 90° I see a person..."
- Summarize: "I completed the scan. I see a person to my right, boxes ahead, and a clear path to my left"

**During Tracking:**
- Continuously adjust direction to keep target in center of frame
- Report tracking status: "I'm following the person"
- Handle lost target: "I lost sight of the person, should I search?"

---

## PART 3: GAP ANALYSIS - What's Missing

### 3.1 LLM Context Gaps

| What LLM Needs | Current Status | Gap |
|----------------|----------------|-----|
| Live sensor distances (s1, s2, s3) | ✅ In world_context | Need better formatting for LLM |
| Obstacle status | ✅ In world_context | Not prominently surfaced |
| Current movement direction | ✅ In robot_state | Works |
| Vision detections | ✅ In world_context | Only LAST detection, not all visible objects |
| Spatial map of surroundings | ❌ Not implemented | Need 360° scan data structure |
| History of what was seen | ❌ Not implemented | Need detection history buffer |
| Distance to detected objects | ❌ Not implemented | Need depth estimation or sensor fusion |
| Object positions (left/center/right) | ⚠️ Partial (bbox exists) | Need interpretation logic |
| Target destination | ❌ Not implemented | Need goal tracking |
| Navigation progress | ❌ Not implemented | Need progress estimation |

### 3.2 Action Capability Gaps

| Capability | Current Status | Gap |
|------------|----------------|-----|
| Move in direction | ✅ Works | - |
| Stop | ✅ Works | - |
| 360° Scan | ✅ ESP32 supports | No integration with vision during scan |
| Timed movement | ❌ Not implemented | Need duration parameter |
| Speed control | ❌ Not implemented | ESP32 may need firmware update |
| Move-until-condition | ❌ Not implemented | Need feedback loop |
| Navigate to object | ❌ Not implemented | Need planning + execution loop |
| Track object | ⚠️ Field exists | No actual tracking behavior |
| Avoid obstacles dynamically | ⚠️ Reactive only | No path planning |
| Multi-step commands | ❌ Not implemented | Need action queue |

### 3.3 Feedback and Status Gaps

| Feedback Type | Current Status | Gap |
|---------------|----------------|-----|
| Movement started | ✅ LED changes | Not spoken |
| Movement stopped | ✅ LED changes | Not spoken |
| Obstacle detected | ✅ ESP32 stops | Not communicated to LLM proactively |
| Destination reached | ❌ Not implemented | Need goal tracking |
| Scan completed | ⚠️ Logged | Not communicated to LLM |
| Tracking target lost | ❌ Not implemented | Need tracking state machine |
| Error conditions | ⚠️ LED shows | Not spoken |

### 3.4 System Prompt Gaps

The current system prompt is:
1. **Too static** - Doesn't reflect actual capabilities
2. **Missing sensor awareness** - LLM doesn't know it has distance sensors
3. **Missing spatial reasoning** - Can't interpret "left of", "near to", "behind"
4. **No action chaining** - Can't plan multi-step behaviors
5. **No feedback protocol** - LLM can't request status updates

---

## PART 4: DETAILED ENHANCEMENT SPECIFICATIONS

### 4.1 Enhanced System Prompt

```markdown
# ROBO - Intelligent Robot Assistant

You are ROBO, an autonomous robot with sensors, camera, and motors. You operate in the physical world and must reason about space, obstacles, and navigation.

## YOUR PHYSICAL CAPABILITIES

### Sensors
- **3 Ultrasonic Sensors**: S1 (left), S2 (center), S3 (right) - measure distance in cm
- **Gas Sensor (MQ2)**: Detects smoke/gas
- **Camera**: Forward-facing with YOLO object detection

### Movement
- **Directions**: forward, backward, left (rotate), right (rotate), stop
- **Speed**: slow (30%), medium (60%), fast (100%) - default medium
- **Duration**: continuous until next command, or timed (seconds)

### Special Actions
- **scan**: Rotate 360° while capturing vision at each angle
- **track <object>**: Continuously follow a detected object
- **navigate <target>**: Plan path to reach a visible object

## RESPONSE FORMAT

You MUST respond with valid JSON:

```json
{
    "speak": "What you say to the user (keep under 40 words)",
    "actions": [
        {
            "type": "move | scan | track | navigate | wait | speak_status",
            "direction": "forward | backward | left | right | stop",
            "speed": "slow | medium | fast",
            "duration_s": 0,
            "target": "",
            "condition": ""
        }
    ],
    "expect_feedback": true | false,
    "internal_note": "Your reasoning (not spoken)"
}
```

### Action Types

| Type | Description | Parameters |
|------|-------------|------------|
| move | Move in a direction | direction, speed, duration_s |
| scan | 360° scan with vision | (none) |
| track | Follow an object | target (object label) |
| navigate | Go to a visible object | target (object label) |
| wait | Pause for duration | duration_s |
| speak_status | Announce current status | (none) |

### Chaining Actions

You can chain up to 5 actions. They execute sequentially:
```json
{
    "actions": [
        {"type": "move", "direction": "left", "duration_s": 1},
        {"type": "move", "direction": "forward", "duration_s": 2},
        {"type": "scan"}
    ]
}
```

## SPATIAL REASONING

When you receive sensor data:
- S1 < 20cm: Obstacle on LEFT
- S2 < 20cm: Obstacle in FRONT
- S3 < 20cm: Obstacle on RIGHT
- All > 50cm: Clear space in that direction

When you receive vision detections:
- bbox x < 200: Object is on LEFT
- bbox x 200-440: Object is in CENTER
- bbox x > 440: Object is on RIGHT
- Higher confidence = more reliable

## FEEDBACK LOOP

If you set `expect_feedback: true`, you will receive an update after actions complete:
- Scan results (what was seen at each angle)
- Navigation outcome (reached, blocked, lost)
- Tracking status (following, lost)

Use this to adapt your next response.

## CURRENT STATE
{robot_state}

## SENSOR READINGS
{sensor_summary}

## VISIBLE OBJECTS
{vision_summary}

## RECENT SCAN DATA
{scan_summary}

## CONVERSATION
{conversation_history}
```

### 4.2 Enhanced World Context Structure

```python
@dataclass
class EnhancedWorldContext:
    # Sensor Data
    sensors: SensorReading  # s1, s2, s3, mq2, obstacle, warning
    sensor_summary: str  # "Left: 45cm clear, Center: 18cm CLOSE, Right: 62cm clear"
    
    # Vision Data
    current_detections: List[Detection]  # All objects currently visible
    detection_summary: str  # "person in center (0.92), chair on left (0.76)"
    
    # Scan Data (if scan was performed)
    scan_complete: bool
    scan_data: List[ScanPoint]  # [{angle: 0, detections: [...], distances: {...}}, ...]
    scan_summary: str  # "0°: wall 30cm, 90°: person 2m, 180°: clear, 270°: boxes 1m"
    
    # Navigation State
    current_goal: Optional[str]  # "door" if navigating to door
    goal_visible: bool  # Can we see the goal?
    goal_direction: Optional[str]  # "left", "center", "right"
    goal_distance_estimate: Optional[str]  # "close", "medium", "far"
    
    # Movement State
    current_motion: str  # "stopped", "forward", "turning_left"
    last_command_result: str  # "completed", "blocked_by_obstacle", "in_progress"
    
    # History
    recent_detections: List[Detection]  # Last 10 detections with timestamps
    recent_obstacles: List[ObstacleEvent]  # When and where obstacles were detected
```

### 4.3 Action Execution Engine

```python
class ActionExecutor:
    """Executes chained actions from LLM response."""
    
    async def execute_action_chain(self, actions: List[Action]) -> ActionResult:
        results = []
        for action in actions:
            result = await self._execute_single(action)
            results.append(result)
            if result.blocked or result.error:
                break  # Stop chain on failure
        return ActionChainResult(results)
    
    async def _execute_single(self, action: Action) -> ActionResult:
        if action.type == "move":
            return await self._execute_move(action)
        elif action.type == "scan":
            return await self._execute_scan()
        elif action.type == "track":
            return await self._execute_track(action.target)
        elif action.type == "navigate":
            return await self._execute_navigate(action.target)
        # ... etc
    
    async def _execute_move(self, action: Action) -> ActionResult:
        """Execute movement with duration and obstacle monitoring."""
        # Send nav.command
        # Monitor for duration_s
        # Check for obstacles during movement
        # Return result: completed, blocked, timeout
    
    async def _execute_scan(self) -> ScanResult:
        """Execute 360° scan with vision integration."""
        # Send SCAN command to ESP32
        # At each angle reported by ESP32:
        #   - Capture vision frame
        #   - Run YOLO detection
        #   - Record: angle, detections, sensor distances
        # Build scan_data structure
        # Generate scan_summary for LLM
        # Return complete scan result
    
    async def _execute_navigate(self, target: str) -> NavigationResult:
        """Navigate to a visible object."""
        # 1. Check if target is currently visible
        # 2. Determine target position (left/center/right)
        # 3. Execute movement towards target
        # 4. Re-check visibility
        # 5. Repeat until target is in center and close
        # 6. Return: reached, lost, blocked
```

### 4.4 Tracking Implementation

```python
class ObjectTracker:
    """Continuously follows a target object."""
    
    def __init__(self, target_label: str):
        self.target_label = target_label
        self.state = TrackingState.SEARCHING
        self.last_seen_ts = 0
        self.lost_timeout_s = 3.0
    
    async def tracking_loop(self):
        while self.state != TrackingState.STOPPED:
            frame, detections = await self.get_latest_vision()
            target = self._find_target(detections)
            
            if target:
                self.last_seen_ts = time.time()
                self.state = TrackingState.FOLLOWING
                command = self._calculate_tracking_command(target)
                await self.send_movement(command)
            else:
                if time.time() - self.last_seen_ts > self.lost_timeout_s:
                    self.state = TrackingState.LOST
                    await self.notify_lost()
                    break
    
    def _calculate_tracking_command(self, target: Detection) -> MotorCommand:
        """Calculate movement to keep target centered."""
        cx = (target.bbox[0] + target.bbox[2]) / 2  # Center x
        frame_center = 320  # Assuming 640px width
        
        if cx < frame_center - 80:
            return MotorCommand(direction="left", speed=30)
        elif cx > frame_center + 80:
            return MotorCommand(direction="right", speed=30)
        else:
            # Target is centered, move forward if not too close
            if target.bbox[3] - target.bbox[1] < 200:  # Not filling frame
                return MotorCommand(direction="forward", speed=50)
            else:
                return MotorCommand(direction="stop")  # Close enough
```

### 4.5 Enhanced Status Messages

```python
class StatusAnnouncer:
    """Generates spoken status updates."""
    
    def on_obstacle_detected(self, direction: str, distance: int) -> str:
        return f"Obstacle detected {direction}, {distance} centimeters away"
    
    def on_navigation_started(self, target: str) -> str:
        return f"Navigating towards the {target}"
    
    def on_navigation_progress(self, target: str, status: str) -> str:
        messages = {
            "approaching": f"Getting closer to the {target}",
            "adjusting": f"Adjusting course to reach the {target}",
            "blocked": f"Path to {target} is blocked, looking for alternative",
        }
        return messages.get(status, f"Navigating to {target}")
    
    def on_navigation_complete(self, target: str, success: bool) -> str:
        if success:
            return f"I've reached the {target}"
        else:
            return f"I couldn't reach the {target}"
    
    def on_scan_complete(self, summary: str) -> str:
        return f"Scan complete. {summary}"
    
    def on_tracking_status(self, target: str, status: str) -> str:
        messages = {
            "following": f"Following the {target}",
            "lost": f"I lost sight of the {target}",
            "found": f"I found the {target} again",
        }
        return messages.get(status, f"Tracking {target}")
```

---

## PART 5: IMPLEMENTATION ROADMAP

### Phase 1: Enhanced LLM Context (1-2 days)

**Goal:** Give LLM richer understanding of environment

1. **Enhance world_context.py:**
   - Add sensor_summary string generation: "Left: 45cm, Center: BLOCKED (12cm), Right: clear"
   - Add vision_summary string: "person (center, 0.92), chair (left, 0.76)"
   - Add spatial interpretation: bbox → left/center/right

2. **Enhance conversation_memory.py:**
   - Update SYSTEM_PROMPT_TEMPLATE with sensor awareness
   - Add spatial reasoning instructions
   - Include formatted sensor/vision data in context

3. **Enhance orchestrator.py:**
   - Format world_context more richly for LLM
   - Include sensor interpretation in llm.request payload

### Phase 2: Multi-Action Response Format (2-3 days)

**Goal:** LLM can output multiple actions

1. **Update LLM Response Schema:**
   - Change from single direction to `actions` array
   - Add support for: type, direction, speed, duration_s, target, condition

2. **Create ActionExecutor:**
   - Parse action chain from LLM response
   - Execute sequentially with monitoring
   - Handle failures and interruptions

3. **Update azure_openai_runner.py:**
   - Parse new response format
   - Handle backward compatibility with simple format

4. **Update orchestrator.py:**
   - Accept action chains from LLM
   - Dispatch to ActionExecutor

### Phase 3: 360° Scan with Vision Integration (2-3 days)

**Goal:** Scan command captures vision at each angle

1. **Update motor_bridge.py:**
   - Emit events for each scan position
   - Include sensor readings at each angle

2. **Create ScanManager:**
   - Listen for SCAN:POS events
   - Trigger vision capture at each angle
   - Aggregate detections with angles
   - Build scan_data structure
   - Generate scan_summary

3. **Update orchestrator.py:**
   - Route scan commands through ScanManager
   - Wait for scan completion
   - Include scan results in next LLM request

4. **Update system prompt:**
   - Add scan data format explanation
   - Add spatial reasoning for scan results

### Phase 4: Object Tracking (2-3 days)

**Goal:** Robot can follow detected objects

1. **Create ObjectTracker:**
   - Continuous tracking loop
   - Vision-based target following
   - Lost target handling

2. **Update orchestrator.py:**
   - Handle "track" action type
   - Manage tracking state
   - Report tracking status

3. **Update world_context.py:**
   - Add tracking state to context

4. **Update system prompt:**
   - Add tracking instructions

### Phase 5: Navigate-to-Object (3-5 days)

**Goal:** Robot can navigate to visible objects

1. **Create NavigationPlanner:**
   - Simple approach: turn towards target, move, repeat
   - Obstacle avoidance during navigation
   - Goal visibility checking

2. **Create NavigationExecutor:**
   - Execute navigation plan
   - Monitor progress
   - Handle failures

3. **Update orchestrator.py:**
   - Handle "navigate" action type
   - Report navigation progress

4. **Add status announcements:**
   - "Navigating to the door"
   - "Obstacle detected, adjusting"
   - "I've reached the door"

### Phase 6: Feedback Loop (2-3 days)

**Goal:** LLM receives feedback after actions complete

1. **Implement expect_feedback handling:**
   - If true, wait for action completion
   - Compile action results
   - Send follow-up LLM request with results

2. **Update orchestrator.py:**
   - Implement feedback loop logic
   - Phase: THINKING → EXECUTING → FEEDBACK → THINKING

3. **Update system prompt:**
   - Explain feedback format
   - Guide LLM on adaptive responses

---

## PART 6: FEASIBILITY ASSESSMENT

### 6.1 What's Definitely Feasible (Today)

| Feature | Reason |
|---------|--------|
| Enhanced LLM context formatting | Just string formatting, no new data |
| Sensor distance interpretation | Data exists, need interpretation layer |
| Vision position interpretation | bbox data exists, need interpretation |
| Multi-action response parsing | Schema change, backward compatible |
| Sequential action execution | Simple loop with delays |
| 360° scan with vision | ESP32 already supports SCAN, vision exists |
| Basic object tracking | Vision + simple control loop |
| Status announcements | TTS already works |

### 6.2 What Requires Careful Design

| Feature | Challenge | Approach |
|---------|-----------|----------|
| Navigate to object | Need planning logic | Simple "turn-and-go" first |
| Move-until-condition | Need continuous monitoring | Poll vision during movement |
| Compound commands | Action queue management | Sequential execution with abort |
| Feedback loop | State machine complexity | New orchestrator phase |

### 6.3 What's NOT Feasible Without Hardware Changes

| Feature | Limitation |
|---------|------------|
| Precise distance-to-object | Need depth camera or stereo vision |
| Speed control | ESP32 firmware may need update |
| Rear obstacle detection | No rear sensors |
| Indoor localization | No encoders or SLAM |
| Object manipulation | No arm or gripper |

### 6.4 What's NOT Feasible Without Major Rework

| Feature | Reason |
|---------|--------|
| Real SLAM navigation | Requires ROS or equivalent stack |
| Multi-room navigation | No persistent map |
| Voice-while-moving | Mic conflict with motor noise |
| Continuous tracking + conversation | Need state machine redesign |

---

## PART 7: PROMPT FOR AI MODEL

Use this as input to an AI assistant for implementation guidance:

```
I am building an intelligent voice-controlled robot car. The system is already working with:
- Wakeword detection (Porcupine)
- Speech-to-text (Azure)
- LLM processing (Azure OpenAI GPT-4o)
- Text-to-speech (Azure)
- Motor control via ESP32 (forward, backward, left, right, stop, scan)
- 3 ultrasonic sensors (left, center, right)
- YOLO object detection via camera

Current working flow:
1. User says "Hey Robo"
2. Robot captures speech, transcribes it
3. LLM receives: user text + world_context (sensors + vision + robot state)
4. LLM responds with JSON: {"speak": "...", "direction": "...", "track": "..."}
5. Robot speaks response and executes direction

Current limitations:
- LLM only outputs one action at a time
- No duration or speed control for movements
- No navigation to objects ("go to the door")
- No visual scanning with object detection
- No tracking behavior implemented
- No feedback loop (LLM doesn't know action results)
- Sensor data is provided but not well-formatted for LLM understanding

I want to enhance the robot to understand commands like:
- "Do a 360 scan and tell me what you see"
- "Go to the boxes"
- "Follow that person"
- "Move forward until you see a chair"
- "What's around you?"

Please help me:
1. Design an enhanced system prompt for the LLM
2. Design a new response schema that supports action chains
3. Design the logic for 360° scan with vision integration
4. Design the object tracking behavior
5. Design the navigate-to-object behavior
6. Explain how to implement a feedback loop

The implementation should work with:
- Python 3.11 on Raspberry Pi 4
- ZeroMQ for inter-service communication
- Existing service architecture (orchestrator, voice_service, llm_runner, tts_runner, motor_bridge, vision_runner)
```

---

## PART 8: SUCCESS CRITERIA

### Minimum Viable Smart Car (MVP)

- [ ] LLM understands sensor readings (can say "obstacle on your left")
- [ ] LLM understands vision (can say "I see a person in front of you")
- [ ] 360° scan generates verbal description ("I see person to my right, wall ahead...")
- [ ] Robot can execute timed movements ("move forward for 2 seconds")
- [ ] Robot announces obstacles when they stop movement

### Full Smart Car

- [ ] All MVP features
- [ ] Navigate to visible objects ("go to the chair")
- [ ] Track moving objects ("follow the person")
- [ ] Multi-step commands work ("scan, then go to the clearest path")
- [ ] Status updates during navigation ("getting closer...", "arrived!")
- [ ] Handles lost tracking gracefully ("I lost sight of the person")

### Stretch Goals

- [ ] Patrol behavior (explore room, report findings)
- [ ] Object search ("find a ball")
- [ ] Simple mapping (remember where objects were seen)
- [ ] Voice-interrupted navigation ("stop" works during movement)

---

## Document End

This document contains:
1. Complete working flow analysis
2. Gap identification
3. Enhancement specifications
4. Implementation roadmap
5. Feasibility assessment
6. AI prompt for assistance
7. Success criteria

Use this as a roadmap for development or as context for AI-assisted implementation.
