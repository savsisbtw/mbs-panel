import base64
import json
import urllib.parse

import links

URL_TEST = "https://www.gstatic.com/generate_204"


def uris_from_subscription_text(encoded: str) -> list:
    if not encoded:
        return []
    raw = base64.b64decode(encoded.encode()).decode()
    return [line for line in raw.split("\n") if line.strip()]


def _query(parsed) -> dict:
    return {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}


def parse_uri(uri: str):
    parsed = urllib.parse.urlparse(uri)
    name = urllib.parse.unquote(parsed.fragment) or parsed.hostname
    q = _query(parsed)
    if parsed.scheme == "vless":
        return {
            "kind": "vless", "name": name, "uuid": urllib.parse.unquote(parsed.username or ""),
            "server": parsed.hostname, "port": parsed.port, "network": q.get("type", "tcp"),
            "security": q.get("security", "none"), "sni": q.get("sni", ""), "fp": q.get("fp", "chrome"),
            "pbk": q.get("pbk", ""), "sid": q.get("sid", ""), "flow": q.get("flow", ""),
            "service_name": q.get("serviceName", ""), "path": q.get("path", ""), "host": q.get("host", ""),
        }
    if parsed.scheme == "hysteria2":
        return {
            "kind": "hysteria2", "name": name, "password": urllib.parse.unquote(parsed.username or ""),
            "server": parsed.hostname, "port": parsed.port, "sni": q.get("sni", ""),
            "obfs": q.get("obfs", ""), "obfs_password": q.get("obfs-password", ""),
        }
    return None


def parse_all(uris: list) -> list:
    out = []
    for uri in uris:
        item = parse_uri(uri)
        if item:
            out.append(item)
    return out


def _clash_proxy(p: dict):
    if p["kind"] == "hysteria2":
        proxy = {
            "name": p["name"], "type": "hysteria2", "server": p["server"], "port": p["port"],
            "password": p["password"], "sni": p["sni"], "skip-cert-verify": True,
        }
        if p["obfs"]:
            proxy["obfs"] = p["obfs"]
            proxy["obfs-password"] = p["obfs_password"]
        return proxy
    if p["network"] == "xhttp":
        return None
    proxy = {
        "name": p["name"], "type": "vless", "server": p["server"], "port": p["port"],
        "uuid": p["uuid"], "network": p["network"], "udp": True, "tls": p["security"] in ("reality", "tls"),
        "servername": p["sni"], "client-fingerprint": p["fp"] or "chrome",
    }
    if p["flow"]:
        proxy["flow"] = p["flow"]
    if p["security"] == "reality":
        proxy["reality-opts"] = {"public-key": p["pbk"], "short-id": p["sid"]}
    if p["network"] == "grpc":
        proxy["grpc-opts"] = {"grpc-service-name": p["service_name"]}
    if p["network"] == "ws":
        proxy["ws-opts"] = {"path": p["path"], "headers": {"Host": p["host"] or p["sni"]}}
    return proxy


def build_clash(uris: list, ru_direct: bool = False) -> str:
    proxies = [x for x in (_clash_proxy(p) for p in parse_all(uris)) if x]
    names = [x["name"] for x in proxies]
    rules = ["GEOIP,PRIVATE,DIRECT,no-resolve"]
    if ru_direct:
        rules += ["GEOSITE,category-ru,DIRECT", "GEOIP,RU,DIRECT"]
    rules.append("MATCH,PROXY")
    config = {
        "mixed-port": 7890,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "warning",
        "proxies": proxies,
        "proxy-groups": [
            {"name": "PROXY", "type": "select", "proxies": ["AUTO"] + names},
            {"name": "AUTO", "type": "url-test", "proxies": names, "url": URL_TEST, "interval": 300},
        ],
        "rules": rules,
    }
    return json.dumps(config, ensure_ascii=False, indent=2)


def _singbox_outbound(p: dict, tag: str):
    if p["kind"] == "hysteria2":
        out = {
            "type": "hysteria2", "tag": tag, "server": p["server"], "server_port": p["port"],
            "password": p["password"],
            "tls": {"enabled": True, "server_name": p["sni"], "insecure": True},
        }
        if p["obfs"]:
            out["obfs"] = {"type": p["obfs"], "password": p["obfs_password"]}
        return out
    if p["network"] == "xhttp":
        return None
    out = {"type": "vless", "tag": tag, "server": p["server"], "server_port": p["port"], "uuid": p["uuid"]}
    if p["flow"]:
        out["flow"] = p["flow"]
    if p["security"] in ("reality", "tls"):
        tls = {
            "enabled": True, "server_name": p["sni"],
            "utls": {"enabled": True, "fingerprint": p["fp"] or "chrome"},
        }
        if p["security"] == "reality":
            tls["reality"] = {"enabled": True, "public_key": p["pbk"], "short_id": p["sid"]}
        out["tls"] = tls
    if p["network"] == "grpc":
        out["transport"] = {"type": "grpc", "service_name": p["service_name"]}
    if p["network"] == "ws":
        out["transport"] = {"type": "ws", "path": p["path"], "headers": {"Host": p["host"] or p["sni"]}}
    return out


def build_singbox(uris: list) -> str:
    outbounds = []
    tags = []
    for p in parse_all(uris):
        out = _singbox_outbound(p, p["name"])
        if out:
            outbounds.append(out)
            tags.append(p["name"])
    config = {
        "log": {"level": "warn"},
        "inbounds": [{"type": "mixed", "tag": "mixed-in", "listen": "127.0.0.1", "listen_port": 2080}],
        "outbounds": [
            {"type": "selector", "tag": "proxy", "outbounds": ["auto"] + tags, "default": "auto"},
            {"type": "urltest", "tag": "auto", "outbounds": tags, "url": URL_TEST, "interval": "5m"},
        ] + outbounds + [{"type": "direct", "tag": "direct"}],
        "route": {
            "rules": [{"ip_is_private": True, "outbound": "direct"}],
            "final": "proxy",
        },
    }
    return json.dumps(config, ensure_ascii=False, indent=2)


def detect_format(user_agent: str, explicit: str) -> str:
    explicit = (explicit or "").strip().lower()
    if explicit in ("clash", "singbox", "base64"):
        return explicit
    ua = (user_agent or "").lower()
    if any(m in ua for m in ("clash", "stash", "mihomo")):
        return "clash"
    if any(m in ua for m in ("sing-box", "singbox")):
        return "singbox"
    return "base64"


def render(subs: list, fmt: str, ru_direct: bool = False):
    encoded = links.build_subscription_text(subs)
    if fmt == "base64":
        return encoded, "text/plain"
    uris = uris_from_subscription_text(encoded)
    if fmt == "clash":
        return build_clash(uris, ru_direct), "text/yaml"
    return build_singbox(uris), "application/json"
