import html
import os

import config

ENV_PATH = os.path.join(config.BASE_DIR, ".env")
SITE_DIR = os.path.join(config.BASE_DIR, "site")

FIELD_KEYS = ["LEGAL_NAME", "LEGAL_INN", "REFUND_HOURS", "SUPPORT_CONTACT", "SUPPORT_EMAIL", "OFFER_EFFECTIVE_DATE"]


def read_env_vars(keys: list) -> dict:
    result = {key: None for key in keys}
    if not os.path.exists(ENV_PATH):
        return result
    wanted = set(keys)
    with open(ENV_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if "=" not in line or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            if key in wanted and result[key] is None:
                result[key] = value
    return result


def read_env_var(key: str, default: str = "") -> str:
    value = read_env_vars([key])[key]
    return default if value is None else value


def update_env_var(key: str, value: str):
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


def get_settings() -> dict:
    raw = read_env_vars(FIELD_KEYS)
    return {key: (raw[key] or "") for key in FIELD_KEYS}


def _fallback(label: str) -> str:
    return f'<span class="fill">{html.escape(label)}</span>'


def _field(value: str, fallback_label: str) -> str:
    return html.escape(value) if value else _fallback(fallback_label)


def live_bot_username() -> str:
    raw = read_env_var("BOT_USERNAME", "")
    return raw.strip() if raw.strip() else config.BOT_USERNAME


def live_brand_name() -> str:
    raw = read_env_var("BRAND_NAME", "")
    return raw.strip() if raw.strip() else config.BRAND_NAME


def render(template_name: str) -> str:
    import plugins

    path = plugins.site_path(template_name, SITE_DIR)
    with open(path, encoding="utf-8") as f:
        content = f.read()

    s = get_settings()
    bot_username = live_bot_username()
    replacements = {
        "EFFECTIVE_DATE": _field(s["OFFER_EFFECTIVE_DATE"], "дата не указана"),
        "LEGAL_NAME": _field(s["LEGAL_NAME"], "название/ФИО не указано"),
        "INN": _field(s["LEGAL_INN"], "ИНН не указан"),
        "BOT_USERNAME": _field(f"@{bot_username}" if bot_username else "", "бот не указан"),
        "REFUND_HOURS": html.escape(s["REFUND_HOURS"]) if s["REFUND_HOURS"] else "24",
        "SUPPORT_CONTACT": _field(s["SUPPORT_CONTACT"], "контакт не указан"),
        "SUPPORT_EMAIL": _field(s["SUPPORT_EMAIL"], "email не указан"),
        "BRAND_NAME": html.escape(live_brand_name()),
        "SITE_DOMAIN": html.escape(config.SITE_DOMAIN),
    }
    for token, value in replacements.items():
        content = content.replace("{{" + token + "}}", value)
    return content


def render_site_page(template_name: str) -> str:
    import plugins

    path = plugins.site_path(template_name, SITE_DIR)
    with open(path, encoding="utf-8") as f:
        content = f.read()

    replacements = {
        "BRAND_NAME": html.escape(live_brand_name()),
        "SITE_DOMAIN": html.escape(config.SITE_DOMAIN),
        "SUB_DOMAIN": html.escape(config.SUB_DOMAIN),
        "BOT_USERNAME": html.escape(live_bot_username()),
    }
    for token, value in replacements.items():
        content = content.replace("{{" + token + "}}", value)
    return content
