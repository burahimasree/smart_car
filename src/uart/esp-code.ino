// car_no_ota.ino
// Derived from existing car.ino but with WiFi and ArduinoOTA removed.
// Purpose: communicate with Raspberry Pi over Serial2 (UART) and
// provide ultrasonic sensors, MQ2 analog read, servo, and motor controls.

#include <ESP32Servo.h>
#include <HardwareSerial.h> // For Serial2

// We will use Serial2 to talk to the Raspberry Pi
HardwareSerial PiSerial(2); // Use UART Port 2 (Pins 16 RX, 17 TX)

// --- Global Objects ---
Servo myServo;

// ===================================
// === PIN DEFINITIONS             ===
// ===================================

// --- Sensor 1 Pins ---
#define TRIG1_PIN 4
#define ECHO1_PIN 5
// --- Sensor 2 Pins ---
#define TRIG2_PIN 18
#define ECHO2_PIN 19
// --- Sensor 3 Pins ---
#define TRIG3_PIN 21
#define ECHO3_PIN 22

// --- New Sensor Pins ---
#define MQ2_PIN 34     // Analog pin for MQ2 (Pin 34 is ADC1_CH6)
#define SERVO_PIN 23   // Pin for Servo motor

// --- Motor PINS (Digital for now) ---
#define IN1 25  // Left motor forward
#define IN2 26  // Left motor backward
#define IN3 27  // Right motor forward
#define IN4 14  // Right motor backward

// --- UART Pins (defined by PiSerial object) ---
#define RXD2 16
#define TXD2 17

// ===================================
// === CONSTANTS AND GLOBALS       ===
// ===================================

// Servo limits
#define SERVO_MIN_ANGLE 0
#define SERVO_MAX_ANGLE 180
#define SERVO_DEFAULT_ANGLE 90

// Command buffer
#define CMD_BUFFER_SIZE 128
char cmdBuffer[CMD_BUFFER_SIZE];
int cmdIndex = 0;

// Current states
int currentServoAngle = SERVO_DEFAULT_ANGLE;
int leftMotorSpeed = 0;
int rightMotorSpeed = 0;
bool motorsEnabled = true;

// ===================================
// === FUNCTION PROTOTYPES         ===
// ===================================
void moveMotors(int leftDirection, int rightDirection);
void moveForward(int speed);
void moveBackward(int speed);
void turnLeft(int speed);
void turnRight(int speed);
void stopMotors();
long readDistance(int trigPin, int echoPin);
void handleCommand(String command, String source);
void sendAck(String cmd, String status);
void sendStatus();
int parseIntParam(String cmd, int defaultVal);

// ===================================
// === SETUP                       ===
// ===================================

void setup() {
  // Serial for USB debugging
  Serial.begin(115200);
  delay(10);
  // Start Serial2 on pins 16 (RX) and 17 (TX)
  PiSerial.begin(115200, SERIAL_8N1, RXD2, TXD2);

  Serial.println("\n--- ESP32 Robot (no OTA) starting ---");
  PiSerial.println("ESP32 Ready (no OTA).");

  // --- Motor Setup (Digital for now) ---
  pinMode(IN1, OUTPUT);
  pinMode(IN2, OUTPUT);
  pinMode(IN3, OUTPUT);
  pinMode(IN4, OUTPUT);
  stopMotors(); // Start with motors stopped
  Serial.println("Motor driver (Digital) ready.");

  // --- Ultrasonic Sensor Setup ---
  pinMode(TRIG1_PIN, OUTPUT);
  pinMode(ECHO1_PIN, INPUT);
  pinMode(TRIG2_PIN, OUTPUT);
  pinMode(ECHO2_PIN, INPUT);
  pinMode(TRIG3_PIN, OUTPUT);
  pinMode(ECHO3_PIN, INPUT);
  Serial.println("Ultrasonic sensors ready.");

  // --- New Sensor Setup ---
  pinMode(MQ2_PIN, INPUT); // Set MQ2 pin as input
  myServo.attach(SERVO_PIN);
  myServo.write(SERVO_DEFAULT_ANGLE); // Center the servo
  Serial.println("MQ2 sensor and Servo ready.");

  Serial.println("--- Autonomous Robot Controller Ready (no OTA) ---");
  Serial.println("Protocol: CMD or CMD:PARAM (sent over Serial2/USB)");
}

// ===================================
// === MAIN LOOP                   ===
// ===================================

void loop() {
  // === 1. READ ALL SENSORS ===
  long dist1 = readDistance(TRIG1_PIN, ECHO1_PIN);
  long dist2 = readDistance(TRIG2_PIN, ECHO2_PIN);
  long dist3 = readDistance(TRIG3_PIN, ECHO3_PIN);
  int mq2_value = analogRead(MQ2_PIN); // Read analog value (0-4095)

  // === 2. SEND DATA TO RASPBERRY PI ===
  // Send data as a single, comma-separated line ending with '\n'
  PiSerial.print("DATA:S1:"); PiSerial.print(dist1);
  PiSerial.print(",S2:"); PiSerial.print(dist2);
  PiSerial.print(",S3:"); PiSerial.print(dist3);
  PiSerial.print(",MQ2:"); PiSerial.print(mq2_value);
  PiSerial.print(",SERVO:"); PiSerial.print(currentServoAngle);
  PiSerial.print(",LMOTOR:"); PiSerial.print(leftMotorSpeed);
  PiSerial.print(",RMOTOR:"); PiSerial.print(rightMotorSpeed);
  PiSerial.println(); // Send newline

  // === 3. CHECK FOR COMMANDS ===
  // Read from USB Serial Monitor
  while (Serial.available() > 0) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      if (cmdIndex > 0) {
        cmdBuffer[cmdIndex] = '\0';
        handleCommand(String(cmdBuffer), "USB");
        cmdIndex = 0;
      }
    } else if (cmdIndex < CMD_BUFFER_SIZE - 1) {
      cmdBuffer[cmdIndex++] = c;
    }
  }

  // Read from Raspberry Pi over UART (Serial2)
  while (PiSerial.available() > 0) {
    char c = PiSerial.read();
    if (c == '\n' || c == '\r') {
      if (cmdIndex > 0) {
        cmdBuffer[cmdIndex] = '\0';
        handleCommand(String(cmdBuffer), "Pi");
        cmdIndex = 0;
      }
    } else if (cmdIndex < CMD_BUFFER_SIZE - 1) {
      cmdBuffer[cmdIndex++] = c;
    }
  }

  // === 4. PRINT TO SERIAL MONITOR (for debugging) ===
  Serial.print("S1: "); Serial.print(dist1);
  Serial.print(" cm | S2: "); Serial.print(dist2);
  Serial.print(" cm | S3: "); Serial.print(dist3);
  Serial.print(" cm | MQ2: "); Serial.print(mq2_value);
  Serial.print(" | Servo: "); Serial.print(currentServoAngle);
  Serial.print(" | LSpeed: "); Serial.print(leftMotorSpeed);
  Serial.print(" | RSpeed: "); Serial.println(rightMotorSpeed);

  // --- DELAY ---
  delay(50); // Send ~20 Hz updates
}

// ===================================
// === COMMAND HANDLER             ===
// ===================================

void handleCommand(String command, String source) {
  Serial.print("Command '");
  Serial.print(command);
  Serial.print("' from ");
  Serial.println(source);

  command.trim();
  // Accept mixed case; normalize command token only
  String cmdu = command;
  cmdu.toUpperCase();

  if (cmdu.startsWith("SERVO:")) {
    int angle = parseIntParam(command, SERVO_DEFAULT_ANGLE);
    angle = constrain(angle, SERVO_MIN_ANGLE, SERVO_MAX_ANGLE);
    myServo.write(angle);
    currentServoAngle = angle;
    sendAck("SERVO", "OK:" + String(angle));
  } else if (cmdu.startsWith("FORWARD")) {
    moveForward(255);  // Digital full speed
    sendAck("FORWARD", "OK");
  } else if (cmdu.startsWith("BACKWARD")) {
    moveBackward(255);
    sendAck("BACKWARD", "OK");
  } else if (cmdu.startsWith("LEFT")) {
    turnLeft(255);
    sendAck("LEFT", "OK");
  } else if (cmdu.startsWith("RIGHT")) {
    turnRight(255);
    sendAck("RIGHT", "OK");
  } else if (cmdu == "STOP") {
    stopMotors();
    sendAck("STOP", "OK");
  } else if (cmdu == "STATUS") {
    sendStatus();
  } else if (cmdu == "RESET") {
    myServo.write(SERVO_DEFAULT_ANGLE);
    currentServoAngle = SERVO_DEFAULT_ANGLE;
    stopMotors();
    sendAck("RESET", "OK");
  } else {
    sendAck(command, "UNKNOWN");
  }
}

// ===================================
// === MOTOR FUNCTIONS (Digital)   ===
// ===================================

void moveMotors(int leftDirection, int rightDirection) {
  if (leftDirection > 0)     { digitalWrite(IN1, HIGH); digitalWrite(IN2, LOW); }
  else if (leftDirection < 0) { digitalWrite(IN1, LOW);  digitalWrite(IN2, HIGH); }
  else                        { digitalWrite(IN1, LOW);  digitalWrite(IN2, LOW); }

  if (rightDirection > 0)     { digitalWrite(IN3, HIGH); digitalWrite(IN4, LOW); }
  else if (rightDirection < 0){ digitalWrite(IN3, LOW);  digitalWrite(IN4, HIGH); }
  else                        { digitalWrite(IN3, LOW);  digitalWrite(IN4, LOW); }
}

void moveForward(int speed) { moveMotors(1, 1); }
void moveBackward(int speed) { moveMotors(-1, -1); }
void turnLeft(int speed) { moveMotors(1, -1); }
void turnRight(int speed) { moveMotors(-1, 1); }
void stopMotors() { moveMotors(0, 0); }

// ===================================
// === ULTRASONIC SENSOR FUNCTION  ===
// ===================================

long readDistance(int trigPin, int echoPin) {
  digitalWrite(trigPin, LOW);
  delayMicroseconds(2);
  digitalWrite(trigPin, HIGH);
  delayMicroseconds(10);
  digitalWrite(trigPin, LOW);

  long duration = pulseIn(echoPin, HIGH, 30000);
  if (duration == 0) return -1; // Timeout
  long distance = duration * 0.01715; // microseconds to cm
  return distance;
}

// ===================================
// === UTILITIES                   ===
// ===================================

void sendAck(String cmd, String status) {
  PiSerial.print("ACK:");
  PiSerial.print(cmd);
  PiSerial.print(":");
  PiSerial.println(status);
}

void sendStatus() {
  PiSerial.print("STATUS:SERVO:");
  PiSerial.print(currentServoAngle);
  PiSerial.print(",LMOTOR:");
  PiSerial.print(leftMotorSpeed);
  PiSerial.print(",RMOTOR:");
  PiSerial.println(rightMotorSpeed);
}

int parseIntParam(String cmd, int defaultVal) {
  int colonIndex = cmd.indexOf(':');
  if (colonIndex != -1) {
    String param = cmd.substring(colonIndex + 1);
    param.trim();
    return param.toInt();
  }
  return defaultVal;
}
