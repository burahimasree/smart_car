# LED Ring Status Guide

This document explains how the new LED ring service (`src/piled/led_ring_service.py`) represents
orchestrator activity on the 8-pixel NeoPixel ring connected to GPIO12. The service runs inside
`.venvs/visn` and subscribes directly to the real voice-pipeline topics (wakeword, STT, LLM, TTS,
health, listen start/stop) so the hardware mirrors the actual state without simulations.

## Runtime Overview

1. **Environment**: `.venvs/visn` (already contains `adafruit-blinka`, `rpi_ws281x`, `RPi.GPIO`, etc.).
2. **Entry point**: `python -m src.piled.led_ring_service` (systemd unit `led-status.service` now uses this).
3. **Inputs**:
   - Upstream topics: `ww.detected`, `stt.transcription`, `llm.response`, `tts.speak` (completion), `system.health`.
   - Downstream topics: `cmd.listen.start`, `cmd.listen.stop`, `llm.request`, `tts.speak` (text queued).
4. **Outputs**: LED color/animation frames updated at ~60 FPS; no other side effects.

## Color / Animation Mapping

| State trigger | Pattern description | Meaning |
|---------------|--------------------|---------|
| Idle / baseline | Soft teal breathing (cyan fade in/out) | System ready, no active requests. Slight white tint you noticed is the cyan breathing; it indicates idle heartbeat. |
| Wakeword detected (`ww.detected`) | Amber flash for ~1.2 s, then hands off to listening | Wakeword hit, orchestrator pausing vision and opening mic. |
| Listening (`cmd.listen.start`) | Deep blue spinner chasing around ring | STT capturing audio; ring rotates to show mic is live. |
| STT result / LLM pending (`stt.transcription` or `llm.request`) | Purple/magenta swirl with flowing gradients | Text handed to LLM; thinking/intent resolution in progress. |
| LLM response queued for speech (`tts.speak` text) | Lime sweep loader (green-yellow arc moving) | TTS request accepted, audio about to play. |
| Speaking / TTS playback (`tts.speak` done pending) | Green breathing/pulse | Voice output is actively playing. |
| TTS completion (`tts.speak` done) | Returns to idle teal | Conversation finished, mic closed, vision resumed. |
| Health fault (`system.health` ok=false) | Red strobe flashing until ok=true received | Some service reported unhealthy; LED stays red to force attention. |
| Manual fallback/timeout | Animator automatically falls back to idle after state-specific hold windows expire to avoid stuck colors. |

### Why you see whitish/bright color in idle
The idle pattern mixes low green and blue to create teal. When brightness is at 0.25 it can look
close to white, especially if the ring diffuser blends colors. This is intended: the subtle teal
breathing indicates everything is idle and healthy. If you prefer a darker or more saturated idle
tone, adjust `_render_idle` inside `LedAnimator` (e.g., drop the green component).

## How to Run

```bash
cd /home/dev/project_root
sudo systemctl daemon-reload        # once after code change
sudo systemctl restart led-status.service
journalctl -u led-status.service -f # watch logs while LEDs update
```

Manual run for debugging:

```bash
cd /home/dev/project_root
sudo -E env PYTHONPATH=/home/dev/project_root \
     PROJECT_ROOT=/home/dev/project_root \
     SYSTEM_CONFIG=config/system.yaml \
     .venvs/visn/bin/python -m src.piled.led_ring_service
```

(Press `Ctrl+C` to exit; service clears LEDs on shutdown.)

## Integration Notes
- Requires root because rpi_ws281x maps `/dev/mem`.
- Shares the same NeoPixel stack as `led-test.py`; we used it as reference when creating the new
  service to guarantee colors render identically.
- No simulators: the ring only reacts to real orchestrator traffic. To test individual states, you
  can publish sample ZMQ messages (e.g., use `src/tools/chat_llm_cli.py` or a one-off script).
- The service lives in `src/piled` per your request; systemd `led-status.service` now points at it.

Feel free to tweak color values or animation math inside `LedAnimator` if you want more contrast
(e.g., a darker idle state). Let me know if you want a helper CLI to cycle through the states for
demonstrations.
