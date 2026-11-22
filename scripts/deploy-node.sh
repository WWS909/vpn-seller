#!/usr/bin/env bash
# One command ON THE SHOP SERVER: set up a fresh VPN server over SSH and add
# it to the shop. Nothing to do on the VPN server by hand.
#
#   curl -fsSL https://raw.githubusercontent.com/WWS909/vpn-seller/main/scripts/deploy-node.sh -o deploy-node.sh
#   sudo bash deploy-node.sh --ip 1.2.3.4 --name "Литва" --country LT
#
# It asks for the VPN server's root password (not shown, not saved),
# updates the shop if it has no own-server support yet, runs node.sh on
# the VPN server, takes the vsnode://… line from its output and adds the
# server to the shop (scripts/add-node.sh).
#
# Options: --ip, --name, --country, --takeover (the server has 3x-ui),
#          --sni NAME, --port N (SSH port, default 22), --bypass
set -euo pipefail

IP="" NAME="" COUNTRY="" SSH_PORT=22 NODE_ARGS=() ADD_ARGS=()
ENV_FILE="${VPN_SELLER_ENV:-/opt/vpn-seller/.env}"
RAW="${VPN_SELLER_RAW:-https://raw.githubusercontent.com/WWS909/vpn-seller}"
while [ $# -gt 0 ]; do
  case "$1" in
    --ip) IP="$2"; shift 2 ;;
    --name) NAME="$2"; shift 2 ;;
    --country) COUNTRY="$2"; shift 2 ;;
    --port) SSH_PORT="$2"; shift 2 ;;
    --takeover) NODE_ARGS+=(--takeover); shift ;;
    --sni) NODE_ARGS+=(--sni "$2"); shift 2 ;;
    --bypass) ADD_ARGS+=(--bypass); shift ;;
    --env) ENV_FILE="$2"; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mОшибка:\033[0m %s\n' "$*" >&2; exit 1; }

[ -r "$ENV_FILE" ] || die "не найден $ENV_FILE — запускайте на сервере магазина через sudo"
[ -n "$IP" ] || read -rp "IP нового VPN-сервера: " IP
[ -n "$NAME" ] || read -rp "Название (его видят клиенты): " NAME
[ -n "$COUNTRY" ] || read -rp "Страна, две буквы (LT, LV, NL…): " COUNTRY
if [ -z "${SSHPASS:-}" ]; then
  read -rsp "Пароль root от $IP (его не будет видно): " SSHPASS; echo
fi
export SSHPASS

SHOP=$(sed -n 's/^PUBLIC_URL=["'\'']\{0,1\}\([^"'\'']*\).*/\1/p' "$ENV_FILE" | tail -1)
SHOP=${SHOP%/}
[ -n "$SHOP" ] || die "в $ENV_FILE нет PUBLIC_URL"

# 1. the shop must know own servers (1.9.0+)
if ! curl -fsS -o /dev/null --connect-timeout 10 "$SHOP/scripts/node.sh"; then
  say "Магазин старый — обновляю (vpn-seller update)"
  command -v vpn-seller >/dev/null || die "нет команды vpn-seller"
  vpn-seller update
  for _ in $(seq 1 60); do
    curl -fsS -o /dev/null --connect-timeout 5 "$SHOP/scripts/node.sh" && break
    sleep 3
  done
  curl -fsS -o /dev/null "$SHOP/scripts/node.sh" || die "после обновления магазин не отдаёт node.sh"
fi

# 2. SSH with a password
if [ -z "${DEPLOY_SSH:-}" ] && ! command -v sshpass >/dev/null; then
  say "Ставлю sshpass"
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq sshpass >/dev/null || \
    { apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq sshpass >/dev/null; }
fi
SSH=(${DEPLOY_SSH:-sshpass -e ssh} -p "$SSH_PORT" -o StrictHostKeyChecking=accept-new
     -o ConnectTimeout=20 -o ServerAliveInterval=15 "root@$IP")

say "Захожу на $IP и ставлю VPN (2–5 минут)"
LOG=$(mktemp)
chmod 600 "$LOG"
trap 'rm -f "$LOG"' EXIT
REMOTE="curl -fsSL --retry 3 '$SHOP/scripts/node.sh' -o /root/node.sh && bash /root/node.sh --shop '$SHOP' ${NODE_ARGS[*]:-}"
if ! "${SSH[@]}" "$REMOTE" 2>&1 | tee "$LOG" | grep -v --line-buffered '^vsnode://'; then
  :
fi
LINE=$(grep -o 'vsnode://[^[:space:]]*' "$LOG" | tail -1 || true)
if [ -z "$LINE" ]; then
  if grep -qi "permission denied" "$LOG"; then die "неверный пароль root"; fi
  die "установка на $IP не закончилась — выше видно, на чём остановилась"
fi
say "VPN на $IP установлен"

# 3. add it to the shop
HERE=$(cd "$(dirname "$0")" && pwd)
if [ -f "$HERE/add-node.sh" ]; then
  ADD="$HERE/add-node.sh"
else
  ADD=$(mktemp)
  curl -fsSL "$RAW/main/scripts/add-node.sh" -o "$ADD" 2>/dev/null \
    || curl -fsSL "$RAW/node-mirror/scripts/add-node.sh" -o "$ADD" \
    || die "не скачался add-node.sh"
fi
VSNODE_LINE="$LINE" bash "$ADD" --name "$NAME" --country "$COUNTRY" --env "$ENV_FILE" ${ADD_ARGS[@]+"${ADD_ARGS[@]}"}
echo
echo "Не забудьте сменить пароль root у $IP в панели хостинга."
