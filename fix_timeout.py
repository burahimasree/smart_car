#!/usr/bin/env python3
import re
from pathlib import Path

config_path = Path("config/system.yaml")
content = config_path.read_text()

# Increase STT timeout from 10 to 45 seconds
content = re.sub(r'timeout_seconds: 10\.0', 'timeout_seconds: 45.0', content)

config_path.write_text(content)
print("Updated STT timeout to 45 seconds")
