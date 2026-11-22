#!/usr/bin/env bash
# Add an own server (VPN Seller Node) to the shop from the shop's server —
# no clicking in the panel. Run it ON THE SHOP SERVER after node.sh printed
# the vsnode://… line on the VPN server:
#
#   curl -fsSL https://raw.githubusercontent.com/WWS909/vpn-seller/main/scripts/add-node.sh -o add-node.sh
#   sudo bash add-node.sh --name "Латвия" --country LV
#
# It asks for the vsnode://… line (typed in, not kept in the shell history),
# logs in to the panel with the admin password from the shop's .env,
# checks the server, adds it, turns off an old 3x-ui entry with the same IP
# and pushes every current customer to the new server.
set -euo pipefail

NAME="" COUNTRY="" ENV_FILE="${VPN_SELLER_ENV:-/opt/vpn-seller/.env}" CAP=200 BYPASS=0
while [ $# -gt 0 ]; do
  case "$1" in
    --name) NAME="$2"; shift 2 ;;
    --country) COUNTRY="$2"; shift 2 ;;
    --env) ENV_FILE="$2"; shift 2 ;;
    --capacity) CAP="$2"; shift 2 ;;
    --bypass) BYPASS=1; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
[ -n "$NAME" ] || read -rp "Название сервера (его видят клиенты): " NAME
[ -n "$COUNTRY" ] || read -rp "Страна, две буквы (LV, NL, DE…): " COUNTRY
[ -r "$ENV_FILE" ] || { echo "Не найден $ENV_FILE — это сервер магазина? Запустите через sudo." >&2; exit 1; }
LINE="${VSNODE_LINE:-}"
if [ -z "$LINE" ]; then
  read -rsp "Вставьте строку vsnode://… (её не будет видно) и нажмите Enter: " LINE
  echo
fi

python3 - "$ENV_FILE" "$NAME" "$COUNTRY" "$CAP" "$BYPASS" "$LINE" <<'PY'
import json
import re
import sys
import time
import urllib.error
import urllib.request

env_file, name, country, cap, bypass, line = sys.argv[1:7]
line = line.strip()
if not line.startswith("vsnode://"):
    sys.exit("Это не строка vsnode://… — скопируйте последнюю строку, которую напечатал установщик.")
env = {}
for ln in open(env_file, encoding="utf-8"):
    m = re.match(r"^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*?)\s*$", ln)
    if m:
        env[m.group(1)] = m.group(2).strip('"').strip("'")
base = (env.get("PUBLIC_URL") or "").rstrip("/")
if not base:
    sys.exit("В .env нет PUBLIC_URL — не знаю адрес магазина.")


def call(method, path, body=None, tok=None):
    headers = {"Content-Type": "application/json", "User-Agent": "add-node"}
    if tok:
        headers["Authorization"] = "Bearer " + tok
    req = urllib.request.Request(base + "/api/admin" + path, method=method, headers=headers,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        if e.code == 404 and path.startswith("/servers/test"):
            sys.exit("Магазин старый — сначала обновите его: vpn-seller update")
        sys.exit(f"Ошибка {e.code} ({path}): {detail}")
    except urllib.error.URLError as e:
        sys.exit(f"Магазин не отвечает по {base}: {e.reason}")


tok = call("POST", "/login", {"login": env.get("ADMIN_LOGIN") or "admin",
                              "password": env.get("ADMIN_PASSWORD", "")})["token"]
body = {"name": name.strip()[:64], "country": country.strip().upper()[:2], "kind": "node", "url": line,
        "capacity": int(cap or 0), "bypass": bypass == "1"}
t = call("POST", "/servers/test", body, tok)
if not t.get("ok"):
    sys.exit("Сервер не отвечает: " + str(t.get("error")) +
             "\nПроверьте, что установка на VPN-сервере закончилась без ошибок.")
print(f"Связь есть: {t.get('version')}, входов: {len(t.get('inbounds') or [])}")

m = re.search(r"@\[?([^\]/]+?)\]?:\d+", line)
ip = m.group(1) if m else ""
for s in call("GET", "/servers", tok=tok):
    if ip and ip in (s.get("url") or "") and s.get("kind") != "node" and s.get("enabled"):
        old = {k: s.get(k) for k in ("name", "country", "url", "username", "inbound_ids", "sub_url",
                                     "verify_tls", "sort", "kind", "capacity", "bypass", "tg_inbounds")}
        old["enabled"] = False
        call("PUT", f"/servers/{s['id']}", old, tok)
        print(f"Старый сервер «{s['name']}» (3x-ui) выключен — удалите его в панели, когда всё заработает.")

new = call("POST", "/servers", body, tok)
print(f"Добавлен: «{new['name']}»")
r = call("POST", f"/servers/{new['id']}/sync", tok=tok)
for _ in range(120):
    if not r.get("running"):
        break
    time.sleep(2)
    r = call("GET", f"/servers/{new['id']}/sync", tok=tok)
print(f"Клиенты залиты: {r.get('done', 0)}" + (f", с ошибкой: {r['errors']}" if r.get("errors") else ""))
print("Готово. Подписки клиентов обновятся сами в течение пары минут.")
PY
