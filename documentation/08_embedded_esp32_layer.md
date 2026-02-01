# Embedded ESP32 Layer

## Document Information

| Attribute | Value |
|-----------|-------|
| Document | 08_embedded_esp32_layer.md |
| Version | 1.0 |
| Last Updated | 2026-02-01 |

---

## Overview

The ESP32 serves as the low-level hardware controller, managing:
- Motor control via L298N driver
- Ultrasonic distance sensing (3 sensors)
- Gas detection (MQ2 sensor)
- Servo control for sensor scanning
- Collision avoidance logic

Communication with the Raspberry Pi occurs over UART at 115200 baud.

---

## Hardware Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              RASPBERRY PI 4                                  │
│                                                                             │
│     ┌─────────────────────────────────────────────────────────────┐         │
│     │                     motor_bridge.py                         │         │
│     │                    (UART interface)                         │         │
│     └─────────────────────────────┬───────────────────────────────┘         │
│                                   │                                          │
└───────────────────────────────────┼──────────────────────────────────────────┘
                                    │
                               USB Serial
                            (/dev/ttyUSB0)
                              115200 baud
                                    │
┌───────────────────────────────────┼──────────────────────────────────────────┐
│                                   │                                          │
│                              ESP32 DevKit                                    │
│                                   │                                          │
│    ┌──────────────────────────────┴───────────────────────────────┐         │
│    │                      UART Command Handler                     │         │
│    └──────────┬────────────────┬────────────────┬─────────────────┘         │
│               │                │                │                            │
│               ▼                ▼                ▼                            │
│    ┌──────────────┐  ┌──────────────┐  ┌──────────────┐                     │
│    │  Motor       │  │  Sensor      │  │  Servo       │                     │
│    │  Control     │  │  Reading     │  │  Control     │                     │
│    └──────┬───────┘  └──────┬───────┘  └──────┬───────┘                     │
│           │                 │                 │                              │
└───────────┼─────────────────┼─────────────────┼──────────────────────────────┘
            │                 │                 │
            ▼                 ▼                 ▼
     ┌─────────────┐   ┌─────────────┐   ┌─────────────┐
     │    L298N    │   │  HC-SR04    │   │    SG90     │
     │  Driver x2  │   │    x3       │   │   Servo     │
     └──────┬──────┘   └─────────────┘   └─────────────┘
            │                 │
            ▼                 │
     ┌─────────────┐         │
     │   Motors    │   ┌─────┴─────┐
     │   (4x DC)   │   │   MQ2     │
     └─────────────┘   │   Gas     │
                       └───────────┘
```

---

## Hardware Components

### ESP32 DevKit

| Parameter | Value |
|-----------|-------|
| Model | ESP32-WROOM-32 |
| Clock | 240 MHz dual-core |
| Flash | 4 MB |
| SRAM | 520 KB |
| Framework | Arduino |

### Motor Driver

| Parameter | Value |
|-----------|-------|
| Model | L298N |
| Quantity | 2 (for 4 motors) |
| Voltage | 12V (logic 5V) |
| Max Current | 2A per channel |
| PWM Frequency | 1 kHz |

### Ultrasonic Sensors

| Parameter | Value |
|-----------|-------|
| Model | HC-SR04 |
| Quantity | 3 (left, center, right) |
| Range | 2-400 cm |
| Angle | 15° cone |
| Frequency | 40 kHz |

### Gas Sensor

| Parameter | Value |
|-----------|-------|
| Model | MQ2 |
| Detection | Combustible gases, smoke |
| Output | Analog (0-4095 on ESP32) |
| Preheat | 20 seconds |

### Servo

| Parameter | Value |
|-----------|-------|
| Model | SG90 |
| Range | 0-180° |
| Speed | 0.1s/60° |
| Torque | 1.8 kg·cm |

---

## Pin Mapping

### Motor Driver Pins

| Function | ESP32 Pin | L298N Pin |
|----------|-----------|-----------|
| Left Motor Enable | GPIO 25 | ENA |
| Left Motor IN1 | GPIO 26 | IN1 |
| Left Motor IN2 | GPIO 27 | IN2 |
| Right Motor Enable | GPIO 14 | ENB |
| Right Motor IN3 | GPIO 12 | IN3 |
| Right Motor IN4 | GPIO 13 | IN4 |

### Ultrasonic Pins

| Sensor | Trigger | Echo |
|--------|---------|------|
| S1 (Left) | GPIO 5 | GPIO 18 |
| S2 (Center) | GPIO 19 | GPIO 21 |
| S3 (Right) | GPIO 22 | GPIO 23 |

### Other Pins

| Function | ESP32 Pin |
|----------|-----------|
| MQ2 Analog | GPIO 34 (ADC) |
| Servo PWM | GPIO 15 |
| Status LED | GPIO 2 (built-in) |
| UART TX | GPIO 1 (USB) |
| UART RX | GPIO 3 (USB) |

---

## UART Protocol

### Physical Layer

| Parameter | Value |
|-----------|-------|
| Baud Rate | 115200 |
| Data Bits | 8 |
| Stop Bits | 1 |
| Parity | None |
| Flow Control | None |

### Command Format (Pi → ESP32)

Plain text commands with newline terminator:

```
COMMAND\n
```

### Supported Commands

| Command | Description |
|---------|-------------|
| `FORWARD\n` | Move forward |
| `BACKWARD\n` | Move backward |
| `LEFT\n` | Turn left (pivot) |
| `RIGHT\n` | Turn right (pivot) |
| `STOP\n` | Stop all motors |
| `SCAN\n` | Perform 180° sensor sweep |
| `STATUS\n` | Request sensor data |
| `RESET\n` | Reset to initial state |
| `CLEARBLOCK\n` | Clear obstacle block flag |
| `SPEED:nnn\n` | Set motor PWM (0-255) |
| `SERVO:nnn\n` | Set servo angle (0-180) |

### Response Format (ESP32 → Pi)

#### Acknowledgment

```
ACK:COMMAND:STATUS\n
```

Examples:
- `ACK:FORWARD:OK\n` - Command executed
- `ACK:FORWARD:BLOCKED:OBSTACLE\n` - Blocked by obstacle
- `ACK:FORWARD:BLOCKED:WARNING_ZONE\n` - In warning zone

#### Sensor Data

Periodic transmission (every 100ms):

```
DATA:S1:nnn,S2:nnn,S3:nnn,MQ2:nnn,SERVO:nnn,LMOTOR:nnn,RMOTOR:nnn,OBSTACLE:n,WARNING:n\n
```

| Field | Type | Range | Description |
|-------|------|-------|-------------|
| S1 | int | 0-400 | Left ultrasonic (cm), -1 if error |
| S2 | int | 0-400 | Center ultrasonic (cm), -1 if error |
| S3 | int | 0-400 | Right ultrasonic (cm), -1 if error |
| MQ2 | int | 0-4095 | Gas sensor ADC value |
| SERVO | int | 0-180 | Current servo angle |
| LMOTOR | int | -255 to 255 | Left motor speed (negative = reverse) |
| RMOTOR | int | -255 to 255 | Right motor speed (negative = reverse) |
| OBSTACLE | int | 0/1 | Obstacle detected (any sensor < min_distance) |
| WARNING | int | 0/1 | Warning zone (any sensor < warning_distance) |

Example:
```
DATA:S1:16,S2:12,S3:-1,MQ2:478,SERVO:90,LMOTOR:-255,RMOTOR:-255,OBSTACLE:0,WARNING:0\n
```

#### Error Messages

```
ERROR:reason\n
```

Examples:
- `ERROR:INVALID_COMMAND\n`
- `ERROR:SENSOR_FAILURE:S3\n`
- `ERROR:MOTOR_FAULT\n`

---

## Collision Avoidance

### Distance Thresholds

| Zone | Distance | Behavior |
|------|----------|----------|
| Safe | > 40 cm | Normal operation |
| Warning | 20-40 cm | Warning flag set |
| Obstacle | < 20 cm | Block forward movement |

### Logic Implementation

```cpp
bool isObstacle() {
    int minDist = min(min(s1, s2), s3);
    // Ignore -1 (sensor error)
    if (s1 > 0 && s1 < MIN_DISTANCE) return true;
    if (s2 > 0 && s2 < MIN_DISTANCE) return true;
    if (s3 > 0 && s3 < MIN_DISTANCE) return true;
    return false;
}

bool isWarning() {
    if (s1 > 0 && s1 < WARNING_DISTANCE) return true;
    if (s2 > 0 && s2 < WARNING_DISTANCE) return true;
    if (s3 > 0 && s3 < WARNING_DISTANCE) return true;
    return false;
}

void executeForward() {
    if (isObstacle()) {
        sendAck("FORWARD", "BLOCKED:OBSTACLE");
        return;
    }
    if (isWarning()) {
        sendAck("FORWARD", "BLOCKED:WARNING_ZONE");
        return;
    }
    setMotors(255, 255);
    sendAck("FORWARD", "OK");
}
```

### Backward Movement

Backward is not blocked by front sensors. Only potential rear sensors (if equipped) would block backward movement.

---

## Motor Control

### Direction Mapping

| Command | Left Motor | Right Motor |
|---------|------------|-------------|
| FORWARD | +255 | +255 |
| BACKWARD | -255 | -255 |
| LEFT | -255 | +255 |
| RIGHT | +255 | -255 |
| STOP | 0 | 0 |

### PWM Control

```cpp
void setMotor(int enablePin, int in1, int in2, int speed) {
    if (speed > 0) {
        digitalWrite(in1, HIGH);
        digitalWrite(in2, LOW);
        analogWrite(enablePin, speed);
    } else if (speed < 0) {
        digitalWrite(in1, LOW);
        digitalWrite(in2, HIGH);
        analogWrite(enablePin, -speed);
    } else {
        digitalWrite(in1, LOW);
        digitalWrite(in2, LOW);
        analogWrite(enablePin, 0);
    }
}
```

---

## Sensor Reading

### Ultrasonic Measurement

```cpp
long measureDistance(int trigPin, int echoPin) {
    digitalWrite(trigPin, LOW);
    delayMicroseconds(2);
    digitalWrite(trigPin, HIGH);
    delayMicroseconds(10);
    digitalWrite(trigPin, LOW);
    
    long duration = pulseIn(echoPin, HIGH, 30000);  // 30ms timeout
    
    if (duration == 0) {
        return -1;  // Timeout / no echo
    }
    
    long distance = duration * 0.034 / 2;  // cm
    
    if (distance > 400) {
        return -1;  // Out of range
    }
    
    return distance;
}
```

### Gas Sensor Reading

```cpp
int readMQ2() {
    return analogRead(MQ2_PIN);  // 0-4095
}

// Note: Raw ADC value, not PPM
// Higher value = more gas detected
// Typical air baseline: 100-300
// Smoke/gas detected: > 500
```

---

## Main Loop Structure

```cpp
void loop() {
    // 1. Read incoming commands
    if (Serial.available()) {
        String cmd = Serial.readStringUntil('\n');
        cmd.trim();
        processCommand(cmd);
    }
    
    // 2. Read sensors (every 50ms)
    if (millis() - lastSensorRead > 50) {
        readAllSensors();
        lastSensorRead = millis();
    }
    
    // 3. Send sensor data (every 100ms)
    if (millis() - lastDataSend > 100) {
        sendSensorData();
        lastDataSend = millis();
    }
    
    // 4. Safety check (every 50ms)
    if (millis() - lastSafetyCheck > 50) {
        checkSafety();
        lastSafetyCheck = millis();
    }
}

void checkSafety() {
    if (isMovingForward && isObstacle()) {
        stopMotors();
        Serial.println("SAFETY:EMERGENCY_STOP:OBSTACLE");
    }
}
```

---

## Servo Scan Mode

When `SCAN\n` command is received:

```cpp
void performScan() {
    int scanData[19];  // 0-180 in 10° steps
    
    for (int angle = 0; angle <= 180; angle += 10) {
        servo.write(angle);
        delay(200);  // Wait for servo to reach position
        scanData[angle/10] = measureDistance(S2_TRIG, S2_ECHO);  // Use center sensor
    }
    
    // Return to center
    servo.write(90);
    
    // Send scan results
    Serial.print("SCAN:");
    for (int i = 0; i < 19; i++) {
        Serial.print(scanData[i]);
        if (i < 18) Serial.print(",");
    }
    Serial.println();
}
```

---

## Power Management

### Power Distribution

```
┌─────────────────┐
│  12V Battery    │
│   (LiPo 3S)     │
└────────┬────────┘
         │
    ┌────┴────┐
    │         │
    ▼         ▼
┌───────┐  ┌───────┐
│ L298N │  │ Buck  │
│ (12V) │  │ 5V    │
└───┬───┘  └───┬───┘
    │          │
    ▼          ▼
 Motors    ┌───────┐
           │ ESP32 │
           │ Servo │
           │Sensors│
           └───────┘
```

### Current Consumption

| Component | Typical | Max |
|-----------|---------|-----|
| ESP32 | 80 mA | 500 mA |
| Motors (4x) | 200 mA | 2 A each |
| Servo | 50 mA | 500 mA |
| Sensors | 50 mA | 100 mA |
| **Total** | 500 mA | 9 A |

---

## Firmware State Machine

```
┌─────────┐
│  INIT   │
│(startup)│
└────┬────┘
     │
     ▼
┌─────────┐        ┌─────────┐
│  IDLE   │◄──────►│ MOVING  │
│(waiting)│ cmds   │(forward/│
└────┬────┘        │backward)│
     │             └────┬────┘
     │                  │
     │             obstacle
     │                  │
     ▼                  ▼
┌─────────┐        ┌─────────┐
│SCANNING │        │ BLOCKED │
│(servo   │        │(stopped)│
│ sweep)  │        │         │
└─────────┘        └─────────┘
```

---

## Known Limitations

| Limitation | Impact |
|------------|--------|
| No rear sensors | Backward movement unprotected |
| Single-threaded | Sensor read blocks command processing |
| USB power dependency | ESP32 resets if USB disconnected |
| No EEPROM persistence | Settings lost on reset |
| Fixed PWM frequency | May cause motor noise |

---

## Debugging

### Serial Monitor

Connect USB and monitor at 115200 baud to see:
- Incoming commands
- Acknowledgments
- Sensor data stream

### LED Indicators

| Pattern | Meaning |
|---------|---------|
| Solid | Normal operation |
| Fast blink | Processing command |
| Slow blink | Waiting for connection |
| Off | Error state |

---

## References

| Document | Purpose |
|----------|---------|
| [05_services_reference.md](05_services_reference.md) | UART bridge details |
| [04_ipc_and_data_flow.md](04_ipc_and_data_flow.md) | UART protocol translation |
| [11_execution_flows.md](11_execution_flows.md) | Motor command flow |
