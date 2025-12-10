# STT Quick Start Guide

## TL;DR - Start Using faster-whisper NOW

```bash
# 1. Switch engine (edit config/system.yaml)
sed -i 's/engine: whispercpp/engine: whisper_fast/' config/system.yaml

# 2. Run the system
./scripts/run.sh
```

That's it! Models auto-download on first run.

---

## Available STT Engines

| Engine | Binary | Speed on Pi 4 | Memory | Setup Difficulty |
|--------|--------|---------------|--------|------------------|
| `whispercpp` | whisper.cpp (C++) | Baseline (5-7s) | 600 MB | Hard (compile) |
| `whisper_fast` | faster-whisper (Python) | **2-3x faster** (2.5s) | **450 MB** | **Easy (pip)** |

**Recommendation**: Use `whisper_fast` (default going forward).

---

## Commands

### Quick Test (Mock Mode)

```bash
source .venvs/stte/bin/activate
python src/stt/faster_whisper_runner.py \
  --mic plughw:3,0 \
  --mock-fast \
  --debug
```

**Expected output**:
```json
{
  "timestamp": 1764253666,
  "text": "simulated command",
  "confidence": 0.95,
  "language": "en",
  "durations_ms": {"capture": 0, "whisper": 0, "total": 2}
}
```

### Test with Real Audio File

```bash
python src/stt/faster_whisper_runner.py \
  --mic plughw:3,0 \
  --fast-model tiny.en \
  --simulate-wav mic_test5.wav \
  --debug
```

### Live Microphone (Continuous)

```bash
python src/stt/faster_whisper_runner.py \
  --mic plughw:3,0 \
  --fast-model tiny.en \
  --continuous \
  --debug
```

Press `Ctrl+C` to stop.

### Download Model Ahead of Time

```bash
./scripts/fetch_whisper_fast_model.sh tiny.en
# Or for better accuracy:
./scripts/fetch_whisper_fast_model.sh small.en
```

### Run Unit Tests

```bash
source .venvs/stte/bin/activate
pytest -q src/tests/test_stt_fast_sim.py
```

---

## Configuration Presets

Copy-paste into `config/system.yaml`:

### Fastest (Real-Time Voice Assistant)

```yaml
stt:
  engine: whisper_fast
  mic_hw: plughw:3,0
  sample_rate: 16000
  language: en
  silence_threshold: 0.35
  silence_duration_ms: 900
  max_capture_seconds: 15
  min_confidence: 0.5
  runner_venv: ${PROJECT_ROOT}/.venvs/stte/bin/python
  
  fast_whisper:
    model: tiny.en
    compute_type: int8
    device: cpu
    beam_size: 1
    download_root: ${PROJECT_ROOT}/third_party/whisper-fast
```

**Latency**: ~2.5s for 10s audio  
**Accuracy**: Good for commands/short phrases

### Balanced (Good Accuracy, Still Fast)

```yaml
stt:
  engine: whisper_fast
  fast_whisper:
    model: small.en      # ← Changed
    beam_size: 1         # Still fast
    # ... rest same
```

**Latency**: ~8s for 10s audio  
**Accuracy**: Much better transcription quality

### Best Quality (Slower)

```yaml
stt:
  engine: whisper_fast
  fast_whisper:
    model: small.en
    beam_size: 5         # ← Changed (slower but better)
    # ... rest same
```

**Latency**: ~18s for 10s audio  
**Accuracy**: Best for critical applications

---

## Model Comparison (Pi 4)

| Model | Params | File Size | Load Time | Transcription (10s) | RAM | Accuracy |
|-------|--------|-----------|-----------|---------------------|-----|----------|
| `tiny.en` | 39M | 75 MB | 1.6s | 2.5s | 450 MB | ⭐⭐⭐ |
| `base.en` | 74M | 142 MB | 2.5s | 4s | 550 MB | ⭐⭐⭐⭐ |
| `small.en` | 244M | 466 MB | 5s | 8s | 900 MB | ⭐⭐⭐⭐⭐ |

**For Pi 4 (4GB)**: Use `tiny.en` or `small.en`  
**For Pi 4 (2GB)**: Use `tiny.en` only

---

## Troubleshooting

### "Module not found: faster_whisper"

```bash
source .venvs/stte/bin/activate
pip install faster-whisper ctranslate2
```

### Slow transcription

1. Check compute type: `compute_type: int8` (not float32)
2. Use `beam_size: 1`
3. Try smaller model: `model: tiny.en`

### Model download fails

Internet down or HuggingFace blocked? Manually download:

```bash
cd third_party/whisper-fast
git clone https://huggingface.co/Systran/faster-whisper-tiny.en
```

Then in config:

```yaml
fast_whisper:
  model: ${PROJECT_ROOT}/third_party/whisper-fast/faster-whisper-tiny.en
```

### Out of memory

- Use `tiny.en` model
- Close desktop/browser
- Reboot Pi: `sudo reboot`

---

## Integration with Orchestrator

No changes needed! The `whisper_fast` engine publishes the same IPC messages as `whispercpp`:

```
Wakeword → Orchestrator → STT (whisper_fast) → LLM → TTS
```

Just change `stt.engine` and restart:

```bash
pkill -f orchestrator
./scripts/run.sh
```

---

## Performance Tips

### Raspberry Pi Optimization

```bash
# 1. Set CPU governor to performance
echo performance | sudo tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor

# 2. Disable swap (if using SD card)
sudo swapoff -a

# 3. Close GUI (run headless)
sudo systemctl set-default multi-user.target
sudo reboot
```

### Thread Count

CTranslate2 auto-detects threads. To override:

```bash
export OMP_NUM_THREADS=4  # Pi 4 has 4 cores
python src/stt/faster_whisper_runner.py ...
```

---

## Next Steps

1. **Switch default engine**: Edit `config/system.yaml`
2. **Test with wake-to-STT**: `./run-tests/wake_to_stt_e2e.sh`
3. **Benchmark your audio**: Compare whisper.cpp vs faster-whisper
4. **Fine-tune config**: Adjust `beam_size`, `silence_threshold`, etc.
5. **Read full docs**: `docs/STT_FASTER_WHISPER.md`

---

## Summary

| Action | Command |
|--------|---------|
| Install | `pip install faster-whisper ctranslate2` |
| Download model | `./scripts/fetch_whisper_fast_model.sh tiny.en` |
| Test mock | `python src/stt/faster_whisper_runner.py --mock-fast --debug` |
| Test live | `python src/stt/faster_whisper_runner.py --continuous --debug` |
| Switch engine | Edit `config/system.yaml`: `engine: whisper_fast` |
| Run system | `./scripts/run.sh` |

**Questions?** See `docs/STT_FASTER_WHISPER.md` or `IMPLEMENTATION_STT_FASTER_WHISPER.md`.
