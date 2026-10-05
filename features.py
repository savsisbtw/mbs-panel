import os
import time

import backup
import db
import nodeprov
import settings
import xray_manager
from settings import GB

REMINDER_STAGES = (("3d", 72), ("1d", 24))


def format_bytes(n: int) -> str:
    v = float(n)
    for unit in ["Б", "КБ", "МБ", "ГБ", "ТБ"]:
        if v < 1024 or unit == "ТБ":
            return f"{int(v)} {unit}" if unit == "Б" else f"{v:.1f} {unit}"
        v /= 1024
    return f"{v:.1f} ТБ"


def collect_all_stats() -> dict:
    all_stats = dict(xray_manager.query_stats())
    for node in db.list_nodes():
        if node["kind"] != "managed" or node["status"] != "active":
            continue
        try:
            remote = nodeprov.remote_query_stats(node)
        except Exception:
            remote = {}
        for key, value in remote.items():
            if key in all_stats:
                all_stats[key] = {
                    "up": all_stats[key]["up"] + value["up"],
                    "down": all_stats[key]["down"] + value["down"],
                }
            else:
                all_stats[key] = value
    return all_stats


def update_traffic_and_find_exceeded() -> list:
    stats = collect_all_stats()
    if not stats:
        return []
    for sub in db.list_active_subscriptions():
        row = stats.get(sub["uuid"])
        if row:
            db.add_traffic_sample(sub["uuid"], row["up"] + row["down"])
    exceeded = db.list_over_limit()
    for sub in exceeded:
        db.mark_limit_hit(sub["uuid"])
    return exceeded


def reminders_due() -> list:
    due = []
    for stage, hours in REMINDER_STAGES:
        for sub in db.list_subscriptions_expiring(hours):
            kind = f"{stage}:{sub['expires_at'][:10]}"
            if db.notice_already_sent(sub["uuid"], kind):
                continue
            due.append((sub, kind, stage))
    seen = set()
    result = []
    for sub, kind, stage in sorted(due, key=lambda x: x[2]):
        if sub["uuid"] in seen:
            continue
        seen.add(sub["uuid"])
        result.append((sub, kind, stage))
    return result


def mark_stage_sent(sub_uuid: str, expires_at: str):
    for stage, _ in REMINDER_STAGES:
        db.mark_notice_sent(sub_uuid, f"{stage}:{expires_at[:10]}")


def pick_trial_node():
    features = settings.get_features()
    wanted = features["trial_node"]
    if wanted:
        node = db.get_node(wanted)
        if node and node["enabled"] and node["status"] == "active":
            return node
    for node in db.list_nodes(enabled_only=True):
        if node["status"] == "active":
            return node
    return None


def traffic_text(sub: dict) -> str:
    used = sub.get("traffic_used") or 0
    limit = sub.get("traffic_limit") or 0
    if limit > 0:
        return f"{format_bytes(used)} из {format_bytes(limit)}"
    return f"{format_bytes(used)}, без лимита"


BACKUP_MARKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".last_tg_backup")
BACKUP_NOW_FLAG = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".backup_now")


def request_backup_now():
    with open(BACKUP_NOW_FLAG, "w") as f:
        f.write("1")


def telegram_backup_due() -> bool:
    feats = settings.get_features()
    if not settings.backup_passphrase():
        return False
    if os.path.exists(BACKUP_NOW_FLAG):
        return True
    if not feats["backup_tg_enabled"]:
        return False
    if not os.path.exists(BACKUP_MARKER):
        return True
    return time.time() - os.path.getmtime(BACKUP_MARKER) >= feats["backup_tg_hours"] * 3600


def make_encrypted_backup():
    data = backup.encrypt_backup(backup.create_backup(), settings.backup_passphrase())
    name = time.strftime("mbs-backup-%Y%m%d-%H%M%S.tar.gz.enc", time.gmtime())
    return name, data


def mark_backup_done():
    with open(BACKUP_MARKER, "w") as f:
        f.write(str(int(time.time())))
    try:
        os.remove(BACKUP_NOW_FLAG)
    except OSError:
        pass
