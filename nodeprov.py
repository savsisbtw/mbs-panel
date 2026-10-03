import contextlib
import fcntl
import hashlib
import json
import secrets
import socket
import subprocess

import paramiko

import chains
from config import PANEL_DOMAIN

MGMT_KEY_PATH = "/root/.ssh/mbs_nodes_ed25519"
MGMT_KNOWN_HOSTS_PATH = "/root/.ssh/mbs_nodes_known_hosts"
REMOTE_CONFIG_PATH = "/usr/local/etc/xray/config.json"

ONE_COMMAND_TEMPLATE = "bash <(curl -Ls https://{panel}/install/{token}.sh)"

CERTBOT_SNIPPET = """echo "issuing a real TLS cert for {address} (needed for WS+TLS)..."
command -v certbot >/dev/null 2>&1 || apt-get install -y certbot
ss -ltnp | grep -q ':80 ' && {{ echo "something is already on port 80, stop it first"; exit 1; }}
certbot certonly --standalone --non-interactive --agree-tos --register-unsafely-without-email -d {address}
mkdir -p /etc/xray/certs
cp /etc/letsencrypt/live/{address}/fullchain.pem /etc/xray/certs/node.crt
cp /etc/letsencrypt/live/{address}/privkey.pem /etc/xray/certs/node.key
chmod 644 /etc/xray/certs/node.crt /etc/xray/certs/node.key
chown nobody:nogroup /etc/xray/certs/node.crt /etc/xray/certs/node.key
mkdir -p /etc/letsencrypt/renewal-hooks/deploy
cat > /etc/letsencrypt/renewal-hooks/deploy/mbs-restart-xray.sh << HOOK
#!/bin/bash
cp /etc/letsencrypt/live/{address}/fullchain.pem /etc/xray/certs/node.crt
cp /etc/letsencrypt/live/{address}/privkey.pem /etc/xray/certs/node.key
chmod 644 /etc/xray/certs/node.crt /etc/xray/certs/node.key
chown nobody:nogroup /etc/xray/certs/node.crt /etc/xray/certs/node.key
systemctl restart xray || true
HOOK
chmod +x /etc/letsencrypt/renewal-hooks/deploy/mbs-restart-xray.sh
"""

HYSTERIA_SNIPPET = """echo "installing Hysteria2..."
bash <(curl -fsSL https://get.hy2.sh/) || true
mkdir -p /etc/hysteria
openssl req -x509 -nodes -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -keyout /etc/hysteria/server.key -out /etc/hysteria/server.crt -subj "/CN={address}" -days 3650
cat > /etc/hysteria/config.yaml << 'HYCFG'
listen: :{hysteria_port}
tls:
  cert: /etc/hysteria/server.crt
  key: /etc/hysteria/server.key
auth:
  type: password
  password: "{hysteria_password}"
obfs:
  type: salamander
  salamander:
    password: "{hysteria_obfs_password}"
masquerade:
  type: proxy
  proxy:
    url: https://{sni}/
    rewriteHost: true
HYCFG
command -v ufw >/dev/null 2>&1 && ufw allow {hysteria_port}/udp || true
systemctl enable --now hysteria-server.service
sleep 1
echo "hysteria status: $(systemctl is-active hysteria-server.service)"
"""

SELF_INSTALL_SCRIPT = """#!/bin/bash
set -e
echo "== MBS Panel node install =="
export DEBIAN_FRONTEND=noninteractive

mkdir -p /root/.ssh
chmod 700 /root/.ssh
curl -Ls https://{panel}/mgmt-pubkey.txt >> /root/.ssh/authorized_keys
sort -u /root/.ssh/authorized_keys -o /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys
echo "management key installed"

if [ ! -f /usr/local/bin/xray ]; then
  bash -c "$(curl -Ls https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install
fi

{certbot_block}
mkdir -p /usr/local/etc/xray
cat > /usr/local/etc/xray/config.json << 'XRAYCFG'
{config_json}
XRAYCFG

command -v ufw >/dev/null 2>&1 && {{ ufw allow 22/tcp || true; {ufw_rules} }}
systemctl enable xray >/dev/null 2>&1 || true
systemctl restart xray
sleep 1
STATUS=$(systemctl is-active xray)
echo "xray status: $STATUS"

{hysteria_block}
MY_IP=$(curl -s https://api.ipify.org || echo unknown)
curl -s -X POST "https://{panel}/nodes/register/{token}" -H "Content-Type: application/json" -d "{{\\"ip\\":\\"$MY_IP\\",\\"status\\":\\"$STATUS\\"}}" >/dev/null

if [ "$STATUS" = "active" ]; then
  echo "Done. Node registered — check the panel."
else
  echo "xray did not start, check: journalctl -u xray -n 50"
fi
"""


def ensure_mgmt_key() -> str:
    try:
        with open(MGMT_KEY_PATH + ".pub") as f:
            return f.read().strip()
    except FileNotFoundError:
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-f", MGMT_KEY_PATH, "-N", "", "-C", "mbs-node-mgmt"],
            check=True, capture_output=True,
        )
        with open(MGMT_KEY_PATH + ".pub") as f:
            return f.read().strip()


def generate_reality_keys():
    out = subprocess.run(["/usr/local/bin/xray", "x25519"], check=True, capture_output=True, text=True).stdout
    private_key = None
    public_key = None
    for line in out.splitlines():
        if line.startswith("PrivateKey:"):
            private_key = line.split(":", 1)[1].strip()
        elif line.startswith("Password (PublicKey):") or line.startswith("PublicKey:"):
            public_key = line.split(":", 1)[1].strip()
    return private_key, public_key


def generate_short_id() -> str:
    return secrets.token_hex(8)


def generate_hysteria_credentials():
    return secrets.token_urlsafe(16), secrets.token_urlsafe(12)


def build_transports(address, tcp_port, sni, public_key, include_ws=False):
    transports = [
        {
            "tag": "vless-tcp-reality", "label": "TCP + Reality (основной)",
            "network": "tcp", "security": "reality", "address": address, "port": tcp_port,
            "public_key": public_key, "short_id": generate_short_id(), "sni": sni,
            "flow": "xtls-rprx-vision",
        },
        {
            "tag": "vless-grpc-reality", "label": "gRPC + Reality",
            "network": "grpc", "security": "reality", "address": address, "port": 2053,
            "public_key": public_key, "short_id": generate_short_id(), "sni": sni,
            "service_name": "mbs-grpc",
        },
        {
            "tag": "vless-xhttp-reality", "label": "XHTTP + Reality",
            "network": "xhttp", "security": "reality", "address": address, "port": 2087,
            "public_key": public_key, "short_id": generate_short_id(), "sni": sni,
            "path": "/mbs-xh",
        },
    ]
    if include_ws:
        transports.append({
            "tag": "vless-ws-tls", "label": "WebSocket + TLS (реальный серт)",
            "network": "ws", "security": "tls", "address": address, "port": 8880,
            "path": "/mbs-ws",
        })
    return transports


def one_command(token: str) -> str:
    return ONE_COMMAND_TEMPLATE.format(panel=PANEL_DOMAIN, token=token)


def _build_config_json(transports, private_key, address):
    inbounds = [{
        "tag": "api", "listen": "127.0.0.1", "port": 10085,
        "protocol": "dokodemo-door", "settings": {"address": "127.0.0.1"},
    }]
    for t in transports:
        ib = {
            "tag": t["tag"], "listen": "0.0.0.0", "port": t["port"],
            "protocol": "vless",
            "settings": {"clients": [], "decryption": "none"},
            "sniffing": {"enabled": True, "destOverride": ["http", "tls"]},
        }
        if t["security"] == "reality":
            ib["streamSettings"] = {"network": t["network"], "security": "reality", "realitySettings": {
                "show": False, "dest": f"{t['sni']}:443", "xver": 0,
                "serverNames": [t["sni"]], "privateKey": private_key, "shortIds": [t["short_id"]],
            }}
            if t["network"] == "grpc":
                ib["streamSettings"]["grpcSettings"] = {"serviceName": t["service_name"]}
            elif t["network"] == "xhttp":
                ib["streamSettings"]["xhttpSettings"] = {"path": t["path"], "mode": "auto"}
        elif t["security"] == "tls":
            ib["streamSettings"] = {"network": "ws", "security": "tls", "wsSettings": {"path": t["path"]},
                "tlsSettings": {"certificates": [{
                    "certificateFile": "/etc/xray/certs/node.crt",
                    "keyFile": "/etc/xray/certs/node.key",
                }]}}
        inbounds.append(ib)

    cfg = {
        "log": {"loglevel": "warning"},
        "api": {"tag": "api", "services": ["HandlerService", "LoggerService", "StatsService"]},
        "stats": {},
        "policy": {
            "levels": {"0": {"statsUserUplink": True, "statsUserDownlink": True}},
            "system": {"statsInboundUplink": True, "statsInboundDownlink": True, "statsOutboundUplink": True, "statsOutboundDownlink": True},
        },
        "routing": {"rules": [{"type": "field", "inboundTag": ["api"], "outboundTag": "api"}]},
        "inbounds": inbounds,
        "outbounds": [{"protocol": "freedom", "tag": "direct"}, {"protocol": "blackhole", "tag": "block"}],
    }
    return json.dumps(cfg, indent=2)


def render_install_script(node: dict) -> str:
    transports = json.loads(node["transports_json"])
    config_json = _build_config_json(transports, node["private_key"], node["address"])
    by_tag = {t["tag"]: t for t in transports}
    has_ws = "vless-ws-tls" in by_tag

    ufw_rules = " ".join(f"ufw allow {t['port']}/tcp || true;" for t in transports)
    certbot_block = CERTBOT_SNIPPET.format(address=node["address"]) if has_ws else ""
    hysteria_block = ""
    if node.get("hysteria_enabled"):
        hysteria_block = HYSTERIA_SNIPPET.format(
            address=node["address"], hysteria_port=node["hysteria_port"],
            hysteria_password=node["hysteria_password"], hysteria_obfs_password=node["hysteria_obfs_password"],
            sni=node["sni"],
        )

    return SELF_INSTALL_SCRIPT.format(
        panel=PANEL_DOMAIN, token=node["provision_token"], config_json=config_json,
        certbot_block=certbot_block, hysteria_block=hysteria_block, ufw_rules=ufw_rules,
    )


def _mgmt_connect(address: str, ssh_port: int = 22) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    try:
        client.load_host_keys(MGMT_KNOWN_HOSTS_PATH)
    except IOError:
        pass
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    key = paramiko.Ed25519Key.from_private_key_file(MGMT_KEY_PATH)
    client.connect(address, port=ssh_port, username="root", pkey=key, timeout=15, banner_timeout=15, auth_timeout=15)
    client.save_host_keys(MGMT_KNOWN_HOSTS_PATH)
    return client


class RemoteConfigError(Exception):
    pass


def _busy_ports(client) -> set:
    _, stdout, _ = client.exec_command("ss -ltnH 2>/dev/null | awk '{print $4}'", timeout=10)
    busy = set()
    for line in stdout.read().decode(errors="replace").splitlines():
        tail = line.rsplit(":", 1)[-1]
        if tail.isdigit():
            busy.add(int(tail))
    return busy


@contextlib.contextmanager
def _node_lock(address: str):
    key = hashlib.sha1(address.encode()).hexdigest()[:12]
    with open(f"/tmp/mbs-node-{key}.lock", "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def _remote_edit_config(node: dict, mutate_fn):
    with _node_lock(node["address"]):
        return _remote_edit_config_locked(node, mutate_fn)


def _remote_edit_config_locked(node: dict, mutate_fn):
    client = _mgmt_connect(node["address"])
    try:
        sftp = client.open_sftp()
        with sftp.open(REMOTE_CONFIG_PATH) as f:
            cfg = json.loads(f.read().decode())
        result = mutate_fn(cfg, client)
        if not result["changed"]:
            sftp.close()
            return result
        data = json.dumps(cfg, indent=2).encode()
        tmp_path = REMOTE_CONFIG_PATH + ".validate.tmp"
        prev_path = REMOTE_CONFIG_PATH + ".mbs-prev"
        with sftp.open(tmp_path, "wb") as f:
            f.write(data)
        _, stdout, stderr = client.exec_command(f"/usr/local/bin/xray run -test -format=json -config {tmp_path}", timeout=15)
        test_exit = stdout.channel.recv_exit_status()
        test_out = (stdout.read().decode(errors="replace") + stderr.read().decode(errors="replace")).strip()
        if test_exit != 0:
            client.exec_command(f"rm -f {tmp_path}")
            sftp.close()
            raise RemoteConfigError(f"config test failed on {node['address']}: {test_out}")
        client.exec_command(f"cp -p {REMOTE_CONFIG_PATH} {prev_path}")[1].channel.recv_exit_status()
        client.exec_command(f"mv {tmp_path} {REMOTE_CONFIG_PATH}")[1].channel.recv_exit_status()
        sftp.close()
        _, stdout, stderr = client.exec_command("systemctl restart xray && sleep 1 && systemctl is-active xray", timeout=30)
        restart_exit = stdout.channel.recv_exit_status()
        if restart_exit != 0:
            err = stderr.read().decode(errors="replace").strip()
            client.exec_command(f"cp -p {prev_path} {REMOTE_CONFIG_PATH} && systemctl restart xray")[1].channel.recv_exit_status()
            raise RemoteConfigError(f"xray не поднялся на {node['address']}, конфиг откатили назад: {err}")
        for port in result.get("new_ports") or []:
            client.exec_command(f"command -v ufw >/dev/null 2>&1 && ufw allow {int(port)}/tcp || true")[1].channel.recv_exit_status()
        return result
    finally:
        client.close()


def _remote_edit_clients(node: dict, mutate_fn):
    def mutate(cfg, client):
        changed = False
        for ib in cfg["inbounds"]:
            tag = ib.get("tag")
            if not chains.is_user_tag(tag):
                continue
            new_clients = mutate_fn(ib["settings"]["clients"], tag)
            if new_clients is not None:
                ib["settings"]["clients"] = new_clients
                changed = True
        return {"changed": changed}
    _remote_edit_config(node, mutate)


def remote_add_client(node: dict, client_uuid: str, email: str):
    def mutate(clients, tag):
        if any(c["id"] == client_uuid for c in clients):
            return None
        entry = {"id": client_uuid, "email": email}
        flow = chains.flow_for_tag(tag)
        if flow:
            entry["flow"] = flow
        clients.append(entry)
        return clients
    _remote_edit_clients(node, mutate)


def remote_remove_client(node: dict, client_uuid: str):
    def mutate(clients, tag):
        new = [c for c in clients if c["id"] != client_uuid]
        return new if len(new) != len(clients) else None
    _remote_edit_clients(node, mutate)


def remote_reconcile(node: dict, wanted: dict, relay_wanted: dict, entry_chains: list, exit_nodes: dict, apply_chains: bool = True):
    def mutate(cfg, client):
        usable = entry_chains
        skipped = []
        if apply_chains:
            usable, skipped = chains.split_busy_chains(cfg, entry_chains, _busy_ports(client))
        new_ports = chains.new_ports_needed(cfg, usable) if apply_chains else []
        changed, problems = chains.sync_config(cfg, wanted, relay_wanted, usable, exit_nodes, apply_chains=apply_chains)
        return {"changed": changed, "new_ports": new_ports, "problems": skipped + problems}
    return _remote_edit_config(node, mutate)


PROBE_SCRIPT = """for i in 1 2 3; do
  s=$(date +%s%N)
  if timeout 3 bash -c 'exec 3<>/dev/tcp/{host}/{port}' 2>/dev/null; then
    e=$(date +%s%N)
    echo $(( (e - s) / 1000000 ))
  else
    echo -1
  fi
done
"""


def remote_probe(node: dict, host: str, port: int) -> list:
    if not chains.HOST_RE.match(host or ""):
        raise ValueError("bad host")
    port = int(port)
    client = _mgmt_connect(node["address"])
    try:
        stdin, stdout, _ = client.exec_command("bash -s", timeout=30)
        stdin.write(PROBE_SCRIPT.format(host=host, port=port))
        stdin.channel.shutdown_write()
        out = stdout.read().decode(errors="replace")
    finally:
        client.close()
    samples = []
    for line in out.split():
        try:
            samples.append(int(line))
        except ValueError:
            continue
    return samples


def remote_query_stats(node: dict) -> dict:
    try:
        client = _mgmt_connect(node["address"])
        try:
            _, stdout, _ = client.exec_command(
                "/usr/local/bin/xray api statsquery --server=127.0.0.1:10085 -pattern 'user>>>'", timeout=10,
            )
            out = stdout.read().decode(errors="replace")
        finally:
            client.close()
        data = json.loads(out) if out.strip() else {"stat": []}
    except Exception:
        return {}
    result = {}
    for entry in data.get("stat") or []:
        parts = entry.get("name", "").split(">>>")
        if len(parts) != 4 or parts[0] != "user":
            continue
        _, email, _, direction = parts
        row = result.setdefault(email, {"up": 0, "down": 0})
        value = entry.get("value", 0)
        if direction == "uplink":
            row["up"] += value
        elif direction == "downlink":
            row["down"] += value
    return result


def remote_reset_stats(node: dict, email: str) -> bool:
    try:
        client = _mgmt_connect(node["address"])
        try:
            client.exec_command(
                f"/usr/local/bin/xray api statsquery --server=127.0.0.1:10085 "
                f"-pattern 'user>>>{email}>>>traffic' -reset",
                timeout=10,
            )
        finally:
            client.close()
        return True
    except Exception:
        return False


STATUS_CMD = (
    "echo LOAD:$(cut -d' ' -f1 /proc/loadavg); "
    "echo MEMTOTAL:$(grep MemTotal /proc/meminfo | awk '{print $2}'); "
    "echo MEMAVAIL:$(grep MemAvailable /proc/meminfo | awk '{print $2}'); "
    "echo UPTIME:$(cut -d'.' -f1 /proc/uptime); "
    "echo XRAY:$(systemctl is-active xray)"
)


def remote_node_status(node: dict) -> dict:
    try:
        client = _mgmt_connect(node["address"])
        try:
            _, stdout, _ = client.exec_command(STATUS_CMD, timeout=10)
            out = stdout.read().decode(errors="replace")
        finally:
            client.close()
        vals = {}
        for line in out.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                vals[k] = v.strip()
        mem_total = int(vals["MEMTOTAL"]) // 1024 if vals.get("MEMTOTAL", "").isdigit() else None
        mem_avail = int(vals["MEMAVAIL"]) // 1024 if vals.get("MEMAVAIL", "").isdigit() else None
        return {
            "ok": True,
            "load1": float(vals["LOAD"]) if vals.get("LOAD") else None,
            "mem_used_mb": (mem_total - mem_avail) if (mem_total is not None and mem_avail is not None) else None,
            "mem_total_mb": mem_total,
            "uptime_s": int(vals["UPTIME"]) if vals.get("UPTIME", "").lstrip("-").isdigit() else None,
            "xray_active": vals.get("XRAY") == "active",
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def check_node_alive(address: str, port: int, timeout: float = 5.0) -> bool:
    try:
        with socket.create_connection((address, port), timeout=timeout):
            return True
    except OSError:
        return False
