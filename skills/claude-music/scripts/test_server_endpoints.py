import http.client
import json
import sys
import urllib.request

BASE_URL = "http://127.0.0.1:8765"

def check(name, condition, details=""):
    if condition:
        print(f" [PASS] {name}")
    else:
        print(f" [FAIL] {name}: {details}")
        sys.exit(1)

print("--- Running server.py endpoint & security suite ---")

# 1. GET /
req = urllib.request.Request(f"{BASE_URL}/")
with urllib.request.urlopen(req) as resp:
    check("GET / returns 200", resp.status == 200)
    html = resp.read().decode("utf-8")
    check("GET / contains dashboard html", "<title>" in html)

# 2. HEAD /
conn = http.client.HTTPConnection("127.0.0.1", 8765)
conn.request("HEAD", "/")
resp = conn.getresponse()
check("HEAD / returns 200", resp.status == 200)
body = resp.read()
check("HEAD / body is empty (0 bytes)", len(body) == 0, f"got {len(body)} bytes")
conn.close()

# 3. GET /api/status
req = urllib.request.Request(f"{BASE_URL}/api/status")
with urllib.request.urlopen(req) as resp:
    check("GET /api/status returns 200", resp.status == 200)
    data = json.loads(resp.read())
    check("GET /api/status has configured and output_dir", "configured" in data and "output_dir" in data)

# 4. GET /api/shorts/library
req = urllib.request.Request(f"{BASE_URL}/api/shorts/library")
with urllib.request.urlopen(req) as resp:
    check("GET /api/shorts/library returns 200", resp.status == 200)
    data = json.loads(resp.read())
    check("GET /api/shorts/library has shorts list", "shorts" in data and len(data["shorts"]) > 0)

# 5. HEAD /api/shorts/video/pro_reel.mp4
conn = http.client.HTTPConnection("127.0.0.1", 8765)
conn.request("HEAD", "/api/shorts/video/pro_reel.mp4")
resp = conn.getresponse()
check("HEAD /api/shorts/video/pro_reel.mp4 returns 200", resp.status == 200)
check("HEAD /api/shorts/video Content-Type is video/mp4", resp.getheader("Content-Type") == "video/mp4")
check("HEAD /api/shorts/video has Accept-Ranges: bytes", resp.getheader("Accept-Ranges") == "bytes")
body = resp.read()
check("HEAD /api/shorts/video body is empty (0 bytes)", len(body) == 0, f"got {len(body)} bytes")
conn.close()

# 6. GET /api/shorts/video/pro_reel.mp4 with Range header
conn = http.client.HTTPConnection("127.0.0.1", 8765)
conn.request("GET", "/api/shorts/video/pro_reel.mp4", headers={"Range": "bytes=0-1023"})
resp = conn.getresponse()
check("Range GET returns 206 Partial Content", resp.status == 206)
check("Content-Range header present", resp.getheader("Content-Range") is not None)
chunk = resp.read()
check("Range GET returned exactly 1024 bytes", len(chunk) == 1024, f"got {len(chunk)} bytes")
conn.close()

# 7. GET /api/shorts/thumb/pro_reel.jpg
req = urllib.request.Request(f"{BASE_URL}/api/shorts/thumb/pro_reel.jpg")
with urllib.request.urlopen(req) as resp:
    check("GET /api/shorts/thumb returns 200", resp.status == 200)
    check("Thumbnail Content-Type is image/jpeg", resp.headers.get("Content-Type") == "image/jpeg")

# 8. Path Traversal Security Checks
for bad_path in [
    "/api/shorts/video/../../config.json",
    "/api/shorts/video/..%2F..%2Fconfig.json",
    "/api/shorts/thumb/../../config.json",
    "/api/audio/../../config.json"
]:
    conn = http.client.HTTPConnection("127.0.0.1", 8765)
    conn.request("GET", bad_path)
    resp = conn.getresponse()
    resp.read()
    check(f"Path traversal blocked for {bad_path}", resp.status in (400, 403, 404), f"status={resp.status}")
    conn.close()

# 9. POST /api/shorts/cancel
req = urllib.request.Request(f"{BASE_URL}/api/shorts/cancel", data=b"{}", headers={"Content-Type": "application/json"})
with urllib.request.urlopen(req) as resp:
    check("POST /api/shorts/cancel returns 200", resp.status == 200)
    data = json.loads(resp.read())
    check("Cancel response has status idle", data.get("status") == "idle")

# 10. POST /api/shorts/recut validation
req = urllib.request.Request(
    f"{BASE_URL}/api/shorts/recut",
    data=json.dumps({"clip_id": "nonexistent", "start_time": 10.0, "end_time": 5.0}).encode(),
    headers={"Content-Type": "application/json"}
)
try:
    urllib.request.urlopen(req)
    check("POST /api/shorts/recut rejects end_time <= start_time", False, "Expected 400")
except urllib.error.HTTPError as e:
    check("POST /api/shorts/recut rejects end_time <= start_time", e.code == 400)

# 11. POST /api/shorts/combine validation (requires >= 2 clips)
req = urllib.request.Request(
    f"{BASE_URL}/api/shorts/combine",
    data=json.dumps({"clip_ids": ["clip_1"]}).encode(),
    headers={"Content-Type": "application/json"}
)
try:
    urllib.request.urlopen(req)
    check("POST /api/shorts/combine requires at least 2 clips", False, "Expected 400")
except urllib.error.HTTPError as e:
    check("POST /api/shorts/combine requires at least 2 clips", e.code == 400)

# 12. POST /api/voiceover validation (empty text rejected)
req = urllib.request.Request(
    f"{BASE_URL}/api/voiceover",
    data=json.dumps({"text": ""}).encode(),
    headers={"Content-Type": "application/json"}
)
try:
    urllib.request.urlopen(req)
    check("POST /api/voiceover rejects empty text", False, "Expected 400")
except urllib.error.HTTPError as e:
    check("POST /api/voiceover rejects empty text", e.code == 400)

print("\nAll server.py endpoint and security tests PASSED cleanly!")
