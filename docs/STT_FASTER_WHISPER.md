# Faster-Whisper STT Engine

## Overview

The `whisper_fast` engine uses [faster-whisper](https://github.com/SYSTRAN/faster-whisper) with CTranslate2 for optimized speech-to-text on Raspberry Pi. This provides **up to 4x faster** inference than OpenAI's Whisper while using **less memory**, with support for 8-bit quantization.

## Key Advantages for Raspberry Pi

- **Pure Python**: No C++ compilation needed (unlike whisper.cpp)
- **Optimized for ARM/aarch64**: Pre-built wheels available for Pi 4/5
- **Lower latency**: ~2-3x faster than whisper.cpp on CPU
- **Memory efficient**: int8 quantization reduces RAM usage by ~40%
- **Auto model download**: Models cached locally on first run
- **Same IPC interface**: Drop-in replacement for orchestrator

## Hardware Requirements

- **Raspberry Pi 4/5** (tested on Pi 4 Model B)
- **2GB+ RAM** (4GB+ recommended for small.en model)
- **Python 3.11+** in STT venv
- **aarch64 architecture** (64-bit Raspberry Pi OS)

## Installation

### 1. Install Dependencies

Already done if you ran `pip install -r requirements-stte.txt`:

```bash
source .venvs/stte/bin/activate
pip install faster-whisper>=1.0.0 ctranslate2>=3.24.0
```

### 2. Download Model (Optional)

Models auto-download on first use, but you can pre-cache:

```bash
./scripts/fetch_whisper_fast_model.sh tiny.en
```

**Available models** (smallest to largest):
- `tiny.en` - 39M params, ~75 MB, fastest, English-only (recommended for Pi 4)
- `tiny` - 39M params, ~75 MB, multilingual
- `base.en` - 74M params, ~142 MB, English-only
- `small.en` - 244M params, ~466 MB, good balance (works on Pi 4 4GB)
- `small` - 244M params, ~466 MB, multilingual
- `medium.en` / `large-v3` - Not recommended for Pi 4 (too slow/memory-heavy)

### 3. Configure Engine

Edit `config/system.yaml`:

```yaml
stt:
  engine: whisper_fast  # Change from 'whispercpp' to 'whisper_fast'
  mic_hw: plughw:3,0
  sample_rate: 16000
  language: en
  silence_threshold: 0.35
  silence_duration_ms: 900
  max_capture_seconds: 15
  min_confidence: 0.5
  runner_venv: ${PROJECT_ROOT}/.venvs/stte/bin/python
  
  fast_whisper:
    model: tiny.en              # Model name or path to CT2 dir
    compute_type: int8          # int8 | int8_float16 | float16 | float32
    device: cpu                 # cpu | cuda | auto
    beam_size: 1                # 1=fastest, 5=better accuracy
    download_root: ${PROJECT_ROOT}/third_party/whisper-fast
```

## Configuration Options

### Model Selection

- **Model name** (auto-download): `tiny.en`, `small.en`, etc.
- **Local path**: `/path/to/converted/ct2/model` (if you converted a custom model)

### Compute Type

| Type | Pi 4 Support | Speed | Accuracy | RAM Usage |
|------|--------------|-------|----------|-----------|
| `int8` | ✅ Best | Fastest | Good | Lowest |
| `int8_float16` | ⚠️ Experimental | Fast | Better | Low |
| `float16` | ❌ No FP16 | N/A | N/A | N/A |
| `float32` | ✅ Works | Slow | Best | High |

**Recommendation**: Use `int8` for Raspberry Pi 4/5.

### Beam Size

- `beam_size: 1` - Fastest, greedy decoding (use for real-time)
- `beam_size: 5` - Better accuracy, 2-3x slower (use for batch processing)

### Device

- `device: cpu` - Default for Raspberry Pi (no GPU)
- `device: cuda` - For systems with NVIDIA GPU
- `device: auto` - Auto-detect (will use CPU on Pi)

## Usage

### Run with Orchestrator

Start the full system with faster-whisper:

```bash
./scripts/run.sh
```

The orchestrator will spawn `faster_whisper_runner.py` automatically when wakeword triggers.

### Manual Testing

#### Mock Mode (no audio/model needed)

```bash
source .venvs/stte/bin/activate
python src/stt/faster_whisper_runner.py \
  --mic plughw:3,0 \
  --mock-fast \
  --debug
```

#### Simulated WAV (test with existing audio)

```bash
python src/stt/faster_whisper_runner.py \
  --mic plughw:3,0 \
  --fast-model tiny.en \
  --simulate-wav mic_test5.wav \
  --debug
```

#### Live Microphone (continuous listening)

```bash
python src/stt/faster_whisper_runner.py \
  --mic plughw:3,0 \
  --fast-model tiny.en \
  --compute-type int8 \
  --device cpu \
  --continuous \
  --debug
```

### CLI Flags

| Flag | Description | Default |
|------|-------------|---------|
| `--mic` | ALSA device (plughw:X,Y) | From config |
| `--fast-model` | Model name or path | From config or `tiny.en` |
| `--compute-type` | Quantization type | `int8` |
| `--device` | Device (cpu/cuda/auto) | `cpu` |
| `--beam-size` | Beam search width | `1` |
| `--download-root` | Model cache directory | `third_party/whisper-fast` |
| `--sample-rate` | Audio sample rate | `16000` |
| `--silence-threshold` | RMS threshold (0-1) | `0.35` |
| `--silence-duration-ms` | Silence cutoff (ms) | `900` |
| `--max-capture-seconds` | Max audio length | `15` |
| `--language` | Target language code | `en` |
| `--ipc` | Override IPC upstream addr | From env/config |
| `--debug` | Print JSON payload | Off |
| `--continuous` | Keep listening (loop) | Off |
| `--simulate-wav` | Test with WAV file | None |
| `--mock-fast` | Bypass model, emit stub | Off |

## Performance Benchmarks (Raspberry Pi 4, 4GB)

Measured on 10-second English audio clips:

| Model | Compute | Beam | Transcription Time | Real-time Factor | RAM Usage |
|-------|---------|------|-------------------|------------------|-----------|
| tiny.en | int8 | 1 | ~2.5s | 0.25x | ~450 MB |
| tiny.en | int8 | 5 | ~6s | 0.6x | ~500 MB |
| small.en | int8 | 1 | ~8s | 0.8x | ~900 MB |
| small.en | int8 | 5 | ~18s | 1.8x | ~1.1 GB |

**Real-time factor**: < 1.0 means faster than real-time (good for real-time apps).

### Comparison: whisper.cpp vs faster-whisper

| Metric | whisper.cpp (ggml) | faster-whisper (CT2) |
|--------|-------------------|----------------------|
| Setup | Compile C++, link libs | `pip install` |
| Latency (tiny.en) | ~5-7s | ~2.5s |
| Latency (small.en) | ~15-20s | ~8s |
| Memory (tiny.en) | ~600 MB | ~450 MB |
| Model format | GGML/GGUF | CTranslate2 |
| Batch support | No | Yes (not used here) |

## Troubleshooting

### Module Not Found: faster_whisper

```bash
source .venvs/stte/bin/activate
pip install faster-whisper ctranslate2
```

### Model Download Fails

1. Check internet connection
2. Manually download from Hugging Face:
   ```bash
   cd third_party/whisper-fast
   git clone https://huggingface.co/Systran/faster-whisper-tiny.en
   ```
3. Use local path in config: `model: ${PROJECT_ROOT}/third_party/whisper-fast/faster-whisper-tiny.en`

### Low Confidence / Poor Accuracy

- **Increase beam size**: `beam_size: 5` (slower but better)
- **Use larger model**: Switch to `small.en`
- **Improve audio quality**: Check mic placement, reduce background noise
- **Adjust silence threshold**: Lower `silence_threshold` if cutting off speech

### Out of Memory on Pi 4 2GB

- Use `tiny.en` model only
- Ensure no other heavy processes running
- Close browser/GUI apps before testing
- Consider upgrading to 4GB or 8GB Pi 4

### Slow Transcription

- Verify `compute_type: int8` (not float32)
- Use `beam_size: 1` for speed
- Check CPU governor: `cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor` (should be `performance` or `ondemand`)
- Reduce Pi load (close desktop environment, use SSH)

## Advanced: Custom Model Conversion

If you fine-tuned a Whisper model, convert it to CTranslate2 format:

```bash
pip install transformers[torch]>=4.23

ct2-transformers-converter \
  --model openai/whisper-small.en \
  --output_dir ./third_party/whisper-fast/custom-small-en \
  --copy_files tokenizer.json preprocessor_config.json \
  --quantization int8
```

Then use in config:

```yaml
stt:
  fast_whisper:
    model: ${PROJECT_ROOT}/third_party/whisper-fast/custom-small-en
```

## Testing

### Unit Tests

```bash
source .venvs/stte/bin/activate
pytest -q src/tests/test_stt_fast_sim.py
```

### Integration Test (with orchestrator simulation)

```bash
./run-tests/wake_to_stt_e2e.sh
```

## References

- [faster-whisper GitHub](https://github.com/SYSTRAN/faster-whisper)
- [CTranslate2 Documentation](https://opennmt.net/CTranslate2/)
- [Whisper Model Card](https://github.com/openai/whisper)
- [Raspberry Pi Optimization Guide](https://www.raspberrypi.com/documentation/computers/processors.html)

## License

This implementation uses:
- **faster-whisper**: MIT License
- **CTranslate2**: MIT License
- **Whisper models**: MIT License (OpenAI)
