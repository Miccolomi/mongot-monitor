import requests

try:
    resp = requests.get("http://10.1.0.35:9946/metrics", timeout=2)
    for line in resp.text.split("\n"):
        if "steady" in line.lower() or "applicable" in line.lower() or "merge" in line.lower():
            if not line.startswith("#"):
                print(line)
except Exception as e:
    print(e)
