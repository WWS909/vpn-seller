#!/usr/bin/env bash
# Start a real Marzban panel on this machine for tests and local development:
# Marzban 0.8.x from source in a virtualenv, SQLite, two inbounds
# (VLESS TCP and Shadowsocks TCP), an admin in its database.
#
#   scripts/dev-marzban.sh          start (installs on first run) and print env vars
#   scripts/dev-marzban.sh stop     stop it
#   scripts/dev-marzban.sh env      only print the env vars
#
# Xray is taken from the 3x-ui download of scripts/dev-3xui.sh (run that first).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIR=${MARZBAN_DEV_DIR:-"$ROOT/.dev/marzban"}
PORT=20540
ADMIN=shopadmin
PASS='Shop-pass-123'
VERSION=${MARZBAN_VERSION:-v0.8.4}

print_env() {
  cat <<EOF
export MARZBAN_TEST_URL=http://127.0.0.1:$PORT/
export MARZBAN_TEST_USER=$ADMIN
export MARZBAN_TEST_PASS='$PASS'
export MARZBAN_TEST_INBOUNDS='VLESS TCP,Shadowsocks TCP'
EOF
}

case "${1:-start}" in
  env) print_env; exit 0 ;;
  stop) pkill -f "$DIR/src/main.py" 2>/dev/null || true; exit 0 ;;
esac

XRAY_DIR=$(dirname "$(find "$ROOT/.dev/3xui" -name 'xray-linux-amd64' 2>/dev/null | head -1)")
if [ -z "$XRAY_DIR" ] || [ "$XRAY_DIR" = "." ]; then
  echo "no Xray binary: run scripts/dev-3xui.sh first" >&2
  exit 1
fi

mkdir -p "$DIR"
if [ ! -d "$DIR/src" ]; then
  echo "downloading Marzban $VERSION ..." >&2
  git clone -q --depth 1 --branch "$VERSION" https://github.com/Gozargah/Marzban.git "$DIR/src"
fi
if [ ! -x "$DIR/venv/bin/python" ]; then
  python3 -m venv "$DIR/venv"
  "$DIR/venv/bin/pip" install -q -r "$DIR/src/requirements.txt"
  # Marzban's scheduler still imports pkg_resources: new Pythons do not put
  # setuptools into a venv, and setuptools 81 dropped it.
  "$DIR/venv/bin/pip" install -q "setuptools<81"
fi

cat > "$DIR/xray.json" <<'EOF'
{
  "log": {"loglevel": "warning"},
  "inbounds": [
    {"tag": "VLESS TCP", "listen": "127.0.0.1", "port": 24450, "protocol": "vless",
     "settings": {"clients": [], "decryption": "none"},
     "streamSettings": {"network": "tcp", "security": "none"}},
    {"tag": "Shadowsocks TCP", "listen": "127.0.0.1", "port": 24451, "protocol": "shadowsocks",
     "settings": {"clients": [], "network": "tcp,udp"}}
  ],
  "outbounds": [{"protocol": "freedom", "tag": "DIRECT"}, {"protocol": "blackhole", "tag": "BLOCK"}]
}
EOF

cat > "$DIR/src/.env" <<EOF
UVICORN_HOST=127.0.0.1
UVICORN_PORT=$PORT
SQLALCHEMY_DATABASE_URL=sqlite:///$DIR/db.sqlite3
XRAY_JSON=$DIR/xray.json
XRAY_EXECUTABLE_PATH=$XRAY_DIR/xray-linux-amd64
XRAY_ASSETS_PATH=$XRAY_DIR
XRAY_SUBSCRIPTION_URL_PREFIX=http://127.0.0.1:$PORT
EOF

cd "$DIR/src"
"$DIR/venv/bin/alembic" upgrade head >/dev/null 2>&1
MARZBAN_ADMIN_PASSWORD="$PASS" "$DIR/venv/bin/python" marzban-cli.py admin create \
  --username "$ADMIN" --sudo --telegram-id 0 --discord-webhook "" >/dev/null 2>&1 || true

if ! pgrep -f "$DIR/src/main.py" >/dev/null; then
  setsid "$DIR/venv/bin/python" "$DIR/src/main.py" > "$DIR/marzban.log" 2>&1 &
fi
up=0
for _ in $(seq 1 40); do
  curl -s --noproxy '*' -o /dev/null "http://127.0.0.1:$PORT/api/system" && { up=1; break; }
  sleep 0.5
done
if [ "$up" != 1 ]; then
  echo "Marzban did not start on port $PORT — see $DIR/marzban.log" >&2
  exit 1
fi
touch "$ROOT/.dev/env.sh"
grep -v '^export MARZBAN_TEST_' "$ROOT/.dev/env.sh" > "$ROOT/.dev/env.sh.tmp" || true
print_env >> "$ROOT/.dev/env.sh.tmp"
mv "$ROOT/.dev/env.sh.tmp" "$ROOT/.dev/env.sh"
print_env
