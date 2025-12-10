# Unified Audio Architecture

## Overview

This document describes the unified audio pipeline architecture that solves **microphone resource contention** on Raspberry Pi.

## The Problem

On Raspberry Pi with ALSA, only ONE process can open a hardware audio device at a time. The previous architecture had:

1. **Wakeword Service** - Opens mic, holds it forever
2. **STT Service** - Tries to open mic when triggered → **FAILS: Device busy**
3. **AudioManager** - Designed to fix this, but not used consistently

This caused the classic error:
```
OSError: [Errno -9997] Invalid sample rate
OSError: [Errno -9985] Device or resource busy
```

## The Solution

### New Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    USB MICROPHONE                               │
│                    (Physical Device)                            │
└─────────────────────────────┬───────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│              UnifiedAudioCapture                                │
│              (src/audio/unified_audio.py)                       │
│                                                                 │
│   • Opens PyAudio ONCE                                         │
│   • Writes to ring buffer continuously                         │
│   • Multiple consumers read via their own pointers             │
└─────────────────────────────┬───────────────────────────────────┘
                              │
              ┌───────────────┴───────────────┐
              ▼                               ▼
┌─────────────────────────┐     ┌─────────────────────────┐
│   Wakeword Consumer     │     │     STT Consumer        │
│   (Porcupine)           │     │   (faster-whisper)      │
│                         │     │                         │
│   read_index: 12345     │     │   read_index: 12300     │
│   Reads 512 samples     │     │   Reads on-demand       │
└─────────────────────────┘     └─────────────────────────┘
```

### Key Components

#### 1. UnifiedAudioCapture (`src/audio/unified_audio.py`)

Single-writer, multi-reader ring buffer for audio:

```python
from src.audio.unified_audio import get_unified_audio, AudioConfig

# Get singleton instance
audio = get_unified_audio(AudioConfig(
    sample_rate=16000,
    chunk_ms=30,
    device_keyword="USB Audio"
))

# Start capture (opens mic ONCE)
audio.start()

# Register multiple consumers
wakeword_consumer = audio.register_consumer("wakeword", priority=5)
stt_consumer = audio.register_consumer("stt", priority=10)

# Each consumer reads independently
samples = audio.read_chunk("wakeword", num_samples=512)
```

#### 2. UnifiedVoicePipeline (`src/audio/unified_voice_pipeline.py`)

Combined wakeword + STT in single process:

```python
from src.audio.unified_voice_pipeline import UnifiedVoicePipeline

pipeline = UnifiedVoicePipeline(Path("config/system.yaml"))
pipeline.start()
pipeline.run()  # Main loop: wakeword → STT → publish
```

### State Machine

```
    ┌──────────────────────────────────────────────────────────┐
    │                                                          │
    ▼                                                          │
┌──────────┐   wakeword    ┌───────────┐  silence  ┌──────────┐
│   IDLE   │ ─────────────▶│ CAPTURING │ ─────────▶│TRANSCRIBE│
│(wakeword)│               │  (STT)    │           │  (STT)   │
└──────────┘               └───────────┘           └────┬─────┘
    ▲                                                   │
    │                                                   │
    └───────────────────────────────────────────────────┘
                        publish result
```

## Configuration

Enable in `config/system.yaml`:

```yaml
audio:
  use_unified_pipeline: true  # Use single-process voice pipeline
  preferred_device_substring: USB Audio  # Match your mic
  wakeword_frame_ms: 30  # Porcupine frame size
  stt_chunk_ms: 500  # STT capture chunks
```

## Deployment

### Using systemd (Recommended)

```bash
# Stop old services
sudo systemctl stop wakeword stt-wrapper

# Disable old services  
sudo systemctl disable wakeword stt-wrapper

# Enable unified pipeline
sudo systemctl enable voice-pipeline
sudo systemctl start voice-pipeline

# Check status
sudo systemctl status voice-pipeline
journalctl -u voice-pipeline -f
```

### Manual Testing

```bash
# Run diagnostics first
python tools/diagnose_audio.py

# Test unified pipeline
python -m src.audio.unified_voice_pipeline --config config/system.yaml
```

## Troubleshooting

### "Device or resource busy"

```bash
# Find processes holding the mic
fuser -v /dev/snd/*

# Kill them
sudo fuser -k /dev/snd/pcmC3D0c
```

### No wakeword detection

```bash
# Check Porcupine access key
echo $PV_ACCESS_KEY

# Verify keyword file exists
ls -la models/wakeword/*.ppn
```

### Low STT accuracy

```bash
# Check audio levels
arecord -D default -f S16_LE -c 1 -r 16000 -d 3 test.wav
aplay test.wav

# Try higher quality model
# Edit config/system.yaml:
# stt.engines.faster_whisper.model: small.en
```

## Migration from Old Architecture

1. **Update config**: Set `audio.use_unified_pipeline: true`
2. **Update systemd**: Use `voice-pipeline.service` instead of `wakeword.service` + `stt-wrapper.service`
3. **Run diagnostics**: `python tools/diagnose_audio.py`
4. **Test**: Say your wakeword and verify STT output in logs

## Files Changed

| File | Purpose |
|------|---------|
| `src/audio/unified_audio.py` | Ring buffer + multi-consumer capture |
| `src/audio/unified_voice_pipeline.py` | Combined wakeword + STT service |
| `systemd/voice-pipeline.service` | New systemd unit |
| `tools/diagnose_audio.py` | Audio troubleshooting |
| `config/system.yaml` | New `use_unified_pipeline` option |
