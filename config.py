import os


def _load_dotenv(path):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_load_dotenv(os.path.join(BASE_DIR, ".env"))


def env(key, default=None, required=False):
    val = os.environ.get(key, default)
    if required and not val:
        raise RuntimeError(f"missing required env var: {key} — copy .env.example to .env and fill it in")
    return val


BOT_TOKEN = env("BOT_TOKEN", required=True)
BOT_USERNAME = env("BOT_USERNAME", required=True)
ADMIN_IDS = {int(x) for x in env("ADMIN_IDS", "").split(",") if x.strip()}

ADMIN_PANEL_PASSWORD = env("ADMIN_PANEL_PASSWORD", required=True)
if ADMIN_PANEL_PASSWORD in ("change-me", "changeme", "admin", "password") or len(ADMIN_PANEL_PASSWORD) < 8:
    raise RuntimeError(
        "ADMIN_PANEL_PASSWORD в .env слишком слабый или дефолтный — поставь случайный пароль "
        "(например: mbs pass)"
    )

PANEL_DOMAIN = env("PANEL_DOMAIN", required=True)
SUB_DOMAIN = env("SUB_DOMAIN", required=True)
SITE_DOMAIN = env("SITE_DOMAIN", required=True)
BRAND_NAME = env("BRAND_NAME", "MBS Panel")
ADMIN_PATH = env("ADMIN_PATH", "admin").strip("/") or "admin"

DB_PATH = os.path.join(BASE_DIR, "mbs.db")
XRAY_CONFIG_PATH = "/usr/local/etc/xray/config.json"

XRAY_PUBLIC_KEY = env("XRAY_PUBLIC_KEY", required=True)
REALITY_SNI = env("REALITY_SNI", "www.wildberries.ru")
XRAY_SHORT_ID_TCP = env("XRAY_SHORT_ID_TCP", required=True)
XRAY_SHORT_ID_GRPC = env("XRAY_SHORT_ID_GRPC", required=True)
XRAY_SHORT_ID_XHTTP = env("XRAY_SHORT_ID_XHTTP", required=True)
DE1_ADDRESS = env("DE1_ADDRESS", f"de1.{SITE_DOMAIN}")

DE1_TRANSPORTS = [
    {
        "tag": "vless-tcp-reality",
        "label": "TCP + Reality (основной)",
        "network": "tcp",
        "security": "reality",
        "address": DE1_ADDRESS,
        "port": 443,
        "public_key": XRAY_PUBLIC_KEY,
        "short_id": XRAY_SHORT_ID_TCP,
        "sni": REALITY_SNI,
        "flow": "xtls-rprx-vision",
    },
    {
        "tag": "vless-grpc-reality",
        "label": "gRPC + Reality",
        "network": "grpc",
        "security": "reality",
        "address": DE1_ADDRESS,
        "port": 2053,
        "public_key": XRAY_PUBLIC_KEY,
        "short_id": XRAY_SHORT_ID_GRPC,
        "sni": REALITY_SNI,
        "service_name": "mbs-grpc",
    },
    {
        "tag": "vless-xhttp-reality",
        "label": "XHTTP + Reality",
        "network": "xhttp",
        "security": "reality",
        "address": DE1_ADDRESS,
        "port": 2087,
        "public_key": XRAY_PUBLIC_KEY,
        "short_id": XRAY_SHORT_ID_XHTTP,
        "sni": REALITY_SNI,
        "path": "/mbs-xh",
    },
    {
        "tag": "vless-ws-tls",
        "label": "WebSocket + TLS",
        "network": "ws",
        "security": "tls",
        "address": DE1_ADDRESS,
        "port": 8880,
        "path": "/mbs-ws",
    },
]

import json as _json

PLANS = [
    {"code": "7d", "label": "7 дней", "days": 7, "price": int(env("PRICE_7D", "150"))},
    {"code": "1m", "label": "1 месяц", "days": 30, "price": int(env("PRICE_1M", "399"))},
    {"code": "3m", "label": "3 месяца", "days": 90, "price": int(env("PRICE_3M", "999"))},
    {"code": "6m", "label": "6 месяцев", "days": 180, "price": int(env("PRICE_6M", "1799"))},
    {"code": "1y", "label": "1 год", "days": 365, "price": int(env("PRICE_1Y", "2999"))},
]
_plans_raw = env("PLANS_JSON", "").strip()
if _plans_raw:
    _custom_plans = _json.loads(_plans_raw)
    for _p in _custom_plans:
        if not {"code", "label", "days", "price"} <= set(_p):
            raise RuntimeError("PLANS_JSON: у каждого тарифа нужны code, label, days, price")
    PLANS = _custom_plans
PLANS_BY_CODE = {p["code"]: p for p in PLANS}

ALL_NODES_MODE = env("ALL_NODES_MODE", "false").lower() == "true"
HWID_BLOCK_REMOVED = env("HWID_BLOCK_REMOVED", "false").lower() == "true"
ABOUT_FOOTER = env("ABOUT_FOOTER", "")

PAYMENTS_ENABLED = env("PAYMENTS_ENABLED", "false").lower() == "true"

YOOKASSA_ENABLED = env("YOOKASSA_ENABLED", "false").lower() == "true"
YOOKASSA_SHOP_ID = env("YOOKASSA_SHOP_ID", "")
YOOKASSA_SECRET_KEY = env("YOOKASSA_SECRET_KEY", "")

PLATEGA_ENABLED = env("PLATEGA_ENABLED", "false").lower() == "true"
PLATEGA_MERCHANT_ID = env("PLATEGA_MERCHANT_ID", "")
PLATEGA_SECRET = env("PLATEGA_SECRET", "")

HWID_LIMIT_ENABLED = env("HWID_LIMIT_ENABLED", "false").lower() == "true"
HWID_FALLBACK_LIMIT = int(env("HWID_FALLBACK_LIMIT", "3"))

REFERRAL_ENABLED = env("REFERRAL_ENABLED", "true").lower() == "true"
REFERRAL_BONUS_DAYS = int(env("REFERRAL_BONUS_DAYS", "3"))
