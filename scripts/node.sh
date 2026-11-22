#!/usr/bin/env bash
# VPN Seller Node — turns a clean Ubuntu/Debian server into a VPN server of the shop.
#
#   curl -fsSL https://YOUR-SHOP/scripts/node.sh -o node.sh && sudo bash node.sh --shop https://YOUR-SHOP
#
# What it does: fixes DNS if it is broken, turns on BBR, installs Xray
# (official XTLS installer), installs the agent from the shop, makes a
# VLESS Reality server on 443 (TCP) and 8443 (XHTTP) and prints ONE line
# to paste into the shop: Servers → Add → Own server.
#
# Safe to run again: keys, customers and the token are kept.
#
# Options:
#   --shop URL        where to take the agent from (the shop that shows this command)
#   --sni NAME        the site Reality borrows (default dl.google.com)
#   --port N          the agent's own port (default: a random free one)
#   --tcp-port N      VLESS TCP port (default 443)
#   --xhttp-port N    VLESS XHTTP port (default 8443)
#   --takeover        stop and disable 3x-ui / x-ui if it holds the ports
#   --no-tune         do not touch the kernel network settings
set -euo pipefail

SHOP=""; SNI="dl.google.com"; APORT=""; TCP_PORT=443; XHTTP_PORT=8443; TAKEOVER=0; TUNE=1
while [ $# -gt 0 ]; do
  case "$1" in
    --shop) SHOP="${2%/}"; shift 2 ;;
    --sni) SNI="$2"; shift 2 ;;
    --port) APORT="$2"; shift 2 ;;
    --tcp-port) TCP_PORT="$2"; shift 2 ;;
    --xhttp-port) XHTTP_PORT="$2"; shift 2 ;;
    --takeover) TAKEOVER=1; shift ;;
    --no-tune) TUNE=0; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mОшибка:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "запустите от root: sudo bash node.sh"
command -v apt-get >/dev/null || die "нужен Ubuntu или Debian"

DIR=/opt/vpn-seller-node
STATE=/etc/vpn-seller-node
mkdir -p "$DIR" "$STATE"
chmod 700 "$STATE"

# ---------------------------------------------------------------- DNS
if ! getent hosts github.com >/dev/null 2>&1; then
  warn "DNS не отвечает — ставлю 1.1.1.1 и 8.8.8.8"
  if [ -f /etc/systemd/resolved.conf ]; then
    sed -i -E 's/^#?DNS=.*/DNS=1.1.1.1 8.8.8.8/; s/^#?FallbackDNS=.*/FallbackDNS=9.9.9.9 1.0.0.1/' /etc/systemd/resolved.conf
    grep -q '^DNS=' /etc/systemd/resolved.conf || echo 'DNS=1.1.1.1 8.8.8.8' >> /etc/systemd/resolved.conf
    systemctl restart systemd-resolved || true
  else
    printf 'nameserver 1.1.1.1\nnameserver 8.8.8.8\n' > /etc/resolv.conf
  fi
  sleep 2
  getent hosts github.com >/dev/null 2>&1 || warn "DNS всё ещё не работает — дальше может не скачаться"
fi

# ---------------------------------------------------------------- packages
say "Пакеты"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq || true
apt-get install -y -qq curl unzip openssl python3 ca-certificates iproute2 >/dev/null

retry() { local n=0; until "$@"; do n=$((n+1)); [ $n -ge 4 ] && return 1; sleep $((n*3)); done; }

# ---------------------------------------------------------------- 3x-ui in the way
busy() { ss -Htln "sport = :$1" 2>/dev/null | grep -q . ; }
if systemctl list-unit-files 2>/dev/null | grep -q '^x-ui'; then
  if [ "$TAKEOVER" = 1 ]; then
    say "Останавливаю 3x-ui (--takeover)"
    systemctl disable --now x-ui >/dev/null 2>&1 || true
    sleep 1
  else
    warn "На сервере есть 3x-ui. Если он держит порты $TCP_PORT/$XHTTP_PORT, запустите с --takeover"
  fi
fi

# ---------------------------------------------------------------- kernel
if [ "$TUNE" = 1 ]; then
  say "BBR и сетевые буферы"
  cat > /etc/sysctl.d/99-vpn-seller-node.conf <<'EOF'
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr
net.core.rmem_max = 33554432
net.core.wmem_max = 33554432
net.ipv4.tcp_rmem = 4096 131072 33554432
net.ipv4.tcp_wmem = 4096 65536 33554432
net.ipv4.tcp_fastopen = 3
net.ipv4.tcp_mtu_probing = 1
net.ipv4.tcp_slow_start_after_idle = 0
net.ipv4.ip_local_port_range = 10000 65000
net.core.somaxconn = 8192
net.ipv4.tcp_max_syn_backlog = 8192
fs.file-max = 1048576
EOF
  modprobe tcp_bbr 2>/dev/null || true
  sysctl -q --system >/dev/null 2>&1 || true
fi

# ---------------------------------------------------------------- Xray
if [ ! -x /usr/local/bin/xray ]; then
  say "Ставлю Xray (официальный установщик XTLS)"
  INST=$(mktemp)
  retry curl -fsSL --connect-timeout 15 -o "$INST" https://github.com/XTLS/Xray-install/raw/main/install-release.sh \
    || die "не скачался установщик Xray с GitHub — проверьте интернет/DNS на сервере"
  retry bash "$INST" install >/dev/null || die "Xray не установился"
  rm -f "$INST"
else
  say "Xray уже стоит: $(/usr/local/bin/xray version | head -1)"
fi
# geoip/geosite for routing Russian addresses past the chain
if [ ! -f /usr/local/share/xray/geoip.dat ]; then
  INST=$(mktemp)
  if curl -fsSL --connect-timeout 15 -o "$INST" https://github.com/XTLS/Xray-install/raw/main/install-release.sh; then
    bash "$INST" install-geodata >/dev/null 2>&1 || true
  fi
  rm -f "$INST"
fi

# ---------------------------------------------------------------- agent
say "Ставлю агент VPN Seller Node"
if [ -n "$SHOP" ]; then
  retry curl -fsSL --connect-timeout 15 -o "$DIR/agent.py.new" "$SHOP/scripts/vpn-seller-node.py" \
    || die "не скачался агент с $SHOP"
elif [ -f "$(dirname "$0")/vpn-seller-node.py" ]; then
  cp "$(dirname "$0")/vpn-seller-node.py" "$DIR/agent.py.new"
else
  die "укажите --shop https://ваш-магазин"
fi
python3 -c "import ast,sys; ast.parse(open('$DIR/agent.py.new').read())" || die "агент скачался битым"
mv "$DIR/agent.py.new" "$DIR/agent.py"
chmod 755 "$DIR/agent.py"

# the agent's own certificate (the shop pins its fingerprint)
if [ ! -s "$STATE/cert.pem" ]; then
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 3650 \
    -subj "/CN=vpn-seller-node" -keyout "$STATE/key.pem" -out "$STATE/cert.pem" >/dev/null 2>&1
  chmod 600 "$STATE/key.pem"
fi

# a free port for the agent
if [ -z "$APORT" ]; then
  if [ -f "$STATE/state.json" ]; then
    APORT=$(python3 -c "import json;print(json.load(open('$STATE/state.json')).get('listen','').rsplit(':',1)[-1])" 2>/dev/null || true)
  fi
  if [ -z "$APORT" ]; then
    for _ in $(seq 1 50); do
      APORT=$(( (RANDOM % 40000) + 20000 ))
      busy "$APORT" || break
    done
  fi
fi

IP=$(curl -4 -fsS --connect-timeout 8 https://api.ipify.org 2>/dev/null \
  || curl -4 -fsS --connect-timeout 8 https://ifconfig.me 2>/dev/null \
  || ip -4 route get 1.1.1.1 | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}')

for p in "$TCP_PORT" "$XHTTP_PORT"; do
  if busy "$p" && ! ss -Htlnp "sport = :$p" | grep -q xray; then
    die "порт $p занят другой программой: $(ss -Htlnp "sport = :$p" | head -1). Освободите его или используйте --takeover"
  fi
done

say "Настраиваю Reality (SNI: $SNI)"
python3 "$DIR/agent.py" init --host "$IP" --sni "$SNI" --listen "0.0.0.0:$APORT" \
  --tcp-port "$TCP_PORT" --xhttp-port "$XHTTP_PORT"

cat > /etc/systemd/system/vpn-seller-node.service <<EOF
[Unit]
Description=VPN Seller Node agent (VPN server of the shop)
After=network-online.target xray.service
Wants=network-online.target

[Service]
ExecStart=/usr/bin/python3 $DIR/agent.py serve
Restart=always
RestartSec=3
User=root
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable xray >/dev/null 2>&1 || true
systemctl enable vpn-seller-node >/dev/null 2>&1
systemctl restart vpn-seller-node

# firewall, only if it is already on
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow "$TCP_PORT"/tcp >/dev/null; ufw allow "$XHTTP_PORT"/tcp >/dev/null; ufw allow "$APORT"/tcp >/dev/null
fi

sleep 2
systemctl is-active --quiet xray || warn "Xray не запустился: journalctl -u xray -n 30"
systemctl is-active --quiet vpn-seller-node || warn "Агент не запустился: journalctl -u vpn-seller-node -n 30"

echo
echo "=================================================================="
echo " Готово. Скопируйте строку ниже в магазин:"
echo " Серверы → Добавить сервер → «Свой сервер» → «Строка подключения»"
echo
python3 "$DIR/agent.py" connect
echo
echo " Это пароль от сервера — никому её не пересылайте."
echo " Показать ещё раз: python3 $DIR/agent.py connect"
echo "=================================================================="
