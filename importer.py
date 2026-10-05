import datetime
import hashlib
import json
import os
import sqlite3
import tempfile

MAX_IMPORT_SIZE = 100 * 1024 * 1024


class ImportError_(Exception):
    pass


def synthetic_tg_id(key: str) -> int:
    digest = hashlib.sha1(key.encode()).hexdigest()
    return -(int(digest[:12], 16) % 9_000_000_000_000 + 1)


def _iso_from_ms(ms) -> str | None:
    try:
        ms = int(ms)
    except (TypeError, ValueError):
        return None
    if ms <= 0:
        return None
    return datetime.datetime.utcfromtimestamp(ms / 1000).isoformat()


def _iso_from_marzban(value) -> str | None:
    if value is None or value == 0 or value == "":
        return None
    if isinstance(value, (int, float)):
        return datetime.datetime.utcfromtimestamp(value).isoformat()
    text = str(value).strip()
    if text.isdigit():
        return datetime.datetime.utcfromtimestamp(int(text)).isoformat()
    try:
        return datetime.datetime.fromisoformat(text.replace("Z", "")).isoformat()
    except ValueError:
        return None


def _open_readonly(path: str):
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
        return conn
    except sqlite3.DatabaseError:
        raise ImportError_("это не файл базы SQLite")


def parse_xui(path: str) -> list:
    conn = _open_readonly(path)
    try:
        tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "inbounds" not in tables:
            raise ImportError_("в файле нет таблицы inbounds, это не база 3x-ui")
        rows = conn.execute("SELECT settings, protocol FROM inbounds").fetchall()
    finally:
        conn.close()
    result = []
    for row in rows:
        if (row["protocol"] or "").lower() != "vless":
            continue
        try:
            clients = json.loads(row["settings"] or "{}").get("clients") or []
        except ValueError:
            continue
        for c in clients:
            client_id = str(c.get("id") or "").strip()
            if not client_id:
                continue
            tg_raw = str(c.get("tgId") or "").strip()
            label = str(c.get("email") or client_id[:8])
            tg_id = int(tg_raw) if tg_raw.lstrip("-").isdigit() and int(tg_raw) != 0 else synthetic_tg_id(label)
            total = c.get("totalGB") or 0
            result.append({
                "uuid": client_id,
                "label": label,
                "tg_id": tg_id,
                "expires_at": _iso_from_ms(c.get("expiryTime")),
                "traffic_limit": int(total) if str(total).isdigit() and int(total) > 0 else None,
                "enabled": c.get("enable", True) is not False,
            })
    return result


def parse_marzban(path: str) -> list:
    conn = _open_readonly(path)
    try:
        tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "users" not in tables or "proxies" not in tables:
            raise ImportError_("в файле нет таблиц users и proxies, это не база Marzban")
        users = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM users").fetchall()}
        proxies = conn.execute("SELECT user_id, type, settings FROM proxies").fetchall()
    finally:
        conn.close()
    result = []
    for p in proxies:
        if str(p["type"]).lower() != "vless":
            continue
        user = users.get(p["user_id"])
        if not user:
            continue
        try:
            settings = json.loads(p["settings"] or "{}")
        except ValueError:
            continue
        client_id = str(settings.get("id") or "").strip()
        if not client_id:
            continue
        label = str(user.get("username") or client_id[:8])
        limit = user.get("data_limit")
        result.append({
            "uuid": client_id,
            "label": label,
            "tg_id": synthetic_tg_id(label),
            "expires_at": _iso_from_marzban(user.get("expire")),
            "traffic_limit": int(limit) if limit and int(limit) > 0 else None,
            "enabled": str(user.get("status") or "active").lower() == "active",
        })
    return result


PARSERS = {"xui": parse_xui, "marzban": parse_marzban}


def parse_upload(source: str, data: bytes) -> list:
    if source not in PARSERS:
        raise ImportError_("источник: xui или marzban")
    if len(data) > MAX_IMPORT_SIZE:
        raise ImportError_("файл слишком большой")
    fd, path = tempfile.mkstemp(suffix=".db")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return PARSERS[source](path)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
