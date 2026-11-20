"""
Load-cell device simulator — demonstrates the device-to-Django data flow without hardware.

Usage (server running on http://127.0.0.1:8000):

    python iot/simulator.py --device sugar-scale-01 --key sisd_xxx --grams 14500
    python iot/simulator.py --device sugar-scale-01 --key sisd_xxx --grams 14500 --every 10 --drift -150

Sends one reading (or one every N seconds) to POST /api/v1/iot/readings/ using
only the Python standard library. Each reading has a unique message id; use
--repeat-last to resend the previous id and see duplicate handling.
"""
import argparse
import json
import random
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone


def send(url, device, key, grams, message_id):
    body = json.dumps({
        "message_id": message_id,
        "measured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "weight_g": round(grams, 1),
        "firmware": "sim-1.0",
    }).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", "X-Device-Id": device, "Authorization": f"Device {key}",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://127.0.0.1:8000/api/v1/iot/readings/")
    ap.add_argument("--device", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--grams", type=float, required=True, help="Gross weight on the scale")
    ap.add_argument("--noise", type=float, default=40, help="Std-dev of measurement noise (g)")
    ap.add_argument("--every", type=float, default=0, help="Repeat every N seconds (0 = once)")
    ap.add_argument("--drift", type=float, default=0, help="Grams added per reading (negative = items removed)")
    ap.add_argument("--repeat-last", action="store_true", help="Resend the same message id (tests de-duplication)")
    a = ap.parse_args()

    grams, msg_id = a.grams, uuid.uuid4().hex[:16]
    while True:
        reading = grams + random.gauss(0, a.noise)
        status, data = send(a.url, a.device, a.key, reading, msg_id)
        print(f"{datetime.now():%H:%M:%S}  {reading:9.1f} g  →  HTTP {status}  {data}")
        if not a.every:
            if a.repeat_last:
                status, data = send(a.url, a.device, a.key, reading, msg_id)
                print(f"(resend same id)  →  HTTP {status}  {data}")
            break
        time.sleep(a.every)
        grams += a.drift
        msg_id = uuid.uuid4().hex[:16]


if __name__ == "__main__":
    main()
