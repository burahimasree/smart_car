#!/usr/bin/env python3
import re
from pathlib import Path

config_path = Path("config/system.yaml")
content = config_path.read_text()

# Fix payload_keyword and payload_variant
content = re.sub(r'payload_keyword: "hey genny"', 'payload_keyword: "hey veera"', content)
content = re.sub(r'payload_variant: "genny"', 'payload_variant: "veera"', content)

# Also update keywords list
content = re.sub(r'- hey genny', '- hey veera', content)
content = re.sub(r'- genny\n', '- veera\n', content)
content = re.sub(r'- genney', '- veera', content)
content = re.sub(r'- hi genny', '- hi veera', content)
content = re.sub(r'- genie', '- veera', content)
content = re.sub(r'- jenni', '- veera', content)

config_path.write_text(content)
print("Updated wakeword config to 'hey veera'")
