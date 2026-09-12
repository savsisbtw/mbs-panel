import base64
import hashlib
import hmac
import os
import struct
import time as timemod
import urllib.parse


def generate_secret() -> str:
    return base64.b32encode(os.urandom(20)).decode("ascii").rstrip("=")


def _hotp(secret_b32: str, counter: int) -> str:
    padded = secret_b32 + "=" * ((8 - len(secret_b32) % 8) % 8)
    key = base64.b32decode(padded.upper())
    msg = struct.pack(">Q", counter)
    h = hmac.new(key, msg, hashlib.sha1).digest()
    offset = h[-1] & 0x0F
    code = (struct.unpack(">I", h[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{code:06d}"


def now_code(secret_b32: str, for_time: float | None = None) -> str:
    t = for_time if for_time is not None else timemod.time()
    counter = int(t // 30)
    return _hotp(secret_b32, counter)


def verify(secret_b32: str, code: str, window: int = 1) -> bool:
    if not code or not code.isdigit() or len(code) != 6:
        return False
    counter = int(timemod.time() // 30)
    for offset in range(-window, window + 1):
        if hmac.compare_digest(_hotp(secret_b32, counter + offset), code):
            return True
    return False


def uri(secret_b32: str, username: str, issuer: str = "MBS Panel") -> str:
    label = urllib.parse.quote(f"{issuer}:{username}")
    return (
        f"otpauth://totp/{label}?secret={secret_b32}"
        f"&issuer={urllib.parse.quote(issuer)}&algorithm=SHA1&digits=6&period=30"
    )
