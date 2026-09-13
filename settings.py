import config
import legal

PRICE_ENV_KEYS = {"7d": "PRICE_7D", "1m": "PRICE_1M", "3m": "PRICE_3M", "6m": "PRICE_6M", "1y": "PRICE_1Y"}


def _bool(raw: str, default: bool) -> bool:
    if raw is None or raw == "":
        return default
    return raw.strip().lower() == "true"


def _positive_int(raw: str, default: int) -> int:
    if raw and raw.strip().lstrip("-").isdigit():
        parsed = int(raw)
        if parsed >= 0:
            return parsed
    return default


def get_plans() -> list:
    raw = legal.read_env_vars(list(PRICE_ENV_KEYS.values()))
    plans = []
    for p in config.PLANS:
        env_key = PRICE_ENV_KEYS[p["code"]]
        plans.append({
            "code": p["code"],
            "label": p["label"],
            "days": p["days"],
            "price": _positive_int(raw.get(env_key), p["price"]),
        })
    return plans


def get_plans_by_code() -> dict:
    return {p["code"]: p for p in get_plans()}


def set_plan_prices(prices: dict):
    for code, price in prices.items():
        if code in PRICE_ENV_KEYS:
            legal.update_env_var(PRICE_ENV_KEYS[code], str(int(price)))


def get_payment_settings() -> dict:
    raw = legal.read_env_vars(["PAYMENTS_ENABLED", "YOOKASSA_ENABLED", "PLATEGA_ENABLED"])
    return {
        "payments_enabled": _bool(raw.get("PAYMENTS_ENABLED"), config.PAYMENTS_ENABLED),
        "yookassa_enabled": _bool(raw.get("YOOKASSA_ENABLED"), config.YOOKASSA_ENABLED),
        "platega_enabled": _bool(raw.get("PLATEGA_ENABLED"), config.PLATEGA_ENABLED),
    }


def yookassa_credentials():
    raw = legal.read_env_vars(["YOOKASSA_SHOP_ID", "YOOKASSA_SECRET_KEY"])
    return (
        raw.get("YOOKASSA_SHOP_ID") or config.YOOKASSA_SHOP_ID,
        raw.get("YOOKASSA_SECRET_KEY") or config.YOOKASSA_SECRET_KEY,
    )


def platega_credentials():
    raw = legal.read_env_vars(["PLATEGA_MERCHANT_ID", "PLATEGA_SECRET"])
    return (
        raw.get("PLATEGA_MERCHANT_ID") or config.PLATEGA_MERCHANT_ID,
        raw.get("PLATEGA_SECRET") or config.PLATEGA_SECRET,
    )


def get_hwid_settings() -> dict:
    raw = legal.read_env_vars(["HWID_LIMIT_ENABLED", "HWID_FALLBACK_LIMIT"])
    limit = _positive_int(raw.get("HWID_FALLBACK_LIMIT"), config.HWID_FALLBACK_LIMIT)
    return {
        "enabled": _bool(raw.get("HWID_LIMIT_ENABLED"), config.HWID_LIMIT_ENABLED),
        "fallback_limit": limit if limit > 0 else config.HWID_FALLBACK_LIMIT,
    }
