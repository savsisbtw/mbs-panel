import config
import legal

PRICE_ENV_KEYS = {p["code"]: "PRICE_" + p["code"].upper() for p in config.PLANS}


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
            "trial": bool(p.get("trial", False)),
        })
    return plans


def get_plans_by_code() -> dict:
    return {p["code"]: p for p in get_plans()}


def set_plan_prices(prices: dict):
    for code, price in prices.items():
        if code in PRICE_ENV_KEYS:
            legal.update_env_var(PRICE_ENV_KEYS[code], str(int(price)))


def get_payment_settings() -> dict:
    raw = legal.read_env_vars(["PAYMENTS_ENABLED", "YOOKASSA_ENABLED", "PLATEGA_ENABLED", "CRYPTOBOT_ENABLED"])
    return {
        "cryptobot_enabled": _bool(raw.get("CRYPTOBOT_ENABLED"), False),
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


def cryptobot_token() -> str:
    return (legal.read_env_var("CRYPTOBOT_TOKEN", "") or "").strip()


def get_brand_name() -> str:
    raw = legal.read_env_var("BRAND_NAME", "")
    return raw.strip() if raw.strip() else config.BRAND_NAME


def bot_credentials():
    raw = legal.read_env_vars(["BOT_TOKEN", "BOT_USERNAME"])
    return (
        raw.get("BOT_TOKEN") or config.BOT_TOKEN,
        raw.get("BOT_USERNAME") or config.BOT_USERNAME,
    )


def get_hwid_settings() -> dict:
    raw = legal.read_env_vars(["HWID_LIMIT_ENABLED", "HWID_FALLBACK_LIMIT"])
    limit = _positive_int(raw.get("HWID_FALLBACK_LIMIT"), config.HWID_FALLBACK_LIMIT)
    return {
        "enabled": _bool(raw.get("HWID_LIMIT_ENABLED"), config.HWID_LIMIT_ENABLED),
        "fallback_limit": limit if limit > 0 else config.HWID_FALLBACK_LIMIT,
    }


def get_referral_settings() -> dict:
    raw = legal.read_env_vars(["REFERRAL_ENABLED", "REFERRAL_BONUS_DAYS"])
    days = _positive_int(raw.get("REFERRAL_BONUS_DAYS"), config.REFERRAL_BONUS_DAYS)
    return {
        "enabled": _bool(raw.get("REFERRAL_ENABLED"), config.REFERRAL_ENABLED),
        "bonus_days": days if days > 0 else config.REFERRAL_BONUS_DAYS,
    }


def set_referral_settings(enabled: bool, bonus_days: int):
    legal.update_env_var("REFERRAL_ENABLED", "true" if enabled else "false")
    legal.update_env_var("REFERRAL_BONUS_DAYS", str(int(bonus_days)))


def get_features() -> dict:
    keys = ["TRIAL_ENABLED", "TRIAL_DAYS", "TRIAL_NODE", "TRIAL_TRAFFIC_GB", "DEFAULT_TRAFFIC_GB",
            "REMINDERS_ENABLED", "NODE_ALERTS_ENABLED", "BACKUP_TG_ENABLED", "BACKUP_TG_HOURS", "BACKUP_PASSPHRASE"]
    raw = legal.read_env_vars(keys)
    trial_days = _positive_int(raw.get("TRIAL_DAYS"), 1)
    return {
        "trial_enabled": _bool(raw.get("TRIAL_ENABLED"), False),
        "trial_days": trial_days if trial_days > 0 else 1,
        "trial_node": (raw.get("TRIAL_NODE") or "").strip(),
        "trial_traffic_gb": _positive_int(raw.get("TRIAL_TRAFFIC_GB"), 2),
        "default_traffic_gb": _positive_int(raw.get("DEFAULT_TRAFFIC_GB"), 0),
        "reminders_enabled": _bool(raw.get("REMINDERS_ENABLED"), True),
        "node_alerts_enabled": _bool(raw.get("NODE_ALERTS_ENABLED"), True),
        "backup_tg_enabled": _bool(raw.get("BACKUP_TG_ENABLED"), False),
        "backup_tg_hours": max(_positive_int(raw.get("BACKUP_TG_HOURS"), 24), 1),
        "backup_has_passphrase": bool((raw.get("BACKUP_PASSPHRASE") or "").strip()),
    }


def set_features(values: dict):
    mapping = {
        "trial_enabled": ("TRIAL_ENABLED", lambda v: "true" if v else "false"),
        "trial_days": ("TRIAL_DAYS", lambda v: str(max(int(v), 1))),
        "trial_node": ("TRIAL_NODE", lambda v: str(v or "").strip()),
        "trial_traffic_gb": ("TRIAL_TRAFFIC_GB", lambda v: str(max(int(v), 0))),
        "default_traffic_gb": ("DEFAULT_TRAFFIC_GB", lambda v: str(max(int(v), 0))),
        "reminders_enabled": ("REMINDERS_ENABLED", lambda v: "true" if v else "false"),
        "node_alerts_enabled": ("NODE_ALERTS_ENABLED", lambda v: "true" if v else "false"),
        "backup_tg_enabled": ("BACKUP_TG_ENABLED", lambda v: "true" if v else "false"),
        "backup_tg_hours": ("BACKUP_TG_HOURS", lambda v: str(max(int(v), 1))),
    }
    for key, (env_key, conv) in mapping.items():
        if key in values:
            legal.update_env_var(env_key, conv(values[key]))


GB = 1024 ** 3


def backup_passphrase() -> str:
    return (legal.read_env_var("BACKUP_PASSPHRASE", "") or "").strip()


def set_backup_passphrase(value: str):
    legal.update_env_var("BACKUP_PASSPHRASE", value.strip())


def default_traffic_limit_bytes():
    gb = get_features()["default_traffic_gb"]
    return gb * GB if gb > 0 else None
