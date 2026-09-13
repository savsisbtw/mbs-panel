import base64
import hashlib
import hmac
import json
import secrets
import urllib.request

from config import PANEL_DOMAIN
import settings

PROVIDER_NAMES = {"yookassa": "ЮKassa", "platega": "Platega"}


def available_providers() -> list[str]:
    enabled = settings.get_payment_settings()
    providers = []
    if enabled["yookassa_enabled"]:
        providers.append("yookassa")
    if enabled["platega_enabled"]:
        providers.append("platega")
    return providers


def new_payment_id() -> str:
    return secrets.token_hex(16)


def _post_json(url: str, body: dict, headers: dict, timeout: int = 15) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _get_json(url: str, headers: dict, timeout: int = 15) -> dict:
    req = urllib.request.Request(url, method="GET", headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def create_yookassa_payment(payment_id: str, amount_rub: int, description: str) -> str:
    shop_id, secret_key = settings.yookassa_credentials()
    auth = base64.b64encode(f"{shop_id}:{secret_key}".encode()).decode()
    data = _post_json(
        "https://api.yookassa.ru/v3/payments",
        {
            "amount": {"value": f"{amount_rub}.00", "currency": "RUB"},
            "confirmation": {"type": "redirect", "return_url": f"https://{PANEL_DOMAIN}/pay/done"},
            "capture": True,
            "description": description,
            "metadata": {"payment_id": payment_id},
        },
        {
            "Content-Type": "application/json",
            "Authorization": f"Basic {auth}",
            "Idempotence-Key": payment_id,
        },
    )
    external_id = data["id"]
    pay_url = data["confirmation"]["confirmation_url"]
    return external_id, pay_url


def validate_yookassa_credentials(shop_id: str, secret_key: str) -> dict:
    auth = base64.b64encode(f"{shop_id}:{secret_key}".encode()).decode()
    return _get_json("https://api.yookassa.ru/v3/me", {"Authorization": f"Basic {auth}"})


def verify_yookassa_notification(body: dict) -> bool:
    return body.get("event") == "payment.succeeded" and "object" in body


def check_yookassa_payment(external_id: str) -> str:
    shop_id, secret_key = settings.yookassa_credentials()
    auth = base64.b64encode(f"{shop_id}:{secret_key}".encode()).decode()
    data = _get_json(
        f"https://api.yookassa.ru/v3/payments/{external_id}",
        {"Authorization": f"Basic {auth}"},
    )
    return data.get("status", "")


def create_platega_payment(payment_id: str, amount_rub: int, description: str) -> str:
    merchant_id, secret = settings.platega_credentials()
    data = _post_json(
        "https://app.platega.io/transaction/process",
        {
            "paymentMethod": 2,
            "id": payment_id,
            "paymentDetails": {"amount": amount_rub, "currency": "RUB"},
            "description": description,
            "return": f"https://{PANEL_DOMAIN}/pay/done",
        },
        {
            "Content-Type": "application/json",
            "X-MerchantId": merchant_id,
            "X-Secret": secret,
        },
    )
    external_id = data.get("id") or data.get("transactionId")
    pay_url = data.get("redirectUrl") or data.get("url")
    return external_id, pay_url


def verify_platega_signature(raw_body: bytes, signature: str) -> bool:
    if not signature:
        return False
    _, secret = settings.platega_credentials()
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def check_platega_payment(external_id: str) -> str:
    merchant_id, secret = settings.platega_credentials()
    data = _get_json(
        f"https://app.platega.io/transaction/{external_id}",
        {"X-MerchantId": merchant_id, "X-Secret": secret},
    )
    return data.get("status", "")


PAID_STATUSES = {"succeeded", "CONFIRMED"}
FAILED_STATUSES = {"canceled", "CANCELED", "CHARGEBACKED"}


def create_payment_link(provider: str, payment_id: str, amount_rub: int, description: str):
    if provider == "yookassa":
        return create_yookassa_payment(payment_id, amount_rub, description)
    if provider == "platega":
        return create_platega_payment(payment_id, amount_rub, description)
    raise ValueError(f"unknown provider: {provider}")


def check_payment_status(provider: str, external_id: str) -> str:
    if provider == "yookassa":
        return check_yookassa_payment(external_id)
    if provider == "platega":
        return check_platega_payment(external_id)
    raise ValueError(f"unknown provider: {provider}")
