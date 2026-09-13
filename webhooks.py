import hashlib
import hmac
import json
import urllib.request

from legal import read_env_var


def send(event: str, data: dict):
    url = read_env_var("WEBHOOK_URL")
    secret = read_env_var("WEBHOOK_SECRET")
    if not url or not secret:
        return
    body = json.dumps({"event": event, "data": data}).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", "X-Signature": signature},
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass
