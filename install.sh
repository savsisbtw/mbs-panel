#!/bin/bash
set -e
set -o pipefail

MIRROR_URL="https://api.savsis.xyz/git/mbs-panel.git/"
REPO_URL="https://github.com/devsavsis/mbs-panel.git"
APP_DIR="/opt/mbs-panel"
WEBROOT="/var/www/certbot"

retry() {
  local n=1 max=3 delay=5
  until "$@"; do
    if [ "$n" -ge "$max" ]; then
      echo "команда не прошла после $max попыток: $*"
      return 1
    fi
    echo "попытка $n не прошла, повтор через ${delay}с..."
    n=$((n + 1))
    sleep "$delay"
  done
}

if [ "$(id -u)" != "0" ]; then
  echo "запусти от root: sudo bash install.sh"
  exit 1
fi

if [ -t 0 ]; then
  READ_TTY="/dev/tty"
else
  READ_TTY="/dev/stdin"
fi

ask() {
  local prompt="$1" default="$2" var
  read -r -p "$prompt${default:+ [$default]}: " var < "$READ_TTY"
  echo "${var:-$default}"
}

echo "== MBS Panel — установка =="
echo

. /etc/os-release
case "$ID" in
  ubuntu) [ "${VERSION_ID%%.*}" -lt 22 ] && echo "поддерживается Ubuntu 22.04+, но пробуем всё равно" ;;
  debian) [ "${VERSION_ID%%.*}" -lt 11 ] && echo "поддерживается Debian 11+, но пробуем всё равно" ;;
  *) echo "тестировалось на Ubuntu 22/24 и Debian 11/12, но пробуем всё равно на $PRETTY_NAME" ;;
esac

PANEL_DOMAIN=$(ask "Домен панели (админка)" "")
SUB_DOMAIN=$(ask "Домен подписок" "")
SITE_DOMAIN=$(ask "Домен сайта (для CORS и ссылок в боте)" "$PANEL_DOMAIN")
DE1_ADDRESS=$(ask "Домен этой же ноды (для VPN-клиентов, отдельный A-record)" "de1.$SITE_DOMAIN")
BOT_TOKEN=$(ask "Токен бота (от @BotFather)" "")
BOT_USERNAME=$(ask "Юзернейм бота (без @, с окончанием _bot/_robot)" "")
ADMIN_IDS=$(ask "Telegram ID админов через запятую" "")
REALITY_SNI=$(ask "SNI-маскировка для Reality (любой крупный сайт с TLS1.3)" "www.microsoft.com")

if [ -z "$PANEL_DOMAIN" ] || [ -z "$SUB_DOMAIN" ] || [ -z "$BOT_TOKEN" ] || [ -z "$ADMIN_IDS" ]; then
  echo "домен панели, домен подписок, токен бота и ID админов — обязательны"
  exit 1
fi

ADMIN_PANEL_PASSWORD=$(tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 14) || true

echo
echo "ставим пакеты..."
export DEBIAN_FRONTEND=noninteractive
retry apt-get update -qq
retry apt-get install -y -qq curl wget git openssl ufw fail2ban \
  python3 python3-venv python3-pip \
  nginx-full certbot > /dev/null

install_xray() {
  bash -c "$(curl -Ls https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install
}
if [ ! -f /usr/local/bin/xray ]; then
  echo "ставим Xray-core..."
  retry install_xray
fi

echo "клонируем репозиторий в $APP_DIR..."
if [ -d "$APP_DIR/.git" ]; then
  retry git -C "$APP_DIR" pull --quiet
else
  if ! git clone --quiet "$MIRROR_URL" "$APP_DIR" 2>/dev/null; then
    echo "зеркало недоступно, клонирую напрямую с GitHub..."
    retry git clone --quiet "$REPO_URL" "$APP_DIR"
    git -C "$APP_DIR" remote set-url origin "$MIRROR_URL"
  fi
  git -C "$APP_DIR" remote add github "$REPO_URL" 2>/dev/null || true
fi

cd "$APP_DIR"
python3 -m venv venv
retry venv/bin/pip install --quiet --upgrade pip
retry venv/bin/pip install --quiet -r requirements.txt

echo "генерируем Reality-ключи..."
KEYS=$(/usr/local/bin/xray x25519)
XRAY_PRIVATE_KEY=$(echo "$KEYS" | grep -i "Private" | awk '{print $NF}')
XRAY_PUBLIC_KEY=$(echo "$KEYS" | grep -i "Password\|Public" | awk '{print $NF}')
XRAY_SHORT_ID_TCP=$(openssl rand -hex 8)
XRAY_SHORT_ID_GRPC=$(openssl rand -hex 8)
XRAY_SHORT_ID_XHTTP=$(openssl rand -hex 8)

cat > "$APP_DIR/.env" << ENVEOF
BOT_TOKEN=$BOT_TOKEN
BOT_USERNAME=$BOT_USERNAME
ADMIN_IDS=$ADMIN_IDS
ADMIN_PANEL_PASSWORD=$ADMIN_PANEL_PASSWORD
PANEL_DOMAIN=$PANEL_DOMAIN
SUB_DOMAIN=$SUB_DOMAIN
SITE_DOMAIN=$SITE_DOMAIN
XRAY_PUBLIC_KEY=$XRAY_PUBLIC_KEY
REALITY_SNI=$REALITY_SNI
XRAY_SHORT_ID_TCP=$XRAY_SHORT_ID_TCP
XRAY_SHORT_ID_GRPC=$XRAY_SHORT_ID_GRPC
XRAY_SHORT_ID_XHTTP=$XRAY_SHORT_ID_XHTTP
DE1_ADDRESS=$DE1_ADDRESS
ENVEOF
chmod 600 "$APP_DIR/.env"

echo "открываем порт 80 для выпуска сертификатов..."
mkdir -p "$WEBROOT"
mkdir -p /etc/nginx/sites-available /etc/nginx/sites-enabled
rm -f /etc/nginx/sites-enabled/default

cat > /etc/nginx/sites-available/mbs-http80.conf << NGINXEOF
server {
    listen 80;
    listen [::]:80;
    server_name $PANEL_DOMAIN $SUB_DOMAIN $DE1_ADDRESS;

    location /.well-known/acme-challenge/ {
        root $WEBROOT;
    }
    location / {
        return 301 https://\$host\$request_uri;
    }
}

server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;
    location /.well-known/acme-challenge/ {
        root $WEBROOT;
    }
    location / {
        return 404;
    }
}
NGINXEOF
ln -sf /etc/nginx/sites-available/mbs-http80.conf /etc/nginx/sites-enabled/mbs-http80.conf

nginx -t && systemctl enable --now nginx && systemctl reload nginx

echo "выпускаем сертификаты..."
retry certbot certonly --webroot -w "$WEBROOT" --non-interactive --agree-tos \
  --register-unsafely-without-email -d "$PANEL_DOMAIN" -d "$SUB_DOMAIN"
retry certbot certonly --webroot -w "$WEBROOT" --non-interactive --agree-tos \
  --register-unsafely-without-email -d "$DE1_ADDRESS"

echo "готовлю серт для xray (он не root, letsencrypt/live ему не почитать)..."
mkdir -p /etc/xray/certs
cp "/etc/letsencrypt/live/$DE1_ADDRESS/fullchain.pem" /etc/xray/certs/de1.crt
cp "/etc/letsencrypt/live/$DE1_ADDRESS/privkey.pem" /etc/xray/certs/de1.key
chmod 644 /etc/xray/certs/de1.crt /etc/xray/certs/de1.key
chown nobody:nogroup /etc/xray/certs/de1.crt /etc/xray/certs/de1.key

mkdir -p /etc/letsencrypt/renewal-hooks/deploy
cat > /etc/letsencrypt/renewal-hooks/deploy/mbs-reload.sh << HOOKEOF
#!/bin/bash
if [ -d "/etc/letsencrypt/live/$DE1_ADDRESS" ]; then
  cp "/etc/letsencrypt/live/$DE1_ADDRESS/fullchain.pem" /etc/xray/certs/de1.crt
  cp "/etc/letsencrypt/live/$DE1_ADDRESS/privkey.pem" /etc/xray/certs/de1.key
  chmod 644 /etc/xray/certs/de1.crt /etc/xray/certs/de1.key
  chown nobody:nogroup /etc/xray/certs/de1.crt /etc/xray/certs/de1.key
fi
systemctl reload nginx || true
systemctl restart xray || true
HOOKEOF
chmod +x /etc/letsencrypt/renewal-hooks/deploy/mbs-reload.sh

echo "настраиваем nginx (SNI-роутер + бэкенды)..."
mkdir -p /etc/nginx/stream.d
if ! grep -q "^stream {" /etc/nginx/nginx.conf; then
  cat >> /etc/nginx/nginx.conf << 'STREAMEOF'

stream {
    include /etc/nginx/stream.d/*.conf;
}
STREAMEOF
fi

cat > /etc/nginx/stream.d/mbs.conf << STREAMCONFEOF
map \$ssl_preread_server_name \$mbs_backend {
    $PANEL_DOMAIN web_backend;
    $SUB_DOMAIN web_backend;
    $DE1_ADDRESS web_backend;
    default xray_reality;
}
upstream xray_reality { server 127.0.0.1:10443; }
upstream web_backend { server 127.0.0.1:8443; }

server {
    listen 443 reuseport;
    listen [::]:443 reuseport;
    proxy_pass \$mbs_backend;
    proxy_protocol on;
    ssl_preread on;
}
STREAMCONFEOF

cat > /etc/nginx/conf.d/mbs-ratelimit.conf << 'RLEOF'
limit_req_zone $binary_remote_addr zone=mbs_login:10m rate=5r/m;
RLEOF

cat > /etc/nginx/sites-available/mbs-https.conf << HTTPSEOF
server {
    listen 127.0.0.1:8443 ssl proxy_protocol;
    server_name $PANEL_DOMAIN;

    ssl_certificate     /etc/letsencrypt/live/$PANEL_DOMAIN/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$PANEL_DOMAIN/privkey.pem;
    set_real_ip_from 127.0.0.1;
    real_ip_header proxy_protocol;

    add_header Strict-Transport-Security "max-age=31536000" always;
    add_header X-Frame-Options "DENY" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header Referrer-Policy "same-origin" always;

    location /admin/api/login {
        limit_req zone=mbs_login burst=3 nodelay;
        proxy_pass http://127.0.0.1:8001;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
    }
    location / {
        proxy_pass http://127.0.0.1:8001;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
    }
}

server {
    listen 127.0.0.1:8443 ssl proxy_protocol;
    server_name $SUB_DOMAIN;

    ssl_certificate     /etc/letsencrypt/live/$PANEL_DOMAIN/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$PANEL_DOMAIN/privkey.pem;
    set_real_ip_from 127.0.0.1;
    real_ip_header proxy_protocol;

    add_header Strict-Transport-Security "max-age=31536000" always;
    add_header X-Frame-Options "DENY" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header Referrer-Policy "same-origin" always;

    location / {
        proxy_pass http://127.0.0.1:8001;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
    }
}

server {
    listen 127.0.0.1:8443 ssl proxy_protocol;
    server_name $DE1_ADDRESS;

    ssl_certificate     /etc/letsencrypt/live/$DE1_ADDRESS/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$DE1_ADDRESS/privkey.pem;
    set_real_ip_from 127.0.0.1;
    real_ip_header proxy_protocol;

    location / {
        return 204;
    }
}
HTTPSEOF
ln -sf /etc/nginx/sites-available/mbs-https.conf /etc/nginx/sites-enabled/mbs-https.conf

echo "пишем конфиг Xray..."
mkdir -p /usr/local/etc/xray
cat > /usr/local/etc/xray/config.json << XRAYEOF
{
  "log": { "loglevel": "warning" },
  "api": { "tag": "api", "services": ["HandlerService", "LoggerService", "StatsService"] },
  "stats": {},
  "policy": {
    "levels": { "0": { "statsUserUplink": true, "statsUserDownlink": true } },
    "system": {
      "statsInboundUplink": true, "statsInboundDownlink": true,
      "statsOutboundUplink": true, "statsOutboundDownlink": true
    }
  },
  "routing": {
    "rules": [ { "type": "field", "inboundTag": ["api"], "outboundTag": "api" } ]
  },
  "inbounds": [
    {
      "tag": "api", "listen": "127.0.0.1", "port": 10085,
      "protocol": "dokodemo-door", "settings": { "address": "127.0.0.1" }
    },
    {
      "tag": "vless-tcp-reality", "listen": "127.0.0.1", "port": 10443,
      "protocol": "vless",
      "settings": { "clients": [], "decryption": "none" },
      "sniffing": { "enabled": true, "destOverride": ["http", "tls"] },
      "streamSettings": {
        "network": "tcp", "security": "reality",
        "realitySettings": {
          "show": false, "dest": "$REALITY_SNI:443", "xver": 1,
          "serverNames": ["$REALITY_SNI"],
          "privateKey": "$XRAY_PRIVATE_KEY",
          "shortIds": ["$XRAY_SHORT_ID_TCP"]
        },
        "sockopt": { "acceptProxyProtocol": true }
      }
    },
    {
      "tag": "vless-grpc-reality", "listen": "0.0.0.0", "port": 2053,
      "protocol": "vless",
      "settings": { "clients": [], "decryption": "none" },
      "sniffing": { "enabled": true, "destOverride": ["http", "tls"] },
      "streamSettings": {
        "network": "grpc", "security": "reality",
        "realitySettings": {
          "show": false, "dest": "$REALITY_SNI:443", "xver": 0,
          "serverNames": ["$REALITY_SNI"],
          "privateKey": "$XRAY_PRIVATE_KEY",
          "shortIds": ["$XRAY_SHORT_ID_GRPC"]
        },
        "grpcSettings": { "serviceName": "mbs-grpc" }
      }
    },
    {
      "tag": "vless-xhttp-reality", "listen": "0.0.0.0", "port": 2087,
      "protocol": "vless",
      "settings": { "clients": [], "decryption": "none" },
      "sniffing": { "enabled": true, "destOverride": ["http", "tls"] },
      "streamSettings": {
        "network": "xhttp", "security": "reality",
        "realitySettings": {
          "show": false, "dest": "$REALITY_SNI:443", "xver": 0,
          "serverNames": ["$REALITY_SNI"],
          "privateKey": "$XRAY_PRIVATE_KEY",
          "shortIds": ["$XRAY_SHORT_ID_XHTTP"]
        },
        "xhttpSettings": { "path": "/mbs-xh", "mode": "auto" }
      }
    },
    {
      "tag": "vless-ws-tls", "listen": "0.0.0.0", "port": 8880,
      "protocol": "vless",
      "settings": { "clients": [], "decryption": "none" },
      "sniffing": { "enabled": true, "destOverride": ["http", "tls"] },
      "streamSettings": {
        "network": "ws", "security": "tls",
        "wsSettings": { "path": "/mbs-ws" },
        "tlsSettings": {
          "certificates": [{
            "certificateFile": "/etc/xray/certs/de1.crt",
            "keyFile": "/etc/xray/certs/de1.key"
          }]
        }
      }
    }
  ],
  "outbounds": [
    { "protocol": "freedom", "tag": "direct" },
    { "protocol": "blackhole", "tag": "block" }
  ]
}
XRAYEOF

echo "systemd-юниты..."
cp "$APP_DIR/systemd/mbs-bot.service" /etc/systemd/system/mbs-bot.service
cp "$APP_DIR/systemd/mbs-api.service" /etc/systemd/system/mbs-api.service
systemctl daemon-reload

echo "ставим CLI mbs..."
cp "$APP_DIR/mbs" /usr/local/bin/mbs
chmod +x /usr/local/bin/mbs

echo "фаервол..."
ufw allow 22/tcp > /dev/null || true
ufw allow 80/tcp > /dev/null || true
ufw allow 443/tcp > /dev/null || true
ufw allow 2053/tcp > /dev/null || true
ufw allow 2087/tcp > /dev/null || true
ufw allow 8880/tcp > /dev/null || true
ufw --force enable > /dev/null || true
systemctl enable --now fail2ban > /dev/null

nginx -t
systemctl reload nginx
systemctl enable --now xray
systemctl enable --now mbs-bot
systemctl enable --now mbs-api

sleep 2

if [ -z "$MBS_SKIP_STATS" ]; then
  curl -s -m 5 -X POST https://stats.api.savsis.xyz/install \
    -H "Content-Type: application/json" -d "{\"os\":\"$ID\"}" > /dev/null 2>&1 || true
fi

echo
echo "== готово =="
echo "Панель:    https://$PANEL_DOMAIN"
echo "Пароль:    $ADMIN_PANEL_PASSWORD  (сменить: mbs pass)"
echo "Подписки:  https://$SUB_DOMAIN"
echo "Нода:      $DE1_ADDRESS"
echo
echo "статус сервисов:"
mbs status || true
