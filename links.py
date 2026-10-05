import base64
import json
import re
import urllib.parse

from config import DE1_TRANSPORTS

TRANSPORT_NAMES = {
    "tcp": "TCP",
    "grpc": "gRPC",
    "xhttp": "XHTTP",
    "ws": "WS",
}


def display_name(node_label: str) -> str:
    return re.sub(r"\s*\([^)]*\)\s*$", "", node_label).strip()


def _tcp_reality_uri(client_uuid: str, address: str, port: int, public_key: str, short_id: str, sni: str, flow: str, remark: str) -> str:
    params = {
        "encryption": "none", "type": "tcp", "security": "reality",
        "sni": sni, "fp": "chrome", "pbk": public_key, "sid": short_id, "flow": flow or "xtls-rprx-vision",
    }
    qs = urllib.parse.urlencode(params)
    frag = urllib.parse.quote(remark)
    return f"vless://{client_uuid}@{address}:{port}?{qs}#{frag}"


def _transport_uris(client_uuid: str, transports: list[dict], base_name: str) -> list[str]:
    uris = []
    multi = len(transports) > 1
    for t in transports:
        params = {"encryption": "none", "type": t["network"]}
        if t["security"] == "reality":
            params.update({
                "security": "reality", "sni": t["sni"], "fp": "chrome",
                "pbk": t["public_key"], "sid": t["short_id"],
            })
            if t["network"] == "tcp":
                params["flow"] = t.get("flow") or "xtls-rprx-vision"
            elif t["network"] == "grpc":
                params["serviceName"] = t["service_name"]
                params["mode"] = "gun"
            elif t["network"] == "xhttp":
                params["path"] = t["path"]
                params["mode"] = "auto"
        elif t["security"] == "tls":
            params["security"] = "tls"
            params["sni"] = t["address"]
            params["fp"] = "chrome"
            params["alpn"] = "http/1.1"
            if t["network"] == "ws":
                params["path"] = t["path"]
                params["host"] = t["address"]
        qs = urllib.parse.urlencode(params)
        name = f"{base_name} ({TRANSPORT_NAMES.get(t['network'], t['network'])})" if multi else base_name
        frag = urllib.parse.quote(name)
        uris.append(f"vless://{client_uuid}@{t['address']}:{t['port']}?{qs}#{frag}")
    return uris


def hysteria_uri_for_node(node: dict, base_name: str) -> str | None:
    if not node or not node.get("hysteria_enabled") or not node.get("hysteria_password"):
        return None
    params = {
        "obfs": "salamander", "obfs-password": node["hysteria_obfs_password"],
        "insecure": "1", "sni": node["sni"],
    }
    qs = urllib.parse.urlencode(params)
    password = urllib.parse.quote(node["hysteria_password"], safe="")
    frag = urllib.parse.quote(f"{base_name} (Hysteria2)")
    return f"hysteria2://{password}@{node['address']}:{node['hysteria_port']}/?{qs}#{frag}"


def vless_uris_for_node(client_uuid: str, node: dict, base_name: str) -> list[str]:
    if not node or not node.get("enabled"):
        return []
    if node["code"] == "de1":
        return _transport_uris(client_uuid, DE1_TRANSPORTS, base_name)
    if node["kind"] == "managed" and node.get("transports_json"):
        transports = json.loads(node["transports_json"])
        if transports:
            return _transport_uris(client_uuid, transports, base_name)
    uuid_to_use = node["shared_uuid"] if node["kind"] == "external" and node.get("shared_uuid") else client_uuid
    return [_tcp_reality_uri(
        uuid_to_use, node["address"], node["port"], node["public_key"],
        node["short_id"], node["sni"], node.get("flow"), base_name,
    )]


def chain_remark(entry_node: dict, exit_node: dict) -> str:
    return f"{display_name(entry_node['label'])} → {display_name(exit_node['label'])}"


def chain_uri(client_uuid: str, entry_node: dict, chain: dict, remark: str) -> str:
    return _tcp_reality_uri(
        client_uuid, entry_node["address"], chain["port"], entry_node["public_key"],
        chain["short_id"], entry_node["sni"], "xtls-rprx-vision", remark,
    )


def _placeholder_text(first: str, second: str) -> str:
    import uuid as uuidlib
    from config import SITE_DOMAIN

    dead_uuid = str(uuidlib.uuid4())
    tcp = DE1_TRANSPORTS[0]
    lines = [
        _tcp_reality_uri(
            dead_uuid, tcp["address"], tcp["port"], tcp["public_key"],
            tcp["short_id"], tcp["sni"], tcp.get("flow"), first,
        ),
        _tcp_reality_uri(
            dead_uuid, tcp["address"], tcp["port"], tcp["public_key"],
            tcp["short_id"], tcp["sni"], tcp.get("flow"), second.format(site=SITE_DOMAIN),
        ),
    ]
    return base64.b64encode("\n".join(lines).encode()).decode()


def build_expired_placeholder_text() -> str:
    return _placeholder_text("❌ Подписка закончилась", "🔄 Продлить тут: {site}")


def build_device_limit_placeholder_text() -> str:
    return _placeholder_text("❌ Превышен лимит устройств", "🔄 Очистить тут: {site}")


def build_device_blocked_placeholder_text() -> str:
    return _placeholder_text("❌ Устройство отключено", "🔄 Переподключить: {site}")


def build_subscription_text(subs: list[dict]) -> str:
    import db
    import config
    import plugins

    if not subs:
        return build_expired_placeholder_text()

    best_by_node = {}
    if config.ALL_NODES_MODE:
        primary = max(subs, key=lambda s: s["expires_at"])
        for n in db.list_nodes(enabled_only=True):
            best_by_node[n["code"]] = primary
    else:
        for s in subs:
            cur = best_by_node.get(s["node"])
            if cur is None or s["expires_at"] > cur["expires_at"]:
                best_by_node[s["node"]] = s

    nodes_by_code = {n["code"]: n for n in db.list_nodes()}
    chains_by_entry = {}
    for chain in db.list_chains(enabled_only=True):
        chains_by_entry.setdefault(chain["entry_node"], []).append(chain)

    lines = []
    for node_code, s in best_by_node.items():
        node = nodes_by_code.get(node_code)
        if not node:
            continue
        base_name = display_name(node["label"])
        lines.extend(vless_uris_for_node(s["uuid"], node, base_name))
        hy = hysteria_uri_for_node(node, base_name)
        if hy:
            lines.append(hy)
        for chain in chains_by_entry.get(node_code, []):
            exit_node = nodes_by_code.get(chain["exit_node"])
            if not node["enabled"] or not exit_node or not exit_node["enabled"]:
                continue
            lines.append(chain_uri(s["uuid"], node, chain, chain_remark(node, exit_node)))
    lines = plugins.call("subscription_lines", lines, lines, subs, nodes_by_code)
    raw = "\n".join(lines)
    return base64.b64encode(raw.encode()).decode()
