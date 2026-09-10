import sqlite3
import secrets
import datetime
import contextlib

from config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    tg_id INTEGER PRIMARY KEY,
    token TEXT UNIQUE NOT NULL,
    username TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id INTEGER NOT NULL,
    hwid TEXT NOT NULL,
    device_os TEXT,
    device_model TEXT,
    user_agent TEXT,
    first_seen TEXT NOT NULL,
    UNIQUE(tg_id, hwid)
);

CREATE TABLE IF NOT EXISTS subscriptions (
    uuid TEXT PRIMARY KEY,
    tg_id INTEGER NOT NULL,
    node TEXT NOT NULL,
    plan TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    source TEXT NOT NULL DEFAULT 'bot'
);

CREATE TABLE IF NOT EXISTS gift_codes (
    code TEXT PRIMARY KEY,
    node TEXT NOT NULL,
    plan TEXT NOT NULL,
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    used_by INTEGER,
    used_at TEXT
);

CREATE TABLE IF NOT EXISTS admin_sessions (
    token TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS payments (
    id TEXT PRIMARY KEY,
    tg_id INTEGER NOT NULL,
    node TEXT NOT NULL,
    plan TEXT NOT NULL,
    provider TEXT NOT NULL,
    external_id TEXT,
    amount INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    pay_url TEXT,
    created_at TEXT NOT NULL,
    paid_at TEXT
);

CREATE TABLE IF NOT EXISTS nodes (
    code TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    address TEXT,
    port INTEGER NOT NULL DEFAULT 443,
    public_key TEXT,
    private_key TEXT,
    short_id TEXT,
    sni TEXT,
    flow TEXT,
    shared_uuid TEXT,
    provision_token TEXT,
    transports_json TEXT,
    hysteria_enabled INTEGER NOT NULL DEFAULT 0,
    hysteria_port INTEGER,
    hysteria_password TEXT,
    hysteria_obfs_password TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
"""

_NEW_NODE_COLUMNS = {
    "transports_json": "TEXT",
    "hysteria_enabled": "INTEGER NOT NULL DEFAULT 0",
    "hysteria_port": "INTEGER",
    "hysteria_password": "TEXT",
    "hysteria_obfs_password": "TEXT",
}

_NEW_USER_COLUMNS = {
    "hwid_limit": "INTEGER",
}


def _migrate():
    with get_conn() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(nodes)").fetchall()}
        for name, decl in _NEW_NODE_COLUMNS.items():
            if name not in cols:
                conn.execute(f"ALTER TABLE nodes ADD COLUMN {name} {decl}")
        ucols = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        for name, decl in _NEW_USER_COLUMNS.items():
            if name not in ucols:
                conn.execute(f"ALTER TABLE users ADD COLUMN {name} {decl}")


def now_iso():
    return datetime.datetime.utcnow().isoformat()


@contextlib.contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)
    _migrate()
    _seed_local_node()


def _seed_local_node():
    from config import XRAY_PUBLIC_KEY, XRAY_SHORT_ID_TCP, REALITY_SNI, DE1_ADDRESS

    with get_conn() as conn:
        row = conn.execute("SELECT 1 FROM nodes WHERE code='de1'").fetchone()
        if row:
            return
        conn.execute(
            "INSERT INTO nodes (code, label, kind, address, port, public_key, short_id, sni, flow, enabled, created_at) "
            "VALUES ('de1', ?, 'local', ?, 443, ?, ?, ?, 'xtls-rprx-vision', 1, ?)",
            ("Локальная нода (de1)", DE1_ADDRESS, XRAY_PUBLIC_KEY, XRAY_SHORT_ID_TCP, REALITY_SNI, now_iso()),
        )


def list_nodes(enabled_only: bool = False):
    q = "SELECT * FROM nodes"
    if enabled_only:
        q += " WHERE enabled=1"
    q += " ORDER BY (code='de1') DESC, created_at ASC"
    with get_conn() as conn:
        rows = conn.execute(q).fetchall()
        return [dict(r) for r in rows]


def get_node(code: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM nodes WHERE code=?", (code,)).fetchone()
        return dict(row) if row else None


def create_node(code, label, kind, address, port, public_key, short_id, sni, flow, shared_uuid=None):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO nodes (code, label, kind, status, address, port, public_key, short_id, sni, flow, shared_uuid, enabled, created_at) "
            "VALUES (?,?,?, 'active', ?,?,?,?,?,?,?,1,?)",
            (code, label, kind, address, port, public_key, short_id, sni, flow, shared_uuid, now_iso()),
        )
    return get_node(code)


def create_pending_node(label, address, port, sni, private_key, public_key, short_id, transports_json=None,
                         hysteria_port=None, hysteria_password=None, hysteria_obfs_password=None):
    import json as jsonmod

    code = "n" + secrets.token_hex(4)
    token = secrets.token_urlsafe(24)
    hysteria_enabled = 1 if hysteria_password else 0
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO nodes (code, label, kind, status, address, port, public_key, private_key, short_id, sni, flow, "
            "provision_token, transports_json, hysteria_enabled, hysteria_port, hysteria_password, hysteria_obfs_password, enabled, created_at) "
            "VALUES (?,?, 'managed', 'pending', ?,?,?,?,?,?, 'xtls-rprx-vision', ?, ?, ?, ?, ?, ?, 0, ?)",
            (code, label, address, port, public_key, private_key, short_id, sni, token,
             transports_json if transports_json is not None else jsonmod.dumps([]),
             hysteria_enabled, hysteria_port, hysteria_password, hysteria_obfs_password, now_iso()),
        )
    return get_node(code), token


def get_node_by_token(token: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM nodes WHERE provision_token=?", (token,)).fetchone()
        return dict(row) if row else None


def activate_node(code: str):
    with get_conn() as conn:
        conn.execute("UPDATE nodes SET status='active', enabled=1 WHERE code=?", (code,))
    return get_node(code)


def update_node(code: str, **fields):
    if not fields:
        return get_node(code)
    cols = ", ".join(f"{k}=?" for k in fields)
    with get_conn() as conn:
        conn.execute(f"UPDATE nodes SET {cols} WHERE code=?", (*fields.values(), code))
    return get_node(code)


def delete_node(code: str):
    if code == "de1":
        raise ValueError("cannot delete the local node")
    with get_conn() as conn:
        conn.execute("DELETE FROM nodes WHERE code=?", (code,))


def get_or_create_user(tg_id: int, username: str | None):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)).fetchone()
        if row:
            if username and row["username"] != username:
                conn.execute("UPDATE users SET username=? WHERE tg_id=?", (username, tg_id))
            return dict(row)
        token = secrets.token_hex(16)
        conn.execute(
            "INSERT INTO users (tg_id, token, username, created_at) VALUES (?,?,?,?)",
            (tg_id, token, username, now_iso()),
        )
        row = conn.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)).fetchone()
        return dict(row)


def get_user_by_token(token: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE token=?", (token,)).fetchone()
        return dict(row) if row else None


def create_subscription(tg_id: int, node: str, plan_days: int, plan_code: str, source: str = "bot", client_uuid: str | None = None):
    import uuid as uuidlib

    cid = client_uuid or str(uuidlib.uuid4())
    created = datetime.datetime.utcnow()
    expires = created + datetime.timedelta(days=plan_days)
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO subscriptions (uuid, tg_id, node, plan, created_at, expires_at, active, source) "
            "VALUES (?,?,?,?,?,?,1,?)",
            (cid, tg_id, node, plan_code, created.isoformat(), expires.isoformat(), source),
        )
    return {"uuid": cid, "tg_id": tg_id, "node": node, "plan": plan_code, "expires_at": expires.isoformat()}


def list_active_subscriptions(tg_id: int | None = None, node: str | None = None):
    q = "SELECT * FROM subscriptions WHERE active=1 AND expires_at > ?"
    params = [now_iso()]
    if tg_id is not None:
        q += " AND tg_id=?"
        params.append(tg_id)
    if node is not None:
        q += " AND node=?"
        params.append(node)
    with get_conn() as conn:
        rows = conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]


def deactivate_expired():
    with get_conn() as conn:
        expired = conn.execute(
            "SELECT * FROM subscriptions WHERE active=1 AND expires_at <= ?", (now_iso(),)
        ).fetchall()
        conn.execute("UPDATE subscriptions SET active=0 WHERE active=1 AND expires_at <= ?", (now_iso(),))
        return [dict(r) for r in expired]


def create_gift_code(node: str, plan_code: str, created_by: int):
    code = secrets.token_hex(6)
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO gift_codes (code, node, plan, created_by, created_at) VALUES (?,?,?,?,?)",
            (code, node, plan_code, created_by, now_iso()),
        )
    return code


def redeem_gift_code(code: str, tg_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM gift_codes WHERE code=?", (code,)).fetchone()
        if not row:
            return None, "not_found"
        if row["used_by"] is not None:
            return None, "already_used"
        conn.execute(
            "UPDATE gift_codes SET used_by=?, used_at=? WHERE code=?",
            (tg_id, now_iso(), code),
        )
        return dict(row), None


def create_admin_session(hours: int = 168):
    token = secrets.token_urlsafe(32)
    expires = datetime.datetime.utcnow() + datetime.timedelta(hours=hours)
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO admin_sessions (token, created_at, expires_at) VALUES (?,?,?)",
            (token, now_iso(), expires.isoformat()),
        )
    return token


def validate_admin_session(token: str) -> bool:
    if not token:
        return False
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM admin_sessions WHERE token=? AND expires_at>?", (token, now_iso())
        ).fetchone()
        return row is not None


def delete_admin_session(token: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM admin_sessions WHERE token=?", (token,))


def list_all_subscriptions(limit: int = 200):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT s.*, u.username FROM subscriptions s "
            "LEFT JOIN users u ON u.tg_id = s.tg_id "
            "ORDER BY s.created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def revoke_subscription(client_uuid: str):
    with get_conn() as conn:
        conn.execute("UPDATE subscriptions SET active=0 WHERE uuid=?", (client_uuid,))


def list_gift_codes(limit: int = 200):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM gift_codes ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def stats():
    with get_conn() as conn:
        users_n = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        active_n = conn.execute(
            "SELECT COUNT(*) c FROM subscriptions WHERE active=1 AND expires_at>?", (now_iso(),)
        ).fetchone()["c"]
        total_subs = conn.execute("SELECT COUNT(*) c FROM subscriptions").fetchone()["c"]
        gifts_created = conn.execute("SELECT COUNT(*) c FROM gift_codes").fetchone()["c"]
        gifts_used = conn.execute("SELECT COUNT(*) c FROM gift_codes WHERE used_by IS NOT NULL").fetchone()["c"]
        return {
            "users": users_n,
            "active_subscriptions": active_n,
            "total_subscriptions": total_subs,
            "gifts_created": gifts_created,
            "gifts_used": gifts_used,
        }


def create_payment(payment_id: str, tg_id: int, node: str, plan: str, provider: str, amount: int):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO payments (id, tg_id, node, plan, provider, amount, status, created_at) "
            "VALUES (?,?,?,?,?,?, 'pending', ?)",
            (payment_id, tg_id, node, plan, provider, amount, now_iso()),
        )
    return get_payment(payment_id)


def get_payment(payment_id: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()
        return dict(row) if row else None


def set_payment_external(payment_id: str, external_id: str, pay_url: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE payments SET external_id=?, pay_url=? WHERE id=?",
            (external_id, pay_url, payment_id),
        )
    return get_payment(payment_id)


def mark_payment_paid(payment_id: str):
    with get_conn() as conn:
        row = conn.execute("SELECT status FROM payments WHERE id=?", (payment_id,)).fetchone()
        if not row or row["status"] == "paid":
            return None
        conn.execute(
            "UPDATE payments SET status='paid', paid_at=? WHERE id=?",
            (now_iso(), payment_id),
        )
    return get_payment(payment_id)


def mark_payment_failed(payment_id: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE payments SET status='failed' WHERE id=? AND status='pending'",
            (payment_id,),
        )
    return get_payment(payment_id)


def list_payments(limit: int = 200):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM payments ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def list_devices(tg_id: int):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM devices WHERE tg_id=? ORDER BY first_seen ASC", (tg_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def count_devices(tg_id: int) -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) c FROM devices WHERE tg_id=?", (tg_id,)).fetchone()["c"]


def get_device(tg_id: int, hwid: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM devices WHERE tg_id=? AND hwid=?", (tg_id, hwid)).fetchone()
        return dict(row) if row else None


def add_device(tg_id: int, hwid: str, device_os: str | None, device_model: str | None, user_agent: str | None):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO devices (tg_id, hwid, device_os, device_model, user_agent, first_seen) "
            "VALUES (?,?,?,?,?,?)",
            (tg_id, hwid, device_os, device_model, user_agent, now_iso()),
        )
    return get_device(tg_id, hwid)


def delete_device(device_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM devices WHERE id=?", (device_id,))


def set_user_hwid_limit(tg_id: int, limit: int | None):
    with get_conn() as conn:
        conn.execute("UPDATE users SET hwid_limit=? WHERE tg_id=?", (limit, tg_id))
