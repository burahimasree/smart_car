#!/usr/bin/env python3
import urllib.request, json
data = json.dumps({"intent": "scan"}).encode()
req = urllib.request.Request(
    "http://127.0.0.1:8770/intent", 
    data=data, 
    headers={"Content-Type": "application/json"}
)
try:
    resp = urllib.request.urlopen(req)
    print("Status:", resp.status)
    print("Response:", resp.read().decode())
except urllib.error.HTTPError as e:
    print("Error:", e.code, e.read().decode())
