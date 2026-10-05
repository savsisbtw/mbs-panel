import hashlib
import hmac
import os
import sqlite3
import secrets
import datetime
import contextlib

import chains as chainsmod
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

CREATE TABLE IF NOT EXISTS admins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_totp (
    token TEXT PRIMARY KEY,
    admin_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS login_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ip TEXT NOT NULL,
    kind TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_login_attempts_lookup ON login_attempts (ip, kind, created_at);

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

CREATE TABLE IF NOT EXISTS chains (
    code TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    entry_node TEXT NOT NULL,
    exit_node TEXT NOT NULL,
    port INTEGER NOT NULL,
    short_id TEXT NOT NULL,
    relay_uuid TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    admin TEXT,
    action TEXT NOT NULL,
    detail TEXT,
    ip TEXT
);

CREATE TABLE IF NOT EXISTS promo_codes (
    code TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    value INTEGER NOT NULL,
    max_uses INTEGER,
    used_count INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS promo_uses (
    code TEXT NOT NULL,
    tg_id INTEGER NOT NULL,
    used_at TEXT NOT NULL,
    PRIMARY KEY (code, tg_id)
);

CREATE TABLE IF NOT EXISTS sub_notices (
    sub_uuid TEXT NOT NULL,
    kind TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    PRIMARY KEY (sub_uuid, kind)
);

CREATE TABLE IF NOT EXISTS api_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    token_hash TEXT UNIQUE NOT NULL,
    prefix TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    revoked INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS traffic_daily (
    day TEXT NOT NULL,
    node TEXT NOT NULL,
    bytes INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, node)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_chains_pair ON chains (entry_node, exit_node);
CREATE UNIQUE INDEX IF NOT EXISTS idx_chains_entry_port ON chains (entry_node, port);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log (ts);
CREATE INDEX IF NOT EXISTS idx_subs_active_expires ON subscriptions (active, expires_at);
CREATE INDEX IF NOT EXISTS idx_subs_tg_id ON subscriptions (tg_id);
CREATE INDEX IF NOT EXISTS idx_subs_node ON subscriptions (node);
CREATE INDEX IF NOT EXISTS idx_devices_tg_id ON devices (tg_id);
CREATE INDEX IF NOT EXISTS idx_payments_status ON payments (status);
CREATE INDEX IF NOT EXISTS idx_gift_codes_used_by ON gift_codes (used_by);
CREATE INDEX IF NOT EXISTS idx_admin_sessions_expires ON admin_sessions (expires_at);
"""

_NEW_NODE_COLUMNS = {
    "transports_json": "TEXT",
    "hysteria_enabled": "INTEGER NOT NULL DEFAULT 0",
    "hysteria_port": "INTEGER",
    "hysteria_password": "TEXT",
    "hysteria_obfs_password": "TEXT",
    "sort_order": "INTEGER NOT NULL DEFAULT 0",
}

_NEW_USER_COLUMNS = {
    "hwid_limit": "INTEGER",
    "ref_code": "TEXT",
    "referred_by": "INTEGER",
    "referral_rewarded": "INTEGER NOT NULL DEFAULT 0",
    "bonus_days_pending": "INTEGER NOT NULL DEFAULT 0",
    "trial_used": "INTEGER NOT NULL DEFAULT 0",
    "promo_pending": "TEXT",
    "note": "TEXT",
}

_NEW_ADMIN_SESSION_COLUMNS = {
    "admin_id": "INTEGER",
}

_NEW_ADMIN_COLUMNS = {
    "totp_secret": "TEXT",
}

_NEW_SUBSCRIPTION_COLUMNS = {
    "held_at": "TEXT",
    "traffic_limit": "INTEGER",
    "traffic_used": "INTEGER NOT NULL DEFAULT 0",
    "traffic_last_raw": "INTEGER NOT NULL DEFAULT 0",
    "limit_hit_at": "TEXT",
}

_NEW_PAYMENT_COLUMNS = {
    "promo_code": "TEXT",
    "original_amount": "INTEGER",
}


def _migrate():
    with get_conn() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(nodes)").fetchall()}
        needs_sort_order_backfill = "sort_order" not in cols
        for name, decl in _NEW_NODE_COLUMNS.items():
            if name not in cols:
                conn.execute(f"ALTER TABLE nodes ADD COLUMN {name} {decl}")
        ucols = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        for name, decl in _NEW_USER_COLUMNS.items():
            if name not in ucols:
                conn.execute(f"ALTER TABLE users ADD COLUMN {name} {decl}")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_ref_code ON users(ref_code)")
        scols = {r["name"] for r in conn.execute("PRAGMA table_info(admin_sessions)").fetchall()}
        for name, decl in _NEW_ADMIN_SESSION_COLUMNS.items():
            if name not in scols:
                conn.execute(f"ALTER TABLE admin_sessions ADD COLUMN {name} {decl}")
        acols = {r["name"] for r in conn.execute("PRAGMA table_info(admins)").fetchall()}
        for name, decl in _NEW_ADMIN_COLUMNS.items():
            if name not in acols:
                conn.execute(f"ALTER TABLE admins ADD COLUMN {name} {decl}")
        subcols = {r["name"] for r in conn.execute("PRAGMA table_info(subscriptions)").fetchall()}
        for name, decl in _NEW_SUBSCRIPTION_COLUMNS.items():
            if name not in subcols:
                conn.execute(f"ALTER TABLE subscriptions ADD COLUMN {name} {decl}")
        pcols = {r["name"] for r in conn.execute("PRAGMA table_info(payments)").fetchall()}
        for name, decl in _NEW_PAYMENT_COLUMNS.items():
            if name not in pcols:
                conn.execute(f"ALTER TABLE payments ADD COLUMN {name} {decl}")
        if needs_sort_order_backfill:
            rows = conn.execute(
                "SELECT code FROM nodes ORDER BY (code='de1') DESC, created_at ASC"
            ).fetchall()
            for i, row in enumerate(rows):
                conn.execute("UPDATE nodes SET sort_order=? WHERE code=?", (i, row["code"]))


def now_iso():
    return datetime.datetime.utcnow().isoformat()


@contextlib.contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
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
    _seed_default_admin()
    for suffix in ("", "-wal", "-shm"):
        path = DB_PATH + suffix
        if os.path.exists(path):
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass


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


def _hash_password(password: str, salt: bytes | None = None) -> str:
    if salt is None:
        salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return salt.hex() + "$" + dk.hex()


def _verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, hash_hex = stored.split("$")
    except ValueError:
        return False
    salt = bytes.fromhex(salt_hex)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return hmac.compare_digest(dk.hex(), hash_hex)


def _seed_default_admin():
    from config import ADMIN_PANEL_PASSWORD

    with get_conn() as conn:
        row = conn.execute("SELECT 1 FROM admins LIMIT 1").fetchone()
        if row or not ADMIN_PANEL_PASSWORD:
            return
        conn.execute(
            "INSERT INTO admins (username, password_hash, created_at) VALUES (?,?,?)",
            ("admin", _hash_password(ADMIN_PANEL_PASSWORD), now_iso()),
        )


def list_nodes(enabled_only: bool = False):
    q = "SELECT * FROM nodes"
    if enabled_only:
        q += " WHERE enabled=1"
    q += " ORDER BY sort_order ASC, created_at ASC"
    with get_conn() as conn:
        rows = conn.execute(q).fetchall()
        return [dict(r) for r in rows]


def get_node(code: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM nodes WHERE code=?", (code,)).fetchone()
        return dict(row) if row else None


def _next_sort_order(conn):
    row = conn.execute("SELECT MAX(sort_order) m FROM nodes").fetchone()
    return (row["m"] or 0) + 1


def reorder_nodes(codes: list):
    with get_conn() as conn:
        existing = {r["code"] for r in conn.execute("SELECT code FROM nodes").fetchall()}
        if set(codes) != existing:
            raise ValueError("reorder list must include exactly all existing node codes")
        for i, code in enumerate(codes):
            conn.execute("UPDATE nodes SET sort_order=? WHERE code=?", (i, code))


def create_node(code, label, kind, address, port, public_key, short_id, sni, flow, shared_uuid=None):
    if get_node(code):
        raise ValueError("node with this code already exists")
    with get_conn() as conn:
        next_order = _next_sort_order(conn)
        conn.execute(
            "INSERT INTO nodes (code, label, kind, status, address, port, public_key, short_id, sni, flow, shared_uuid, sort_order, enabled, created_at) "
            "VALUES (?,?,?, 'active', ?,?,?,?,?,?,?,?,1,?)",
            (code, label, kind, address, port, public_key, short_id, sni, flow, shared_uuid, next_order, now_iso()),
        )
    return get_node(code)


def create_pending_node(label, address, port, sni, private_key, public_key, short_id, transports_json=None,
                         hysteria_port=None, hysteria_password=None, hysteria_obfs_password=None):
    import json as jsonmod

    code = "n" + secrets.token_hex(4)
    token = secrets.token_urlsafe(24)
    hysteria_enabled = 1 if hysteria_password else 0
    with get_conn() as conn:
        next_order = _next_sort_order(conn)
        conn.execute(
            "INSERT INTO nodes (code, label, kind, status, address, port, public_key, private_key, short_id, sni, flow, "
            "provision_token, transports_json, hysteria_enabled, hysteria_port, hysteria_password, hysteria_obfs_password, sort_order, enabled, created_at) "
            "VALUES (?,?, 'managed', 'pending', ?,?,?,?,?,?, 'xtls-rprx-vision', ?, ?, ?, ?, ?, ?, ?, 0, ?)",
            (code, label, address, port, public_key, private_key, short_id, sni, token,
             transports_json if transports_json is not None else jsonmod.dumps([]),
             hysteria_enabled, hysteria_port, hysteria_password, hysteria_obfs_password, next_order, now_iso()),
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
        active = conn.execute(
            "SELECT COUNT(*) c FROM subscriptions WHERE node=? AND active=1 AND expires_at>?",
            (code, now_iso()),
        ).fetchone()["c"]
        if active:
            raise ValueError(f"node has {active} active subscriptions, revoke them first")
        in_chains = conn.execute(
            "SELECT COUNT(*) c FROM chains WHERE entry_node=? OR exit_node=?", (code, code)
        ).fetchone()["c"]
        if in_chains:
            raise ValueError(f"node is used in {in_chains} chain(s), delete them first")
        conn.execute("DELETE FROM nodes WHERE code=?", (code,))


def list_chains(enabled_only: bool = False):
    q = "SELECT * FROM chains"
    if enabled_only:
        q += " WHERE enabled=1"
    q += " ORDER BY sort_order ASC, created_at ASC"
    with get_conn() as conn:
        rows = conn.execute(q).fetchall()
        return [dict(r) for r in rows]


def get_chain(code: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM chains WHERE code=?", (code,)).fetchone()
        return dict(row) if row else None


def create_chain(label: str, entry_node: str, exit_node: str, relay_uuid: str | None):
    with get_conn() as conn:
        dup = conn.execute(
            "SELECT 1 FROM chains WHERE entry_node=? AND exit_node=?", (entry_node, exit_node)
        ).fetchone()
        if dup:
            raise ValueError("такая цепочка уже есть")
        used = {r["port"] for r in conn.execute(
            "SELECT port FROM chains WHERE entry_node=?", (entry_node,)
        ).fetchall()}
        port = None
        for candidate in range(chainsmod.PORT_MIN, chainsmod.PORT_MAX + 1):
            if candidate not in used:
                port = candidate
                break
        if port is None:
            raise ValueError("закончились свободные порты под цепочки на этой ноде")
        row = conn.execute("SELECT MAX(sort_order) m FROM chains").fetchone()
        next_order = (row["m"] or 0) + 1
        code = "c" + secrets.token_hex(3)
        conn.execute(
            "INSERT INTO chains (code, label, entry_node, exit_node, port, short_id, relay_uuid, enabled, sort_order, created_at) "
            "VALUES (?,?,?,?,?,?,?,1,?,?)",
            (code, label, entry_node, exit_node, port, secrets.token_hex(8), relay_uuid, next_order, now_iso()),
        )
    return get_chain(code)


def update_chain(code: str, **fields):
    if not fields:
        return get_chain(code)
    cols = ", ".join(f"{k}=?" for k in fields)
    with get_conn() as conn:
        conn.execute(f"UPDATE chains SET {cols} WHERE code=?", (*fields.values(), code))
    return get_chain(code)


def delete_chain(code: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM chains WHERE code=?", (code,))


def add_audit(admin: str | None, action: str, detail: str = "", ip: str | None = None):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO audit_log (ts, admin, action, detail, ip) VALUES (?,?,?,?,?)",
            (now_iso(), admin, action, detail[:500], ip),
        )
        conn.execute("DELETE FROM audit_log WHERE id <= (SELECT MAX(id) FROM audit_log) - 5000")


def list_audit(limit: int = 100):
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


def _generate_ref_code(conn) -> str:
    for _ in range(20):
        code = secrets.token_hex(4)
        if not conn.execute("SELECT 1 FROM users WHERE ref_code=?", (code,)).fetchone():
            return code
    raise RuntimeError("could not generate a unique ref_code")


def get_or_create_user(tg_id: int, username: str | None):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)).fetchone()
        if row:
            if username and row["username"] != username:
                conn.execute("UPDATE users SET username=? WHERE tg_id=?", (username, tg_id))
            if not row["ref_code"]:
                conn.execute(
                    "UPDATE users SET ref_code=? WHERE tg_id=?", (_generate_ref_code(conn), tg_id)
                )
            row = conn.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)).fetchone()
            return dict(row)
        token = secrets.token_hex(16)
        ref_code = _generate_ref_code(conn)
        conn.execute(
            "INSERT INTO users (tg_id, token, username, ref_code, created_at) VALUES (?,?,?,?,?)",
            (tg_id, token, username, ref_code, now_iso()),
        )
        row = conn.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)).fetchone()
        return dict(row)


def get_user_by_ref_code(ref_code: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE ref_code=?", (ref_code,)).fetchone()
        return dict(row) if row else None


def set_referred_by(tg_id: int, referrer_tg_id: int) -> bool:
    """First-touch attribution: only takes effect for a brand-new account
    (no subscriptions yet) that isn't already attributed, and never to self."""
    if tg_id == referrer_tg_id:
        return False
    with get_conn() as conn:
        row = conn.execute("SELECT referred_by FROM users WHERE tg_id=?", (tg_id,)).fetchone()
        if not row or row["referred_by"] is not None:
            return False
        has_sub = conn.execute("SELECT 1 FROM subscriptions WHERE tg_id=?", (tg_id,)).fetchone()
        if has_sub:
            return False
        referrer = conn.execute("SELECT 1 FROM users WHERE tg_id=?", (referrer_tg_id,)).fetchone()
        if not referrer:
            return False
        conn.execute("UPDATE users SET referred_by=? WHERE tg_id=?", (referrer_tg_id, tg_id))
        return True


def _apply_bonus_days(conn, tg_id: int, days: int):
    if days <= 0:
        return
    row = conn.execute(
        "SELECT uuid, expires_at FROM subscriptions WHERE tg_id=? AND active=1 AND held_at IS NULL "
        "ORDER BY expires_at DESC LIMIT 1",
        (tg_id,),
    ).fetchone()
    if row:
        new_expires = datetime.datetime.fromisoformat(row["expires_at"]) + datetime.timedelta(days=days)
        conn.execute("UPDATE subscriptions SET expires_at=? WHERE uuid=?", (new_expires.isoformat(), row["uuid"]))
    else:
        conn.execute(
            "UPDATE users SET bonus_days_pending = COALESCE(bonus_days_pending, 0) + ? WHERE tg_id=?",
            (days, tg_id),
        )


def credit_bonus_days(tg_id: int, days: int):
    with get_conn() as conn:
        _apply_bonus_days(conn, tg_id, days)


def referral_stats(tg_id: int) -> dict:
    with get_conn() as conn:
        user = conn.execute("SELECT ref_code, bonus_days_pending FROM users WHERE tg_id=?", (tg_id,)).fetchone()
        count = conn.execute(
            "SELECT COUNT(*) c FROM users WHERE referred_by=? AND referral_rewarded=1", (tg_id,)
        ).fetchone()["c"]
        return {
            "ref_code": user["ref_code"] if user else None,
            "bonus_days_pending": user["bonus_days_pending"] if user else 0,
            "referred_count": count,
        }


def get_user_by_token(token: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE token=?", (token,)).fetchone()
        return dict(row) if row else None


def create_subscription(tg_id: int, node: str, plan_days: int, plan_code: str, source: str = "bot", client_uuid: str | None = None, traffic_limit: int | None = None):
    import uuid as uuidlib
    import config

    cid = client_uuid or str(uuidlib.uuid4())
    created = datetime.datetime.utcnow()
    expires = created + datetime.timedelta(days=plan_days)
    with get_conn() as conn:
        is_first = conn.execute("SELECT 1 FROM subscriptions WHERE tg_id=?", (tg_id,)).fetchone() is None
        urow = conn.execute(
            "SELECT referred_by, referral_rewarded, bonus_days_pending FROM users WHERE tg_id=?", (tg_id,)
        ).fetchone()
        pending = urow["bonus_days_pending"] if urow else 0
        if pending:
            expires += datetime.timedelta(days=pending)
            conn.execute("UPDATE users SET bonus_days_pending=0 WHERE tg_id=?", (tg_id,))
        conn.execute(
            "INSERT INTO subscriptions (uuid, tg_id, node, plan, created_at, expires_at, active, source, traffic_limit) "
            "VALUES (?,?,?,?,?,?,1,?,?)",
            (cid, tg_id, node, plan_code, created.isoformat(), expires.isoformat(), source, traffic_limit),
        )
        if is_first and urow and urow["referred_by"] and not urow["referral_rewarded"] and config.REFERRAL_ENABLED:
            conn.execute("UPDATE users SET referral_rewarded=1 WHERE tg_id=?", (tg_id,))
            bonus = config.REFERRAL_BONUS_DAYS
            _apply_bonus_days(conn, tg_id, bonus)
            _apply_bonus_days(conn, urow["referred_by"], bonus)
        expires_final = conn.execute("SELECT expires_at FROM subscriptions WHERE uuid=?", (cid,)).fetchone()["expires_at"]
    return {"uuid": cid, "tg_id": tg_id, "node": node, "plan": plan_code, "expires_at": expires_final}


def list_active_subscriptions(tg_id: int | None = None, node: str | None = None):
    q = "SELECT * FROM subscriptions WHERE active=1 AND held_at IS NULL AND expires_at > ?"
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


def create_admin_session(admin_id: int | None = None, hours: int = 168):
    token = secrets.token_urlsafe(32)
    expires = datetime.datetime.utcnow() + datetime.timedelta(hours=hours)
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO admin_sessions (token, admin_id, created_at, expires_at) VALUES (?,?,?,?)",
            (token, admin_id, now_iso(), expires.isoformat()),
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


def get_session_admin(token: str):
    if not token:
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT a.id, a.username FROM admin_sessions s "
            "JOIN admins a ON a.id = s.admin_id "
            "WHERE s.token=? AND s.expires_at>?", (token, now_iso())
        ).fetchone()
        return dict(row) if row else None


def delete_admin_session(token: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM admin_sessions WHERE token=?", (token,))


def delete_expired_admin_sessions():
    with get_conn() as conn:
        conn.execute("DELETE FROM admin_sessions WHERE expires_at<=?", (now_iso(),))


def verify_admin_login(username: str, password: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM admins WHERE username=?", (username,)).fetchone()
        if not row or not _verify_password(password, row["password_hash"]):
            return None
        return dict(row)


def list_admins():
    with get_conn() as conn:
        rows = conn.execute("SELECT id, username, created_at FROM admins ORDER BY created_at ASC").fetchall()
        return [dict(r) for r in rows]


def create_admin(username: str, password: str):
    with get_conn() as conn:
        existing = conn.execute("SELECT 1 FROM admins WHERE username=?", (username,)).fetchone()
        if existing:
            raise ValueError("username already taken")
        conn.execute(
            "INSERT INTO admins (username, password_hash, created_at) VALUES (?,?,?)",
            (username, _hash_password(password), now_iso()),
        )
        row = conn.execute("SELECT id, username, created_at FROM admins WHERE username=?", (username,)).fetchone()
        return dict(row)


def delete_admin(admin_id: int):
    with get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) c FROM admins").fetchone()["c"]
        if count <= 1:
            raise ValueError("cannot delete the last remaining admin")
        conn.execute("DELETE FROM admins WHERE id=?", (admin_id,))
        conn.execute("DELETE FROM admin_sessions WHERE admin_id=?", (admin_id,))


def get_admin_by_id(admin_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT id, username, created_at, totp_secret FROM admins WHERE id=?", (admin_id,)).fetchone()
        return dict(row) if row else None


def verify_admin_password_by_id(admin_id: int, password: str) -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT password_hash FROM admins WHERE id=?", (admin_id,)).fetchone()
        return bool(row) and _verify_password(password, row["password_hash"])


def set_admin_totp_secret(admin_id: int, secret: str | None):
    with get_conn() as conn:
        conn.execute("UPDATE admins SET totp_secret=? WHERE id=?", (secret, admin_id))


def create_pending_totp(admin_id: int, minutes: int = 5) -> str:
    token = secrets.token_urlsafe(24)
    expires = datetime.datetime.utcnow() + datetime.timedelta(minutes=minutes)
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO pending_totp (token, admin_id, created_at, expires_at) VALUES (?,?,?,?)",
            (token, admin_id, now_iso(), expires.isoformat()),
        )
    return token


def resolve_pending_totp(token: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM pending_totp WHERE token=? AND expires_at>?", (token, now_iso())
        ).fetchone()
        return dict(row) if row else None


def delete_pending_totp(token: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM pending_totp WHERE token=?", (token,))


def delete_expired_pending_totp():
    with get_conn() as conn:
        conn.execute("DELETE FROM pending_totp WHERE expires_at<=?", (now_iso(),))


def record_login_attempt(ip: str, kind: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO login_attempts (ip, kind, created_at) VALUES (?,?,?)",
            (ip, kind, now_iso()),
        )


def count_recent_login_attempts(ip: str, kind: str, minutes: int) -> int:
    since = (datetime.datetime.utcnow() - datetime.timedelta(minutes=minutes)).isoformat()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) c FROM login_attempts WHERE ip=? AND kind=? AND created_at>?",
            (ip, kind, since),
        ).fetchone()
        return row["c"]


def clear_login_attempts(ip: str, kind: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM login_attempts WHERE ip=? AND kind=?", (ip, kind))


def delete_old_login_attempts(hours: int = 1):
    cutoff = (datetime.datetime.utcnow() - datetime.timedelta(hours=hours)).isoformat()
    with get_conn() as conn:
        conn.execute("DELETE FROM login_attempts WHERE created_at<=?", (cutoff,))


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


def hold_subscription(client_uuid: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE subscriptions SET held_at=? WHERE uuid=? AND active=1 AND held_at IS NULL AND expires_at > ?",
            (now_iso(), client_uuid, now_iso()),
        )
        return cur.rowcount > 0


def resume_subscription(client_uuid: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM subscriptions WHERE uuid=? AND held_at IS NOT NULL", (client_uuid,)
        ).fetchone()
        if not row:
            return None
        held_at = datetime.datetime.fromisoformat(row["held_at"])
        shift = datetime.datetime.utcnow() - held_at
        new_expires = (datetime.datetime.fromisoformat(row["expires_at"]) + shift).isoformat()
        cur = conn.execute(
            "UPDATE subscriptions SET expires_at=?, held_at=NULL WHERE uuid=? AND held_at IS NOT NULL",
            (new_expires, client_uuid),
        )
        if cur.rowcount == 0:
            return None
    return get_subscription(client_uuid)


def get_subscription(client_uuid: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT s.*, u.username FROM subscriptions s "
            "LEFT JOIN users u ON u.tg_id = s.tg_id WHERE s.uuid=?",
            (client_uuid,),
        ).fetchone()
        return dict(row) if row else None


def list_users(q: str = "", limit: int = 200):
    limit = max(1, min(int(limit), 1000))
    q = (q or "").strip()
    where = ""
    params = [now_iso(), now_iso()]
    if q:
        if q.lstrip("-").isdigit():
            where = "WHERE u.tg_id = ? OR instr(lower(COALESCE(u.username, '')), ?) > 0 OR instr(lower(COALESCE(u.note, '')), ?) > 0"
            params += [int(q), q.lower(), q.lower()]
        else:
            where = "WHERE instr(lower(COALESCE(u.username, '')), ?) > 0 OR instr(lower(COALESCE(u.note, '')), ?) > 0"
            params += [q.lower().lstrip("@"), q.lower()]
    params.append(limit)
    query = (
        "SELECT u.tg_id, u.username, u.created_at, u.note, "
        "(SELECT COUNT(*) FROM subscriptions s WHERE s.tg_id=u.tg_id) AS subs_total, "
        "(SELECT COUNT(*) FROM subscriptions s WHERE s.tg_id=u.tg_id AND s.active=1 AND s.held_at IS NULL AND s.expires_at > ?) AS subs_active, "
        "(SELECT MAX(s.expires_at) FROM subscriptions s WHERE s.tg_id=u.tg_id AND s.active=1 AND s.held_at IS NULL AND s.expires_at > ?) AS active_until, "
        "(SELECT COUNT(*) FROM devices d WHERE d.tg_id=u.tg_id) AS devices "
        "FROM users u " + where + " ORDER BY u.created_at DESC LIMIT ?"
    )
    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def get_user(tg_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)).fetchone()
        return dict(row) if row else None


def list_subscriptions_for_user(tg_id: int):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM subscriptions WHERE tg_id=? ORDER BY created_at DESC", (tg_id,)
        ).fetchall()
        return [dict(r) for r in rows]


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
        nodes_n = conn.execute("SELECT COUNT(*) c FROM nodes WHERE enabled=1").fetchone()["c"]
        chains_n = conn.execute("SELECT COUNT(*) c FROM chains WHERE enabled=1").fetchone()["c"]
        return {
            "users": users_n,
            "active_subscriptions": active_n,
            "total_subscriptions": total_subs,
            "gifts_created": gifts_created,
            "gifts_used": gifts_used,
            "nodes": nodes_n,
            "chains": chains_n,
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
        cur = conn.execute(
            "UPDATE payments SET status='paid', paid_at=? WHERE id=? AND status='pending'",
            (now_iso(), payment_id),
        )
        if cur.rowcount == 0:
            return None
        row = conn.execute("SELECT tg_id, promo_code FROM payments WHERE id=?", (payment_id,)).fetchone()
        if row and row["promo_code"]:
            _consume_promo(conn, row["promo_code"], row["tg_id"])
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


def add_device_if_under_limit(tg_id: int, hwid: str, limit: int, device_os: str | None, device_model: str | None, user_agent: str | None):
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute("SELECT * FROM devices WHERE tg_id=? AND hwid=?", (tg_id, hwid)).fetchone()
        if existing:
            return dict(existing), True
        count = conn.execute("SELECT COUNT(*) c FROM devices WHERE tg_id=?", (tg_id,)).fetchone()["c"]
        if count >= limit:
            return None, False
        conn.execute(
            "INSERT INTO devices (tg_id, hwid, device_os, device_model, user_agent, first_seen) VALUES (?,?,?,?,?,?)",
            (tg_id, hwid, device_os, device_model, user_agent, now_iso()),
        )
        row = conn.execute("SELECT * FROM devices WHERE tg_id=? AND hwid=?", (tg_id, hwid)).fetchone()
        return dict(row), True


def delete_device(device_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM devices WHERE id=?", (device_id,))


def set_user_hwid_limit(tg_id: int, limit: int | None):
    with get_conn() as conn:
        conn.execute("UPDATE users SET hwid_limit=? WHERE tg_id=?", (limit, tg_id))


def add_traffic_sample(client_uuid: str, raw_total: int) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT traffic_used, traffic_last_raw, node FROM subscriptions WHERE uuid=?", (client_uuid,)
        ).fetchone()
        if not row:
            return 0
        last = row["traffic_last_raw"]
        delta = raw_total - last if raw_total >= last else raw_total
        used = row["traffic_used"] + max(delta, 0)
        if delta > 0:
            conn.execute(
                "INSERT INTO traffic_daily (day, node, bytes) VALUES (?,?,?) "
                "ON CONFLICT(day, node) DO UPDATE SET bytes = bytes + excluded.bytes",
                (datetime.datetime.utcnow().date().isoformat(), row["node"], delta),
            )
        conn.execute(
            "UPDATE subscriptions SET traffic_used=?, traffic_last_raw=? WHERE uuid=?",
            (used, raw_total, client_uuid),
        )
        return used


def set_traffic_limit(client_uuid: str, limit_bytes: int | None):
    with get_conn() as conn:
        conn.execute("UPDATE subscriptions SET traffic_limit=? WHERE uuid=?", (limit_bytes, client_uuid))
        conn.execute(
            "UPDATE subscriptions SET active=1, limit_hit_at=NULL "
            "WHERE uuid=? AND limit_hit_at IS NOT NULL AND expires_at > ? "
            "AND (traffic_limit IS NULL OR traffic_limit <= 0 OR traffic_used < traffic_limit)",
            (client_uuid, now_iso()),
        )


def list_over_limit():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM subscriptions WHERE active=1 AND traffic_limit IS NOT NULL AND traffic_limit > 0 "
            "AND traffic_used >= traffic_limit"
        ).fetchall()
        return [dict(r) for r in rows]


def mark_limit_hit(client_uuid: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE subscriptions SET active=0, limit_hit_at=? WHERE uuid=? AND active=1",
            (now_iso(), client_uuid),
        )


def reset_traffic_counter(client_uuid: str) -> bool:
    with get_conn() as conn:
        conn.execute(
            "UPDATE subscriptions SET traffic_used=0, traffic_last_raw=0 WHERE uuid=?", (client_uuid,)
        )
        cur = conn.execute(
            "UPDATE subscriptions SET active=1, limit_hit_at=NULL "
            "WHERE uuid=? AND limit_hit_at IS NOT NULL AND expires_at > ?",
            (client_uuid, now_iso()),
        )
        return cur.rowcount > 0


def list_subscriptions_expiring(within_hours: int):
    now = datetime.datetime.utcnow()
    until = (now + datetime.timedelta(hours=within_hours)).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM subscriptions WHERE active=1 AND held_at IS NULL AND expires_at > ? AND expires_at <= ?",
            (now.isoformat(), until),
        ).fetchall()
        return [dict(r) for r in rows]


def notice_already_sent(sub_uuid: str, kind: str) -> bool:
    with get_conn() as conn:
        return conn.execute(
            "SELECT 1 FROM sub_notices WHERE sub_uuid=? AND kind=?", (sub_uuid, kind)
        ).fetchone() is not None


def mark_notice_sent(sub_uuid: str, kind: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO sub_notices (sub_uuid, kind, sent_at) VALUES (?,?,?)",
            (sub_uuid, kind, now_iso()),
        )


def claim_trial(tg_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("UPDATE users SET trial_used=1 WHERE tg_id=? AND trial_used=0", (tg_id,))
        return cur.rowcount > 0


def trial_available(tg_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT trial_used FROM users WHERE tg_id=?", (tg_id,)).fetchone()
        if not row or row["trial_used"]:
            return False
        has_sub = conn.execute("SELECT 1 FROM subscriptions WHERE tg_id=?", (tg_id,)).fetchone()
        return has_sub is None


PROMO_KINDS = ("percent", "fixed", "days")


def create_promo(code: str, kind: str, value: int, max_uses: int | None = None, expires_at: str | None = None):
    code = code.strip().upper()
    if not code or len(code) > 40 or not all(c.isalnum() or c in "-_" for c in code):
        raise ValueError("код: только буквы, цифры, - и _, до 40 символов")
    if kind not in PROMO_KINDS:
        raise ValueError("тип промокода: percent, fixed или days")
    value = int(value)
    if value <= 0 or (kind == "percent" and value > 100):
        raise ValueError("значение должно быть больше нуля, для процентов не больше 100")
    if max_uses is not None and int(max_uses) <= 0:
        max_uses = None
    with get_conn() as conn:
        if conn.execute("SELECT 1 FROM promo_codes WHERE code=?", (code,)).fetchone():
            raise ValueError("такой промокод уже есть")
        conn.execute(
            "INSERT INTO promo_codes (code, kind, value, max_uses, expires_at, created_at) VALUES (?,?,?,?,?,?)",
            (code, kind, value, max_uses, expires_at or None, now_iso()),
        )
    return get_promo(code)


def get_promo(code: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM promo_codes WHERE code=?", ((code or "").strip().upper(),)).fetchone()
        return dict(row) if row else None


def list_promos():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM promo_codes ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]


def set_promo_active(code: str, active: bool):
    with get_conn() as conn:
        conn.execute("UPDATE promo_codes SET active=? WHERE code=?", (1 if active else 0, code.strip().upper()))


def delete_promo(code: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM promo_codes WHERE code=?", (code.strip().upper(),))


def validate_promo(code: str, tg_id: int):
    promo = get_promo(code)
    if not promo or not promo["active"]:
        return None, "not_found"
    if promo["expires_at"] and promo["expires_at"] <= now_iso():
        return None, "expired"
    if promo["max_uses"] is not None and promo["used_count"] >= promo["max_uses"]:
        return None, "exhausted"
    with get_conn() as conn:
        used = conn.execute(
            "SELECT 1 FROM promo_uses WHERE code=? AND tg_id=?", (promo["code"], tg_id)
        ).fetchone()
    if used:
        return None, "already_used"
    return promo, None


def discounted_price(price: int, promo: dict | None) -> int:
    if not promo or promo["kind"] == "days":
        return price
    if promo["kind"] == "percent":
        return max(price - price * promo["value"] // 100, 0)
    return max(price - promo["value"], 0)


def _consume_promo(conn, code: str, tg_id: int) -> bool:
    cur = conn.execute(
        "INSERT OR IGNORE INTO promo_uses (code, tg_id, used_at) VALUES (?,?,?)", (code, tg_id, now_iso())
    )
    if cur.rowcount == 0:
        return False
    conn.execute("UPDATE promo_codes SET used_count=used_count+1 WHERE code=?", (code,))
    return True


def redeem_days_promo(code: str, tg_id: int):
    promo, err = validate_promo(code, tg_id)
    if err:
        return None, err
    if promo["kind"] != "days":
        return None, "not_days"
    with get_conn() as conn:
        if not _consume_promo(conn, promo["code"], tg_id):
            return None, "already_used"
        _apply_bonus_days(conn, tg_id, promo["value"])
    return promo, None


def set_promo_pending(tg_id: int, code: str | None):
    with get_conn() as conn:
        conn.execute("UPDATE users SET promo_pending=? WHERE tg_id=?", (code, tg_id))


def get_pending_promo(tg_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT promo_pending FROM users WHERE tg_id=?", (tg_id,)).fetchone()
    if not row or not row["promo_pending"]:
        return None
    promo, err = validate_promo(row["promo_pending"], tg_id)
    if err:
        set_promo_pending(tg_id, None)
        return None
    return promo


def set_payment_promo(payment_id: str, promo_code: str | None, original_amount: int | None):
    with get_conn() as conn:
        conn.execute(
            "UPDATE payments SET promo_code=?, original_amount=? WHERE id=?",
            (promo_code, original_amount, payment_id),
        )


def consume_promo(code: str, tg_id: int) -> bool:
    with get_conn() as conn:
        return _consume_promo(conn, code, tg_id)


def _hash_api_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_api_token(name: str):
    name = (name or "").strip()[:60]
    if not name:
        raise ValueError("дай токену название")
    token = "mbs_" + secrets.token_urlsafe(32)
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO api_tokens (name, token_hash, prefix, created_at) VALUES (?,?,?,?)",
            (name, _hash_api_token(token), token[:10], now_iso()),
        )
        token_id = cur.lastrowid
    return {"id": token_id, "name": name, "token": token, "prefix": token[:10]}


def list_api_tokens():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, name, prefix, created_at, last_used_at, revoked FROM api_tokens ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def revoke_api_token(token_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE api_tokens SET revoked=1 WHERE id=?", (token_id,))


def verify_api_token(token: str):
    if not token:
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, name FROM api_tokens WHERE token_hash=? AND revoked=0", (_hash_api_token(token),)
        ).fetchone()
        if not row:
            return None
        conn.execute("UPDATE api_tokens SET last_used_at=? WHERE id=?", (now_iso(), row["id"]))
        return dict(row)


def extend_subscription(client_uuid: str, days: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM subscriptions WHERE uuid=?", (client_uuid,)).fetchone()
        if not row:
            return None
        now = datetime.datetime.utcnow()
        current = datetime.datetime.fromisoformat(row["expires_at"])
        base = current if current > now else now
        new_expires = (base + datetime.timedelta(days=days)).isoformat()
        was_inactive = not row["active"]
        conn.execute(
            "UPDATE subscriptions SET expires_at=?, active=1 WHERE uuid=?", (new_expires, client_uuid)
        )
        if was_inactive and row["limit_hit_at"]:
            conn.execute("UPDATE subscriptions SET limit_hit_at=NULL, traffic_used=0, traffic_last_raw=0 WHERE uuid=?", (client_uuid,))
    return get_subscription(client_uuid)


def traffic_history(days: int = 30):
    days = max(1, min(int(days), 365))
    today = datetime.datetime.utcnow().date()
    since = (today - datetime.timedelta(days=days - 1)).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT day, node, bytes FROM traffic_daily WHERE day >= ? ORDER BY day", (since,)
        ).fetchall()
    by_day = {}
    for r in rows:
        entry = by_day.setdefault(r["day"], {"day": r["day"], "total": 0, "nodes": {}})
        entry["total"] += r["bytes"]
        entry["nodes"][r["node"]] = r["bytes"]
    result = []
    for i in range(days):
        d = (today - datetime.timedelta(days=days - 1 - i)).isoformat()
        result.append(by_day.get(d, {"day": d, "total": 0, "nodes": {}}))
    return result


def set_user_note(tg_id: int, note: str | None):
    note = (note or "").strip()[:500] or None
    with get_conn() as conn:
        conn.execute("UPDATE users SET note=? WHERE tg_id=?", (note, tg_id))
