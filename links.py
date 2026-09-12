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


def build_subscription_text(subs: list[dict]) -> str:
    import db

    best_by_node = {}
    for s in subs:
        cur = best_by_node.get(s["node"])
        if cur is None or s["expires_at"] > cur["expires_at"]:
            best_by_node[s["node"]] = s

    nodes_by_code = {n["code"]: n for n in db.list_nodes()}
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
    raw = "\n".join(lines)
    return base64.b64encode(raw.encode()).decode()
