Patched files:
- config/system.yaml (STT model set to tiny, LLM set to TinyLlama 1.1B Q4_K_M)
- src/core/ipc.py (ZeroMQ topics + helpers)
- src/wakeword/porcupine_runner.py (simulator-capable wakeword emitter)
- src/uart/sim_uart.py (TCP ACK simulator)
- src/uart/bridge.py (serial/TCP bridge)
- docs/ipc_uart_contract.md (seed contract)

Backups:
- None required; consider backing up docs/deep-research.md before manual dedupe.