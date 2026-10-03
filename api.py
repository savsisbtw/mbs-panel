import asyncio
import csv
import datetime
import io
import json
import os
import re
import secrets
import subprocess
import urllib.request
import uuid as uuidlib
from concurrent.futures import ThreadPoolExecutor
from fastapi import FastAPI, HTTPException, Request, Response, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi import Body
from fastapi.responses import HTMLResponse, PlainTextResponse, FileResponse

import backup
import chains
import db
import legal
import links
import nodeprov
import payments
import settings
import totp
import webhooks
import xray_manager
from config import SITE_DOMAIN, SUB_DOMAIN, PANEL_DOMAIN, ADMIN_PATH, BASE_DIR

HWID_RE = re.compile(r"^[a-zA-Z0-9=-]{10,64}$")
NODE_CODE_RE = re.compile(r"^[a-zA-Z0-9_-]{1,32}$")
ENV_PATH = os.path.join(BASE_DIR, ".env")

db.init_db()


def _update_env_var(key: str, value: str):
    legal.update_env_var(key, value)

app = FastAPI(title="mbs-api")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[f"https://{SITE_DOMAIN}", f"https://www.{SITE_DOMAIN}", f"https://{PANEL_DOMAIN}"],
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["*"],
    allow_credentials=True,
)

ADMIN_COOKIE = "mbs_admin"


def require_admin(request: Request):
    token = request.cookies.get(ADMIN_COOKIE)
    if not db.validate_admin_session(token):
        raise HTTPException(401, "unauthorized")


AUDIT_RULES = [
    ("POST", r"^/admin/api/nodes$", "node.add"),
    ("POST", r"^/admin/api/nodes/provision-guide$", "node.provision"),
    ("POST", r"^/admin/api/nodes/reorder$", "node.reorder"),
    ("PATCH", r"^/admin/api/nodes/[^/]+$", "node.edit"),
    ("DELETE", r"^/admin/api/nodes/[^/]+$", "node.delete"),
    ("POST", r"^/admin/api/chains$", "chain.create"),
    ("PATCH", r"^/admin/api/chains/[^/]+$", "chain.edit"),
    ("DELETE", r"^/admin/api/chains/[^/]+$", "chain.delete"),
    ("POST", r"^/admin/api/subscriptions/[^/]+/revoke$", "sub.revoke"),
    ("POST", r"^/admin/api/subscriptions/[^/]+/hold$", "sub.hold"),
    ("POST", r"^/admin/api/subscriptions/[^/]+/resume$", "sub.resume"),
    ("POST", r"^/admin/api/subscriptions/[^/]+/reset-traffic$", "sub.reset_traffic"),
    ("POST", r"^/admin/api/users/-?\d+/grant$", "user.grant"),
    ("POST", r"^/admin/api/users/-?\d+/hwid-limit$", "user.hwid_limit"),
    ("DELETE", r"^/admin/api/users/-?\d+/devices/\d+$", "user.device_delete"),
    ("POST", r"^/admin/api/gift-codes$", "gift.create"),
    ("POST", r"^/admin/api/admins$", "admin.add"),
    ("DELETE", r"^/admin/api/admins/\d+$", "admin.delete"),
    ("POST", r"^/admin/api/2fa/enable$", "2fa.enable"),
    ("POST", r"^/admin/api/2fa/disable$", "2fa.disable"),
    ("GET", r"^/admin/api/backup$", "backup.download"),
    ("POST", r"^/admin/api/backup/restore$", "backup.restore"),
    ("POST", r"^/admin/api/settings/bot$", "settings.bot"),
    ("POST", r"^/admin/api/branding$", "settings.brand"),
    ("POST", r"^/admin/api/webhook-settings$", "settings.webhook"),
    ("POST", r"^/admin/api/hwid-settings$", "settings.hwid"),
    ("POST", r"^/admin/api/payments/(yookassa-settings|platega-settings|plan-settings|legal-settings)$", "settings.payments"),
]
AUDIT_COMPILED = [(method, re.compile(pattern), action) for method, pattern, action in AUDIT_RULES]


def _audit_action(method: str, path: str):
    for rule_method, pattern, action in AUDIT_COMPILED:
        if rule_method == method and pattern.match(path):
            return action
    return None


@app.middleware("http")
async def audit_middleware(request: Request, call_next):
    path = request.url.path
    action = _audit_action(request.method, path) if path.startswith("/admin/api/") else None
    admin_name = None
    if action:
        admin = await asyncio.to_thread(db.get_session_admin, request.cookies.get(ADMIN_COOKIE))
        admin_name = admin["username"] if admin else None
    response = await call_next(request)
    if action and response.status_code < 400:
        await asyncio.to_thread(db.add_audit, admin_name, action, path, _client_ip(request))
    return response


def _days_left(sub: dict) -> int:
    exp = datetime.datetime.fromisoformat(sub["expires_at"])
    reference = datetime.datetime.fromisoformat(sub["held_at"]) if sub.get("held_at") else datetime.datetime.utcnow()
    delta = exp - reference
    return max(0, delta.days)


CLIENT_UA_MARKERS = (
    "happ", "v2ray", "v2box", "nekoray", "nekobox", "clash", "hiddify",
    "streisand", "shadowrocket", "sing-box", "singbox", "karing", "loon",
    "quantumult", "surge", "stash",
)


def _is_app_client(user_agent: str) -> bool:
    ua = (user_agent or "").lower()
    return any(m in ua for m in CLIENT_UA_MARKERS)


SUB_PAGE_TEMPLATE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{brand_name} — подписка</title>
<style>
  :root {{
    --bg: #0a0b0f; --card: #131519; --border: #1e2128;
    --text: #eceef2; --muted: #868c99; --accent: #7c6cf0;
    --ease: cubic-bezier(0.16, 1, 0.3, 1);
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
    background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    -webkit-font-smoothing: antialiased;
    padding: 24px;
  }}
  .card {{
    max-width: 400px; width: 100%; background: var(--card); border: 1px solid var(--border);
    border-radius: 16px; padding: 32px 28px; text-align: center;
    opacity: 0; transform: translateY(10px); filter: blur(6px);
    animation: enter 0.6s var(--ease) forwards;
  }}
  @keyframes enter {{ to {{ opacity: 1; transform: translateY(0); filter: blur(0); }} }}
  .badge {{
    font-size: 12px; color: var(--muted); letter-spacing: 0.06em;
    text-transform: uppercase; margin-bottom: 10px;
  }}
  h1 {{ font-size: 20px; margin: 0 0 6px; font-weight: 600; letter-spacing: -0.01em; }}
  p.sub {{ color: var(--muted); font-size: 14px; margin: 0 0 26px; line-height: 1.5; }}
  .btn {{
    display: block; width: 100%; padding: 14px 18px; border-radius: 10px;
    background: var(--text); color: var(--bg); text-decoration: none;
    font-weight: 600; font-size: 15px; margin-bottom: 10px; border: none; cursor: pointer;
    transition: transform 0.15s var(--ease), opacity 0.15s var(--ease);
  }}
  .btn:hover {{ opacity: 0.85; }}
  .btn:active {{ transform: scale(0.97); }}
  .btn.secondary {{ background: transparent; color: var(--text); border: 1px solid var(--border); }}
  .btn.secondary:hover {{ opacity: 1; border-color: #333947; }}
  .link-box {{
    background: var(--bg); border: 1px solid var(--border); border-radius: 10px; padding: 12px;
    font-size: 12px; color: var(--muted); word-break: break-all; margin-bottom: 22px; text-align: left;
    opacity: 0; animation: fadeIn 0.5s var(--ease) 0.2s forwards;
  }}
  @keyframes fadeIn {{ to {{ opacity: 1; }} }}
  .thanks {{ font-size: 13px; color: var(--muted); line-height: 1.5; }}
  .thanks a {{ color: var(--accent); text-decoration: none; }}
  .thanks a:hover {{ text-decoration: underline; }}
  .qr-box {{
    display: flex; justify-content: center; margin-bottom: 22px;
    opacity: 0; animation: fadeIn 0.5s var(--ease) 0.3s forwards;
  }}
  .qr-box img, .qr-box canvas {{ border-radius: 10px; background: #fff; padding: 8px; }}

  @media (prefers-reduced-motion: reduce) {{
    *, *::before, *::after {{ animation-duration: 0.01ms !important; transition-duration: 0.01ms !important; }}
    .card, .link-box, .qr-box {{ opacity: 1 !important; transform: none !important; filter: none !important; }}
  }}
</style>
</head>
<body>
  <div class="card">
    <div class="badge">{brand_name}</div>
    <h1>Подписка готова</h1>
    <p class="sub">Нажми кнопку — сервер добавится в Happ автоматически, или отсканируй QR другим устройством</p>
    <a class="btn" href="happ://add/{sub_url}">Добавить в Happ</a>
    <a class="btn secondary" href="{sub_url}">Открыть ссылку подписки</a>
    <div class="qr-box" id="qr"></div>
    <div class="link-box">{sub_url}</div>
    <div class="thanks">Спасибо, что пользуетесь {brand_name}.<br>Нет Happ? Скачай: <a href="https://apps.apple.com/us/app/happ-proxy-utility/id6504287215" target="_blank">App Store</a> · <a href="https://play.google.com/store/apps/details?id=com.happproxy" target="_blank">Google Play</a></div>
  </div>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js"></script>
  <script>
    new QRCode(document.getElementById("qr"), {{
      text: "{sub_url}", width: 160, height: 160,
      colorDark: "#0a0b0f", colorLight: "#ffffff", correctLevel: QRCode.CorrectLevel.M,
    }});
  </script>
</body>
</html>"""


SUB_PAGE_EXPIRED_TEMPLATE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{brand_name} — подписка</title>
<style>
  :root {{
    --bg: #0a0b0f; --card: #131519; --border: #1e2128;
    --text: #eceef2; --muted: #868c99; --accent: #7c6cf0; --red: #e5686b;
    --ease: cubic-bezier(0.16, 1, 0.3, 1);
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
    background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    -webkit-font-smoothing: antialiased; padding: 24px;
  }}
  .card {{
    max-width: 400px; width: 100%; background: var(--card); border: 1px solid var(--border);
    border-radius: 16px; padding: 32px 28px; text-align: center;
    opacity: 0; transform: translateY(10px); filter: blur(6px);
    animation: enter 0.6s var(--ease) forwards;
  }}
  @keyframes enter {{ to {{ opacity: 1; transform: translateY(0); filter: blur(0); }} }}
  .badge {{ font-size: 12px; color: var(--muted); letter-spacing: 0.06em; text-transform: uppercase; margin-bottom: 10px; }}
  h1 {{ font-size: 20px; margin: 0 0 6px; font-weight: 600; letter-spacing: -0.01em; color: var(--red); }}
  p.sub {{ color: var(--muted); font-size: 14px; margin: 0 0 26px; line-height: 1.5; }}
  .btn {{
    display: block; width: 100%; padding: 14px 18px; border-radius: 10px;
    background: var(--text); color: var(--bg); text-decoration: none;
    font-weight: 600; font-size: 15px; border: none; cursor: pointer;
    transition: transform 0.15s var(--ease), opacity 0.15s var(--ease);
  }}
  .btn:hover {{ opacity: 0.85; }}
  .btn:active {{ transform: scale(0.97); }}
  @media (prefers-reduced-motion: reduce) {{
    *, *::before, *::after {{ animation-duration: 0.01ms !important; transition-duration: 0.01ms !important; }}
    .card {{ opacity: 1 !important; transform: none !important; filter: none !important; }}
  }}
</style>
</head>
<body>
  <div class="card">
    <div class="badge">{brand_name}</div>
    <h1>Подписка истекла</h1>
    <p class="sub">Доступ по этой ссылке закончился. Продли подписку в боте — ссылка останется той же, ничего заново настраивать не нужно.</p>
    <a class="btn" href="https://t.me/{bot_username}" target="_blank">Продлить в боте</a>
  </div>
</body>
</html>"""


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/sub/{token}")
def get_subscription(token: str, request: Request):
    user = db.get_user_by_token(token)
    if not user:
        raise HTTPException(404, "not found")
    subs = db.list_active_subscriptions(tg_id=user["tg_id"])
    ua = request.headers.get("user-agent", "")
    if not _is_app_client(ua):
        brand_name = settings.get_brand_name()
        if not subs:
            _, bot_username = settings.bot_credentials()
            return HTMLResponse(SUB_PAGE_EXPIRED_TEMPLATE.format(bot_username=bot_username, brand_name=brand_name))
        sub_url = f"https://{SUB_DOMAIN}/sub/{token}"
        return HTMLResponse(SUB_PAGE_TEMPLATE.format(sub_url=sub_url, brand_name=brand_name))

    hwid_cfg = settings.get_hwid_settings()
    if hwid_cfg["enabled"]:
        hwid = request.headers.get("x-hwid", "")
        if not HWID_RE.match(hwid):
            raise HTTPException(404, "hwid required")
        limit = user["hwid_limit"] if user["hwid_limit"] is not None else hwid_cfg["fallback_limit"]
        _, allowed = db.add_device_if_under_limit(
            user["tg_id"], hwid, limit,
            request.headers.get("x-device-os"),
            request.headers.get("x-device-model"),
            ua,
        )
        if not allowed:
            raise HTTPException(404, "device limit reached", headers={"x-hwid-max-devices-reached": "true"})

    content = links.build_subscription_text(subs)
    return Response(content=content, media_type="text/plain")


@app.get("/api/cabinet/{token}")
def cabinet(token: str):
    user = db.get_user_by_token(token)
    if not user:
        raise HTTPException(404, "not found")
    subs = db.list_active_subscriptions(tg_id=user["tg_id"])
    plans_by_code = settings.get_plans_by_code()
    out = []
    for s in subs:
        plan = plans_by_code.get(s["plan"])
        out.append({
            "node": s["node"],
            "plan": s["plan"],
            "plan_label": plan["label"] if plan else s["plan"],
            "expires_at": s["expires_at"],
            "days_left": _days_left(s),
        })
    return {
        "username": user["username"],
        "subscriptions": out,
        "sub_link": f"https://{SUB_DOMAIN}/sub/{token}",
        "nodes_available": [n["label"] for n in db.list_nodes(enabled_only=True)],
    }



@app.get("/mgmt-pubkey.txt", response_class=PlainTextResponse)
def mgmt_pubkey():
    return nodeprov.ensure_mgmt_key() + "\n"


@app.get("/install/{token}.sh", response_class=PlainTextResponse)
def install_script(token: str):
    node = db.get_node_by_token(token)
    if not node:
        raise HTTPException(404, "unknown token")
    return nodeprov.render_install_script(node)


def _tg_send_message(tg_id: int, text: str):
    bot_token, _ = settings.bot_credentials()
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    data = json.dumps({"chat_id": tg_id, "text": text, "parse_mode": "HTML"}).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


def _grant_paid_subscription(payment_id: str):
    payment = db.get_payment(payment_id)
    if not payment or payment["status"] == "paid":
        return
    plan = settings.get_plans_by_code().get(payment["plan"])
    node = db.get_node(payment["node"])
    if not plan or not node:
        return
    payment = db.mark_payment_paid(payment_id)
    if not payment:
        return
    sub = db.create_subscription(payment["tg_id"], payment["node"], plan["days"], payment["plan"], source="payment")
    xray_manager.add_client_to_node(node, sub["uuid"], email=sub["uuid"])
    user = db.get_or_create_user(payment["tg_id"], None)
    _tg_send_message(
        payment["tg_id"],
        f"<b>Оплата получена</b>\n\n"
        f"Сервер: {node['label']}\n"
        f"Срок: {plan['label']} — до {sub['expires_at'][:10]}\n\n"
        f"Ссылка-подписка:\nhttps://{SUB_DOMAIN}/sub/{user['token']}",
    )
    webhooks.send("payment.paid", {
        "tg_id": payment["tg_id"],
        "amount": payment["amount"],
        "provider": payment["provider"],
        "node": payment["node"],
        "plan": payment["plan"],
        "subscription_uuid": sub["uuid"],
        "expires_at": sub["expires_at"],
    })


def _check_and_reconcile_payment(payment: dict) -> str:
    if not payment.get("external_id"):
        return payment["status"]
    try:
        status = payments.check_payment_status(payment["provider"], payment["external_id"])
    except Exception:
        return payment["status"]
    if status in payments.PAID_STATUSES:
        _grant_paid_subscription(payment["id"])
        return "paid"
    if status in payments.FAILED_STATUSES:
        db.mark_payment_failed(payment["id"])
        return "failed"
    return payment["status"]


@app.get("/admin/api/payments")
def admin_list_payments(request: Request):
    require_admin(request)
    nodes_by_code = {n["code"]: n for n in db.list_nodes()}
    plans_by_code = settings.get_plans_by_code()
    out = []
    for p in db.list_payments():
        node = nodes_by_code.get(p["node"])
        plan = plans_by_code.get(p["plan"])
        out.append({
            **p,
            "node_label": node["label"] if node else p["node"],
            "plan_label": plan["label"] if plan else p["plan"],
            "provider_label": payments.PROVIDER_NAMES.get(p["provider"], p["provider"]),
        })
    return out


@app.post("/admin/api/payments/{payment_id}/check")
def admin_check_payment(payment_id: str, request: Request):
    require_admin(request)
    payment = db.get_payment(payment_id)
    if not payment:
        raise HTTPException(404, "not found")
    if payment["status"] != "pending":
        return {"status": payment["status"]}
    status = _check_and_reconcile_payment(payment)
    return {"status": status}


@app.get("/admin/api/payments/legal-settings")
def admin_get_legal_settings(request: Request):
    require_admin(request)
    return legal.get_settings()


@app.post("/admin/api/payments/legal-settings")
def admin_set_legal_settings(request: Request, body: dict = Body(...)):
    require_admin(request)
    for key in ("LEGAL_NAME", "LEGAL_INN", "REFUND_HOURS", "SUPPORT_CONTACT", "SUPPORT_EMAIL"):
        if key in body:
            _update_env_var(key, str(body[key]).strip())
    if not legal.read_env_var("OFFER_EFFECTIVE_DATE"):
        _update_env_var("OFFER_EFFECTIVE_DATE", datetime.date.today().strftime("%d.%m.%Y"))
    return {"ok": True, "settings": legal.get_settings()}


@app.get("/admin/api/payments/yookassa-settings")
def admin_get_yookassa_settings(request: Request):
    require_admin(request)
    shop_id, secret_key = settings.yookassa_credentials()
    return {
        "enabled": settings.get_payment_settings()["yookassa_enabled"],
        "shop_id": shop_id,
        "has_secret": bool(secret_key),
    }


@app.post("/admin/api/payments/yookassa-settings")
def admin_set_yookassa_settings(request: Request, body: dict = Body(...)):
    require_admin(request)
    shop_id = (body.get("shop_id") or "").strip()
    secret_key = (body.get("secret_key") or "").strip()
    if not shop_id or not secret_key:
        raise HTTPException(400, "shop_id и secret_key обязательны")
    try:
        payments.validate_yookassa_credentials(shop_id, secret_key)
    except Exception:
        raise HTTPException(401, "ЮKassa не приняла эти ключи — проверь shop_id и секретный ключ")
    _update_env_var("YOOKASSA_SHOP_ID", shop_id)
    _update_env_var("YOOKASSA_SECRET_KEY", secret_key)
    _update_env_var("YOOKASSA_ENABLED", "true")
    _update_env_var("PAYMENTS_ENABLED", "true")
    restarted = False
    try:
        subprocess.run(["systemctl", "restart", "mbs-bot"], check=True, timeout=15)
        restarted = True
    except Exception:
        restarted = False
    return {"ok": True, "restarted_bot": restarted}


@app.get("/admin/api/payments/platega-settings")
def admin_get_platega_settings(request: Request):
    require_admin(request)
    merchant_id, secret = settings.platega_credentials()
    return {
        "enabled": settings.get_payment_settings()["platega_enabled"],
        "merchant_id": merchant_id,
        "has_secret": bool(secret),
    }


@app.post("/admin/api/payments/platega-settings")
def admin_set_platega_settings(request: Request, body: dict = Body(...)):
    require_admin(request)
    merchant_id = (body.get("merchant_id") or "").strip()
    secret = (body.get("secret") or "").strip()
    if not merchant_id or not secret:
        raise HTTPException(400, "merchant_id и secret обязательны")
    _update_env_var("PLATEGA_MERCHANT_ID", merchant_id)
    _update_env_var("PLATEGA_SECRET", secret)
    _update_env_var("PLATEGA_ENABLED", "true")
    _update_env_var("PAYMENTS_ENABLED", "true")
    restarted = False
    try:
        subprocess.run(["systemctl", "restart", "mbs-bot"], check=True, timeout=15)
        restarted = True
    except Exception:
        restarted = False
    return {"ok": True, "restarted_bot": restarted}


@app.get("/admin/api/payments/plan-settings")
def admin_get_plan_settings(request: Request):
    require_admin(request)
    return {
        "payments_enabled": settings.get_payment_settings()["payments_enabled"],
        "plans": settings.get_plans(),
    }


@app.post("/admin/api/payments/plan-settings")
def admin_set_plan_settings(request: Request, body: dict = Body(...)):
    require_admin(request)
    prices = body.get("prices") or {}
    known_codes = settings.PRICE_ENV_KEYS.keys()
    clean_prices = {}
    for code, value in prices.items():
        if code not in known_codes:
            continue
        try:
            price = int(value)
        except (TypeError, ValueError):
            raise HTTPException(400, f"цена для тарифа {code} должна быть целым числом")
        if price < 0:
            raise HTTPException(400, f"цена для тарифа {code} не может быть отрицательной")
        clean_prices[code] = price
    settings.set_plan_prices(clean_prices)
    if "payments_enabled" in body:
        _update_env_var("PAYMENTS_ENABLED", "true" if body["payments_enabled"] else "false")
    return {
        "payments_enabled": settings.get_payment_settings()["payments_enabled"],
        "plans": settings.get_plans(),
    }


@app.get("/admin/api/hwid-settings")
def admin_get_hwid_settings(request: Request):
    require_admin(request)
    return settings.get_hwid_settings()


@app.post("/admin/api/hwid-settings")
def admin_set_hwid_settings(request: Request, body: dict = Body(...)):
    require_admin(request)
    if "enabled" in body:
        _update_env_var("HWID_LIMIT_ENABLED", "true" if body["enabled"] else "false")
    if "fallback_limit" in body:
        try:
            limit = int(body["fallback_limit"])
        except (TypeError, ValueError):
            raise HTTPException(400, "лимит устройств должен быть целым числом")
        if not (1 <= limit <= 1000):
            raise HTTPException(400, "лимит устройств должен быть от 1 до 1000")
        _update_env_var("HWID_FALLBACK_LIMIT", str(limit))
    return settings.get_hwid_settings()


@app.get("/admin/api/webhook-settings")
def admin_get_webhook_settings(request: Request):
    require_admin(request)
    return {
        "url": legal.read_env_var("WEBHOOK_URL", ""),
        "secret": legal.read_env_var("WEBHOOK_SECRET", ""),
    }


@app.post("/admin/api/webhook-settings")
def admin_set_webhook_settings(request: Request, body: dict = Body(...)):
    require_admin(request)
    url = (body.get("url") or "").strip()
    if url and not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(400, "URL должен начинаться с http:// или https://")
    _update_env_var("WEBHOOK_URL", url)
    if url and not legal.read_env_var("WEBHOOK_SECRET", ""):
        _update_env_var("WEBHOOK_SECRET", secrets.token_hex(24))
    return {"url": legal.read_env_var("WEBHOOK_URL", ""), "secret": legal.read_env_var("WEBHOOK_SECRET", "")}


@app.post("/payments/webhook/yookassa")
async def yookassa_webhook(request: Request):
    body = await request.json()
    if not payments.verify_yookassa_notification(body):
        raise HTTPException(400, "unexpected event")
    obj = body.get("object", {})
    payment_id = (obj.get("metadata") or {}).get("payment_id")
    if not payment_id:
        raise HTTPException(400, "missing payment_id")
    payment = db.get_payment(payment_id)
    if not payment or not payment.get("external_id"):
        raise HTTPException(400, "unknown payment")
    status = await asyncio.to_thread(payments.check_yookassa_payment, payment["external_id"])
    if status not in payments.PAID_STATUSES:
        return {"ok": True}
    await asyncio.to_thread(_grant_paid_subscription, payment_id)
    return {"ok": True}


@app.post("/payments/webhook/platega")
async def platega_webhook(request: Request):
    raw = await request.body()
    signature = request.headers.get("x-signature") or request.headers.get("signature") or ""
    if not payments.verify_platega_signature(raw, signature):
        raise HTTPException(401, "bad signature")
    body = json.loads(raw)
    status = (body.get("status") or "").lower()
    payment_id = body.get("id") or body.get("paymentId")
    if status not in ("succeeded", "success", "paid") or not payment_id:
        return {"ok": True}
    await asyncio.to_thread(_grant_paid_subscription, payment_id)
    return {"ok": True}


@app.get("/pay/done", response_class=HTMLResponse)
def pay_done():
    return (
        "<!doctype html><html lang='ru'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>Оплата</title></head>"
        "<body style='background:#0a0b0f;color:#eceef2;font-family:sans-serif;"
        "display:flex;align-items:center;justify-content:center;min-height:100vh;text-align:center'>"
        "<div><h2>Спасибо!</h2><p>Возвращайся в Telegram — подписка придёт туда автоматически "
        "в течение минуты после подтверждения оплаты.</p></div></body></html>"
    )


@app.post("/nodes/register/{token}")
async def register_node(token: str, request: Request):
    node = db.get_node_by_token(token)
    if not node:
        raise HTTPException(404, "unknown token")
    try:
        body = await request.json()
    except Exception:
        body = {}
    if body.get("status") == "active":
        db.activate_node(node["code"])
    return {"ok": True}



def _client_ip(request: Request) -> str:
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "unknown")


@app.post("/admin/api/login")
def admin_login(request: Request, response: Response, body: dict = Body(...)):
    ip = _client_ip(request)
    if db.count_recent_login_attempts(ip, "password", minutes=15) >= 10:
        raise HTTPException(429, "too many attempts, try again later")
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    admin = db.verify_admin_login(username, password)
    if not admin:
        db.record_login_attempt(ip, "password")
        db.add_audit(username[:64] or None, "login.failed", "", ip)
        raise HTTPException(401, "wrong username or password")
    db.clear_login_attempts(ip, "password")
    if admin.get("totp_secret"):
        pending_token = db.create_pending_totp(admin["id"])
        return {"ok": True, "needs_totp": True, "pending_token": pending_token}
    db.add_audit(admin["username"], "login.ok", "", ip)
    token = db.create_admin_session(admin["id"])
    response.set_cookie(ADMIN_COOKIE, token, httponly=True, secure=True, samesite="strict", max_age=7 * 24 * 3600)
    return {"ok": True}


@app.post("/admin/api/login/totp")
def admin_login_totp(request: Request, response: Response, body: dict = Body(...)):
    ip = _client_ip(request)
    if db.count_recent_login_attempts(ip, "totp", minutes=5) >= 10:
        raise HTTPException(429, "too many attempts, try again later")
    pending_token = body.get("pending_token") or ""
    code = (body.get("code") or "").strip()
    pending = db.resolve_pending_totp(pending_token)
    if not pending:
        raise HTTPException(401, "login session expired, log in again")
    admin = db.get_admin_by_id(pending["admin_id"])
    if not admin or not admin.get("totp_secret") or not totp.verify(admin["totp_secret"], code):
        db.record_login_attempt(ip, "totp")
        db.add_audit(admin["username"] if admin else None, "login.totp_failed", "", ip)
        raise HTTPException(401, "wrong code")
    db.clear_login_attempts(ip, "totp")
    db.delete_pending_totp(pending_token)
    db.add_audit(admin["username"], "login.ok", "2fa", ip)
    token = db.create_admin_session(admin["id"])
    response.set_cookie(ADMIN_COOKIE, token, httponly=True, secure=True, samesite="strict", max_age=7 * 24 * 3600)
    return {"ok": True}


@app.post("/admin/api/logout")
def admin_logout(request: Request, response: Response):
    token = request.cookies.get(ADMIN_COOKIE)
    if token:
        db.delete_admin_session(token)
    response.delete_cookie(ADMIN_COOKIE)
    return {"ok": True}


@app.get("/admin/api/me")
def admin_me(request: Request):
    token = request.cookies.get(ADMIN_COOKIE)
    admin = db.get_session_admin(token)
    return {"authenticated": admin is not None, "username": admin["username"] if admin else None}


@app.get("/admin/api/admins")
def admin_list_admins(request: Request):
    require_admin(request)
    return db.list_admins()


@app.post("/admin/api/admins")
def admin_create_admin(request: Request, body: dict = Body(...)):
    require_admin(request)
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if len(username) < 3:
        raise HTTPException(400, "username too short")
    if len(password) < 8:
        raise HTTPException(400, "password too short")
    try:
        return db.create_admin(username, password)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/admin/api/admins/{admin_id}")
def admin_delete_admin(admin_id: int, request: Request):
    current = _require_current_admin(request)
    if current["id"] == admin_id:
        raise HTTPException(400, "cannot delete your own account while logged in as it")
    try:
        db.delete_admin(admin_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


def _require_current_admin(request: Request):
    token = request.cookies.get(ADMIN_COOKIE)
    current = db.get_session_admin(token)
    if not current:
        raise HTTPException(401, "unauthorized")
    return current


@app.get("/admin/api/2fa/status")
def admin_2fa_status(request: Request):
    current = _require_current_admin(request)
    admin = db.get_admin_by_id(current["id"])
    return {"enabled": bool(admin and admin.get("totp_secret"))}


@app.post("/admin/api/2fa/setup")
def admin_2fa_setup(request: Request):
    current = _require_current_admin(request)
    secret = totp.generate_secret()
    return {"secret": secret, "uri": totp.uri(secret, current["username"], issuer=settings.get_brand_name())}


@app.post("/admin/api/2fa/enable")
def admin_2fa_enable(request: Request, body: dict = Body(...)):
    current = _require_current_admin(request)
    secret = body.get("secret") or ""
    code = (body.get("code") or "").strip()
    if not secret or not totp.verify(secret, code):
        raise HTTPException(400, "wrong code")
    db.set_admin_totp_secret(current["id"], secret)
    return {"ok": True}


@app.post("/admin/api/2fa/disable")
def admin_2fa_disable(request: Request, body: dict = Body(...)):
    current = _require_current_admin(request)
    password = body.get("password") or ""
    if not db.verify_admin_password_by_id(current["id"], password):
        raise HTTPException(401, "wrong password")
    db.set_admin_totp_secret(current["id"], None)
    return {"ok": True}



@app.get("/admin/api/settings/bot")
def admin_get_bot_settings(request: Request):
    require_admin(request)
    bot_token, bot_username = settings.bot_credentials()
    masked = f"{bot_token[:8]}...{bot_token[-4:]}" if len(bot_token) > 14 else "***"
    return {"username": bot_username, "token_masked": masked}


@app.post("/admin/api/settings/bot")
def admin_set_bot_settings(request: Request, body: dict = Body(...)):
    require_admin(request)
    token = (body.get("token") or "").strip()
    if not token:
        raise HTTPException(400, "token required")
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getMe", timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception:
        raise HTTPException(400, "не получилось проверить токен — нет связи с Telegram")
    if not data.get("ok"):
        raise HTTPException(400, "Telegram отклонил этот токен")
    username = data["result"]["username"]
    _update_env_var("BOT_TOKEN", token)
    _update_env_var("BOT_USERNAME", username)
    try:
        subprocess.run(["systemctl", "restart", "mbs-bot"], check=True, timeout=15)
        restarted = True
    except Exception:
        restarted = False
    return {"ok": True, "username": username, "restarted": restarted}


@app.get("/admin/api/backup")
def admin_download_backup(request: Request):
    require_admin(request)
    data = backup.create_backup()
    filename = f"mbs-backup-{datetime.datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.tar.gz"
    return Response(
        content=data, media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/admin/api/backup/restore")
async def admin_restore_backup(request: Request, file: UploadFile = File(...)):
    require_admin(request)
    data = await file.read()
    try:
        result = backup.restore_backup(data)
    except backup.RestoreError as e:
        raise HTTPException(400, str(e))
    restarted_bot = False
    if result["restored_env"]:
        try:
            subprocess.run(["systemctl", "restart", "mbs-bot"], check=True, timeout=15)
            restarted_bot = True
        except Exception:
            restarted_bot = False
    return {"ok": True, **result, "restarted_bot": restarted_bot}


@app.get("/admin/api/stats")
def admin_stats(request: Request):
    require_admin(request)
    return db.stats()


def _fmt_bytes(n: int) -> str:
    v = float(n)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if v < 1024 or unit == "TB":
            return f"{v:.1f} {unit}" if unit != "B" else f"{int(v)} {unit}"
        v /= 1024
    return f"{v:.1f} TB"


@app.get("/admin/api/traffic")
def admin_traffic(request: Request):
    require_admin(request)
    all_stats = dict(xray_manager.query_stats())
    for node in db.list_nodes():
        if node["kind"] != "managed" or node["status"] != "active":
            continue
        try:
            remote = nodeprov.remote_query_stats(node)
        except Exception:
            remote = {}
        for k, v in remote.items():
            if k in all_stats:
                all_stats[k]["up"] += v["up"]
                all_stats[k]["down"] += v["down"]
            else:
                all_stats[k] = v

    total_up = sum(v["up"] for v in all_stats.values())
    total_down = sum(v["down"] for v in all_stats.values())

    subs = db.list_all_subscriptions(limit=5000)
    nodes_by_code = {n["code"]: n for n in db.list_nodes()}
    per_sub = []
    for s in subs:
        st = all_stats.get(s["uuid"])
        if not st:
            continue
        node = nodes_by_code.get(s["node"])
        per_sub.append({
            "uuid": s["uuid"],
            "username": ("@" + s["username"]) if s.get("username") else f"tg{s['tg_id']}",
            "node_label": node["label"] if node else s["node"],
            "up": st["up"], "down": st["down"],
            "up_fmt": _fmt_bytes(st["up"]), "down_fmt": _fmt_bytes(st["down"]),
            "total_fmt": _fmt_bytes(st["up"] + st["down"]),
        })
    per_sub.sort(key=lambda r: r["up"] + r["down"], reverse=True)

    return {
        "total_up": total_up, "total_down": total_down,
        "total_up_fmt": _fmt_bytes(total_up), "total_down_fmt": _fmt_bytes(total_down),
        "total_fmt": _fmt_bytes(total_up + total_down),
        "per_subscription": per_sub,
    }


@app.get("/admin/api/subscriptions")
def admin_subscriptions(request: Request, limit: int = 200):
    require_admin(request)
    subs = db.list_all_subscriptions(limit=limit)
    nodes_by_code = {n["code"]: n for n in db.list_nodes()}
    plans_by_code = settings.get_plans_by_code()
    out = []
    for s in subs:
        node = nodes_by_code.get(s["node"])
        plan = plans_by_code.get(s["plan"])
        out.append({
            **s,
            "node_label": node["label"] if node else s["node"],
            "plan_label": plan["label"] if plan else s["plan"],
            "days_left": _days_left(s),
        })
    return out


@app.post("/admin/api/subscriptions/{uuid}/revoke")
def admin_revoke_subscription(uuid: str, request: Request):
    require_admin(request)
    sub = db.get_subscription(uuid)
    if not sub:
        raise HTTPException(404, "not found")
    node = db.get_node(sub["node"])
    if node:
        xray_manager.remove_client_from_node(node, uuid)
    db.revoke_subscription(uuid)
    webhooks.send("subscription.revoked", {
        "tg_id": sub["tg_id"], "node": sub["node"], "plan": sub["plan"], "subscription_uuid": uuid,
    })
    return {"ok": True}


@app.post("/admin/api/subscriptions/{uuid}/hold")
def admin_hold_subscription(uuid: str, request: Request):
    require_admin(request)
    sub = db.get_subscription(uuid)
    if not sub:
        raise HTTPException(404, "not found")
    if not db.hold_subscription(uuid):
        raise HTTPException(400, "подписка уже на паузе, истекла или отозвана")
    node = db.get_node(sub["node"])
    if node:
        xray_manager.remove_client_from_node(node, uuid)
    webhooks.send("subscription.held", {
        "tg_id": sub["tg_id"], "node": sub["node"], "plan": sub["plan"], "subscription_uuid": uuid,
    })
    return {"ok": True}


@app.post("/admin/api/subscriptions/{uuid}/resume")
def admin_resume_subscription(uuid: str, request: Request):
    require_admin(request)
    resumed = db.resume_subscription(uuid)
    if not resumed:
        raise HTTPException(400, "подписка не на паузе")
    node = db.get_node(resumed["node"])
    if node:
        xray_manager.add_client_to_node(node, uuid, email=uuid)
    webhooks.send("subscription.resumed", {
        "tg_id": resumed["tg_id"], "node": resumed["node"], "plan": resumed["plan"],
        "subscription_uuid": uuid, "expires_at": resumed["expires_at"],
    })
    return {"ok": True, "expires_at": resumed["expires_at"]}


@app.post("/admin/api/subscriptions/{uuid}/reset-traffic")
def admin_reset_traffic(uuid: str, request: Request):
    require_admin(request)
    sub = db.get_subscription(uuid)
    if not sub:
        raise HTTPException(404, "not found")
    node = db.get_node(sub["node"])
    if not node:
        raise HTTPException(404, "node not found")
    ok = xray_manager.reset_stats_for_node(node, uuid)
    return {"ok": ok}


@app.get("/admin/api/users")
def admin_list_users(request: Request, q: str = "", limit: int = 200):
    require_admin(request)
    return db.list_users(q=q, limit=limit)


@app.get("/admin/api/users/{tg_id}")
def admin_user_card(tg_id: int, request: Request):
    require_admin(request)
    user = db.get_user(tg_id)
    if not user:
        return {
            "tg_id": tg_id, "username": None, "created_at": None, "token": None, "exists": False,
            "subscriptions": [], "devices": [], "hwid_limit": None,
            "hwid_fallback_limit": settings.get_hwid_settings()["fallback_limit"],
        }
    subs = db.list_subscriptions_for_user(tg_id)
    nodes_by_code = {n["code"]: n for n in db.list_nodes()}
    plans_by_code = settings.get_plans_by_code()
    out_subs = []
    for s in subs:
        node = nodes_by_code.get(s["node"])
        plan = plans_by_code.get(s["plan"])
        out_subs.append({
            **s,
            "node_label": node["label"] if node else s["node"],
            "plan_label": plan["label"] if plan else s["plan"],
            "days_left": _days_left(s),
        })
    return {
        "tg_id": user["tg_id"],
        "username": user["username"],
        "created_at": user["created_at"],
        "token": user["token"],
        "exists": True,
        "subscriptions": out_subs,
        "devices": db.list_devices(tg_id),
        "hwid_limit": user.get("hwid_limit"),
        "hwid_fallback_limit": settings.get_hwid_settings()["fallback_limit"],
    }


@app.post("/admin/api/users/{tg_id}/grant")
def admin_grant_subscription(tg_id: int, request: Request, body: dict = Body(...)):
    require_admin(request)
    node_code = body.get("node")
    plan_code = body.get("plan")
    node = db.get_node(node_code)
    plan = settings.get_plans_by_code().get(plan_code)
    if not node or not plan:
        raise HTTPException(400, "unknown node or plan")
    db.get_or_create_user(tg_id, None)
    sub = db.create_subscription(tg_id, node_code, plan["days"], plan_code, source="admin")
    xray_manager.add_client_to_node(node, sub["uuid"], email=sub["uuid"])
    webhooks.send("subscription.granted_by_admin", {
        "tg_id": tg_id, "node": node_code, "plan": plan_code,
        "subscription_uuid": sub["uuid"], "expires_at": sub["expires_at"],
    })
    return sub


@app.get("/admin/api/users/{tg_id}/devices")
def admin_list_devices(tg_id: int, request: Request):
    require_admin(request)
    return {
        "devices": db.list_devices(tg_id),
        "limit": db.get_or_create_user(tg_id, None).get("hwid_limit"),
        "fallback_limit": settings.get_hwid_settings()["fallback_limit"],
    }


@app.delete("/admin/api/users/{tg_id}/devices/{device_id}")
def admin_delete_device(tg_id: int, device_id: int, request: Request):
    require_admin(request)
    db.delete_device(device_id)
    return {"ok": True}


@app.post("/admin/api/users/{tg_id}/hwid-limit")
def admin_set_hwid_limit(tg_id: int, request: Request, body: dict = Body(...)):
    require_admin(request)
    raw_limit = body.get("limit")
    if raw_limit in (None, ""):
        db.set_user_hwid_limit(tg_id, None)
        return {"ok": True}
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        raise HTTPException(400, "лимит должен быть целым числом")
    if not (0 <= limit <= 1000):
        raise HTTPException(400, "лимит должен быть от 0 до 1000")
    db.set_user_hwid_limit(tg_id, limit)
    return {"ok": True}



@app.get("/admin/api/gift-codes")
def admin_gift_codes(request: Request):
    require_admin(request)
    codes = db.list_gift_codes()
    nodes_by_code = {n["code"]: n for n in db.list_nodes()}
    plans_by_code = settings.get_plans_by_code()
    _, bot_username = settings.bot_credentials()
    out = []
    for c in codes:
        node = nodes_by_code.get(c["node"])
        plan = plans_by_code.get(c["plan"])
        out.append({
            **c,
            "node_label": node["label"] if node else c["node"],
            "plan_label": plan["label"] if plan else c["plan"],
            "link": f"https://t.me/{bot_username}?start=gift_{c['code']}",
        })
    return out


@app.post("/admin/api/gift-codes")
def admin_create_gift_code(request: Request, body: dict = Body(...)):
    require_admin(request)
    node, plan = body.get("node"), body.get("plan")
    if node not in {n["code"] for n in db.list_nodes()} or plan not in settings.get_plans_by_code():
        raise HTTPException(400, "invalid node/plan")
    code = db.create_gift_code(node, plan, created_by=0)
    _, bot_username = settings.bot_credentials()
    return {"code": code, "link": f"https://t.me/{bot_username}?start=gift_{code}"}


@app.get("/admin/api/plans")
def admin_plans(request: Request):
    require_admin(request)
    return settings.get_plans()



@app.get("/admin/api/nodes")
def admin_nodes(request: Request):
    require_admin(request)
    nodes = db.list_nodes()
    for n in nodes:
        n.pop("private_key", None)
        n.pop("provision_token", None)
    return nodes


@app.post("/admin/api/nodes/reorder")
def admin_reorder_nodes(request: Request, body: dict = Body(...)):
    require_admin(request)
    codes = body.get("codes")
    if not isinstance(codes, list) or not codes:
        raise HTTPException(400, "codes must be a non-empty list")
    try:
        db.reorder_nodes(codes)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/admin/api/nodes")
def admin_create_node(request: Request, body: dict = Body(...)):
    require_admin(request)
    code = str(body.get("code", "")).strip()
    if not NODE_CODE_RE.match(code):
        raise HTTPException(400, "code: только буквы/цифры/-/_, от 1 до 32 символов")
    label = str(body.get("label", "")).strip()
    if not label:
        raise HTTPException(400, "label не может быть пустым")
    try:
        port = int(body.get("port", 443))
    except (TypeError, ValueError):
        raise HTTPException(400, "port должен быть числом")
    if not (1 <= port <= 65535):
        raise HTTPException(400, "port должен быть от 1 до 65535")
    try:
        node = db.create_node(
            code=code, label=label, kind=body.get("kind", "external"),
            address=body["address"], port=port,
            public_key=body["public_key"], short_id=body["short_id"],
            sni=body["sni"], flow=body.get("flow", "xtls-rprx-vision"),
            shared_uuid=body.get("shared_uuid"),
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    webhooks.send("node.added", {"code": node["code"], "label": node["label"], "kind": node["kind"]})
    return node


@app.patch("/admin/api/nodes/{code}")
def admin_update_node(code: str, request: Request, body: dict = Body(...)):
    require_admin(request)
    editable = {"label", "enabled", "address", "port", "sni", "public_key", "short_id", "flow", "shared_uuid"}
    if code == "de1":
        editable = {"label"}
    allowed = {k: v for k, v in body.items() if k in editable}
    before = db.get_node(code)
    updated = db.update_node(code, **allowed)
    if before and updated and "enabled" in allowed and bool(before["enabled"]) != bool(updated["enabled"]):
        webhooks.send("node.enabled" if updated["enabled"] else "node.disabled", {
            "code": code, "label": updated["label"],
        })
    return updated


@app.delete("/admin/api/nodes/{code}")
def admin_delete_node(code: str, request: Request):
    require_admin(request)
    node = db.get_node(code)
    try:
        db.delete_node(code)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if node:
        webhooks.send("node.deleted", {"code": code, "label": node["label"]})
    return {"ok": True}


@app.post("/admin/api/nodes/{code}/check")
def admin_check_node(code: str, request: Request):
    require_admin(request)
    node = db.get_node(code)
    if not node:
        raise HTTPException(404, "not found")
    alive = nodeprov.check_node_alive(node["address"], node["port"])
    return {"alive": alive}


def _fmt_uptime(seconds):
    if seconds is None:
        return "—"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}д {hours}ч"
    if hours:
        return f"{hours}ч {minutes}м"
    return f"{minutes}м"


@app.get("/admin/api/nodes/{code}/metrics")
def admin_node_metrics(code: str, request: Request):
    require_admin(request)
    node = db.get_node(code)
    if not node:
        raise HTTPException(404, "not found")
    status = xray_manager.local_node_status() if node["kind"] == "local" else nodeprov.remote_node_status(node)
    if status.get("ok") and status.get("mem_total_mb"):
        status["mem_fmt"] = f"{status['mem_used_mb']} / {status['mem_total_mb']} MB"
    else:
        status["mem_fmt"] = "—"
    status["uptime_fmt"] = _fmt_uptime(status.get("uptime_s"))
    return status


@app.post("/admin/api/nodes/provision-guide")
def admin_provision_guide(request: Request, body: dict = Body(...)):
    require_admin(request)
    label = body["label"]
    address = body["address"]
    try:
        port = int(body.get("port", 443))
    except (TypeError, ValueError):
        raise HTTPException(400, "port должен быть числом")
    if not (1 <= port <= 65535):
        raise HTTPException(400, "port должен быть от 1 до 65535")
    sni = body.get("sni") or "www.wildberries.ru"
    include_ws = bool(body.get("include_ws"))
    include_hysteria2 = bool(body.get("include_hysteria2"))

    private_key, public_key = nodeprov.generate_reality_keys()
    transports = nodeprov.build_transports(address, port, sni, public_key, include_ws=include_ws)
    short_id = transports[0]["short_id"]

    hysteria_port = hysteria_password = hysteria_obfs_password = None
    if include_hysteria2:
        try:
            hysteria_port = int(body.get("hysteria_port", 443))
        except (TypeError, ValueError):
            raise HTTPException(400, "hysteria_port должен быть числом")
        if not (1 <= hysteria_port <= 65535):
            raise HTTPException(400, "hysteria_port должен быть от 1 до 65535")
        hysteria_password, hysteria_obfs_password = nodeprov.generate_hysteria_credentials()

    node, token = db.create_pending_node(
        label, address, port, sni, private_key, public_key, short_id,
        transports_json=json.dumps(transports),
        hysteria_port=hysteria_port, hysteria_password=hysteria_password,
        hysteria_obfs_password=hysteria_obfs_password,
    )
    return {"code": node["code"], "command": nodeprov.one_command(token)}


@app.get("/admin/api/nodes/{code}/status")
def admin_node_status(code: str, request: Request):
    require_admin(request)
    node = db.get_node(code)
    if not node:
        raise HTTPException(404, "not found")
    return {"status": node["status"], "enabled": bool(node["enabled"])}


def _panel_latency(node: dict) -> dict:
    samples = chains.tcp_connect_ms(node["address"], node["port"], samples=2, timeout=2.0)
    return {"code": node["code"], "ms": chains.median_ms(samples)}


@app.get("/admin/api/nodes/latency")
def admin_nodes_latency(request: Request):
    require_admin(request)
    targets = [n for n in db.list_nodes() if n["address"] and n["status"] == "active" and n["enabled"]]
    if not targets:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(targets))) as pool:
        return list(pool.map(_panel_latency, targets))


def _chain_view(chain: dict, nodes_by_code: dict) -> dict:
    entry = nodes_by_code.get(chain["entry_node"])
    exit_node = nodes_by_code.get(chain["exit_node"])
    return {
        "code": chain["code"], "label": chain["label"],
        "entry_node": chain["entry_node"], "exit_node": chain["exit_node"],
        "entry_label": entry["label"] if entry else chain["entry_node"],
        "exit_label": exit_node["label"] if exit_node else chain["exit_node"],
        "entry_address": entry["address"] if entry else None,
        "port": chain["port"], "enabled": bool(chain["enabled"]),
        "created_at": chain["created_at"],
    }


def _csv_safe(value) -> str:
    text = str(value)
    if text and text[0] in "=+-@\t\r":
        return "'" + text
    return text


def _sync_chain_nodes(chain: dict) -> dict:
    results = {}
    for code in dict.fromkeys([chain["entry_node"], chain["exit_node"]]):
        node = db.get_node(code)
        if not node:
            continue
        try:
            res = xray_manager.sync_node(node)
            results[code] = {"ok": not res["problems"], "problems": res["problems"]}
        except Exception as e:
            results[code] = {"ok": False, "problems": [str(e)]}
    return results


def _chain_failures(results: dict) -> list:
    failures = []
    for code, res in results.items():
        for problem in res["problems"]:
            failures.append(f"{code}: {problem}")
    return failures


@app.get("/admin/api/chains")
def admin_list_chains(request: Request):
    require_admin(request)
    nodes_by_code = {n["code"]: n for n in db.list_nodes()}
    return [_chain_view(c, nodes_by_code) for c in db.list_chains()]


@app.post("/admin/api/chains")
def admin_create_chain(request: Request, body: dict = Body(...)):
    require_admin(request)
    entry_code = str(body.get("entry", "")).strip()
    exit_code = str(body.get("exit", "")).strip()
    entry = db.get_node(entry_code)
    exit_node = db.get_node(exit_code)
    if not entry or not exit_node:
        raise HTTPException(400, "выбери входной и выходной серверы из списка нод")
    if entry_code == exit_code:
        raise HTTPException(400, "вход и выход цепочки должны быть разными серверами")
    if entry["kind"] not in chains.CHAIN_KINDS_ENTRY:
        raise HTTPException(400, "входной сервер должен быть под управлением панели (локальный или управляемый)")
    if exit_node["kind"] not in chains.CHAIN_KINDS_EXIT:
        raise HTTPException(400, "этот тип ноды нельзя использовать как выход цепочки")
    for node in (entry, exit_node):
        if not node["enabled"] or node["status"] != "active":
            raise HTTPException(400, f"нода «{node['label']}» выключена или ещё не установлена")
    relay_uuid = None
    if exit_node["kind"] == "external":
        if not exit_node.get("shared_uuid"):
            raise HTTPException(400, "у внешней ноды не задан shared UUID — через неё цепочку не построить")
    else:
        relay_uuid = str(uuidlib.uuid4())
    label = str(body.get("label", "")).strip()[:80] or f"{entry['label']} → {exit_node['label']}"
    try:
        chain = db.create_chain(label, entry_code, exit_code, relay_uuid)
    except ValueError as e:
        raise HTTPException(400, str(e))
    results = _sync_chain_nodes(chain)
    failures = _chain_failures(results)
    if failures:
        db.delete_chain(chain["code"])
        _sync_chain_nodes(chain)
        raise HTTPException(502, "цепочка не применилась, всё откатили назад: " + "; ".join(failures))
    webhooks.send("chain.created", {
        "code": chain["code"], "label": chain["label"], "entry": entry_code, "exit": exit_code,
    })
    nodes_by_code = {n["code"]: n for n in db.list_nodes()}
    return _chain_view(chain, nodes_by_code)


@app.patch("/admin/api/chains/{code}")
def admin_update_chain(code: str, request: Request, body: dict = Body(...)):
    require_admin(request)
    chain = db.get_chain(code)
    if not chain:
        raise HTTPException(404, "not found")
    fields = {}
    if "label" in body:
        label = str(body["label"]).strip()[:80]
        if not label:
            raise HTTPException(400, "название не может быть пустым")
        fields["label"] = label
    toggled = "enabled" in body and bool(body["enabled"]) != bool(chain["enabled"])
    if "enabled" in body:
        fields["enabled"] = 1 if body["enabled"] else 0
    updated = db.update_chain(code, **fields)
    if toggled:
        failures = _chain_failures(_sync_chain_nodes(updated))
        if failures:
            db.update_chain(code, enabled=chain["enabled"])
            _sync_chain_nodes(chain)
            raise HTTPException(502, "не получилось применить: " + "; ".join(failures))
        webhooks.send("chain.enabled" if updated["enabled"] else "chain.disabled", {
            "code": code, "label": updated["label"],
        })
    nodes_by_code = {n["code"]: n for n in db.list_nodes()}
    return _chain_view(db.get_chain(code), nodes_by_code)


@app.delete("/admin/api/chains/{code}")
def admin_delete_chain(code: str, request: Request):
    require_admin(request)
    chain = db.get_chain(code)
    if not chain:
        raise HTTPException(404, "not found")
    db.delete_chain(code)
    results = _sync_chain_nodes(chain)
    webhooks.send("chain.deleted", {"code": code, "label": chain["label"]})
    return {"ok": True, "warnings": _chain_failures(results)}


def _hop_probe(entry: dict, exit_node: dict) -> dict:
    try:
        samples = xray_manager.probe_from_node(entry, exit_node["address"], exit_node["port"])
    except Exception as e:
        return {"rtt_ms": None, "samples": [], "level": "unknown", "error": str(e)}
    rtt = chains.median_ms(samples)
    return {"rtt_ms": rtt, "samples": samples, "level": chains.latency_level(rtt)}


@app.get("/admin/api/chains/probe")
def admin_probe_chain(entry: str, exit: str, request: Request):
    require_admin(request)
    entry_node = db.get_node(entry)
    exit_node = db.get_node(exit)
    if not entry_node or not exit_node or entry == exit:
        raise HTTPException(400, "нужны две разные ноды")
    return _hop_probe(entry_node, exit_node)


@app.post("/admin/api/chains/{code}/check")
def admin_check_chain(code: str, request: Request):
    require_admin(request)
    chain = db.get_chain(code)
    if not chain:
        raise HTTPException(404, "not found")
    entry = db.get_node(chain["entry_node"])
    exit_node = db.get_node(chain["exit_node"])
    if not entry or not exit_node:
        raise HTTPException(400, "одна из нод цепочки удалена")
    entry_alive = nodeprov.check_node_alive(entry["address"], chain["port"])
    hop = _hop_probe(entry, exit_node)
    return {"entry_alive": entry_alive, **hop}


@app.get("/admin/api/audit")
def admin_audit(request: Request, limit: int = 100):
    require_admin(request)
    return db.list_audit(limit=max(1, min(limit, 500)))


@app.get("/admin/api/subscriptions/export.csv")
def admin_export_subscriptions(request: Request):
    require_admin(request)
    nodes_by_code = {n["code"]: n for n in db.list_nodes()}
    plans_by_code = settings.get_plans_by_code()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["tg_id", "username", "node", "plan", "created_at", "expires_at", "days_left", "status", "uuid"])
    for s in db.list_all_subscriptions(limit=100000):
        node = nodes_by_code.get(s["node"])
        plan = plans_by_code.get(s["plan"])
        if s.get("held_at"):
            status = "held"
        elif not s["active"] or s["expires_at"] <= db.now_iso():
            status = "expired"
        else:
            status = "active"
        writer.writerow([_csv_safe(v) for v in [
            s["tg_id"], s.get("username") or "", node["label"] if node else s["node"],
            plan["label"] if plan else s["plan"], s["created_at"], s["expires_at"],
            _days_left(s), status, s["uuid"],
        ]])
    return Response(
        content="﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="subscriptions.csv"'},
    )



ADMIN_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "admin.html")


_NO_CACHE = {"Cache-Control": "no-cache, must-revalidate"}


@app.get("/", response_class=HTMLResponse)
def root(request: Request):
    host = request.headers.get("host", "").split(":")[0]
    if host == PANEL_DOMAIN and ADMIN_PATH == "admin":
        return FileResponse(ADMIN_HTML_PATH, headers=_NO_CACHE)
    return legal.render_site_page("index.html")


@app.get(f"/{ADMIN_PATH}")
def admin_page():
    return FileResponse(ADMIN_HTML_PATH, headers=_NO_CACHE)


@app.get("/cabinet.html", response_class=HTMLResponse)
def cabinet_page():
    return legal.render_site_page("cabinet.html")


@app.get("/offer", response_class=HTMLResponse)
def offer_page():
    return legal.render("offer.html")


@app.get("/privacy", response_class=HTMLResponse)
def privacy_page():
    return legal.render("privacy.html")


@app.get("/api/plans")
def public_plans():
    return {
        "payments_enabled": settings.get_payment_settings()["payments_enabled"],
        "plans": settings.get_plans(),
    }


@app.get("/api/branding")
def public_branding():
    return {"brand_name": settings.get_brand_name()}


@app.post("/admin/api/branding")
def admin_set_branding(request: Request, body: dict = Body(...)):
    require_admin(request)
    brand_name = (body.get("brand_name") or "").strip()
    if not brand_name:
        raise HTTPException(400, "название не может быть пустым")
    if len(brand_name) > 60:
        raise HTTPException(400, "слишком длинное название")
    _update_env_var("BRAND_NAME", brand_name)
    return {"brand_name": settings.get_brand_name()}
