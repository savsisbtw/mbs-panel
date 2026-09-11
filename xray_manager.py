import json
import os
import subprocess
import fcntl
import contextlib

from config import XRAY_CONFIG_PATH, DE1_TRANSPORTS

_LOCK_PATH = XRAY_CONFIG_PATH + ".lock"
_LOCAL_TAGS = {t["tag"] for t in DE1_TRANSPORTS}
_TAG_FLOW = {t["tag"]: t.get("flow") for t in DE1_TRANSPORTS}


@contextlib.contextmanager
def _locked():
    with open(_LOCK_PATH, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _load():
    with open(XRAY_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(cfg):
    tmp = XRAY_CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, XRAY_CONFIG_PATH)


def _reload_xray():
    subprocess.run(["systemctl", "restart", "xray"], check=True, timeout=20)


def _local_inbounds(cfg):
    return [ib for ib in cfg["inbounds"] if ib.get("tag") in _LOCAL_TAGS]


def add_client(client_uuid: str, email: str):
    with _locked():
        cfg = _load()
        changed = False
        for ib in _local_inbounds(cfg):
            clients = ib["settings"]["clients"]
            if any(c["id"] == client_uuid for c in clients):
                continue
            entry = {"id": client_uuid, "email": email}
            flow = _TAG_FLOW.get(ib["tag"])
            if flow:
                entry["flow"] = flow
            clients.append(entry)
            changed = True
        if changed:
            _save(cfg)
            _reload_xray()


def remove_client(client_uuid: str):
    with _locked():
        cfg = _load()
        changed = False
        for ib in _local_inbounds(cfg):
            clients = ib["settings"]["clients"]
            new_clients = [c for c in clients if c["id"] != client_uuid]
            if len(new_clients) != len(clients):
                ib["settings"]["clients"] = new_clients
                changed = True
        if changed:
            _save(cfg)
            _reload_xray()


def sync_from_db():
    import db as dbmod

    expired = dbmod.deactivate_expired()
    active = dbmod.list_active_subscriptions(node="de1")
    active_by_id = {s["uuid"]: s for s in active}

    with _locked():
        cfg = _load()
        changed = False
        for ib in _local_inbounds(cfg):
            clients = ib["settings"]["clients"]
            current_ids = {c["id"] for c in clients}
            if current_ids == set(active_by_id.keys()):
                continue
            new_clients = [c for c in clients if c["id"] in active_by_id]
            existing_ids = {c["id"] for c in new_clients}
            flow = _TAG_FLOW.get(ib["tag"])
            for cid, sub in active_by_id.items():
                if cid not in existing_ids:
                    entry = {"id": cid, "email": cid}
                    if flow:
                        entry["flow"] = flow
                    new_clients.append(entry)
            ib["settings"]["clients"] = new_clients
            changed = True

        if changed:
            _save(cfg)
            _reload_xray()

    return {"removed_expired": len(expired), "active_now": len(active_by_id), "reloaded": changed}


def add_client_to_node(node: dict, client_uuid: str, email: str):
    if node["kind"] == "local":
        add_client(client_uuid, email)
    elif node["kind"] == "managed":
        import nodeprov
        nodeprov.remote_add_client(node, client_uuid, email)


def remove_client_from_node(node: dict, client_uuid: str):
    if node["kind"] == "local":
        remove_client(client_uuid)
    elif node["kind"] == "managed":
        import nodeprov
        nodeprov.remote_remove_client(node, client_uuid)


def query_stats() -> dict:
    try:
        out = subprocess.run(
            ["/usr/local/bin/xray", "api", "statsquery", "--server=127.0.0.1:10085", "-pattern", "user>>>"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        data = json.loads(out) if out.strip() else {"stat": []}
    except Exception:
        return {}
    result = {}
    for entry in data.get("stat") or []:
        name = entry.get("name", "")
        value = entry.get("value", 0)
        parts = name.split(">>>")
        if len(parts) != 4 or parts[0] != "user":
            continue
        _, email, _, direction = parts
        row = result.setdefault(email, {"up": 0, "down": 0})
        if direction == "uplink":
            row["up"] += value
        elif direction == "downlink":
            row["down"] += value
    return result


def reset_stats(email: str) -> bool:
    try:
        subprocess.run(
            ["/usr/local/bin/xray", "api", "statsquery", "--server=127.0.0.1:10085",
             "-pattern", f"user>>>{email}>>>traffic", "-reset"],
            capture_output=True, text=True, timeout=10,
        )
        return True
    except Exception:
        return False


def reset_stats_for_node(node: dict, email: str) -> bool:
    if node["kind"] == "local":
        return reset_stats(email)
    if node["kind"] == "managed":
        import nodeprov
        return nodeprov.remote_reset_stats(node, email)
    return False


def local_node_status() -> dict:
    try:
        with open("/proc/loadavg") as f:
            load1 = float(f.read().split()[0])
    except Exception:
        load1 = None
    mem_total = mem_avail = None
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    mem_total = int(line.split()[1]) // 1024
                elif line.startswith("MemAvailable:"):
                    mem_avail = int(line.split()[1]) // 1024
    except Exception:
        pass
    uptime_s = None
    try:
        with open("/proc/uptime") as f:
            uptime_s = int(float(f.read().split()[0]))
    except Exception:
        pass
    xray_active = subprocess.run(["systemctl", "is-active", "xray"], capture_output=True, text=True).stdout.strip()
    return {
        "ok": True, "load1": load1,
        "mem_used_mb": (mem_total - mem_avail) if (mem_total and mem_avail is not None) else None,
        "mem_total_mb": mem_total, "uptime_s": uptime_s, "xray_active": xray_active == "active",
    }


def sync_all():
    import db as dbmod
    import nodeprov

    expired = dbmod.deactivate_expired()
    results = {}
    for node in dbmod.list_nodes():
        if node["kind"] == "local":
            results[node["code"]] = sync_from_db()
        elif node["kind"] == "managed":
            active = dbmod.list_active_subscriptions(node=node["code"])
            try:
                nodeprov.remote_sync(node, active)
                results[node["code"]] = {"active_now": len(active), "ok": True}
            except Exception as e:
                results[node["code"]] = {"active_now": len(active), "ok": False, "error": str(e)}
    return {"removed_expired": len(expired), "nodes": results}
