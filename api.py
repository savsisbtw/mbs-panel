import asyncio
import datetime
import json
import os
import re
import secrets
import subprocess
import urllib.request
from fastapi import FastAPI, HTTPException, Request, Response, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi import Body
from fastapi.responses import HTMLResponse, PlainTextResponse, FileResponse

import backup
import db
import legal
import links
import nodeprov
import payments
import totp
import webhooks
import xray_manager
from config import (
    PLANS, PLANS_BY_CODE, SITE_DOMAIN, SUB_DOMAIN, PANEL_DOMAIN,
    BOT_USERNAME, BOT_TOKEN, BASE_DIR,
    HWID_LIMIT_ENABLED, HWID_FALLBACK_LIMIT,
)

HWID_RE = re.compile(r"^[a-zA-Z0-9=-]{10,64}$")
ENV_PATH = os.path.join(BASE_DIR, ".env")

db.init_db()


def _update_env_var(key: str, value: str):
    lines = []
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, encoding="utf-8") as f:
            lines = f.readlines()
    found = False
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[i] = f"{key}={value}\n"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}\n")
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.writelines(lines)

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


def _days_left(expires_at: str) -> int:
    exp = datetime.datetime.fromisoformat(expires_at)
    delta = exp - datetime.datetime.utcnow()
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
<title>MBS Panel — подписка</title>
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
    <div class="badge">MBS Panel</div>
    <h1>Подписка готова</h1>
    <p class="sub">Нажми кнопку — сервер добавится в Happ автоматически, или отсканируй QR другим устройством</p>
    <a class="btn" href="happ://add/{sub_url}">Добавить в Happ</a>
    <a class="btn secondary" href="{sub_url}">Открыть ссылку подписки</a>
    <div class="qr-box" id="qr"></div>
    <div class="link-box">{sub_url}</div>
    <div class="thanks">Спасибо, что пользуетесь MBS Panel.<br>Нет Happ? Скачай: <a href="https://apps.apple.com/us/app/happ-proxy-utility/id6504287215" target="_blank">App Store</a> · <a href="https://play.google.com/store/apps/details?id=com.happproxy" target="_blank">Google Play</a></div>
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
<title>MBS Panel — подписка</title>
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
    <div class="badge">MBS Panel</div>
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
        if not subs:
            return HTMLResponse(SUB_PAGE_EXPIRED_TEMPLATE.format(bot_username=BOT_USERNAME))
        sub_url = f"https://{SUB_DOMAIN}/sub/{token}"
        return HTMLResponse(SUB_PAGE_TEMPLATE.format(sub_url=sub_url))

    if HWID_LIMIT_ENABLED:
        hwid = request.headers.get("x-hwid", "")
        if not HWID_RE.match(hwid):
            raise HTTPException(404, "hwid required")
        limit = user["hwid_limit"] if user["hwid_limit"] is not None else HWID_FALLBACK_LIMIT
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
    out = []
    for s in subs:
        plan = PLANS_BY_CODE.get(s["plan"])
        out.append({
            "node": s["node"],
            "plan": s["plan"],
            "plan_label": plan["label"] if plan else s["plan"],
            "expires_at": s["expires_at"],
            "days_left": _days_left(s["expires_at"]),
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
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
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
    plan = PLANS_BY_CODE.get(payment["plan"])
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
    out = []
    for p in db.list_payments():
        node = nodes_by_code.get(p["node"])
        plan = PLANS_BY_CODE.get(p["plan"])
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
    return {
        "enabled": payments.YOOKASSA_ENABLED,
        "shop_id": legal.read_env_var("YOOKASSA_SHOP_ID", ""),
        "has_secret": bool(legal.read_env_var("YOOKASSA_SECRET_KEY", "")),
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
    return {
        "enabled": payments.PLATEGA_ENABLED,
        "merchant_id": legal.read_env_var("PLATEGA_MERCHANT_ID", ""),
        "has_secret": bool(legal.read_env_var("PLATEGA_SECRET", "")),
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
        raise HTTPException(401, "wrong username or password")
    db.clear_login_attempts(ip, "password")
    if admin.get("totp_secret"):
        pending_token = db.create_pending_totp(admin["id"])
        return {"ok": True, "needs_totp": True, "pending_token": pending_token}
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
        raise HTTPException(401, "wrong code")
    db.clear_login_attempts(ip, "totp")
    db.delete_pending_totp(pending_token)
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
    return {"secret": secret, "uri": totp.uri(secret, current["username"])}


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
    masked = f"{BOT_TOKEN[:8]}...{BOT_TOKEN[-4:]}" if len(BOT_TOKEN) > 14 else "***"
    return {"username": BOT_USERNAME, "token_masked": masked}


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
    out = []
    for s in subs:
        node = nodes_by_code.get(s["node"])
        plan = PLANS_BY_CODE.get(s["plan"])
        out.append({
            **s,
            "node_label": node["label"] if node else s["node"],
            "plan_label": plan["label"] if plan else s["plan"],
            "days_left": _days_left(s["expires_at"]),
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
    return {"ok": True}


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


@app.get("/admin/api/users/{tg_id}")
def admin_user_card(tg_id: int, request: Request):
    require_admin(request)
    user = db.get_user(tg_id)
    if not user:
        raise HTTPException(404, "not found")
    subs = db.list_subscriptions_for_user(tg_id)
    nodes_by_code = {n["code"]: n for n in db.list_nodes()}
    out_subs = []
    for s in subs:
        node = nodes_by_code.get(s["node"])
        plan = PLANS_BY_CODE.get(s["plan"])
        out_subs.append({
            **s,
            "node_label": node["label"] if node else s["node"],
            "plan_label": plan["label"] if plan else s["plan"],
            "days_left": _days_left(s["expires_at"]),
        })
    return {
        "tg_id": user["tg_id"],
        "username": user["username"],
        "created_at": user["created_at"],
        "token": user["token"],
        "subscriptions": out_subs,
        "devices": db.list_devices(tg_id),
        "hwid_limit": user.get("hwid_limit"),
        "hwid_fallback_limit": HWID_FALLBACK_LIMIT,
    }


@app.post("/admin/api/users/{tg_id}/grant")
def admin_grant_subscription(tg_id: int, request: Request, body: dict = Body(...)):
    require_admin(request)
    node_code = body.get("node")
    plan_code = body.get("plan")
    node = db.get_node(node_code)
    plan = PLANS_BY_CODE.get(plan_code)
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
        "fallback_limit": HWID_FALLBACK_LIMIT,
    }


@app.delete("/admin/api/users/{tg_id}/devices/{device_id}")
def admin_delete_device(tg_id: int, device_id: int, request: Request):
    require_admin(request)
    db.delete_device(device_id)
    return {"ok": True}


@app.post("/admin/api/users/{tg_id}/hwid-limit")
def admin_set_hwid_limit(tg_id: int, request: Request, body: dict = Body(...)):
    require_admin(request)
    limit = body.get("limit")
    db.set_user_hwid_limit(tg_id, int(limit) if limit else None)
    return {"ok": True}



@app.get("/admin/api/gift-codes")
def admin_gift_codes(request: Request):
    require_admin(request)
    codes = db.list_gift_codes()
    nodes_by_code = {n["code"]: n for n in db.list_nodes()}
    out = []
    for c in codes:
        node = nodes_by_code.get(c["node"])
        plan = PLANS_BY_CODE.get(c["plan"])
        out.append({
            **c,
            "node_label": node["label"] if node else c["node"],
            "plan_label": plan["label"] if plan else c["plan"],
            "link": f"https://t.me/{BOT_USERNAME}?start=gift_{c['code']}",
        })
    return out


@app.post("/admin/api/gift-codes")
def admin_create_gift_code(request: Request, body: dict = Body(...)):
    require_admin(request)
    node, plan = body.get("node"), body.get("plan")
    if node not in {n["code"] for n in db.list_nodes()} or plan not in PLANS_BY_CODE:
        raise HTTPException(400, "invalid node/plan")
    code = db.create_gift_code(node, plan, created_by=0)
    return {"code": code, "link": f"https://t.me/{BOT_USERNAME}?start=gift_{code}"}


@app.get("/admin/api/plans")
def admin_plans(request: Request):
    require_admin(request)
    return PLANS



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
    node = db.create_node(
        code=body["code"], label=body["label"], kind=body.get("kind", "external"),
        address=body["address"], port=int(body.get("port", 443)),
        public_key=body["public_key"], short_id=body["short_id"],
        sni=body["sni"], flow=body.get("flow", "xtls-rprx-vision"),
        shared_uuid=body.get("shared_uuid"),
    )
    return node


@app.patch("/admin/api/nodes/{code}")
def admin_update_node(code: str, request: Request, body: dict = Body(...)):
    require_admin(request)
    editable = {"label", "enabled", "address", "port", "sni", "public_key", "short_id", "flow", "shared_uuid"}
    if code == "de1":
        editable = {"label"}
    allowed = {k: v for k, v in body.items() if k in editable}
    return db.update_node(code, **allowed)


@app.delete("/admin/api/nodes/{code}")
def admin_delete_node(code: str, request: Request):
    require_admin(request)
    try:
        db.delete_node(code)
    except ValueError as e:
        raise HTTPException(400, str(e))
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
    port = int(body.get("port", 443))
    sni = body.get("sni") or "www.wildberries.ru"
    include_ws = bool(body.get("include_ws"))
    include_hysteria2 = bool(body.get("include_hysteria2"))

    private_key, public_key = nodeprov.generate_reality_keys()
    transports = nodeprov.build_transports(address, port, sni, public_key, include_ws=include_ws)
    short_id = transports[0]["short_id"]

    hysteria_port = hysteria_password = hysteria_obfs_password = None
    if include_hysteria2:
        hysteria_port = int(body.get("hysteria_port", 443))
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



ADMIN_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "admin.html")


_NO_CACHE = {"Cache-Control": "no-cache, must-revalidate"}


@app.get("/")
def root(request: Request):
    if request.headers.get("host", "").split(":")[0] == PANEL_DOMAIN:
        return FileResponse(ADMIN_HTML_PATH, headers=_NO_CACHE)
    raise HTTPException(404)


@app.get("/admin")
def admin_page():
    return FileResponse(ADMIN_HTML_PATH, headers=_NO_CACHE)


@app.get("/offer", response_class=HTMLResponse)
def offer_page():
    return legal.render("offer.html")


@app.get("/privacy", response_class=HTMLResponse)
def privacy_page():
    return legal.render("privacy.html")
