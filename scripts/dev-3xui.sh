#!/usr/bin/env bash
# Start two real 3x-ui panels on this machine for tests and local development:
# the current 3.x and the older 2.x, each with one VLESS inbound.
#
#   scripts/dev-3xui.sh          start (downloads on first run) and print env vars
#   scripts/dev-3xui.sh stop     stop both panels
#
# Afterwards:  eval "$(scripts/dev-3xui.sh env)" && pytest
set -euo pipefail

V3=${XUI_V3_VERSION:-v3.8.5}
V2=${XUI_V2_VERSION:-v2.9.4}
DIR=${XUI_DEV_DIR:-"$(cd "$(dirname "$0")/.." && pwd)/.dev/3xui"}
USER_=tester
PASS='Test-pass-123'
mkdir -p "$DIR"

port_of() { [ "$1" = v3 ] && echo 20531 || echo 20532; }
base_of() { [ "$1" = v3 ] && echo /panel3/ || echo /panel2/; }

fetch() {  # fetch <version> <dir>
  [ -x "$2/x-ui/x-ui" ] && return
  mkdir -p "$2"
  echo "downloading 3x-ui $1 ..." >&2
  curl -fsSL "https://github.com/MHSanaei/3x-ui/releases/download/$1/x-ui-linux-amd64.tar.gz" \
    | tar -xz -C "$2"
}

run() {  # run <v3|v2> <version>
  local kind=$1 ver=$2 d="$DIR/$1"
  fetch "$ver" "$d"
  mkdir -p "$d/db" "$d/log"
  export XUI_DB_FOLDER="$d/db" XUI_BIN_FOLDER="$d/x-ui/bin" XUI_LOG_FOLDER="$d/log"
  if pgrep -f "$d/x-ui/x-ui run" >/dev/null; then return; fi
  "$d/x-ui/x-ui" setting -port "$(port_of "$kind")" -username "$USER_" -password "$PASS" \
    -webBasePath "$(base_of "$kind")" -listenIP 127.0.0.1 >/dev/null 2>&1
  if [ "$kind" = v2 ]; then
    # Both panels would fight for the subscription and Xray API ports.
    python3 - "$d/db/x-ui.db" <<'PY'
import json, sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.execute("create table if not exists settings(id integer primary key autoincrement, key text, value text)")
if not c.execute("select 1 from settings where key='subPort'").fetchone():
    c.execute("insert into settings(key,value) values('subPort','20962')")
c.commit()
PY
  fi
  (cd "$d/x-ui" && setsid nohup "$d/x-ui/x-ui" run >"$d/log/run.out" 2>&1 </dev/null &)
}

wait_up() { for _ in $(seq 1 40); do curl -fs -o /dev/null "$1" && return; sleep 0.5; done; echo "panel $1 did not start" >&2; exit 1; }

inbound() {  # inbound <v3|v2> <port> -> id of a VLESS inbound (created if missing)
  python3 - "$1" "$2" <<'PY'
import json, sys, httpx
kind, port = sys.argv[1], int(sys.argv[2])
base = f"http://127.0.0.1:{20531 if kind == 'v3' else 20532}/{'panel3' if kind == 'v3' else 'panel2'}/"
c = httpx.Client(trust_env=False, timeout=10)
hdr = {}
if kind == "v3":
    tok = c.get(base + "csrf-token").json()["obj"]
    c.post(base + "login", json={"username": "tester", "password": "Test-pass-123"}, headers={"X-CSRF-Token": tok})
    hdr = {"X-CSRF-Token": tok}
else:
    c.post(base + "login", data={"username": "tester", "password": "Test-pass-123"})
rows = c.get(base + "panel/api/inbounds/list").json()["obj"] or []
for ib in rows:
    if ib["port"] == port:
        print(ib["id"]); sys.exit()
settings = {"clients": [], "decryption": "none", "fallbacks": []}
stream = {"network": "tcp", "security": "none", "tcpSettings": {"header": {"type": "none"}}}
sniff = {"enabled": True, "destOverride": ["http", "tls"]}
body = {"enable": True, "remark": f"dev-{kind}", "listen": "", "port": port, "protocol": "vless",
        "expiryTime": 0, "total": 0}
if kind == "v3":
    r = c.post(base + "panel/api/inbounds/add", json={**body, "settings": settings,
               "streamSettings": stream, "sniffing": sniff}, headers=hdr)
else:
    r = c.post(base + "panel/api/inbounds/add", data={**body, "settings": json.dumps(settings),
               "streamSettings": json.dumps(stream), "sniffing": json.dumps(sniff)})
print(r.json()["obj"]["id"])
PY
}

token() {
  export XUI_DB_FOLDER="$DIR/v3/db" XUI_BIN_FOLDER="$DIR/v3/x-ui/bin"
  if [ ! -s "$DIR/v3/token" ]; then
    "$DIR/v3/x-ui/x-ui" setting -getApiToken 2>/dev/null | sed -n 's/^apiToken: //p' >"$DIR/v3/token"
  fi
  cat "$DIR/v3/token"
}

case "${1:-start}" in
  stop)
    pkill -f "$DIR/v3/x-ui/x-ui run" || true
    pkill -f "$DIR/v2/x-ui/x-ui run" || true
    ;;
  start|env)
    run v3 "$V3"; run v2 "$V2"
    wait_up "http://127.0.0.1:20531/panel3/"; wait_up "http://127.0.0.1:20532/panel2/"
    IB3=$(inbound v3 24443); IB2=$(inbound v2 24444); TOK=$(token)
    cat <<EOF
export XUI_TEST_V3_URL=http://127.0.0.1:20531/panel3/
export XUI_TEST_V3_TOKEN=$TOK
export XUI_TEST_V3_INBOUND=$IB3
export XUI_TEST_V2_URL=http://127.0.0.1:20532/panel2/
export XUI_TEST_V2_USER=$USER_
export XUI_TEST_V2_PASS='$PASS'
export XUI_TEST_V2_INBOUND=$IB2
export XUI_TEST_V2_SUB=http://127.0.0.1:20962/sub/
EOF
    ;;
esac
