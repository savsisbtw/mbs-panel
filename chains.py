import copy
import json
import re
import socket
import time

BASE_TAGS = ("vless-tcp-reality", "vless-grpc-reality", "vless-xhttp-reality", "vless-ws-tls")
TCP_TAG = "vless-tcp-reality"
VISION = "xtls-rprx-vision"
CHAIN_PREFIX = "chain-"
RELAY_EMAIL_PREFIX = "relay-"
MAX_SERVERS = 2
PORT_MIN = 10443
PORT_MAX = 10999
CODE_RE = re.compile(r"^[a-z0-9]{1,16}$")
HOST_RE = re.compile(r"^[A-Za-z0-9.-]{1,253}$")
CHAIN_KINDS_ENTRY = ("local", "managed")
CHAIN_KINDS_EXIT = ("local", "managed", "external")


class ChainConfigError(Exception):
    pass


def inbound_tag(code):
    return CHAIN_PREFIX + code


def outbound_tag(code):
    return CHAIN_PREFIX + code + "-out"


def relay_email(code):
    return RELAY_EMAIL_PREFIX + code


def is_chain_inbound_tag(tag):
    return bool(tag) and tag.startswith(CHAIN_PREFIX) and not tag.endswith("-out")


def is_user_tag(tag):
    return tag in BASE_TAGS or is_chain_inbound_tag(tag)


def flow_for_tag(tag):
    if tag == TCP_TAG or is_chain_inbound_tag(tag):
        return VISION
    return None


def sync_clients(clients, wanted, flow):
    kept = []
    seen = set()
    for c in clients:
        cid = c.get("id")
        if cid in wanted and cid not in seen:
            kept.append(c)
            seen.add(cid)
    for cid in wanted:
        if cid in seen:
            continue
        entry = {"id": cid, "email": wanted[cid]}
        if flow:
            entry["flow"] = flow
        kept.append(entry)
    return kept


def find_inbound(cfg, tag):
    for ib in cfg.get("inbounds", []):
        if ib.get("tag") == tag:
            return ib
    return None


def build_chain_inbound(template, chain, wanted, old_clients):
    reality = (template.get("streamSettings") or {}).get("realitySettings")
    if not reality:
        raise ChainConfigError("у входной ноды нет TCP+Reality inbound — цепочку строить не из чего")
    ib = copy.deepcopy(template)
    ib["tag"] = inbound_tag(chain["code"])
    ib["port"] = chain["port"]
    ib["streamSettings"]["realitySettings"]["shortIds"] = [chain["short_id"]]
    ib["settings"]["clients"] = sync_clients(old_clients, wanted, VISION)
    return ib


def build_chain_outbound(chain, exit_node, relay_uuid):
    return {
        "tag": outbound_tag(chain["code"]),
        "protocol": "vless",
        "settings": {
            "vnext": [{
                "address": exit_node["address"],
                "port": int(exit_node["port"]),
                "users": [{"id": relay_uuid, "encryption": "none", "flow": VISION}],
            }],
        },
        "streamSettings": {
            "network": "tcp",
            "security": "reality",
            "realitySettings": {
                "serverName": exit_node["sni"],
                "fingerprint": "chrome",
                "publicKey": exit_node["public_key"],
                "shortId": exit_node["short_id"],
                "spiderX": "",
            },
        },
    }


def build_chain_rule(chain):
    return {
        "type": "field",
        "inboundTag": [inbound_tag(chain["code"])],
        "outboundTag": outbound_tag(chain["code"]),
    }


def relay_for_chain(chain, exit_node):
    if exit_node["kind"] == "external":
        return exit_node.get("shared_uuid")
    return chain.get("relay_uuid")


def split_busy_chains(cfg, entry_chains, busy_ports):
    new_ports = set(new_ports_needed(cfg, entry_chains))
    usable = []
    problems = []
    for chain in entry_chains:
        if chain["port"] in new_ports and chain["port"] in busy_ports:
            problems.append(f"{chain['code']}: порт {chain['port']} уже занят другим процессом, цепочка не применена")
            continue
        usable.append(chain)
    return usable, problems


def sync_config(cfg, wanted, relay_wanted, entry_chains, exit_nodes, apply_chains=True):
    before = json.dumps(cfg, sort_keys=True)
    problems = []

    template = find_inbound(cfg, TCP_TAG)

    for ib in cfg["inbounds"]:
        tag = ib.get("tag")
        if not is_user_tag(tag):
            continue
        want = dict(wanted)
        if tag == TCP_TAG:
            want.update(relay_wanted)
        ib["settings"]["clients"] = sync_clients(ib["settings"]["clients"], want, flow_for_tag(tag))

    if not apply_chains:
        changed = json.dumps(cfg, sort_keys=True) != before
        return changed, problems

    old_chain_inbounds = {}
    for ib in cfg["inbounds"]:
        if is_chain_inbound_tag(ib.get("tag")):
            old_chain_inbounds[ib["tag"]] = ib

    kept_inbounds = [ib for ib in cfg["inbounds"] if not is_chain_inbound_tag(ib.get("tag"))]
    kept_outbounds = [ob for ob in cfg.get("outbounds", []) if not (ob.get("tag") or "").startswith(CHAIN_PREFIX)]
    routing = cfg.setdefault("routing", {})
    kept_rules = [r for r in routing.get("rules", []) if not (r.get("outboundTag") or "").startswith(CHAIN_PREFIX)]

    for chain in entry_chains:
        exit_node = exit_nodes.get(chain["exit_node"])
        if template is None:
            problems.append(f"{chain['code']}: нет TCP+Reality inbound на входной ноде")
            continue
        if not exit_node:
            problems.append(f"{chain['code']}: выходная нода не найдена")
            continue
        relay_uuid = relay_for_chain(chain, exit_node)
        if not relay_uuid:
            problems.append(f"{chain['code']}: у выходной ноды нет ключа для цепочки")
            continue
        old = old_chain_inbounds.get(inbound_tag(chain["code"]))
        old_clients = old["settings"]["clients"] if old else []
        kept_inbounds.append(build_chain_inbound(template, chain, wanted, old_clients))
        kept_outbounds.append(build_chain_outbound(chain, exit_node, relay_uuid))
        kept_rules.append(build_chain_rule(chain))

    cfg["inbounds"] = kept_inbounds
    cfg["outbounds"] = kept_outbounds
    routing["rules"] = kept_rules

    changed = json.dumps(cfg, sort_keys=True) != before
    return changed, problems


def new_ports_needed(cfg, entry_chains):
    existing = set()
    for ib in cfg.get("inbounds", []):
        if is_chain_inbound_tag(ib.get("tag")):
            existing.add(ib["tag"])
    ports = []
    for chain in entry_chains:
        if inbound_tag(chain["code"]) not in existing:
            ports.append(chain["port"])
    return ports


def tcp_connect_ms(host, port, samples=3, timeout=3.0):
    results = []
    for _ in range(samples):
        start = time.perf_counter()
        try:
            with socket.create_connection((host, int(port)), timeout=timeout):
                pass
            results.append(round((time.perf_counter() - start) * 1000))
        except OSError:
            results.append(-1)
    return results


def median_ms(samples):
    good = sorted(s for s in samples if s >= 0)
    if not good:
        return None
    return good[len(good) // 2]


def latency_level(rtt_ms):
    if rtt_ms is None:
        return "unknown"
    if rtt_ms < 40:
        return "low"
    if rtt_ms < 120:
        return "medium"
    return "high"
