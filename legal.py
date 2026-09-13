import html
import os

from config import BASE_DIR, BOT_USERNAME

ENV_PATH = os.path.join(BASE_DIR, ".env")
SITE_DIR = os.path.join(BASE_DIR, "site")

FIELD_KEYS = ["LEGAL_NAME", "LEGAL_INN", "REFUND_HOURS", "SUPPORT_CONTACT", "SUPPORT_EMAIL", "OFFER_EFFECTIVE_DATE"]


def read_env_var(key: str, default: str = "") -> str:
    if not os.path.exists(ENV_PATH):
        return default
    with open(ENV_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith(f"{key}="):
                return line[len(key) + 1:]
    return default


def get_settings() -> dict:
    return {key: read_env_var(key) for key in FIELD_KEYS}


def _fallback(label: str) -> str:
    return f'<span class="fill">{html.escape(label)}</span>'


def _field(value: str, fallback_label: str) -> str:
    return html.escape(value) if value else _fallback(fallback_label)


def render(template_name: str) -> str:
    path = os.path.join(SITE_DIR, template_name)
    with open(path, encoding="utf-8") as f:
        content = f.read()

    s = get_settings()
    replacements = {
        "EFFECTIVE_DATE": _field(s["OFFER_EFFECTIVE_DATE"], "дата не указана"),
        "LEGAL_NAME": _field(s["LEGAL_NAME"], "название/ФИО не указано"),
        "INN": _field(s["LEGAL_INN"], "ИНН не указан"),
        "BOT_USERNAME": _field(f"@{BOT_USERNAME}" if BOT_USERNAME else "", "бот не указан"),
        "REFUND_HOURS": html.escape(s["REFUND_HOURS"]) if s["REFUND_HOURS"] else "24",
        "SUPPORT_CONTACT": _field(s["SUPPORT_CONTACT"], "контакт не указан"),
        "SUPPORT_EMAIL": _field(s["SUPPORT_EMAIL"], "email не указан"),
    }
    for token, value in replacements.items():
        content = content.replace("{{" + token + "}}", value)
    return content
