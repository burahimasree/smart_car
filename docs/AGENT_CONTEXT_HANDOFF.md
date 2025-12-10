# Agent Context Handoff Document
## Smart Car Project - ESP32 Obstacle Avoidance Implementation

**Created:** Session handoff for AI agent continuation  
**Priority:** CRITICAL - Safety implementation required before driving

---

## 🚨 CRITICAL ISSUE IDENTIFIED

**The robot car CAN CRASH.** The ESP32 has 3 ultrasonic sensors that READ distances but there is **NO COLLISION AVOIDANCE LOGIC** anywhere in the codebase. The car will drive into walls/obstacles at full speed.

### What Exists (❌ Incomplete)
- ESP32 reads `S1`, `S2`, `S3` ultrasonic distances every 50ms
- ESP32 sends `DATA:S1:xx,S2:yy,S3:zz,...` to Raspberry Pi over UART
- Pi's `motor_bridge.py` receives this data but **ignores it**
- Orchestrator doesn't subscribe to `TOPIC_ESP` sensor data
- **NO `if (distance < threshold) stopMotors()` anywhere!**

### What's Missing (🎯 MUST IMPLEMENT)
1. **ESP32-level safety:** Stop motors if ANY sensor reads < 10cm
2. **Pi-level safety:** Process `TOPIC_ESP` data, veto dangerous moves
3. **360° scanning:** Use servo sweep before movement to map environment
4. **Intelligent navigation:** Choose safest direction based on sensor map

---

## 📁 Project Structure Overview

```
smart_car/
├── src/
│   ├── uart/
│   │   ├── esp-code.ino        # ⚠️ ESP32 firmware - NEEDS SAFETY LOGIC
│   │   └── motor_bridge.py     # Bridge nav.command to UART - NEEDS SENSOR PROCESSING
│   ├── core/
│   │   ├── orchestrator.py     # Central event hub - NEEDS ESP32 DATA SUBSCRIPTION
│   │   ├── ipc.py              # ZMQ topics including TOPIC_ESP
│   │   └── config_loader.py    # YAML config loader
│   ├── llm/
│   │   ├── gemini_runner.py    # Gemini API with memory (implemented)
│   │   └── conversation_memory.py  # ConversationMemory (implemented)
│   ├── stt/                    # faster-whisper STT (implemented)
│   ├── tts/                    # Piper TTS (implemented)
│   ├── wake/                   # Porcupine wakeword (implemented)
│   └── vision/                 # YOLO detection (implemented)
├── config/
│   ├── system.yaml             # Main config file
│   └── settings.yaml           # Additional settings
└── docs/
    └── CONTROL_FLOW_AUDIT.md   # Previous audit (missed this issue!)
```

---

## 🔧 Hardware Configuration

### ESP32 Pin Mapping
```cpp
// Ultrasonic Sensors (HC-SR04 style)
#define TRIG1_PIN 4     // Sensor 1 (Front-Left?)
#define ECHO1_PIN 5
#define TRIG2_PIN 18    // Sensor 2 (Front-Center?)
#define ECHO2_PIN 19
#define TRIG3_PIN 21    // Sensor 3 (Front-Right?)
#define ECHO3_PIN 22

// Servo (for 360° scanning)
#define SERVO_PIN 23    // 0-180°, default 90°, controllable via SERVO:angle

// Motor Driver (L298N style, digital control)
#define IN1 25  // Left motor forward
#define IN2 26  // Left motor backward
#define IN3 27  // Right motor forward
#define IN4 14  // Right motor backward

// Gas Sensor
#define MQ2_PIN 34  // Analog 0-4095

// UART to Pi
#define RXD2 16
#define TXD2 17
// Baud: 115200
```

### Commands ESP32 Accepts
| Command | Description |
|---------|-------------|
| `FORWARD` | Both motors forward at full speed |
| `BACKWARD` | Both motors backward |
| `LEFT` | Tank turn left (left forward, right backward) |
| `RIGHT` | Tank turn right |
| `STOP` | Stop all motors |
| `SERVO:angle` | Set servo to angle (0-180) |
| `STATUS` | Request current state |
| `RESET` | Center servo + stop motors |

### Data ESP32 Sends (every 50ms)
```
DATA:S1:<dist1>,S2:<dist2>,S3:<dist3>,MQ2:<gas>,SERVO:<angle>,LMOTOR:<ls>,RMOTOR:<rs>
ACK:<CMD>:<STATUS>
STATUS:SERVO:<angle>,LMOTOR:<ls>,RMOTOR:<rs>
```

---

## 🔴 Implementation Tasks

### Task 1: ESP32 Hardware-Level Safety (HIGHEST PRIORITY)

**File:** `src/uart/esp-code.ino`

Add collision avoidance in the main loop BEFORE processing commands:

```cpp
// In loop(), after reading sensors:
#define STOP_DISTANCE_CM 10
#define WARNING_DISTANCE_CM 20

// Emergency stop check
if (dist1 > 0 && dist1 < STOP_DISTANCE_CM ||
    dist2 > 0 && dist2 < STOP_DISTANCE_CM ||
    dist3 > 0 && dist3 < STOP_DISTANCE_CM) {
    
    if (leftMotorSpeed != 0 || rightMotorSpeed != 0) {
        stopMotors();
        PiSerial.println("WARN:ESTOP:OBSTACLE");
        Serial.println("! EMERGENCY STOP - Obstacle detected !");
    }
}

// Warning zone - allow only stop or backward
if (dist1 > 0 && dist1 < WARNING_DISTANCE_CM ||
    dist2 > 0 && dist2 < WARNING_DISTANCE_CM ||
    dist3 > 0 && dist3 < WARNING_DISTANCE_CM) {
    
    motorsEnabled = false;  // Block forward commands
    PiSerial.println("WARN:PROXIMITY:CLOSE");
} else {
    motorsEnabled = true;
}
```

Also modify `handleCommand()` to respect `motorsEnabled`:
```cpp
} else if (cmdu.startsWith("FORWARD")) {
    if (motorsEnabled) {
        moveForward(255);
        sendAck("FORWARD", "OK");
    } else {
        sendAck("FORWARD", "BLOCKED:OBSTACLE");
    }
}
```

### Task 2: Pi-Side Sensor Data Processing

**File:** `src/uart/motor_bridge.py`

Currently `_process_rx()` publishes ESP32 data to `TOPIC_ESP` but nothing subscribes to it.

Option A: Add safety check in `motor_bridge.py`:
```python
class UARTMotorBridge:
    STOP_DISTANCE_CM = 10
    
    def _process_rx(self) -> None:
        while not self._rx_queue.empty():
            line = self._rx_queue.get_nowait()
            if line.startswith("DATA:"):
                distances = self._parse_sensor_data(line)
                if self._is_obstacle_close(distances):
                    self._send_command(MotorCommand(direction="stop"))
                    logger.warning("Pi safety override: obstacle detected!")
    
    def _parse_sensor_data(self, line: str) -> dict:
        # Parse DATA:S1:xx,S2:yy,S3:zz,...
        result = {}
        parts = line.split(",")
        for part in parts:
            if ":" in part:
                key, val = part.split(":")[-2:]
                result[key] = int(val) if val.isdigit() else val
        return result
    
    def _is_obstacle_close(self, distances: dict) -> bool:
        for key in ["S1", "S2", "S3"]:
            dist = distances.get(key, 999)
            if 0 < dist < self.STOP_DISTANCE_CM:
                return True
        return False
```

Option B: Process in orchestrator by subscribing to `TOPIC_ESP`.

### Task 3: 360° Environment Scanning

**Concept:** Before executing a move command, sweep the servo and build a distance map:

```python
# In motor_bridge.py or new nav_safety.py
def scan_environment(self) -> dict:
    """Sweep servo 0-180° and record distances at each angle."""
    env_map = {}
    for angle in range(0, 181, 15):  # 13 readings
        self._send_command(MotorCommand(direction="servo", target=str(angle)))
        time.sleep(0.1)  # Wait for servo + reading
        # Read latest DATA from ESP32
        distances = self._get_latest_distances()
        env_map[angle] = distances
    # Return servo to center
    self._send_command(MotorCommand(direction="servo", target="90"))
    return env_map

def find_safest_direction(self, env_map: dict) -> str:
    """Analyze scan data and return safest direction."""
    # Left sector: 150-180°
    # Center sector: 60-120°  
    # Right sector: 0-30°
    left_clear = all(env_map.get(a, {}).get("S1", 0) > 30 for a in [150, 165, 180])
    right_clear = all(env_map.get(a, {}).get("S3", 0) > 30 for a in [0, 15, 30])
    center_clear = all(env_map.get(a, {}).get("S2", 0) > 30 for a in [75, 90, 105])
    
    if center_clear:
        return "forward"
    elif left_clear:
        return "left"
    elif right_clear:
        return "right"
    else:
        return "backward"  # All blocked, retreat
```

### Task 4: Intelligent Navigation Controller

**New File:** `src/nav/navigator.py`

```python
class SafeNavigator:
    """Autonomous navigation with obstacle avoidance."""
    
    def __init__(self, motor_bridge: UARTMotorBridge):
        self.bridge = motor_bridge
        self.env_map = {}
        self.last_scan_ts = 0
        self.scan_interval = 5.0  # Rescan every 5 seconds
    
    def execute_move(self, direction: str) -> bool:
        """Execute move with safety checks."""
        # Check if we need fresh scan
        if time.time() - self.last_scan_ts > self.scan_interval:
            self.env_map = self.bridge.scan_environment()
            self.last_scan_ts = time.time()
        
        # Validate requested direction is safe
        if direction == "forward" and not self._is_forward_safe():
            # Auto-correct to safe direction
            direction = self.bridge.find_safest_direction(self.env_map)
            logger.info(f"Auto-corrected forward -> {direction}")
        
        return self.bridge._send_command(MotorCommand(direction=direction))
    
    def _is_forward_safe(self) -> bool:
        # Check center sector from last scan
        for angle in [75, 90, 105]:
            if self.env_map.get(angle, {}).get("S2", 0) < 20:
                return False
        return True
```

---

## 🔗 Comparison with Other Repos

### NVIDIA JetRacer
- Uses AI-based road following (no ultrasonic)
- Camera-only navigation via neural network
- No explicit obstacle avoidance - relies on learned behavior

### DiffBot (ROS)
- Uses Grove Ultrasonic Ranger
- Lidar-based SLAM for mapping
- move_base + costmaps for navigation
- Overkill for this project but good architecture reference

### ESP32 Obstacle Avoidance Bots (GitHub search)
- Most implement the basic pattern we need:
  ```cpp
  if (distance < threshold) {
      stopMotors();
      // scan
      // choose direction
      // resume
  }
  ```

---

## 📋 IPC Topics Reference

```python
# From src/core/ipc.py
TOPIC_WW_DETECTED = b"ww.detected"    # Wakeword triggered
TOPIC_STT = b"stt.result"             # Speech-to-text result
TOPIC_LLM_REQ = b"llm.request"        # Request to LLM
TOPIC_LLM_RESP = b"llm.response"      # LLM response
TOPIC_TTS = b"tts.request"            # Text-to-speech request
TOPIC_NAV = b"nav.command"            # Navigation command
TOPIC_ESP = b"esp.data"               # ESP32 sensor data (UNUSED!)
TOPIC_VISN = b"visn.detection"        # Vision detection result
```

**Key Gap:** `TOPIC_ESP` is published by `motor_bridge.py` but nothing subscribes to it!

---

## 🧪 Testing Approach

### 1. Bench Test ESP32 Safety (no wheels)
```cpp
// Test sequence via Serial Monitor:
1. Check sensors working: observe DATA output
2. Block S1 with hand: should see WARN:ESTOP:OBSTACLE
3. Try FORWARD command with blocked sensor: should fail
4. Unblock sensor: FORWARD should work
```

### 2. Stationary Integration Test
```bash
# On Pi, run motor_bridge in sim mode first:
python -m src.uart.motor_bridge --sim

# Publish test commands:
python -c "
from src.core.ipc import make_publisher, TOPIC_NAV, publish_json
pub = make_publisher({'ipc': {'downstream': 'tcp://127.0.0.1:6011'}})
publish_json(pub, TOPIC_NAV, {'direction': 'forward'})
"
```

### 3. Moving Test (LOW SPEED)
- Add PWM speed control to ESP32 (currently digital full-speed)
- Start at 25% speed for safety
- Test in open area with soft obstacles

---

## 📝 Files to Modify (Summary)

| File | Changes Needed |
|------|----------------|
| `src/uart/esp-code.ino` | Add collision avoidance logic in loop() |
| `src/uart/motor_bridge.py` | Add sensor data parsing and safety checks |
| `src/core/orchestrator.py` | Subscribe to TOPIC_ESP (optional, can do in motor_bridge) |
| NEW: `src/nav/navigator.py` | Intelligent navigation controller |

---

## 🎯 Success Criteria

1. ✅ Car STOPS automatically when obstacle < 10cm
2. ✅ Car WARNS at 20cm (blocks forward, allows backward)
3. ✅ 360° scan before moving in unknown environment
4. ✅ Auto-selects safest direction when requested path is blocked
5. ✅ Integrates with voice commands ("go forward" respects safety)
6. ✅ LLM can reference obstacle status in responses

---

## 🔄 Voice Pipeline Integration

The safety system must integrate with the existing voice pipeline:

```
User: "Go forward"
  ↓
Wakeword → STT → LLM → NAV → motor_bridge → ESP32
                              ↓
                        Safety Check
                              ↓
                        BLOCKED? → Auto-redirect or report
```

**LLM should know about obstacles.** When publishing to `TOPIC_LLM_REQ`, include sensor context:

```python
# In orchestrator or motor_bridge
payload = {
    "text": user_transcript,
    "obstacles": {
        "front_left": dist1,
        "front_center": dist2,
        "front_right": dist3
    },
    "movement_blocked": True/False
}
```

Then LLM can respond: *"I can't go forward, there's an obstacle 8cm ahead. Should I turn left instead?"*

---

## 🆘 Contact/Context

This document was created for AI agent handoff. The previous audit declared the system "99% complete" but **missed this critical safety gap**. The robot WILL crash without these changes.

Priority order:
1. ESP32 hardware safety (immediate stop)
2. Pi-side backup safety
3. 360° scanning
4. Intelligent navigation

Good luck! 🤖
