#!/usr/bin/env python3
import re
from pathlib import Path

config_path = Path("config/system.yaml")
content = config_path.read_text()

# Fix payload_keyword and payload_variant
content = re.sub(r'payload_keyword: "hey genny"', 'payload_keyword: "hey robo"', content)
content = re.sub(r'payload_variant: "genny"', 'payload_variant: "robo"', content)

# Also update keywords list
content = re.sub(r'- hey genny', '- hey robo', content)
content = re.sub(r'- genny\n', '- robo\n', content)
content = re.sub(r'- genney', '- robo', content)
content = re.sub(r'- hi genny', '- hi robo', content)
content = re.sub(r'- genie', '- robo', content)
content = re.sub(r'- jenni', '- robo', content)

config_path.write_text(content)
print("Updated wakeword config to 'hey robo'")
