import json
import os
import socket
import subprocess
import fcntl
import contextlib
import time

import chains
from config import XRAY_CONFIG_PATH

_LOCK_PATH = XRAY_CONFIG_PATH + ".lock"


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


class ConfigValidationError(Exception):
    pass


def _readable_by(path, uid, gid) -> bool:
    try:
        st = os.stat(path)
    except OSError:
        return False
    mode = st.st_mode
    if st.st_uid == uid and mode & 0o400:
        return True
    if st.st_gid == gid and mode & 0o040:
        return True
    if mode & 0o004:
        return True
    return False


def check_cert_permissions(cfg, uid=65534, gid=65534) -> list:
    problems = []
    for ib in cfg.get("inbounds", []):
        stream = ib.get("streamSettings") or {}
        tls = stream.get("tlsSettings")
        if not tls:
            continue
        for cert in tls.get("certificates") or []:
            for key in ("certificateFile", "keyFile"):
                path = cert.get(key)
                if path and not _readable_by(path, uid, gid):
                    problems.append(f"{ib.get('tag', '?')}: {key}={path} not readable by the xray service user")
    return problems


def validate_config(cfg, xray_bin="/usr/local/bin/xray") -> tuple:
    tmp = XRAY_CONFIG_PATH + ".validate.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    try:
        result = subprocess.run(
            [xray_bin, "run", "-test", "-format=json", "-config", tmp],
            capture_output=True, text=True, timeout=15,
        )
        ok = result.returncode == 0
        detail = (result.stdout + result.stderr).strip()
    except Exception as e:
        return False, str(e)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    if not ok:
        return False, detail
    perm_problems = check_cert_permissions(cfg)
    if perm_problems:
        return False, "cert permission problem(s): " + "; ".join(perm_problems)
    return True, detail


def _save(cfg):
    ok, detail = validate_config(cfg)
    if not ok:
        raise ConfigValidationError(detail)
    tmp = XRAY_CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, XRAY_CONFIG_PATH)


def _reload_xray():
    subprocess.run(["systemctl", "restart", "xray"], check=True, timeout=20)


def _local_inbounds(cfg):
    return [ib for ib in cfg["inbounds"] if chains.is_user_tag(ib.get("tag"))]


def add_client(client_uuid: str, email: str):
    with _locked():
        cfg = _load()
        changed = False
        for ib in _local_inbounds(cfg):
            clients = ib["settings"]["clients"]
            if any(c["id"] == client_uuid for c in clients):
                continue
            entry = {"id": client_uuid, "email": email}
            flow = chains.flow_for_tag(ib["tag"])
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


def _node_usable(node):
    return bool(node["enabled"]) and node["status"] == "active"


def desired_state(node):
    import db as dbmod

    active = dbmod.list_active_subscriptions(node=node["code"])
    wanted = {s["uuid"]: s["uuid"] for s in active}
    nodes_by_code = {n["code"]: n for n in dbmod.list_nodes()}
    entry_chains = []
    exit_nodes = {}
    relay_wanted = {}
    for chain in dbmod.list_chains(enabled_only=True):
        entry = nodes_by_code.get(chain["entry_node"])
        exit_node = nodes_by_code.get(chain["exit_node"])
        if not entry or not exit_node:
            continue
        if not _node_usable(entry) or not _node_usable(exit_node):
            continue
        if chain["entry_node"] == node["code"]:
            entry_chains.append(chain)
            exit_nodes[chain["exit_node"]] = exit_node
        if chain["exit_node"] == node["code"] and node["kind"] in ("local", "managed") and chain.get("relay_uuid"):
            relay_wanted[chain["relay_uuid"]] = chains.relay_email(chain["code"])
    return wanted, relay_wanted, entry_chains, exit_nodes


def _read_config_text():
    with open(XRAY_CONFIG_PATH, "r", encoding="utf-8") as f:
        return f.read()


def _restore_config_text(text):
    tmp = XRAY_CONFIG_PATH + ".restore.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, XRAY_CONFIG_PATH)
    subprocess.run(["systemctl", "restart", "xray"], timeout=20)


def _reload_and_verify():
    subprocess.run(["systemctl", "restart", "xray"], check=True, timeout=20)
    time.sleep(1)
    state = subprocess.run(["systemctl", "is-active", "xray"], capture_output=True, text=True).stdout.strip()
    if state != "active":
        raise ConfigValidationError("xray не поднялся после применения конфига, вернули старый")


def _port_busy(port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("0.0.0.0", int(port)))
        return False
    except OSError:
        return True
    finally:
        sock.close()


def _open_firewall(port):
    subprocess.run(
        ["sh", "-c", f"command -v ufw >/dev/null 2>&1 && ufw allow {int(port)}/tcp || true"],
        timeout=20,
    )


def _reconcile_local(wanted, relay_wanted, entry_chains, exit_nodes, apply_chains=True):
    with _locked():
        before_text = _read_config_text()
        cfg = json.loads(before_text)
        usable = entry_chains
        skipped = []
        new_ports = []
        if apply_chains:
            busy = {c["port"] for c in entry_chains if _port_busy(c["port"])}
            usable, skipped = chains.split_busy_chains(cfg, entry_chains, busy)
            new_ports = chains.new_ports_needed(cfg, usable)
        changed, problems = chains.sync_config(cfg, wanted, relay_wanted, usable, exit_nodes, apply_chains=apply_chains)
        if changed:
            _save(cfg)
            try:
                _reload_and_verify()
            except Exception:
                _restore_config_text(before_text)
                raise
            for port in new_ports:
                _open_firewall(port)
    return {"changed": changed, "new_ports": new_ports, "problems": skipped + problems}


def sync_node(node):
    wanted, relay_wanted, entry_chains, exit_nodes = desired_state(node)
    if node["kind"] == "managed":
        import nodeprov
        reconcile = nodeprov.remote_reconcile
        args = (node, wanted, relay_wanted, entry_chains, exit_nodes)
    elif node["kind"] == "local":
        reconcile = _reconcile_local
        args = (wanted, relay_wanted, entry_chains, exit_nodes)
    else:
        return {"changed": False, "new_ports": [], "problems": []}
    try:
        return reconcile(*args)
    except Exception as first_error:
        if not entry_chains:
            raise
        result = reconcile(*args, apply_chains=False)
        result["problems"].append(f"цепочки не применились, клиенты синхронизированы: {first_error}")
        return result


def sync_from_db():
    import db as dbmod

    expired = dbmod.deactivate_expired()
    node = dbmod.get_node("de1")
    result = sync_node(node)
    wanted = desired_state(node)[0]
    return {
        "removed_expired": len(expired), "active_now": len(wanted),
        "reloaded": result["changed"], "problems": result["problems"],
    }


def probe_from_node(node: dict, host: str, port: int):
    if node["kind"] == "local":
        return chains.tcp_connect_ms(host, port)
    if node["kind"] == "managed":
        import nodeprov
        return nodeprov.remote_probe(node, host, port)
    raise ValueError("нода не под управлением панели, замерить с неё нельзя")


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

    expired = dbmod.deactivate_expired()
    results = {}
    for node in dbmod.list_nodes():
        if node["kind"] == "local":
            results[node["code"]] = sync_from_db()
        elif node["kind"] == "managed":
            active = dbmod.list_active_subscriptions(node=node["code"])
            try:
                res = sync_node(node)
                results[node["code"]] = {
                    "active_now": len(active), "ok": True,
                    "changed": res["changed"], "problems": res["problems"],
                }
            except Exception as e:
                results[node["code"]] = {"active_now": len(active), "ok": False, "error": str(e)}
    reloaded = any(r.get("changed") or r.get("reloaded") for r in results.values())
    return {
        "removed_expired": len(expired), "active_now": len(dbmod.list_active_subscriptions()),
        "reloaded": reloaded, "nodes": results,
    }
