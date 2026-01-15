#!/usr/bin/env python3
import re
from pathlib import Path

config_path = Path("config/system.yaml")
content = config_path.read_text()

# Lower min_confidence from 0.5 to 0.25
content = re.sub(r'min_confidence: 0\.5', 'min_confidence: 0.25', content)

config_path.write_text(content)
print("Updated min_confidence to 0.25")
