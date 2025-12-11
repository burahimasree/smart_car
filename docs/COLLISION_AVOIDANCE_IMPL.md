# Collision Avoidance Implementation

**Date:** December 11, 2024  
**Status:** ✅ FULLY IMPLEMENTED  
**Priority:** Safety-Critical

---

## Summary

Implemented dual-layer collision avoidance:
1. **ESP32 (Hardware Level)** - Immediate reaction, no network latency
2. **Pi (Software Level)** - Backup safety, IPC integration

---

## ESP32 Implementation (`src/uart/esp-code.ino`)

### Constants Added
```cpp
#define STOP_DISTANCE_CM 10       // Emergency stop
#define WARNING_DISTANCE_CM 20    // Block forward commands
#define SCAN_ROTATE_TIME_MS 200   // Rotation per scan step
#define SCAN_PAUSE_MS 100         // Settle time after rotation
```

### Safety State Variables
```cpp
bool motorsEnabled = true;           // Can drive forward?
bool obstacleDetected = false;       // Emergency stop active?
bool inWarningZone = false;          // Warning zone active?
long lastSensorDistances[3];         // Cached S1, S2, S3
```

### Main Loop Changes
1. Reads all 3 sensors (S1, S2, S3)
2. **Calls `checkCollision()` BEFORE processing commands**
3. Sends extended DATA with `OBSTACLE:0|1,WARNING:0|1` flags
4. Debug output shows OBS/WARN/MotorOK status

### `checkCollision()` Logic
```
IF minDist <= 10cm:
  - emergencyStop() - STOPS MOTORS IMMEDIATELY
  - obstacleDetected = true
  - motorsEnabled = false
  - sendCollisionAlert("EMERGENCY_STOP")

ELIF minDist <= 20cm:
  - inWarningZone = true
  - motorsEnabled = true (can turn/backup)
  - sendCollisionAlert("WARNING_ZONE")

ELSE (>20cm):
  - All flags cleared
  - sendCollisionAlert("CLEAR") if was blocked
```

### Command Handler Changes
- **FORWARD blocked** if `!motorsEnabled || obstacleDetected || inWarningZone`
- Returns `ACK:FORWARD:BLOCKED:OBSTACLE` or `ACK:FORWARD:BLOCKED:WARNING_ZONE`
- **BACKWARD always allowed** (escape maneuver)
- New commands: `SCAN`, `CLEARBLOCK`, `RESET` clears safety state

### New UART Messages
```
# Collision Alerts
ALERT:COLLISION:EMERGENCY_STOP,S1:5,S2:8,S3:15
ALERT:COLLISION:WARNING_ZONE,S1:12,S2:18,S3:25
ALERT:COLLISION:CLEAR,S1:30,S2:45,S3:50

# Extended DATA
DATA:S1:30,S2:45,S3:50,MQ2:100,SERVO:90,LMOTOR:0,RMOTOR:0,OBSTACLE:0,WARNING:0

# Scan Output
SCAN:START
SCAN:POS:0,S1:30,S2:40,S3:35
SCAN:POS:45,S1:25,S2:30,S3:28
... (8 positions)
SCAN:BEST:180,DIST:85
SCAN:COMPLETE
```

### 360° Scan (`performScan()`)
1. Robot rotates in 8 steps (45° each)
2. At each position, reads all 3 sensors
3. Reports scan data via UART
4. Finds best direction (max average distance)
5. Reports `SCAN:BEST:angle,DIST:distance`

---

## Pi Implementation (`src/uart/motor_bridge.py`)

### New `SensorData` Dataclass
```python
@dataclass
class SensorData:
    s1: int = -1
    s2: int = -1
    s3: int = -1
    mq2: int = 0
    obstacle: bool = False
    warning: bool = False
    
    @property
    def min_distance(self) -> int
    
    @property
    def is_safe(self) -> bool
```

### Pi-Side Safety Check
```python
def _check_pi_side_safety(self, cmd: MotorCommand) -> tuple[bool, str]:
    # Always allow: stop, backward, left, right, scan, etc.
    # For forward: check sensor data
    if direction == "forward" and self._last_sensor_data:
        if sd.obstacle: return False, "ESP32 obstacle detected"
        if sd.warning: return False, "ESP32 warning zone"
        if sd.min_distance < 10: return False, "Pi safety: too close"
        if sd.min_distance < 20: return False, "Pi safety: warning"
    return True, ""
```

### Enhanced `_process_rx()`
- Parses new `OBSTACLE:` and `WARNING:` fields
- Handles `ALERT:COLLISION:*` messages
- Handles `SCAN:*` messages
- Publishes structured data to `TOPIC_ESP`

### New Public Methods
```python
def request_scan(self) -> bool       # Trigger 360° scan
def get_sensor_data() -> SensorData  # Get latest readings
def is_safe_to_move() -> bool        # Quick safety check
```

---

## IPC Integration

### TOPIC_ESP Payloads
```json
// Sensor data
{
  "data": {
    "s1": 30, "s2": 45, "s3": 50,
    "mq2": 100,
    "min_distance": 30,
    "obstacle": false,
    "warning": false,
    "is_safe": true
  }
}

// Collision alert
{"alert": "COLLISION", "alert_data": "EMERGENCY_STOP,S1:5,S2:8,S3:15"}

// Command blocked
{"blocked": true, "command": "forward", "reason": "ESP32 obstacle detected"}

// Scan progress
{"scan": "POS", "scan_data": "45,S1:25,S2:30,S3:28"}
```

---

## Testing Checklist

### ESP32 Tests (Serial Monitor)
- [ ] Send `FORWARD` when obstacle <10cm → should get `BLOCKED:OBSTACLE`
- [ ] Send `FORWARD` when obstacle 10-20cm → should get `BLOCKED:WARNING_ZONE`
- [ ] Send `FORWARD` when clear (>20cm) → should get `OK`
- [ ] Place obstacle <10cm while moving → motors should stop
- [ ] Send `BACKWARD` when blocked → should work
- [ ] Send `LEFT`/`RIGHT` when blocked → should work
- [ ] Send `SCAN` → should rotate 360° and report
- [ ] Send `CLEARBLOCK` → should clear safety block
- [ ] Send `RESET` → should reset all state

### Pi Tests (motor_bridge.py)
- [ ] Start with `--sim` mode
- [ ] Publish `{"direction": "forward"}` to `TOPIC_NAV`
- [ ] Monitor `TOPIC_ESP` for sensor data
- [ ] Check logs for safety blocks

### Integration Tests
- [ ] Wakeword → "go forward" → check collision avoidance
- [ ] LLM command "scan" → 360° scan executes
- [ ] Emergency stop triggers alert to display

---

## Files Modified

| File | Changes |
|------|---------|
| `src/uart/esp-code.ino` | Added collision avoidance, scan, alerts |
| `src/uart/motor_bridge.py` | Added SensorData, safety check, scan handling |

---

## Notes

- **No servo scanning** - Robot rotates itself to look around
- **Dual-layer safety** - ESP32 is primary, Pi is backup
- **No lag** - ESP32 collision check runs at 20Hz (50ms loop)
- **Backward always allowed** - Escape from obstacles
- **CLEARBLOCK** - Manual override (use with caution)
