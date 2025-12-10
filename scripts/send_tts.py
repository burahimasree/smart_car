#!/usr/bin/env python3
from __future__ import annotations
import sys
from pathlib import Path
import argparse

# Ensure project root in sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.config_loader import load_config
from src.core.ipc import make_publisher, TOPIC_TTS, publish_json


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--text', required=True, help='Text to speak')
    args = ap.parse_args()
    cfg = load_config(Path('config/system.yaml'))
    pub = make_publisher(cfg, channel='downstream')
    publish_json(pub, TOPIC_TTS, {'text': args.text})
    print('sent TTS:', args.text)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
