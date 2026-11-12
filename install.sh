#!/usr/bin/env bash
# =============================================================================
#  VPN Seller — установка и управление / install and manage
#
#  Установка одной командой (на чистом сервере Ubuntu/Debian, от root):
#    bash <(curl -fsSL https://raw.githubusercontent.com/WWS909/vpn-seller/main/install.sh)
#  или
#    wget -qO install.sh https://raw.githubusercontent.com/WWS909/vpn-seller/main/install.sh && bash install.sh
#
#  Откроется меню, установщик задаст шесть вопросов и сделает всё сам:
#  Docker, код, настройки, HTTPS (свой Caddy, уже стоящий Caddy/nginx
#  или временный туннель Cloudflare), бот.
#
#  Потом:  vpn-seller — то же меню (обновить, состояние, журнал, бэкап…); старое имя vpn-shop тоже работает
#
#  Без вопросов / unattended:
#    bash install.sh install --token 123:ABC --admin-id 123456789 \
#         (--domain vpn.example.com | --tunnel) [--password …] [--currency RUB] \
#         [--db sqlite|postgres] [--en] --yes
#  Команды: install | update | status | logs | backup | restore <zip> |
#           payments | address | settings | postgres | uninstall | menu
#
#  Обновление берёт новую версию с GitHub (git pull или архив репозитория),
#  делает копию базы и возвращает прежний код, если новая версия не запустилась.
#  Кнопка «Обновить» в админке и в боте работает через systemd-юнит
#  vpn-seller-update.path, который ставит этот скрипт.
# =============================================================================
set -Eeuo pipefail

GITHUB_REPO="${VPN_SHOP_GITHUB_REPO:-WWS909/vpn-seller}"   # set by scripts/publish-github.sh
REPO_URL="${VPN_SHOP_REPO:-https://github.com/$GITHUB_REPO.git}"
BRANCH="${VPN_SHOP_BRANCH:-main}"
TG_API="${TG_API_BASE:-https://api.telegram.org}"
GITHUB_API="${VPN_SHOP_GITHUB_API:-https://api.github.com}"
SYSTEMD_DIR="${VPN_SELLER_SYSTEMD_DIR:-/etc/systemd/system}"
UPDATER="vpn-seller-update"                       # systemd units: the panel's "Update" button
LOG="${VPN_SHOP_LOG:-/var/log/vpn-shop-install.log}"
BIN_LINK="/usr/local/bin/vpn-seller"
OLD_LINK="/usr/local/bin/vpn-shop"                 # the name before VPN Seller: kept working
MARK_BEGIN="# >>> vpn-shop (added by install.sh, removed by uninstall)"
MARK_END="# <<< vpn-shop"
ALIAS="vpnshop-app"                               # the shop's name on proxy networks

# Where the shop lives: VPN_SHOP_DIR, else next to this script when it is
# the installed copy (the "vpn-seller" command), else /opt/vpn-seller —
# or /opt/vpn-shop, where a shop installed under the old name already lives.
_self=$(readlink -f "${BASH_SOURCE[0]:-}" 2>/dev/null || true)
if [ -n "${VPN_SHOP_DIR:-}" ]; then DIR=$VPN_SHOP_DIR
elif [ -n "$_self" ] && [ -f "$_self" ] && [ -f "$(dirname "$_self")/docker-compose.yml" ] \
     && [ -f "$(dirname "$_self")/.env" ]; then DIR=$(dirname "$_self")
elif [ -f /opt/vpn-shop/.env ] && [ ! -e /opt/vpn-seller ]; then DIR=/opt/vpn-shop
else DIR=/opt/vpn-seller; fi

DOMAIN="" TOKEN="" ADMIN_ID="" PASSWORD="" CURRENCY="" ADDR="" ASSUME_YES=0 FROM_PANEL=0 DB=
GENERATED=0 BOT="" MODE="" PROXY_CTR="" PROXY_NET="" PROXY_KIND="" PROXY_NOTE=""
CADDY_CFG="" CADDY_HOSTFILE="" URL=""
case "${LANG:-}" in en*) UI=en ;; *) UI=ru ;; esac
for a in "$@"; do case "$a" in --en) UI=en ;; --ru) UI=ru ;; esac; done

# ------------------------------------------------------------------ output
case "${LC_ALL:-${LC_CTYPE:-${LANG:-}}}" in
  *UTF-8*|*utf8*|*UTF8*|*utf-8*) ;;
  *) if locale -a 2>/dev/null | grep -qiE '^c\.utf-?8$'; then export LC_ALL=C.UTF-8; fi ;;
esac
t() { if [ "$UI" = en ]; then printf '%s' "$2"; else printf '%s' "$1"; fi; }
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  A=$'\e[38;5;214m' B=$'\e[1m' D=$'\e[2m' G=$'\e[32m' Y=$'\e[33m' R=$'\e[31m' N=$'\e[0m'
else A="" B="" D="" G="" Y="" R="" N=""; fi
say()  { printf '%s\n' "$*"; }
ok()   { printf '  %s✓%s %s\n' "$G" "$N" "$*"; }
info() { printf '  %s·%s %s\n' "$D" "$N" "$*"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$*"; }
die()  { printf '\n  %s✗ %s%s\n\n' "$R" "$*" "$N" >&2; exit 1; }
hint() { printf '    %s%s%s\n' "$D" "$*" "$N"; }
step() {  # step "2/6" "Title"
  printf '\n  %s%s%s  %s%s%s\n' "$A" "$1" "$N" "$B" "$2" "$N"
}
row() {  # row "Label" "value" — a two-column line; pads by characters
  local l=$1 w=16; shift
  printf '  %s%*s %s\n' "$l" $(( w > ${#l} ? w - ${#l} : 1 )) '' "$*"
}
rule() { printf '  %s%s%s\n' "$D" "────────────────────────────────────────────────────────" "$N"; }

banner() {
  [ -t 1 ] && [ -z "${NO_CLEAR:-}" ] && printf '\e[2J\e[H'
  local c1=$A c2=$A c3=$A c4=$A c5=$A c6=$A
  printf '\n'
  printf '%s   ██╗   ██╗██████╗ ███╗   ██╗%s\n' "$c1" "$N"
  printf '%s   ██║   ██║██╔══██╗████╗  ██║%s\n' "$c2" "$N"
  printf '%s   ██║   ██║██████╔╝██╔██╗ ██║%s   %sS E L L E R%s\n' "$c3" "$N" "$B" "$N"
  printf '%s   ╚██╗ ██╔╝██╔═══╝ ██║╚██╗██║%s   %s───────────%s\n' "$c4" "$N" "$D" "$N"
  printf '%s    ╚████╔╝ ██║     ██║ ╚████║%s   %s%s%s\n' "$c5" "$N" "$D" "$(t "магазин VPN-подписок" "a VPN subscription shop")" "$N"
  printf '%s     ╚═══╝  ╚═╝     ╚═╝  ╚═══╝%s   %s%s%s\n' "$c6" "$N" "$D" "$(t "внутри Telegram" "inside Telegram")" "$N"
  printf '\n'
}

on_err() {
  local code=$? line=$1
  printf '\n  %s✗ %s %s (%s %s).%s\n' "$R" "$(t "Установка остановилась на строке" "Stopped at line")" \
    "$line" "$(t "код" "code")" "$code" "$N" >&2
  printf '    %s %s\n\n' "$(t "Подробности:" "Details:")" "$LOG" >&2
  exit "$code"
}
trap 'on_err $LINENO' ERR
trap_int() {
  trap 'printf "\n\n  %s\n\n" "$(t "Прервано. Ничего не сломано — запустите снова, когда будете готовы." "Interrupted. Nothing is broken — run it again when ready.")"; exit 130' INT
}
trap_int

# Answers come from the terminal even with "curl … | bash".
IN=/dev/tty OUT=/dev/tty
{ : < /dev/tty; } 2>/dev/null || { IN=/dev/stdin; OUT=/dev/stderr; }
# Every helper below writes into the caller's variable by name, so its own
# locals carry a unique prefix (a caller's "ans" must not be shadowed).
_read() {  # _read var [-s]
  local _rd_v
  if [ "${2:-}" = -s ]; then IFS= read -rs _rd_v < "$IN" || die "$(t "нет ответа — ввод закрыт" "no answer — input is closed")"
    printf '\n' > "$OUT"
  else IFS= read -r _rd_v < "$IN" || die "$(t "нет ответа — ввод закрыт" "no answer — input is closed")"; fi
  printf -v "$1" '%s' "$_rd_v"
}
ask() {  # ask var "Question" [default]
  local _ask_q=$2 _ask_def=${3:-} _ask_v
  if [ -n "$_ask_def" ]; then printf '  %s›%s %s %s[%s]%s ' "$A" "$N" "$_ask_q" "$D" "$_ask_def" "$N" > "$OUT"
  else printf '  %s›%s %s ' "$A" "$N" "$_ask_q" > "$OUT"; fi
  _read _ask_v
  _ask_v=${_ask_v#"${_ask_v%%[![:space:]]*}"}; _ask_v=${_ask_v%"${_ask_v##*[![:space:]]}"}
  printf -v "$1" '%s' "${_ask_v:-$_ask_def}"
}
ask_secret() {  # ask_secret var "Question"
  printf '  %s›%s %s ' "$A" "$N" "$2" > "$OUT"
  local _as_v; _read _as_v -s; printf -v "$1" '%s' "$_as_v"
}
confirm() {  # confirm "Question" [y|n default]
  [ "$ASSUME_YES" = 1 ] && return 0
  local _cf_d=${2:-y} _cf_v
  ask _cf_v "$1 $(t "(д/н)" "(y/n)")" "$( [ "$_cf_d" = y ] && t "д" "y" || t "н" "n")"
  case "$_cf_v" in y|Y|yes|Yes|д|Д|да|Да) return 0 ;; *) return 1 ;; esac
}
choose() {  # choose var "1" "Label" "hint" "2" "Label" "hint" … (default = first)
  local _ch_var=$1 _ch_first="" _ch_keys="" _ch_k _ch_l _ch_h _ch_v; shift
  while [ $# -ge 3 ]; do
    _ch_k=$1 _ch_l=$2 _ch_h=$3; shift 3
    if [ "$_ch_k" = "--" ]; then                       # a section heading, not a choice
      printf '\n  %s%s%s\n' "$D" "$_ch_l" "$N" > "$OUT"
      continue
    fi
    [ -z "$_ch_first" ] && _ch_first=$_ch_k
    _ch_keys="$_ch_keys $_ch_k"
    printf '   %s%3s%s  %s\n' "$A" "$_ch_k" "$N" "$_ch_l" > "$OUT"
    if [ -n "$_ch_h" ]; then printf '         %s%s%s\n' "$D" "$_ch_h" "$N" > "$OUT"; fi
  done
  while :; do
    ask _ch_v "$(t "Ваш выбор" "Your choice")" "$_ch_first"
    case " $_ch_keys " in *" $_ch_v "*) printf -v "$_ch_var" '%s' "$_ch_v"; return 0 ;; esac
    warn "$(t "введите один из номеров:" "enter one of:")$_ch_keys"
  done
}

# Long, noisy commands go to the log; the person sees one line with a spinner.
# The command runs in a subshell without the ERR trap: a failure here is
# reported by the caller, not as "stopped at line N".
spin() {  # spin "Text" cmd…
  local msg=$1; shift
  printf '\n=== %s: %s\n' "$(date '+%F %T')" "$*" >> "$LOG"
  if [ ! -t 1 ]; then
    info "$msg…"
    if ( trap - ERR; set +e; "$@" ) >> "$LOG" 2>&1; then return 0; fi
    tail -n 15 "$LOG" | sed 's/^/      /'
    return 1
  fi
  ( trap - ERR INT; set +e; "$@" ) >> "$LOG" 2>&1 &
  local pid=$! i=0 frames='|/-\' start=$SECONDS
  case "${LC_ALL:-${LC_CTYPE:-${LANG:-}}}" in *UTF-8*|*utf8*|*UTF8*) frames='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏' ;; esac
  while kill -0 "$pid" 2>/dev/null; do
    printf '\r  %s%s%s %s %s%ss%s ' "$A" "${frames:i++%${#frames}:1}" "$N" "$msg" "$D" "$((SECONDS - start))" "$N"
    sleep 0.1
  done
  if wait "$pid"; then printf '\r\e[K'; ok "$msg"; return 0; fi
  printf '\r\e[K'; printf '  %s✗%s %s\n' "$R" "$N" "$msg"
  tail -n 15 "$LOG" | sed 's/^/      /'
  return 1
}

# ------------------------------------------------------------------ helpers
have() { command -v "$1" >/dev/null 2>&1; }
need_root() {
  [ "$(id -u)" = 0 ] && return
  die "$(t "нужны права root. Запустите: sudo bash install.sh  (или войдите как root)" \
           "root is required. Run: sudo bash install.sh (or log in as root)")"
}
installed() { [ -f "$DIR/docker-compose.yml" ] && [ -f "$DIR/.env" ]; }
compose() { (cd "$DIR" && docker compose "$@"); }

get_env() { [ -f "$DIR/.env" ] && grep -m1 "^$1=" "$DIR/.env" | cut -d= -f2- || true; }
set_env() {  # set_env KEY VALUE — replace the line (also a commented one) or add it
  local f="$DIR/.env" tmp
  tmp=$(mktemp)
  awk -v k="$1" -v v="$2" '
    !done && ($0 ~ "^"k"=" || $0 ~ "^# ?"k"=") { print k"="v; done=1; next }
    { print }
    END { if (!done) print k"="v }' "$f" > "$tmp"
  cat "$tmp" > "$f"; rm -f "$tmp"
}

random_hex()  { od -An -tx1 -N"$1" /dev/urandom | tr -d ' \n'; }
random_pass() { head -c 600 /dev/urandom | LC_ALL=C tr -dc 'A-HJ-NP-Za-km-z2-9' | cut -c1-16; }

# ------------------------------------------------------------------ database
# SQLite (a file in data/) or PostgreSQL (the "db" service, profile "postgres").
# The shop reaches PostgreSQL by its own alias, never a plain "db" (see
# docker-compose.yml). PG_COMFORT_MB: from this much memory PostgreSQL is
# the default choice.
SQLITE_URL="sqlite:////app/data/vpn.db"
PG_HOST="vpnseller-db"
PG_COMFORT_MB=1900
mem_mb() {
  if [ -n "${VPN_SHOP_MEM_MB:-}" ]; then printf '%s' "$VPN_SHOP_MEM_MB"; return; fi
  awk '/^MemTotal:/{print int($2/1024); f=1} END{if(!f) print 0}' /proc/meminfo 2>/dev/null || printf 0
}
mem_human() { awk -v m="$1" 'BEGIN{ if (m >= 1024) printf "%.1f %s", m/1024, (ui=="en"?"GB":"ГБ"); else printf "%d %s", m, (ui=="en"?"MB":"МБ") }' ui="$UI" | sed 's/\.0 / /'; }
db_kind() { case "$(get_env DATABASE_URL)" in postgres*) echo postgres ;; *) echo sqlite ;; esac; }
db_label() { if [ "$1" = postgres ]; then echo PostgreSQL; else echo SQLite; fi; }
pg_url() { printf 'postgresql+psycopg://vpn:%s@%s:5432/vpn' "$1" "$PG_HOST"; }
# COMPOSE_PROFILES = the address mode (caddy | tunnel | nothing) + postgres when it is the database
set_profiles() {  # set_profiles "caddy|tunnel|"
  local p=$1
  if [ "$(db_kind)" = postgres ]; then p=${p:+$p,}postgres; fi
  set_env COMPOSE_PROFILES "$p"
}
base_profile() { get_env COMPOSE_PROFILES | tr ',' '\n' | grep -v -e '^postgres$' -e '^$' | paste -sd, - || true; }

public_ip() {
  local u ip
  for u in https://api.ipify.org https://ifconfig.me https://icanhazip.com; do
    ip=$(curl -fsS --max-time 6 "$u" 2>/dev/null | tr -d '[:space:]' || true)
    [[ "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] && { printf '%s' "$ip"; return; }
  done
}
domain_ip() { { getent ahostsv4 "$1" 2>/dev/null || true; } | awk '{print $1; exit}'; }

tg() {  # tg METHOD [json] — Bot API call with $TOKEN; prints the raw answer
  local body=${2:-}
  [ -z "$body" ] && body='{}'
  curl -sS --max-time "${TG_TIMEOUT:-12}" -H 'Content-Type: application/json' \
       -d "$body" "$TG_API/bot$TOKEN/$1" 2>/dev/null || true
}
json_str() { printf '%s' "$1" | sed -n "s/.*\"$2\" *: *\"\\([^\"]*\\)\".*/\\1/p" | head -1; }

# ------------------------------------------------------------------ steps
ensure_tools() {
  local miss=""
  have curl || miss="$miss curl"
  have git  || miss="$miss git"
  have ss   || miss="$miss iproute2"
  [ -z "$miss" ] && return
  if have apt-get; then
    spin "$(t "Ставлю" "Installing")$miss" sh -c "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq$miss"
  else
    die "$(t "не хватает программ:" "missing tools:")$miss"
  fi
}

install_docker() {
  if have docker && docker compose version >/dev/null 2>&1; then
    ok "Docker $(docker --version | awk '{print $3}' | tr -d ,) $(t "уже есть" "is installed")"
    systemctl start docker >/dev/null 2>&1 || true
    return
  fi
  if ! spin "$(t "Ставлю Docker (2–3 минуты)" "Installing Docker (2–3 minutes)")" sh -c "curl -fsSL https://get.docker.com | sh"; then
    warn "$(t "официальный установщик не сработал — ставлю Docker из репозитория системы" "the official installer failed — using the system package")"
    spin "$(t "Ставлю Docker из репозитория" "Installing Docker from the distro")" \
      sh -c "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker.io docker-compose-v2 || DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker.io docker-compose-plugin" \
      || die "$(t "Docker не поставился. Подробности:" "Docker did not install. Details:") $LOG"
  fi
  systemctl enable --now docker >> "$LOG" 2>&1 || true
  docker compose version >/dev/null 2>&1 || die "$(t "Docker Compose не найден после установки" "Docker Compose is missing after the install")"
}

get_code() {
  if [ -f "$DIR/docker-compose.yml" ] && [ -d "$DIR/app" ]; then
    ok "$(t "Код уже на месте:" "Code is in place:") $DIR"
    return
  fi
  # Started from an unpacked copy (a real file, not "curl | bash"): install from it.
  local here=""
  [ -n "$_self" ] && [ -f "$_self" ] && here=$(dirname "$_self")
  if [ -n "$here" ] && [ -f "$here/docker-compose.yml" ] && [ -d "$here/app" ]; then
    mkdir -p "$DIR"
    [ "$here" != "$DIR" ] && cp -a "$here/." "$DIR/"
    ok "$(t "Код скопирован в" "Code copied to") $DIR"
    return
  fi
  if ! spin "$(t "Скачиваю VPN Seller" "Downloading VPN Seller")" git clone -q --depth 1 --branch "$BRANCH" "$REPO_URL" "$DIR"; then
    rm -rf "$DIR"
    die "$(t "не удалось скачать $REPO_URL. Если репозиторий закрытый — скачайте архив, распакуйте и запустите install.sh из папки." \
             "could not download $REPO_URL. If the repository is private, unpack the archive and run install.sh from its folder.")"
  fi
}

# --------------------------------------------------------------- questions
q_token() {
  step "1/6" "$(t "Токен бота" "Bot token")"
  hint "$(t "Откройте @BotFather → /newbot (или /mybots → бот → API Token) и вставьте токен." \
            "Open @BotFather → /newbot (or /mybots → your bot → API Token) and paste the token.")"
  hint "$(t "При вставке символы не видны — так и задумано." "The token is hidden while you paste it.")"
  local res code
  while :; do
    [ -z "$TOKEN" ] && ask_secret TOKEN "$(t "Токен" "Token")"
    TOKEN=$(printf '%s' "$TOKEN" | tr -d '[:space:]')
    if [[ ! "$TOKEN" =~ ^[0-9]{5,}:[A-Za-z0-9_-]{30,}$ ]]; then
      warn "$(t "не похоже на токен: он выглядит как 1234567890:AAH…" "that is not a token: it looks like 1234567890:AAH…")"
      [ "$ASSUME_YES" = 1 ] && die "--token"
      TOKEN=""; continue
    fi
    res=$(tg getMe)
    BOT=$(json_str "$res" username)
    if [ -n "$BOT" ]; then ok "$(t "Бот" "Bot") @$BOT"; return; fi
    code=$(printf '%s' "$res" | sed -n 's/.*"error_code" *: *\([0-9]*\).*/\1/p')
    if [ -n "$code" ]; then
      warn "$(t "Telegram не принял токен (ошибка $code). Скопируйте его из @BotFather заново." "Telegram rejected the token (error $code). Copy it from @BotFather again.")"
      [ "$ASSUME_YES" = 1 ] && die "--token"
      TOKEN=""; continue
    fi
    warn "$(t "Сервер не может достучаться до api.telegram.org — проверить токен сейчас нельзя." \
              "This server cannot reach api.telegram.org — the token cannot be checked now.")"
    hint "$(t "Без доступа к Telegram бот работать не будет. Проверьте сеть сервера." "The bot needs Telegram. Check the server's network.")"
    confirm "$(t "Продолжить без проверки?" "Continue without the check?")" n && return
    TOKEN=""
  done
}

detect_admin_id() {
  # Works only while the bot has no webhook yet (a fresh bot): read the
  # message the owner sends it right now.
  local hook res id upd deadline
  hook=$(json_str "$(tg getWebhookInfo)" url)
  [ -n "$hook" ] && return 1
  tg getUpdates '{"offset":-1,"timeout":0}' >/dev/null     # skip old messages
  upd=$(tg getUpdates '{"timeout":0}' | grep -oE '"update_id": *[0-9]+' | tail -1 | grep -oE '[0-9]+$' || true)
  upd=$(( ${upd:-0} + 1 ))
  info "$(t "Откройте @$BOT в Telegram и отправьте ему любое сообщение. Жду до 2 минут…" \
            "Open @$BOT in Telegram and send it any message. Waiting up to 2 minutes…")"
  deadline=$((SECONDS + 120))
  while [ $SECONDS -lt $deadline ]; do
    res=$(TG_TIMEOUT=30 tg getUpdates "{\"offset\":$upd,\"timeout\":20,\"allowed_updates\":[\"message\"]}")
    id=$(printf '%s' "$res" | grep -oE '"from": *\{ *"id": *[0-9]+' | head -1 | grep -oE '[0-9]+$' || true)
    if [ -n "$id" ]; then ADMIN_ID=$id; return 0; fi
    [ -z "$res" ] && sleep 2
  done
  return 1
}

q_admin() {
  step "2/5" "$(t "Ваш Telegram ID" "Your Telegram ID")"
  hint "$(t "Администратор получает уведомления об оплатах и открывает панель прямо из бота (/admin)." \
            "The admin gets payment notices and opens the panel right from the bot (/admin).")"
  if [ -z "$ADMIN_ID" ] && [ "$ASSUME_YES" != 1 ] && [ -n "$BOT" ]; then
    local how
    choose how \
      1 "$(t "Узнать автоматически" "Detect it")" "$(t "вы пишете боту, установщик видит ваш ID" "you message the bot, the installer sees your ID")" \
      2 "$(t "Ввести вручную" "Type it")" "$(t "ID можно узнать у @userinfobot" "get it from @userinfobot")"
    if [ "$how" = 1 ]; then
      if detect_admin_id; then ok "$(t "Ваш ID:" "Your ID:") $ADMIN_ID"
      else warn "$(t "не получилось — введите вручную" "that did not work — type it in")"; fi
    fi
  fi
  while :; do
    [ -z "$ADMIN_ID" ] && ask ADMIN_ID "$(t "Telegram ID (цифры; несколько — через запятую)" "Telegram ID (digits; several — comma-separated)")"
    ADMIN_ID=$(printf '%s' "$ADMIN_ID" | tr -d '[:space:]')
    [[ "$ADMIN_ID" =~ ^[0-9]{4,15}(,[0-9]{4,15})*$ ]] && return
    warn "$(t "ID — это только цифры, например 123456789" "the ID is digits only, e.g. 123456789")"
    [ "$ASSUME_YES" = 1 ] && die "--admin-id"
    ADMIN_ID=""
  done
}

check_dns() {  # 0 = points here (or cannot tell), 1 = does not
  local ip dip
  ip=$(public_ip); dip=$(domain_ip "$DOMAIN")
  if [ -z "$dip" ]; then
    warn "$(t "$DOMAIN пока никуда не указывает." "$DOMAIN does not resolve yet.")"
    hint "$(t "У регистратора домена создайте A-запись: $DOMAIN → ${ip:-IP этого сервера}" "At your registrar add an A record: $DOMAIN → ${ip:-the IP of this server}")"
    return 1
  fi
  if [ -n "$ip" ] && [ "$dip" != "$ip" ]; then
    warn "$(t "$DOMAIN указывает на $dip, а этот сервер — $ip." "$DOMAIN points to $dip, but this server is $ip.")"
    hint "$(t "Исправьте A-запись: $DOMAIN → $ip (если домен за Cloudflare с оранжевым облаком — выключите его)." \
              "Fix the A record: $DOMAIN → $ip (behind Cloudflare's orange cloud? turn it off).")"
    return 1
  fi
  ok "$DOMAIN → $dip"
}

q_address() {
  step "3/6" "$(t "Адрес магазина" "Shop address")"
  hint "$(t "Telegram открывает мини-приложения только по https — адрес нужен обязательно." \
            "Telegram opens mini apps over https only, so the shop needs an address.")"
  if [ -z "$ADDR" ]; then
    if [ -n "$DOMAIN" ]; then ADDR=domain
    else
      if [ "${ADDR_BACK:-0}" = 1 ]; then                # from the menu: a way back
        choose ADDR \
          1 "$(t "У меня есть домен" "I have a domain")" "$(t "постоянный адрес — для настоящих продаж" "a permanent address — for real sales")" \
          2 "$(t "Домена пока нет" "No domain yet")" "$(t "временный адрес Cloudflare — чтобы попробовать; домен подключите потом" "a temporary Cloudflare address to try it out; add a domain later")" \
          0 "$(t "Назад" "Back")" ""
      else
        choose ADDR \
          1 "$(t "У меня есть домен" "I have a domain")" "$(t "постоянный адрес — для настоящих продаж" "a permanent address — for real sales")" \
          2 "$(t "Домена пока нет" "No domain yet")" "$(t "временный адрес Cloudflare — чтобы попробовать; домен подключите потом" "a temporary Cloudflare address to try it out; add a domain later")"
      fi
      [ "$ADDR" = 0 ] && return
      [ "$ADDR" = 1 ] && ADDR=domain || ADDR=tunnel
    fi
  fi
  if [ "$ADDR" = tunnel ]; then
    warn "$(t "Временный адрес меняется при перезапуске сервера. Бот перенастроится сам," \
              "The temporary address changes when the server restarts. The bot follows it,")"
    hint "$(t "но ссылки подписок, уже выданные клиентам, перестанут обновляться. Для продаж нужен домен:" \
              "but subscription links already given to customers stop updating. Sales need a domain:")"
    hint "$(t "его можно подключить позже командой vpn-seller → «Сменить адрес»." "add one later with vpn-seller → Change address.")"
    DOMAIN=""
    return
  fi
  while :; do
    [ -z "$DOMAIN" ] && ask DOMAIN "$(t "Домен, например vpn.example.com" "Domain, e.g. vpn.example.com")"
    DOMAIN=$(printf '%s' "$DOMAIN" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')
    DOMAIN=${DOMAIN#https://}; DOMAIN=${DOMAIN#http://}; DOMAIN=${DOMAIN%%/*}
    if [[ ! "$DOMAIN" =~ ^([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$ ]]; then
      warn "$(t "это не похоже на домен" "that does not look like a domain")"
      [ "$ASSUME_YES" = 1 ] && die "--domain"
      DOMAIN=""; continue
    fi
    check_dns && return
    [ "$ASSUME_YES" = 1 ] && return
    local next
    choose next \
      1 "$(t "Проверить ещё раз" "Check again")" "$(t "после изменения записи подождите 1–5 минут" "wait 1–5 minutes after changing the record")" \
      2 "$(t "Продолжить так" "Continue anyway")" "$(t "сертификат выпустится, когда домен заработает" "the certificate is issued once the domain works")" \
      3 "$(t "Другой домен" "Another domain")" "" \
      4 "$(t "Временный адрес Cloudflare" "Temporary Cloudflare address")" ""
    case "$next" in
      1) continue ;;
      2) return ;;
      3) DOMAIN="" ;;
      4) ADDR=tunnel; DOMAIN=""; return ;;
    esac
  done
}

q_password() {
  step "4/6" "$(t "Пароль админ-панели" "Admin panel password")"
  hint "$(t "Для входа в панель с компьютера. Из бота (/admin) панель открывается без пароля." \
            "For the panel on a computer. From the bot (/admin) it opens without one.")"
  while :; do
    if [ -z "$PASSWORD" ] && [ "$ASSUME_YES" != 1 ]; then
      ask_secret PASSWORD "$(t "Пароль (Enter — придумаю сам)" "Password (Enter — generate one)")"
    fi
    if [ -z "$PASSWORD" ]; then PASSWORD=$(random_pass); GENERATED=1; ok "$(t "придумал надёжный пароль — покажу в конце" "generated a strong one — shown at the end")"; return; fi
    # These break .env (Docker reads $ as a variable) or the shell.
    local bad_re
    bad_re=$(printf '[[:space:]$"\\\\%s%s]' "'" '`')
    # 10+: the panel is on the internet, and the login name is always "admin".
    if [ ${#PASSWORD} -lt 10 ]; then warn "$(t "нужно от 10 символов" "at least 10 characters")"
    elif [[ "$PASSWORD" =~ $bad_re ]]; then warn "$(t "без пробелов, кавычек и знаков \$ и обратной косой черты" "no spaces, quotes, dollar signs or backslashes")"
    else ok "$(t "пароль принят" "password set")"; return; fi
    [ "$ASSUME_YES" = 1 ] && die "--password"
    PASSWORD=""
  done
}

q_currency() {
  step "5/6" "$(t "Валюта цен" "Price currency")"
  hint "$(t "Цены тарифов меняются в админке; валюта — одна на магазин." "Plan prices are edited in the panel; one currency per shop.")"
  while :; do
    if [ -z "$CURRENCY" ]; then
      if [ "$ASSUME_YES" = 1 ]; then CURRENCY=RUB
      else ask CURRENCY "$(t "RUB, USD, EUR, UAH, KZT, BYN…" "RUB, USD, EUR, UAH, KZT, BYN…")" "$(t RUB USD)"; fi
    fi
    CURRENCY=$(printf '%s' "$CURRENCY" | tr '[:lower:]' '[:upper:]' | tr -d '[:space:]')
    [[ "$CURRENCY" =~ ^[A-Z]{3}$ ]] && { ok "$CURRENCY"; return; }
    warn "$(t "три латинские буквы, например RUB" "three letters, e.g. USD")"
    [ "$ASSUME_YES" = 1 ] && die "--currency"
    CURRENCY=""
  done
}

q_database() {
  step "6/6" "$(t "База данных" "Database")"
  case "$DB" in ""|sqlite|postgres) ;; *) die "--db sqlite | postgres" ;; esac
  if installed && [ -n "$(get_env SECRET_KEY)" ]; then
    # reinstall: the customers stay where they are; the move has its own command
    DB=$(db_kind)
    ok "$(t "остаётся как была:" "kept as it is:") $(db_label "$DB")"
    if [ "$DB" = sqlite ]; then
      hint "$(t "Перейти на PostgreSQL можно в любой момент: админка → «База данных» или меню vpn-seller." \
                "Move to PostgreSQL any time: admin → Database or the vpn-seller menu.")"
    fi
    return
  fi
  local mem rec c memh
  mem=$(mem_mb); memh=$(mem_human "$mem")
  if [ "${mem:-0}" -ge "$PG_COMFORT_MB" ]; then rec=postgres; else rec=sqlite; fi
  if [ -z "$DB" ]; then
    if [ "$ASSUME_YES" = 1 ]; then DB=$rec
    else
      hint "$(t "Где хранить клиентов, подписки и платежи. На сервере $memh памяти." \
                "Where customers, subscriptions and payments are kept. This server has $memh of memory.")"
      local l_sq l_pg h_sq h_pg
      l_sq="$(t "SQLite — встроенная, ничего лишнего" "SQLite — built in, nothing extra")"
      l_pg="$(t "PostgreSQL — для большого магазина" "PostgreSQL — for a bigger shop")"
      h_sq="$(t "хватает до ~500 клиентов с подпиской" "enough for ~500 paying customers")"
      h_pg="$(t "занимает ~100–150 МБ памяти, лучше от 2 ГБ" "takes ~100–150 MB of memory, best with 2 GB+")"
      if [ "$rec" = postgres ]; then
        choose c 1 "$l_pg" "$(t "рекомендуем для этого сервера" "recommended for this server") · $h_pg" 2 "$l_sq" "$h_sq"
        if [ "$c" = 1 ]; then DB=postgres; else DB=sqlite; fi
      else
        choose c 1 "$l_sq" "$(t "рекомендуем для этого сервера" "recommended for this server") · $h_sq" 2 "$l_pg" "$h_pg"
        if [ "$c" = 1 ]; then DB=sqlite; else DB=postgres; fi
      fi
    fi
  fi
  ok "$(db_label "$DB")"
  if [ "$DB" = postgres ] && [ "${mem:-0}" -lt "$PG_COMFORT_MB" ]; then
    warn "$(t "памяти $memh — PostgreSQL заработает, но магазину останется меньше" \
              "$memh of memory — PostgreSQL works, but leaves the shop less room")"
  fi
  if [ "$DB" = sqlite ]; then
    hint "$(t "Перейти на PostgreSQL можно в любой момент: админка → «База данных»." \
              "You can move to PostgreSQL any time: admin → Database.")"
  fi
}

# ------------------------------------------------------------ who has 80/443
port_holder() {  # prints "docker <container>" | "proc <name>" | ""
  local p=$1 ctr line name
  ctr=$(docker ps --filter "publish=$p" --format '{{.Names}}' 2>/dev/null | head -1 || true)
  [ -n "$ctr" ] && { printf 'docker %s' "$ctr"; return; }
  line=$(ss -ltnpH "( sport = :$p )" 2>/dev/null | head -1 || true)
  [ -z "$line" ] && return 0
  name=$(printf '%s' "$line" | grep -o 'users:(("[^"]*"' | cut -d'"' -f2 || true)
  printf 'proc %s' "${name:-?}"
}

is_ours() {  # the container belongs to this installation
  local wd
  wd=$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' "$1" 2>/dev/null || true)
  [ -n "$wd" ] && [ "$(readlink -f "$wd")" = "$(readlink -f "$DIR")" ]
}

proxy_network() {  # a user-defined network of the proxy container
  docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{"\n"}}{{end}}' "$1" 2>/dev/null \
    | grep -vE '^(bridge|host|none)?$' | head -1 || true
}

find_caddyfile() {  # sets CADDY_CFG (inside the container) and CADDY_HOSTFILE (on this server)
  local ctr=$1 cmd src dst
  CADDY_CFG=/etc/caddy/Caddyfile CADDY_HOSTFILE=""
  cmd=$(docker inspect -f '{{join .Config.Cmd " "}}' "$ctr" 2>/dev/null || true)
  if [[ " $cmd " =~ --config[=\ ]([^ ]+) ]]; then CADDY_CFG=${BASH_REMATCH[1]}; fi
  while IFS='|' read -r src dst; do
    [ -z "$dst" ] && continue
    if [ "$dst" = "$CADDY_CFG" ]; then CADDY_HOSTFILE=$src; break; fi
    case "$CADDY_CFG" in "$dst"/*) CADDY_HOSTFILE="$src${CADDY_CFG#"$dst"}" ;; esac
  done < <(docker inspect -f '{{range .Mounts}}{{.Source}}|{{.Destination}}{{"\n"}}{{end}}' "$ctr" 2>/dev/null || true)
  [ -n "$CADDY_HOSTFILE" ] && [ -f "$CADDY_HOSTFILE" ] && [[ "$CADDY_CFG" != *.json ]]
}

decide_mode() {
  if [ "$ADDR" = tunnel ]; then MODE=tunnel; return; fi
  local h80 h443 h kind ctr image
  h443=$(port_holder 443); h80=$(port_holder 80)
  h=${h443:-$h80}
  if [ -z "$h" ]; then MODE=caddy; ok "$(t "порты 80 и 443 свободны — поставлю свой Caddy с сертификатом" "ports 80 and 443 are free — installing Caddy with a certificate")"; return; fi
  kind=${h%% *}; ctr=${h#* }
  if [ "$kind" = docker ] && is_ours "$ctr"; then MODE=caddy; ok "$(t "порты 80/443 заняты нашим же Caddy" "ports 80/443 are held by our own Caddy")"; return; fi
  if [ "$kind" = docker ]; then
    image=$(docker inspect -f '{{.Config.Image}}' "$ctr" 2>/dev/null || true)
    PROXY_CTR=$ctr
    PROXY_NET=$(proxy_network "$ctr")
    MODE=external
    if [[ "$image" == *caddy* ]]; then
      PROXY_KIND=caddy
      info "$(t "Порты 80/443 занимает Caddy другого проекта (контейнер $ctr)." "Ports 80/443 belong to another project's Caddy (container $ctr).")"
      if find_caddyfile "$ctr"; then
        ok "$(t "Подключусь к нему: магазин войдёт в его сеть, а в его Caddyfile допишу блок сайта" "I will plug into it: the shop joins its network and I add a site block to its Caddyfile")"
        hint "$(t "Перед правкой сделаю копию, проверю конфиг (caddy validate) и при ошибке верну как было." \
                  "A backup is made first; the config is validated and restored on any error.")"
      else
        PROXY_NOTE=manual
        warn "$(t "Его Caddyfile не лежит на диске сервера — вписать блок сам не смогу, покажу что добавить." \
                  "Its Caddyfile is not a file on this server — I will show what to add.")"
      fi
    else
      PROXY_KIND=other PROXY_NOTE=manual
      info "$(t "Порты 80/443 занимает контейнер $ctr ($image)." "Ports 80/443 belong to container $ctr ($image).")"
      ok "$(t "Магазин войдёт в его сеть под именем $ALIAS; что вписать в его настройки — покажу в конце." \
              "The shop joins its network as $ALIAS; I will show what to add to its config.")"
    fi
    return
  fi
  # A program on the server itself (nginx, apache, a host Caddy…)
  PROXY_KIND=$ctr MODE=local PROXY_NOTE=manual
  info "$(t "Порты 80/443 занимает программа «$ctr» на самом сервере." "Ports 80/443 are held by $ctr on the server itself.")"
  if [ "$ASSUME_YES" = 1 ]; then
    ok "$(t "Магазин будет слушать только 127.0.0.1:8080 — «$ctr» будет проксировать на него." "The shop listens on 127.0.0.1:8080 only; $ctr proxies to it.")"
    return
  fi
  local c
  choose c \
    1 "$(t "Подключить через «$ctr»" "Go through $ctr")" "$(t "магазин слушает 127.0.0.1:8080, я покажу 3 строки для настроек «$ctr»" "the shop listens on 127.0.0.1:8080; I show 3 lines for $ctr")" \
    2 "$(t "Временный адрес Cloudflare" "Temporary Cloudflare address")" "$(t "ничего не трогать, домен подключить позже" "touch nothing, add a domain later")"
  if [ "$c" = 2 ]; then MODE=tunnel ADDR=tunnel DOMAIN="" PROXY_KIND="" PROXY_NOTE=""; fi
}

# ------------------------------------------------------------------ Caddyfile of another project
caddy_exec() { docker exec "$PROXY_CTR" caddy "$@" --config "$CADDY_CFG" --adapter caddyfile; }

caddy_strip_block() {  # remove our block (between the markers); keeps the inode (bind mounts)
  local f=$1 tmp
  grep -qF "$MARK_BEGIN" "$f" || return 0
  tmp=$(mktemp)
  # Also drops the one blank line that was added in front of the block.
  awk -v b="$MARK_BEGIN" -v e="$MARK_END" '
    $0 == b { skip = 1; if (pend > 0) pend--; next }
    skip    { if ($0 == e) skip = 0; next }
    /^$/    { pend++; next }
            { while (pend > 0) { print ""; pend-- } print }
    END     { while (pend > 0) { print ""; pend-- } }' "$f" > "$tmp"
  cat "$tmp" > "$f"; rm -f "$tmp"
}

caddy_add_site() {
  local f=$CADDY_HOSTFILE bak="$CADDY_HOSTFILE.vpnshop.bak" out
  cp -p "$f" "$bak"
  caddy_strip_block "$f"
  # Append only (never sed -i or mv): a Caddyfile mounted as a single file
  # must keep its inode, or the container keeps seeing the old one.
  if [ -n "$(tail -c1 "$f")" ]; then printf '\n' >> "$f"; fi
  if [ -s "$f" ] && [ -n "$(tail -n1 "$f")" ]; then printf '\n' >> "$f"; fi
  printf '%s\n%s {\n\tencode zstd gzip\n\treverse_proxy %s:8000\n}\n%s\n' \
    "$MARK_BEGIN" "$DOMAIN" "$ALIAS" "$MARK_END" >> "$f"
  if ! out=$(caddy_exec validate 2>&1); then
    cat "$bak" > "$f"
    warn "$(t "caddy validate нашёл ошибку — вернул Caddyfile как было:" "caddy validate found an error — Caddyfile restored:")"
    printf '%s\n' "$out" | grep -iE 'error|Error' | tail -n 3 | sed 's/^/      /'
    return 1
  fi
  ok "$(t "caddy validate: конфиг в порядке" "caddy validate: config is fine")"
  if caddy_exec reload >> "$LOG" 2>&1; then ok "$(t "Caddy перечитал настройки без остановки" "Caddy reloaded without downtime")"
  else
    info "$(t "reload недоступен — перезапускаю контейнер $PROXY_CTR" "reload is unavailable — restarting $PROXY_CTR")"
    docker restart "$PROXY_CTR" >> "$LOG" 2>&1 || { cat "$bak" > "$f"; return 1; }
  fi
  grep -q 'auto_https *off' "$f" && warn "$(t "в этом Caddyfile выключен auto_https — сертификат сам не выпустится" "auto_https is off in this Caddyfile — no automatic certificate")"
  return 0
}

caddy_remove_site() {  # used by uninstall / address change
  [ -n "$CADDY_HOSTFILE" ] && [ -f "$CADDY_HOSTFILE" ] || return 0
  grep -qF "$MARK_BEGIN" "$CADDY_HOSTFILE" || return 0
  local bak="$CADDY_HOSTFILE.vpnshop.bak"
  cp -p "$CADDY_HOSTFILE" "$bak"
  caddy_strip_block "$CADDY_HOSTFILE"
  if caddy_exec validate >> "$LOG" 2>&1; then
    caddy_exec reload >> "$LOG" 2>&1 || docker restart "$PROXY_CTR" >> "$LOG" 2>&1 || true
    ok "$(t "блок магазина убран из Caddyfile $PROXY_CTR" "the shop block was removed from $PROXY_CTR's Caddyfile")"
  else
    cat "$bak" > "$CADDY_HOSTFILE"
  fi
}

# ------------------------------------------------------------------ files
write_override() {
  local f="$DIR/docker-compose.override.yml"
  case "$MODE" in
    external)
      if [ -z "$PROXY_NET" ]; then
        # The proxy sits on the default bridge, where names do not resolve:
        # give it and the shop a small shared network.
        PROXY_NET=vpnshop_proxy
        docker network inspect "$PROXY_NET" >/dev/null 2>&1 || docker network create "$PROXY_NET" >> "$LOG" 2>&1
        docker network connect "$PROXY_NET" "$PROXY_CTR" >> "$LOG" 2>&1 || true
      fi
      cat > "$f" <<EOF
# Written by install.sh: the existing proxy ($PROXY_CTR) reaches the shop
# as $ALIAS:8000 on its network. Nothing is published to the internet.
services:
  app:
    networks:
      default:
        aliases: [$ALIAS]
      proxy:
        aliases: [$ALIAS]
networks:
  proxy:
    external: true
    name: $PROXY_NET
EOF
      ;;
    local)
      cat > "$f" <<EOF
# Written by install.sh: a proxy on this server (${PROXY_KIND:-nginx}) reaches the
# shop at 127.0.0.1:8080. Bound to localhost only — not reachable from outside.
services:
  app:
    ports:
      - "127.0.0.1:8080:8000"
EOF
      ;;
    *) rm -f "$f" ;;
  esac
}

write_env() {
  # Readable by root only — and so before the first secret is written.
  if [ ! -f "$DIR/.env" ]; then
    (umask 077; cp "$DIR/.env.example" "$DIR/.env")
    chmod 600 "$DIR/.env"
    set_env SECRET_KEY "$(random_hex 32)"
  else
    chmod 600 "$DIR/.env"
    (umask 077; cp -p "$DIR/.env" "$DIR/.env.bak")
    chmod 600 "$DIR/.env.bak"
    [ "$(get_env SECRET_KEY | wc -c)" -gt 32 ] || set_env SECRET_KEY "$(random_hex 32)"
  fi
  set_env BOT_TOKEN "$TOKEN"
  set_env ADMIN_LOGIN admin
  set_env ADMIN_PASSWORD "$PASSWORD"
  set_env ADMIN_TELEGRAM_IDS "$ADMIN_ID"
  set_env CURRENCY "$CURRENCY"
  set_env DEFAULT_LANG "$UI"
  if [ "$DB" = postgres ] && [ "$(db_kind)" != postgres ]; then
    local pgp; pgp=$(get_env POSTGRES_PASSWORD); [ -n "$pgp" ] || pgp=$(random_pass)
    set_env POSTGRES_PASSWORD "$pgp"
    set_env DATABASE_URL "$(pg_url "$pgp")"
  fi
  write_address_env
  mkdir -p "$DIR/data"
}

write_address_env() {
  set_env INSTALL_MODE "$MODE"
  set_env PROXY_CONTAINER "$PROXY_CTR"
  if [ "$MODE" = tunnel ]; then
    set_env DOMAIN ""
    set_env PUBLIC_URL ""
    set_env TUNNEL_METRICS_URL "http://tunnel:2000"
    set_profiles tunnel
  else
    set_env DOMAIN "$DOMAIN"
    set_env PUBLIC_URL "https://$DOMAIN"
    set_env TUNNEL_METRICS_URL ""
    if [ "$MODE" = caddy ]; then set_profiles caddy; else set_profiles ""; fi
  fi
}

load_env() {  # read the saved answers back (update, status, menu)
  TOKEN=$(get_env BOT_TOKEN); DOMAIN=$(get_env DOMAIN); MODE=$(get_env INSTALL_MODE)
  PROXY_CTR=$(get_env PROXY_CONTAINER); ADMIN_ID=$(get_env ADMIN_TELEGRAM_IDS)
  PASSWORD=$(get_env ADMIN_PASSWORD); CURRENCY=$(get_env CURRENCY)
  if [ -z "$MODE" ]; then   # installed by an older install.sh
    if [ "$(get_env NO_CADDY)" = 1 ]; then MODE=local; else MODE=caddy; fi
  fi
  if [ "$MODE" = external ] && [ -n "$PROXY_CTR" ]; then find_caddyfile "$PROXY_CTR" || true; fi
}

# ------------------------------------------------------------------ start + checks
app_healthy() {
  compose exec -T app python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)" >/dev/null 2>&1
}
wait_app() {
  local i
  for i in $(seq 1 "${VPN_SHOP_WAIT_TRIES:-60}"); do app_healthy && return 0; sleep 2; done
  return 1
}
tunnel_url() {
  compose logs --no-color tunnel 2>/dev/null | grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' | tail -1 || true
}
wait_url() {  # wait_url URL seconds
  local end=$((SECONDS + $2))
  while [ $SECONDS -lt $end ]; do
    curl -fsS --max-time 8 "$1/health" >/dev/null 2>&1 && return 0
    sleep 3
  done
  return 1
}

start_shop() {
  step "·" "$(t "Запуск" "Starting")"
  # --force-recreate: the containers must pick up the .env just written.
  spin "$(t "Собираю и запускаю (первый раз — 2–5 минут)" "Building and starting (first time: 2–5 minutes)")" \
    compose up -d --build --remove-orphans --force-recreate \
    || die "$(t "не запустилось. Подробности:" "did not start. Details:") $LOG"
  spin "$(t "Жду, пока магазин ответит" "Waiting for the shop to answer")" wait_app \
    || die "$(t "магазин не отвечает. Журнал: vpn-seller → «Журнал» или docker compose -f $DIR/docker-compose.yml logs app" "the shop does not answer. See: docker compose logs app")"

  case "$MODE" in
    external)
      if [ "$PROXY_KIND" = caddy ] && [ "$PROXY_NOTE" != manual ]; then
        caddy_add_site || PROXY_NOTE=manual
      fi
      URL="https://$DOMAIN" ;;
    tunnel)
      local i
      for i in $(seq 1 30); do URL=$(tunnel_url); [ -n "$URL" ] && break; sleep 2; done
      [ -n "$URL" ] || die "$(t "туннель Cloudflare не выдал адрес. Попробуйте ещё раз через минуту: vpn-seller → «Сменить адрес»" "the Cloudflare tunnel gave no address. Try again in a minute.")"
      ok "$(t "Временный адрес:" "Temporary address:") $URL" ;;
    *) URL="https://$DOMAIN" ;;
  esac

  if [ "$MODE" = local ] || [ "$PROXY_NOTE" = manual ]; then
    return 0            # the proxy is configured by hand: checked after that
  fi
  if spin "$(t "Проверяю $URL (сертификат — до 2 минут)" "Checking $URL (certificate: up to 2 minutes)")" wait_url "$URL" 150; then
    CHECK_URL=ok
  else
    CHECK_URL=fail
    warn "$(t "$URL пока не открывается снаружи." "$URL does not open from outside yet.")"
    case "$MODE" in
      tunnel) hint "$(t "Новому адресу Cloudflare иногда нужна минута. Проверьте позже: vpn-seller → «Состояние»." "A new Cloudflare address sometimes needs a minute. Check later: vpn-seller → Status.")" ;;
      *) hint "$(t "Проверьте A-запись домена и что хостинг не закрывает порты 80/443 (фаервол в панели хостинга)." "Check the domain's A record and that the host's firewall allows ports 80/443.")" ;;
    esac
  fi
}

check_bot() {  # the webhook is set by the shop itself at start; show what Telegram says
  [ -n "$TOKEN" ] || return 0
  local want="$URL/api/bot/webhook" res got err i
  for i in $(seq 1 20); do
    res=$(tg getWebhookInfo); got=$(json_str "$res" url)
    [ "$got" = "$want" ] && break
    sleep 3
  done
  if [ "$got" != "$want" ]; then
    warn "$(t "бот ещё не подключён к магазину (Telegram видит: ${got:-пусто})" "the bot is not linked yet (Telegram has: ${got:-nothing})")"
    return 0
  fi
  err=$(json_str "$res" last_error_message)
  if [ -n "$err" ] && [ "${CHECK_URL:-}" != ok ]; then
    ok "$(t "бот подключён; Telegram пока не достучался: $err" "bot linked; Telegram cannot reach it yet: $err")"
  else
    ok "$(t "бот подключён: вебхук и кнопка «Открыть» настроены" "bot linked: webhook and the Open button are set")"
  fi
}

manual_proxy_help() {
  local up
  if [ "$MODE" = local ]; then up="127.0.0.1:8080"; else up="$ALIAS:8000"; fi
  printf '\n'
  rule
  say "  $B$(t "Осталось одно: направить домен на магазин" "One thing left: point the domain at the shop")$N"
  rule
  case "$PROXY_KIND" in
    caddy)
      say "  $(t "Добавьте в Caddyfile и перезагрузите Caddy:" "Add to the Caddyfile and reload Caddy:")"
      printf '\n    %s {\n        reverse_proxy %s\n    }\n\n' "$DOMAIN" "$up"
      if [ "$MODE" = external ]; then hint "docker exec $PROXY_CTR caddy reload --config ${CADDY_CFG:-/etc/caddy/Caddyfile}"
      else hint "systemctl reload caddy"; fi ;;
    nginx|*nginx*|other|"")
      say "  $(t "Для nginx — новый файл /etc/nginx/sites-enabled/vpn-seller:" "For nginx — a new file /etc/nginx/sites-enabled/vpn-seller:")"
      printf '\n    server {\n        server_name %s;\n        location / {\n            proxy_pass http://%s;\n            proxy_set_header Host $host;\n            proxy_set_header X-Forwarded-For $remote_addr;\n            proxy_set_header X-Forwarded-Proto $scheme;\n        }\n    }\n\n' "$DOMAIN" "$up"
      hint "$(t "затем: certbot --nginx -d $DOMAIN  и  systemctl reload nginx" "then: certbot --nginx -d $DOMAIN  and  systemctl reload nginx")" ;;
    *)
      say "  $(t "В настройках «$PROXY_KIND» направьте $DOMAIN на" "In $PROXY_KIND send $DOMAIN to") http://$up" ;;
  esac
  hint "$(t "После этого проверьте: vpn-seller → «Состояние»." "Then check: vpn-seller → Status.")"
}

summary() {
  local gen="" admin_url
  [ "$GENERATED" = 1 ] && gen="  $D$(t "(придуман — сохраните)" "(generated — save it)")$N"
  admin_url="${URL:-https://$DOMAIN}/admin"
  printf '\n'
  rule
  printf '  %s✓ %s%s\n' "$G$B" "$(t "VPN Seller установлен" "VPN Seller is installed")" "$N"
  rule
  row "$(t "Бот" "Bot")" "https://t.me/${BOT:-$(t "ваш_бот" "your_bot")}"
  row "$(t "Админ-панель" "Admin panel")" "$admin_url"
  row "$(t "Логин" "Login")" "admin"
  row "$(t "Пароль" "Password")" "$PASSWORD""$gen"
  row "$(t "Управление" "Manage")" "vpn-seller  $D$(t "(меню: обновить, журнал, бэкап…)" "(menu: update, logs, backup…)")$N"
  if [ "$MODE" = local ] || [ "${PROXY_NOTE:-}" = manual ]; then manual_proxy_help; fi
  printf '\n  %s%s%s\n' "$B" "$(t "Дальше — 5 минут в Telegram:" "Next — 5 minutes in Telegram:")" "$N"
  say "   1. $(t "Откройте бота и отправьте /admin — откроется панель, вкладка «Запуск» ведёт по шагам." "Open the bot and send /admin — the panel opens; the Launch tab walks you through.")"
  say "   2. $(t "«Серверы» → «Добавить»: адрес вашей панели 3x-ui (или Marzban, Remnawave) и API-токен → «Проверить»." "Servers → Add: your 3x-ui (or Marzban, Remnawave) panel address and API token → Check.")"
  say "   3. $(t "Магазин пустой: «Тарифы» → «Из шаблона», «Документы» → «Вставить шаблон». Скидок и рефералки нет, пока не включите." "The shop is empty: Plans → From a template, Documents → Insert the template. No discounts or referrals until you turn them on.")"
  say "   4. $(t "Stars работают сразу; карты и крипта — на сервере: vpn-seller → «Способы оплаты». Затем купите подписку сами и подключите ключ в Happ." "Stars work at once; cards and crypto — on the server: vpn-seller → Payment methods. Then buy a subscription yourself and add the key to Happ.")"
  printf '\n  %s%s %s/.env%s\n\n' "$D" "$(t "Пароль и настройки:" "Password and settings:")" "$DIR" "$N"
}

link_command() {
  ln -sf "$DIR/install.sh" "$BIN_LINK" 2>/dev/null && chmod +x "$DIR/install.sh" || true
  ln -sf "$DIR/install.sh" "$OLD_LINK" 2>/dev/null || true
}

# ------------------------------------------------------------------ commands
cmd_install() {
  need_root
  if installed && [ "$ASSUME_YES" != 1 ]; then
    warn "$(t "Магазин уже установлен в $DIR." "The shop is already installed in $DIR.")"
    confirm "$(t "Установить заново? Клиенты и заказы сохранятся" "Reinstall? Customers and orders are kept")" n || return 0
  fi
  step "·" "$(t "Подготовка сервера" "Preparing the server")"
  if [ -f /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release; info "${PRETTY_NAME:-Linux}, $(free -m 2>/dev/null | awk '/^Mem:/{print $2" MB RAM"}')"
  fi
  ensure_tools
  install_docker
  get_code
  q_token; q_admin; q_address; q_password; q_currency; q_database
  step "·" "$(t "Как магазин будет доступен" "How the shop will be reached")"
  decide_mode
  review
  write_env
  write_override
  start_shop
  check_bot
  link_command
  install_updater
  summary
}

review() {
  local how
  case "$MODE" in
    caddy) how="https://$DOMAIN  $D$(t "(свой Caddy, сертификат сам)" "(own Caddy, automatic certificate)")$N" ;;
    external) how="https://$DOMAIN  $D$(t "(через Caddy/прокси $PROXY_CTR)" "(through $PROXY_CTR)")$N" ;;
    local) how="https://$DOMAIN  $D$(t "(через «$PROXY_KIND» на сервере)" "(through $PROXY_KIND on the server)")$N" ;;
    tunnel) how="$(t "временный адрес Cloudflare" "temporary Cloudflare address")" ;;
  esac
  printf '\n'; rule
  row "$(t "Бот" "Bot")" "@${BOT:-?}"
  row "$(t "Админ" "Admin")" "$ADMIN_ID"
  row "$(t "Адрес" "Address")" "$how"
  row "$(t "Валюта" "Currency")" "$CURRENCY"
  row "$(t "База" "Database")" "$(db_label "${DB:-sqlite}")"
  row "$(t "Папка" "Folder")" "$DIR"
  rule
  confirm "$(t "Всё верно — начинаю?" "All correct — start?")" || die "$(t "остановлено, ничего не изменено" "stopped, nothing changed")"
}

cmd_address() {  # switch domain <-> tunnel, or change the domain
  need_root; installed || die "$(t "магазин не установлен" "the shop is not installed")"
  load_env
  local old_mode=$MODE old_ctr=$PROXY_CTR old_cfg=$CADDY_CFG old_file=$CADDY_HOSTFILE
  ADDR="" DOMAIN=""
  ADDR_BACK=1 q_address
  if [ "$ADDR" = 0 ]; then load_env; return 0; fi      # back: nothing changes
  step "·" "$(t "Как магазин будет доступен" "How the shop will be reached")"
  PROXY_CTR="" PROXY_NET="" PROXY_KIND="" PROXY_NOTE=""
  decide_mode
  confirm "$(t "Применить?" "Apply?")" || return 0
  if [ "$old_mode" = external ] && [ -n "$old_file" ]; then
    ( PROXY_CTR=$old_ctr CADDY_CFG=$old_cfg CADDY_HOSTFILE=$old_file; caddy_remove_site )
  fi
  write_address_env
  write_override
  start_shop
  check_bot
  printf '\n  %s %s/admin\n\n' "$(t "Админ-панель:" "Admin panel:")" "$URL"
  if [ "$MODE" = local ] || [ "${PROXY_NOTE:-}" = manual ]; then manual_proxy_help; fi
  return 0
}

cmd_settings() {  # change the token, admin IDs or the password
  need_root; installed || die "$(t "магазин не установлен" "the shop is not installed")"
  load_env
  local what
  step "·" "$(t "Что поменять?" "What to change?")"
  choose what \
    1 "$(t "Токен бота" "Bot token")" "$(t "например, после /revoke в @BotFather" "e.g. after /revoke in @BotFather")" \
    2 "$(t "ID администраторов" "Admin IDs")" "" \
    3 "$(t "Пароль админ-панели" "Admin panel password")" "" \
    0 "$(t "Назад" "Back")" ""
  case "$what" in
    1) TOKEN=""; q_token; set_env BOT_TOKEN "$TOKEN" ;;
    2) ADMIN_ID=""; q_admin; set_env ADMIN_TELEGRAM_IDS "$ADMIN_ID" ;;
    3) PASSWORD=""; q_password; set_env ADMIN_PASSWORD "$PASSWORD"
       [ "$GENERATED" = 1 ] && say "  $(t "Новый пароль:" "New password:") $PASSWORD" ;;
    *) return 0 ;;
  esac
  spin "$(t "Перезапускаю магазин" "Restarting the shop")" compose up -d --force-recreate app
  spin "$(t "Жду ответа" "Waiting")" wait_app || warn "$(t "не отвечает — смотрите журнал" "no answer — see the log")"
  URL=$( [ "$MODE" = tunnel ] && tunnel_url || printf 'https://%s' "$DOMAIN")
  [ "$what" = 1 ] && check_bot
  ok "$(t "готово" "done")"
}

ask_key() {  # ask_key ENV_NAME "Label" [secret] — keeps the old value on Enter; 0 — back
  local key=$1 label=$2 cur v bad
  [ "${KEY_CANCEL:-0}" = 1 ] && return 0              # already went back: ask nothing more
  cur=$(get_env "$key")
  label="$label $(t "(0 — назад)" "(0 — back)")"
  bad=$(printf '[[:space:]$"\\\\%s%s]' "'" '`')
  while :; do
    if [ "${3:-}" = secret ]; then
      ask_secret v "$label$( [ -n "$cur" ] && t " (Enter — оставить)" " (Enter — keep)")"
    else
      ask v "$label" "$cur"
    fi
    if [ "$v" = 0 ]; then KEY_CANCEL=1; return 0; fi
    v=${v:-$cur}
    if [ -z "$v" ]; then warn "$(t "нужно заполнить" "required")"; continue; fi
    if [[ "$v" =~ $bad ]]; then warn "$(t "в ключе не бывает пробелов, кавычек и \$ — скопируйте ещё раз" "keys have no spaces, quotes or \$ — copy it again")"; continue; fi
    set_env "$key" "$v"; return 0
  done
}

cmd_payments() {  # payment gateways: asked like the install, written to .env
  need_root; installed || die "$(t "магазин не установлен" "the shop is not installed")"
  load_env
  local base gw c
  base=$( [ "$MODE" = tunnel ] && tunnel_url || printf 'https://%s' "$DOMAIN")
  step "·" "$(t "Способы оплаты" "Payment methods")"
  gw=$(get_env CARD_GATEWAY)
  row "Telegram Stars" "$( [ "$(get_env STARS_ENABLED)" = false ] && t "выключены" "off" || t "работают" "on")"
  row "CryptoBot" "$( [ -n "$(get_env CRYPTOBOT_TOKEN)" ] && t "подключён" "connected" || t "нет" "no")"
  row "$(t "Карты и СБП" "Cards")" "${gw:-$(t "нет" "no")}"
  row "$(t "Чеки «Мой налог»" "«Мой налог» receipts")" "$( [ -n "$(get_env NALOG_INN)" ] && t "включены" "on" || t "нет" "no")"
  printf '\n'
  choose c \
    1 "CryptoBot" "$(t "крипта через @CryptoBot → Crypto Pay → Create App → токен" "crypto via @CryptoBot → Crypto Pay → Create App → token")" \
    2 "$(t "ЮKassa" "YooKassa")" "$(t "карты и СБП, нужны ИП или самозанятость" "cards and SBP (Russia)")" \
    3 "FreeKassa" "" \
    4 "Platega" "" \
    5 "PayPalych (Pally)" "" \
    8 "Lava" "$(t "карты и СБП, только рубли" "cards and SBP, roubles only")" \
    9 "WATA" "" \
    10 "Heleket" "$(t "крипта (вместо CryptoBot или вместе с ним)" "crypto (instead of CryptoBot or with it)")" \
    13 "Tribute" "$(t "карты, СБП, Telegram-кошелёк" "cards, SBP, Telegram wallet")" \
    14 "CloudPayments" "" \
    15 "MulenPay" "" \
    16 "Lave Pay ($(t "Лавешка" "Laveshka"))" "$(t "крипта и СБП, счета в долларах" "crypto and SBP, invoices in USD")" \
    17 "RollyPay" "$(t "СБП, карты и крипта; баланс в USDT" "SBP, cards and crypto; balance in USDT")" \
    18 "cisPay" "$(t "карты и СБП в рублях" "cards and SBP in roubles")" \
    11 "$(t "Чеки «Мой налог»" "«Мой налог» receipts")" "$(t "для самозанятых: чек сам уходит в налоговую после оплаты" "self-employed (Russia): a receipt after every payment")" \
    12 "$(t "Выключить чеки «Мой налог»" "Turn «Мой налог» receipts off")" "" \
    6 "$(t "Выключить оплату картой" "Turn card payments off")" "" \
    7 "$(t "Stars: включить / выключить" "Stars: on / off")" "" \
    0 "$(t "Назад" "Back")" ""
  [ "$c" = 0 ] && return 0
  KEY_CANCEL=0
  (umask 077; cp -p "$DIR/.env" "$DIR/.env.menu")
  case "$c" in
    1) ask_key CRYPTOBOT_TOKEN "$(t "Токен Crypto Pay" "Crypto Pay token")" secret
       hint "$(t "В @CryptoBot → Crypto Pay → ваше приложение → Webhooks включите и вставьте:" "In @CryptoBot → Crypto Pay → your app → Webhooks, enable and paste:")"
       say "    $base/api/pay/cryptobot" ;;
    2) ask_key YOOKASSA_SHOP_ID "shopId"; ask_key YOOKASSA_SECRET_KEY "$(t "Секретный ключ" "Secret key")" secret
       set_env CARD_GATEWAY yookassa
       hint "$(t "В кабинете ЮKassa → Интеграция → HTTP-уведомления укажите:" "In YooKassa → Integration → HTTP notifications set:")"
       say "    $base/api/pay/yookassa  $(t "(событие payment.succeeded)" "(event payment.succeeded)")" ;;
    3) ask_key FREEKASSA_MERCHANT_ID "$(t "ID магазина" "Shop ID")"
       ask_key FREEKASSA_SECRET1 "$(t "Секретное слово 1" "Secret word 1")" secret
       ask_key FREEKASSA_SECRET2 "$(t "Секретное слово 2" "Secret word 2")" secret
       set_env CARD_GATEWAY freekassa
       hint "$(t "В кабинете FreeKassa → Настройки магазина → URL оповещения:" "In FreeKassa → shop settings → notification URL:")"
       say "    $base/api/pay/freekassa" ;;
    4) ask_key PLATEGA_MERCHANT_ID "Merchant ID"; ask_key PLATEGA_SECRET "$(t "Секрет" "Secret")" secret
       set_env CARD_GATEWAY platega
       hint "$(t "В кабинете Platega укажите Callback URL:" "In Platega set the callback URL:")"
       say "    $base/api/pay/platega" ;;
    5) ask_key PAYPALYCH_TOKEN "$(t "API-токен" "API token")" secret; ask_key PAYPALYCH_SHOP_ID "$(t "ID магазина" "Shop ID")"
       set_env CARD_GATEWAY paypalych
       hint "$(t "В кабинете Pally → магазин → Result URL:" "In Pally → shop → Result URL:")"
       say "    $base/api/pay/paypalych" ;;
    8) ask_key LAVA_SHOP_ID "$(t "ID магазина" "Shop ID")"
       ask_key LAVA_SECRET_KEY "$(t "Секретный ключ" "Secret key")" secret
       ask_key LAVA_ADDITIONAL_KEY "$(t "Дополнительный ключ" "Additional key")" secret
       set_env CARD_GATEWAY lava
       hint "$(t "В кабинете Lava → магазин → Webhook URL:" "In Lava → shop → webhook URL:")"
       say "    $base/api/pay/lava" ;;
    9) ask_key WATA_TOKEN "$(t "API-токен" "API token")" secret
       set_env CARD_GATEWAY wata
       hint "$(t "В кабинете WATA укажите адрес уведомлений:" "In WATA set the webhook URL:")"
       say "    $base/api/pay/wata" ;;
    10) ask_key HELEKET_MERCHANT_ID "Merchant ID"; ask_key HELEKET_API_KEY "$(t "API-ключ (Payment key)" "API key (Payment key)")" secret
       set_env CRYPTO_GATEWAY heleket
       hint "$(t "Адрес уведомлений уже передаётся в каждом счёте, ничего вводить в кабинете не нужно:" "The callback URL is sent with every invoice, nothing to set in the dashboard:")"
       say "    $base/api/pay/heleket" ;;
    11) hint "$(t "ИНН и пароль — те же, что для входа на lknpd.nalog.ru. Если в пароле есть пробел, кавычки или \$, смените его там на пароль без них." "The INN and password you use on lknpd.nalog.ru. If the password has spaces, quotes or \$, change it there first.")"
       local inn
       while :; do
         ask inn "$(t "ИНН (12 цифр, 0 — назад)" "INN (12 digits, 0 — back)")" "$(get_env NALOG_INN)"
         [ "$inn" = 0 ] && { KEY_CANCEL=1; break; }
         [[ "$inn" =~ ^[0-9]{12}$ ]] && break
         warn "$(t "ИНН самозанятого — ровно 12 цифр" "a personal INN is exactly 12 digits")"
       done
       [ "${KEY_CANCEL:-0}" = 1 ] || set_env NALOG_INN "$inn"
       ask_key NALOG_PASSWORD "$(t "Пароль от «Мой налог»" "«Мой налог» password")" secret
       hint "$(t "Чеки создаются для оплат через FreeKassa, Platega, Pally, Lava и WATA. ЮKassa для самозанятых отправляет чеки сама. Проверить вход: админка → Настройки → «Чеки «Мой налог»»." "Receipts are made for FreeKassa, Platega, Pally, Lava and WATA payments (YooKassa sends its own). Check: admin → Settings.")" ;;
    12) set_env NALOG_INN ""; set_env NALOG_PASSWORD "" ;;
    13) ask_key TRIBUTE_API_KEY "$(t "API-ключ" "API key")" secret
       set_env CARD_GATEWAY tribute
       hint "$(t "В Tribute → Магазин → API → Вебхуки укажите:" "In Tribute → Shop → API → Webhooks set:")"
       say "    $base/api/pay/tribute" ;;
    14) ask_key CLOUDPAYMENTS_PUBLIC_ID "Public ID"; ask_key CLOUDPAYMENTS_API_SECRET "$(t "Пароль для API" "API secret")" secret
       set_env CARD_GATEWAY cloudpayments
       hint "$(t "В кабинете CloudPayments → Настройки → Уведомления → Pay укажите:" "In CloudPayments → Settings → Notifications → Pay set:")"
       say "    $base/api/pay/cloudpayments" ;;
    15) ask_key MULENPAY_SHOP_ID "$(t "ID магазина" "Shop ID")"
       ask_key MULENPAY_API_KEY "$(t "API-ключ" "API key")" secret
       ask_key MULENPAY_SECRET_KEY "$(t "Секретный ключ" "Secret key")" secret
       set_env CARD_GATEWAY mulenpay
       hint "$(t "В кабинете MulenPay укажите Callback URL:" "In MulenPay set the callback URL:")"
       say "    $base/api/pay/mulenpay" ;;
    17) ask_key ROLLYPAY_API_KEY "$(t "API-ключ кассы (rpk_live_…)" "Terminal API key (rpk_live_…)")" secret
       ask_key ROLLYPAY_SIGNING_SECRET "$(t "Signing secret кассы" "Terminal signing secret")" secret
       set_env CARD_GATEWAY rollypay
       hint "$(t "В кассе RollyPay укажите Callback URL:" "In the RollyPay terminal set the callback URL:")"
       say "    $base/api/pay/rollypay" ;;
    18) ask_key CISPAY_SHOP_ID "$(t "ID магазина" "Shop ID")"
       ask_key CISPAY_API_KEY "$(t "Секретный ключ (cis_sec_…)" "Secret key (cis_sec_…)")" secret
       set_env CARD_GATEWAY cispay
       hint "$(t "В кабинете cisPay укажите адрес вебхука:" "In cisPay set the webhook address:")"
       say "    $base/api/pay/cispay" ;;
    16) ask_key LAVEPAY_API_KEY "$(t "API-ключ (LAVE-…)" "API key (LAVE-…)")" secret
       ask_key LAVEPAY_WEBHOOK_SECRET "$(t "Секрет вебхука (whsec_…)" "Webhook secret (whsec_…)")" secret
       if [ "$(get_env CURRENCY)" != USD ]; then ask_key LAVEPAY_RUB_PER_USD "$(t "Курс: сколько рублей стоит 1 доллар" "Rate: roubles per 1 USD")"; fi
       set_env CRYPTO_GATEWAY lavepay
       hint "$(t "В Лавешке → Сервисы → Для бизнеса → Lave Pay укажите вебхук:" "In Laveshka → Services → For business → Lave Pay set the webhook:")"
       say "    $base/api/pay/lavepay" ;;
    6) set_env CARD_GATEWAY "" ;;
    7) if [ "$(get_env STARS_ENABLED)" = false ]; then set_env STARS_ENABLED true; else set_env STARS_ENABLED false; fi ;;
    *) rm -f "$DIR/.env.menu"; return 0 ;;
  esac
  if [ "$KEY_CANCEL" = 1 ]; then
    mv -f "$DIR/.env.menu" "$DIR/.env"
    info "$(t "назад — ничего не поменялось" "back — nothing changed")"
    return 0
  fi
  rm -f "$DIR/.env.menu"
  spin "$(t "Перезапускаю магазин с новыми настройками" "Restarting with the new settings")" compose up -d --force-recreate app \
    && spin "$(t "Жду ответа" "Waiting")" wait_app \
    && ok "$(t "готово — проверьте оплату в мини-приложении; кассы настраиваются и в админке → «Оплата»" "done — try a payment in the mini app; gateways are also set up in admin → Payments")" \
    || warn "$(t "магазин не отвечает — смотрите журнал" "the shop does not answer — see the log")"
}

# ------------------------------------------------------------------ updates
# The version of the code in $DIR (the VERSION file; older copies have none).
code_version() { { tr -d ' \r\n' < "$DIR/VERSION"; } 2>/dev/null || true; }

# fetch_code runs inside spin (a subshell): why it failed goes through a file.
upd_fail() { printf '%s' "$1" > "${UPD_REASON_FILE:-/dev/null}" 2>/dev/null || true; }

json_str_esc() {  # stdin → the inside of a JSON string
  sed -e 's/\x1b\[[0-9;?]*[A-Za-z]//g' -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e 's/\t/ /g' -e 's/\r//g' \
    | tr -d '\000-\010\013\014\016-\037' | awk 'BEGIN{ORS=""} NR>1{print "\\n"} {print}'
}

# The state of an update for the panel and the owners' message:
# running | ok | failed, the versions, why it failed and the last lines of the log.
upd_status() {  # upd_status state [reason]
  local f="$DIR/data/update.status" reason log
  mkdir -p "$DIR/data" 2>/dev/null || return 0
  reason=$(printf '%s' "${2:-}" | json_str_esc)
  log=$(tail -n 40 "$LOG" 2>/dev/null | json_str_esc || true)
  printf '{"state":"%s","from":"%s","to":"%s","at":%s,"reason":"%s","log":"%s"}\n' \
    "$1" "${FROM_VER:-}" "${TO_VER:-}" "$(date +%s)" "$reason" "$log" > "$f.tmp" 2>/dev/null \
    && chmod 644 "$f.tmp" && mv -f "$f.tmp" "$f" || true
}

repo_slug() {  # owner/name the new code comes from, or "" when not set
  local s
  s=$(get_env UPDATE_REPO); s=${s:-$GITHUB_REPO}
  s=${s#https://github.com/}; s=${s#http://github.com/}; s=${s#github.com/}; s=${s#git@github.com:}
  s=${s%.git}; s=${s%/}
  case "$s" in OWNER/*|"") return 0 ;; esac
  if [[ "$s" =~ ^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$ ]]; then printf '%s' "$s"; fi
  return 0
}

# Keep the code as it is now, to put it back if the new version does not start.
snapshot_code() {
  PREV_REV="" PREV_TGZ="$DIR/data/update-prev.tgz"
  if [ -d "$DIR/.git" ]; then PREV_REV=$(git -C "$DIR" rev-parse HEAD 2>/dev/null || true); fi
  tar -czf "$PREV_TGZ" -C "$DIR" --exclude=./data --exclude=./.env --exclude=./.git \
    --exclude=./docker-compose.override.yml --exclude=./docs . 2>> "$LOG"
}
restore_code() {
  if [ -n "${PREV_REV:-}" ]; then git -C "$DIR" reset -q --hard "$PREV_REV" >> "$LOG" 2>&1 || true; fi
  # tar puts a new file in place of each old one: a script that is running keeps reading its own copy
  if [ -f "${PREV_TGZ:-}" ]; then tar -xzf "$PREV_TGZ" -C "$DIR" 2>> "$LOG" || true; fi
  return 0
}

# The archive of the repository from GitHub, over the code in $DIR
# (.env, data/ and the proxy override are not in it and stay as they are).
fetch_tarball() {  # fetch_tarball owner/name
  local slug=$1 branch tok tmp hdr=()
  branch=$(get_env UPDATE_BRANCH); branch=${branch:-$BRANCH}
  tok=$(get_env GITHUB_TOKEN)
  if [ -n "$tok" ]; then hdr=(-H "Authorization: Bearer $tok"); fi
  tmp=$(mktemp -d)
  if ! curl -fsSL --max-time 300 -H "Accept: application/vnd.github+json" -H "User-Agent: vpn-seller" \
       ${hdr[@]+"${hdr[@]}"} "$GITHUB_API/repos/$slug/tarball/$branch" -o "$tmp/code.tgz"; then
    rm -rf "$tmp"
    upd_fail "$(t "GitHub не отдал код $slug ($branch). Закрытый репозиторий? Впишите GITHUB_TOKEN в .env" \
                  "GitHub did not give the code of $slug ($branch). A private repository? Put GITHUB_TOKEN into .env")"
    return 1
  fi
  mkdir "$tmp/x"
  if ! tar -xzf "$tmp/code.tgz" -C "$tmp/x" --strip-components=1 \
     || [ ! -f "$tmp/x/docker-compose.yml" ] || [ ! -d "$tmp/x/app" ] || [ ! -f "$tmp/x/install.sh" ]; then
    rm -rf "$tmp"
    upd_fail "$(t "архив с GitHub не похож на VPN Seller" "the archive from GitHub does not look like VPN Seller")"
    return 1
  fi
  rm -rf "$tmp/x/data" "$tmp/x/.env" "$tmp/x/docker-compose.override.yml"
  # new files, not rewritten ones: a script that is running keeps its own copy
  cp -a --remove-destination "$tmp/x/." "$DIR/"
  chmod +x "$DIR/install.sh" 2>/dev/null || true
  rm -rf "$tmp"
}

fetch_code() {  # 0 = the new code is in place, 1 = failed (upd_fail says why), 2 = nowhere to take it from
  if [ -d "$DIR/.git" ]; then
    git -C "$DIR" pull -q --ff-only && return 0
    upd_fail "$(t "git pull не прошёл — в $DIR есть свои правки кода" "git pull failed — $DIR has local code changes")"
    return 1
  fi
  local slug; slug=$(repo_slug)
  [ -n "$slug" ] || return 2
  fetch_tarball "$slug"
}

# The panel's and the bot's "Update" button: the shop leaves data/update.request,
# this unit notices it and runs the update here, on the server.
install_updater() {
  if [ -z "${VPN_SELLER_SYSTEMD_DIR:-}" ] && { ! have systemctl || [ ! -d /run/systemd/system ]; }; then
    rm -f "$DIR/data/updater.json"
    info "$(t "без systemd кнопка «Обновить» в панели не работает — обновляйте командой vpn-seller update" \
              "without systemd the panel's Update button does not work — update with vpn-seller update")"
    return 0
  fi
  mkdir -p "$SYSTEMD_DIR" "$DIR/data"
  local lang=""
  if [ "$UI" = en ]; then lang=" --en"; fi
  cat > "$SYSTEMD_DIR/$UPDATER.path" <<EOF
[Unit]
Description=VPN Seller: update when the admin panel asks

[Path]
PathExists=$DIR/data/update.request
Unit=$UPDATER.service

[Install]
WantedBy=multi-user.target
EOF
  cat > "$SYSTEMD_DIR/$UPDATER.service" <<EOF
[Unit]
Description=VPN Seller update (asked from the admin panel or the bot)
After=docker.service

[Service]
Type=oneshot
Environment=VPN_SHOP_DIR=$DIR
ExecStart=/bin/bash $DIR/install.sh update --from-panel$lang
TimeoutStartSec=1800
EOF
  systemctl daemon-reload >> "$LOG" 2>&1 || true
  if systemctl enable --now "$UPDATER.path" >> "$LOG" 2>&1; then
    printf '{"installed":%s}\n' "$(date +%s)" > "$DIR/data/updater.json" && chmod 644 "$DIR/data/updater.json"
    ok "$(t "кнопка «Обновить» в панели и в боте включена" "the Update button in the panel and the bot is on")"
  else
    rm -f "$DIR/data/updater.json"
    warn "$(t "не удалось включить кнопку «Обновить» (systemd)" "could not turn on the Update button (systemd)")"
  fi
}
remove_updater() {
  systemctl disable --now "$UPDATER.path" >> "$LOG" 2>&1 || true
  rm -f "$SYSTEMD_DIR/$UPDATER.path" "$SYSTEMD_DIR/$UPDATER.service"
  systemctl daemon-reload >> "$LOG" 2>&1 || true
}

# The new version did not start: the previous code goes back and is started again.
rollback() {  # rollback "why"
  warn "$1"
  info "$(t "возвращаю версию" "going back to") ${FROM_VER:-?}"
  restore_code
  if spin "$(t "Запускаю прежнюю версию" "Starting the previous version")" compose up -d --build --remove-orphans \
     && spin "$(t "Жду ответа" "Waiting for an answer")" wait_app; then
    upd_status failed "$1"
    die "$(t "обновление не удалось — вернул версию ${FROM_VER:-?}, магазин работает. Подробности: $LOG" \
             "the update failed — back on ${FROM_VER:-?}, the shop works. Details: $LOG")"
  fi
  upd_status failed "$1; $(t "прежняя версия тоже не отвечает" "the previous version does not answer either")"
  die "$(t "обновление не удалось, и прежняя версия не отвечает. Журнал: $LOG" \
           "the update failed and the previous version does not answer. Log: $LOG")"
}

cmd_update() {
  need_root; installed || die "$(t "не установлено в" "not installed in") $DIR"
  # The panel's database page asks through the same unit as the Update button.
  if [ "$FROM_PANEL" = 1 ] && grep -qs '"action": *"postgres"' "$DIR/data/update.request"; then
    rm -f "$DIR/data/update.request"
    cmd_db_postgres
    return
  fi
  load_env
  step "·" "$(t "Обновление" "Update")"
  [ "$FROM_PANEL" = 1 ] && info "$(t "запущено кнопкой «Обновить»" "started with the Update button")"
  FROM_VER=$(code_version) TO_VER="" UPD_REASON=""
  UPD_REASON_FILE=$(mktemp)
  TO_VER=$(sed -n 's/.*"to": *"\([0-9.]*\)".*/\1/p' "$DIR/data/update.request" 2>/dev/null | head -1 || true)
  rm -f "$DIR/data/update.request"
  upd_status running
  spin "$(t "Резервная копия базы" "Database backup")" compose exec -T -u app app python -m app.backup make \
    || warn "$(t "копия не получилась — продолжаю" "the backup failed — continuing")"
  snapshot_code || warn "$(t "не удалось сохранить прежний код — откат будет невозможен" \
                             "could not keep the previous code — no rollback possible")"
  local rc=0
  if [ -d "$DIR/.git" ]; then
    spin "$(t "Скачиваю новую версию (git)" "Downloading the new version (git)")" fetch_code || rc=$?
  elif [ -n "$(repo_slug)" ]; then
    spin "$(t "Скачиваю новую версию с GitHub" "Downloading the new version from GitHub")" fetch_code || rc=$?
  else
    rc=2
  fi
  case "$rc" in
    0) ;;
    2) warn "$(t "откуда брать новую версию, не задано (UPDATE_REPO в .env) — только пересобираю" \
                "where to take new versions from is not set (UPDATE_REPO in .env) — rebuilding only")" ;;
    *) UPD_REASON=$(cat "$UPD_REASON_FILE" 2>/dev/null || true); rm -f "$UPD_REASON_FILE"
       [ -n "$UPD_REASON" ] || UPD_REASON=$(t "не удалось скачать новую версию" "could not download the new version")
       rm -f "${PREV_TGZ:-}"
       upd_status failed "$UPD_REASON"; die "$UPD_REASON" ;;
  esac
  rm -f "$UPD_REASON_FILE"
  TO_VER=$(code_version)
  if [ -n "$FROM_VER" ] && [ "$TO_VER" = "$FROM_VER" ]; then info "$(t "версия" "version") $TO_VER"
  else info "$(t "версия" "version") ${FROM_VER:-?} → ${TO_VER:-?}"; fi
  # Older installs: no INSTALL_MODE/COMPOSE_PROFILES in .env yet.
  [ -n "$(get_env INSTALL_MODE)" ] || { set_env INSTALL_MODE "$MODE"; [ "$MODE" = caddy ] && set_profiles caddy; }
  grep -q '^COMPOSE_PROFILES=' "$DIR/.env" || { [ "$MODE" = caddy ] && set_profiles caddy || set_profiles ""; }
  spin "$(t "Пересобираю и перезапускаю" "Rebuilding and restarting")" compose up -d --build --remove-orphans \
    || rollback "$(t "новая версия не собралась" "the new version did not build")"
  spin "$(t "Жду ответа" "Waiting for an answer")" wait_app \
    || rollback "$(t "новая версия не отвечает" "the new version does not answer")"
  docker image prune -f >> "$LOG" 2>&1 || true
  rm -f "${PREV_TGZ:-}"
  link_command
  install_updater
  upd_status ok
  ok "$(t "обновлено" "updated")${TO_VER:+ — $TO_VER}"
}

# ------------------------------------------------------------------ SQLite → PostgreSQL
# Asked from the panel (admin → Database: data/update.request with
# "action":"postgres", taken by the Update button's unit), the menu or
# `vpn-seller postgres`. The SQLite file is only read; a backup comes first;
# the copy is counted table by table; on any failure the shop starts again
# on SQLite as it was. Progress goes to data/db.status for the panel.
db_status() {  # db_status state [reason] [customers]
  local f="$DIR/data/db.status" reason log
  mkdir -p "$DIR/data" 2>/dev/null || return 0
  reason=$(printf '%s' "${2:-}" | json_str_esc)
  log=$(tail -n 40 "$LOG" 2>/dev/null | json_str_esc || true)
  printf '{"state":"%s","at":%s,"reason":"%s","users":%s,"log":"%s"}\n' \
    "$1" "$(date +%s)" "$reason" "${3:-0}" "$log" > "$f.tmp" 2>/dev/null \
    && chmod 644 "$f.tmp" && mv -f "$f.tmp" "$f" || true
}
db_fail() {  # nothing was changed yet: just say why
  db_status failed "$1"
  die "$1"
}
db_back() {  # the shop was stopped or switched: back to SQLite, untouched
  warn "$1"
  set_env DATABASE_URL "$DB_OLD_URL"
  set_env COMPOSE_PROFILES "$DB_OLD_PROF"
  compose rm -sf db >> "$LOG" 2>&1 || true          # its volume stays: the next try starts it over
  if spin "$(t "Возвращаю магазин на SQLite" "Putting the shop back on SQLite")" compose up -d --force-recreate app \
     && spin "$(t "Жду ответа" "Waiting for an answer")" wait_app; then
    db_status failed "$1"
    die "$(t "переход не удался — магазин работает на SQLite, как раньше, данные не тронуты. Подробности: $LOG" \
             "the move failed — the shop works on SQLite as before, nothing was lost. Details: $LOG")"
  fi
  db_status failed "$1; $(t "магазин на SQLite тоже не ответил" "the shop on SQLite did not answer either")"
  die "$(t "переход не удался, и магазин не отвечает. Журнал: $LOG" "the move failed and the shop does not answer. Log: $LOG")"
}
pg_ready() {
  local i
  for i in $(seq 1 "${VPN_SHOP_PG_TRIES:-45}"); do
    compose exec -T db pg_isready -U vpn -d vpn >/dev/null 2>&1 && return 0
    sleep 2
  done
  return 1
}
db_copy() {  # db_copy URL OUTFILE — one-off container, the shop stopped
  compose run --rm --no-deps -T -e "DATABASE_URL=$SQLITE_URL" app \
    python -m app.dbmove copy --to "$1" --force > "$2"
}

cmd_db_postgres() {
  need_root; installed || die "$(t "не установлено в" "not installed in") $DIR"
  load_env
  step "·" "$(t "Переход на PostgreSQL" "Moving to PostgreSQL")"
  [ "$FROM_PANEL" = 1 ] && info "$(t "запущено из админ-панели" "started from the admin panel")"
  if [ "$(db_kind)" = postgres ]; then ok "$(t "магазин уже работает на PostgreSQL" "the shop already runs on PostgreSQL")"; return 0; fi
  local mem; mem=$(mem_mb)
  if [ "$FROM_PANEL" != 1 ]; then
    say "  $(t "Что будет:" "What happens:")"
    say "   · $(t "копия базы (как каждый день)" "a database backup (as every day)")"
    say "   · $(t "запуск PostgreSQL рядом с магазином" "PostgreSQL starts next to the shop")"
    say "   · $(t "пауза магазина на время переноса — обычно меньше минуты" "the shop pauses during the copy — usually under a minute")"
    say "   · $(t "проверка: в новой базе столько же записей, сколько в старой" "a check: the new database holds as many rows as the old one")"
    say "   · $(t "если что-то не так — магазин сам вернётся на SQLite" "if anything is off, the shop goes back to SQLite by itself")"
    if [ "${mem:-0}" -lt "$PG_COMFORT_MB" ]; then
      warn "$(t "на сервере $(mem_human "$mem") памяти — PostgreSQL займёт около 100–150 МБ" \
                "this server has $(mem_human "$mem") of memory — PostgreSQL takes about 100–150 MB")"
    fi
    printf '\n'
    confirm "$(t "Перейти на PostgreSQL?" "Move to PostgreSQL?")" || return 0
  fi
  DB_OLD_URL=$(get_env DATABASE_URL) DB_OLD_PROF=$(get_env COMPOSE_PROFILES)
  db_status running
  local sq="$DIR/data/vpn.db" need free
  # PostgreSQL holds about as much as the file, the backup about as much again
  need=$(( $(stat -c %s "$sq" 2>/dev/null || echo 0) * 3 / 1024 + 300 * 1024 ))
  free=$(df -Pk "$DIR" 2>/dev/null | awk 'NR==2{print $4}')
  if [ -n "$free" ] && [ "$free" -lt "$need" ]; then
    db_fail "$(t "мало места на диске: нужно около $((need / 1024)) МБ, свободно $((free / 1024)) МБ" \
                 "not enough disk space: about $((need / 1024)) MB needed, $((free / 1024)) MB free")"
  fi
  spin "$(t "Резервная копия базы" "Database backup")" compose exec -T -u app app python -m app.backup make \
    || db_fail "$(t "не получилась резервная копия — ничего не менял" "the backup failed — nothing was changed")"
  local pgp url base out res users
  pgp=$(get_env POSTGRES_PASSWORD)
  if [ -z "$pgp" ]; then pgp=$(random_pass); set_env POSTGRES_PASSWORD "$pgp"; fi
  url=$(pg_url "$pgp")
  base=$(base_profile)
  set_env COMPOSE_PROFILES "${base:+$base,}postgres"
  spin "$(t "Запускаю PostgreSQL" "Starting PostgreSQL")" compose up -d db \
    || db_back "$(t "PostgreSQL не запустился" "PostgreSQL did not start")"
  spin "$(t "Жду, пока PostgreSQL ответит" "Waiting for PostgreSQL to answer")" pg_ready \
    || db_back "$(t "PostgreSQL не отвечает" "PostgreSQL does not answer")"
  spin "$(t "Ставлю магазин на паузу" "Pausing the shop")" compose stop app \
    || db_back "$(t "магазин не остановился" "the shop did not stop")"
  out=$(mktemp)
  if ! spin "$(t "Переношу данные и сверяю" "Copying the data and checking it")" db_copy "$url" "$out"; then
    res=$(tail -n 1 "$out" 2>/dev/null || true); rm -f "$out"
    db_back "$(t "перенос не прошёл проверку" "the copy did not pass the check"): $(printf '%s' "$res" | sed -n 's/.*"error": *"\([^"]*\)".*"detail": *"\([^"]*\)".*/\1 \2/p' | cut -c1-200)"
  fi
  res=$(tail -n 1 "$out"); rm -f "$out"
  users=$(printf '%s' "$res" | sed -n 's/.*"users": *\([0-9]*\).*/\1/p')
  users=${users:-0}
  info "$(t "перенесено клиентов:" "customers moved:") $users"
  set_env DATABASE_URL "$url"
  spin "$(t "Запускаю магазин на PostgreSQL" "Starting the shop on PostgreSQL")" compose up -d --force-recreate app \
    || db_back "$(t "магазин не запустился на PostgreSQL" "the shop did not start on PostgreSQL")"
  spin "$(t "Жду ответа" "Waiting for an answer")" wait_app \
    || db_back "$(t "магазин не ответил на PostgreSQL" "the shop did not answer on PostgreSQL")"
  local k
  k=$(compose exec -T -u app app python -m app.dbmove kind 2>/dev/null | tail -n 1 | tr -d '\r' || true)
  [ "$k" = postgresql ] || db_back "$(t "магазин запустился не на PostgreSQL" "the shop did not start on PostgreSQL")"
  # The old file stays, renamed: kept for safety, never read by mistake.
  local stamp f; stamp=$(date +%Y%m%d-%H%M%S)
  for f in vpn.db vpn.db-wal vpn.db-shm; do
    if [ -f "$DIR/data/$f" ]; then mv -f "$DIR/data/$f" "$DIR/data/vpn-before-postgres-$stamp.${f#vpn.}"; fi
  done
  db_status ok "" "$users"
  ok "$(t "магазин работает на PostgreSQL" "the shop runs on PostgreSQL")"
  hint "$(t "Прежний файл SQLite сохранён: $DIR/data/vpn-before-postgres-$stamp.db" \
            "The old SQLite file is kept: $DIR/data/vpn-before-postgres-$stamp.db")"
}

cmd_status() {
  installed || die "$(t "магазин не установлен" "the shop is not installed")"
  load_env
  step "·" "$(t "Состояние" "Status")"
  compose ps --format '{{.Service}}\t{{.State}}\t{{.Status}}' 2>/dev/null | sed 's/^/    /' || compose ps
  if app_healthy; then ok "$(t "магазин отвечает" "the shop answers")"; else warn "$(t "магазин не отвечает" "the shop does not answer")"; fi
  URL=$( [ "$MODE" = tunnel ] && tunnel_url || printf 'https://%s' "$DOMAIN")
  if [ -n "$URL" ] && curl -fsS --max-time 8 "$URL/health" >/dev/null 2>&1; then ok "$URL $(t "открывается снаружи" "opens from outside")"
  else warn "${URL:-?} $(t "снаружи не открывается" "does not open from outside")"; fi
  CHECK_URL=ok check_bot
  info "$(t "Режим:" "Mode:") $MODE   $(t "Папка:" "Folder:") $DIR   $(t "Версия:" "Version:") $(code_version)"
}

cmd_restore() {
  need_root
  local file=${1:-}
  [ -f "$file" ] || die "vpn-seller restore <backup-….zip>"
  confirm "$(t "Заменить текущую базу архивом" "Replace the current database with") $(basename "$file")?" n || die "$(t "остановлено" "stopped")"
  mkdir -p "$DIR/data"
  cp "$file" "$DIR/data/restore.zip"
  compose stop app
  if [ "$(db_kind)" = postgres ]; then compose up -d db && pg_ready || die "$(t "PostgreSQL не отвечает" "PostgreSQL does not answer")"; fi
  compose run --rm app python -m app.backup restore /app/data/restore.zip --force
  compose start app
  rm -f "$DIR/data/restore.zip"
  ok "$(t "восстановлено" "restored")"
}

cmd_uninstall() {
  need_root; installed || die "$(t "не установлено в" "not installed in") $DIR"
  load_env
  confirm "$(t "Остановить и удалить контейнеры VPN Seller?" "Stop and remove the VPN Seller containers?")" n || return 0
  [ "$MODE" = external ] && caddy_remove_site
  compose down --remove-orphans
  remove_updater
  rm -f "$BIN_LINK" "$OLD_LINK"
  if confirm "$(t "Удалить и ВСЕ данные (клиенты, заказы, бэкапы) в $DIR?" "Also delete ALL data (customers, orders, backups) in $DIR?")" n; then
    compose down -v >> "$LOG" 2>&1 || true            # the PostgreSQL volume, if any
    rm -rf "$DIR"; ok "$(t "удалено вместе с данными" "removed with the data")"
  else
    ok "$(t "контейнеры удалены, данные остались в $DIR/data" "containers removed, data kept in $DIR/data")"
  fi
}

menu() {
  while :; do
    banner
    local c
    if installed; then
      load_env
      menu_head
      local upd_hint="" auto_label
      [ -n "$M_LATEST" ] && upd_hint="$(t "вышла версия" "version") $M_LATEST$(t "" " is out")"
      if [ "$M_AUTO" = 1 ]; then auto_label="$(t "Автообновление ночью: включено" "Night auto-update: on") ($(printf '%02d' "${M_HOUR:-1}"):00)"
      else auto_label="$(t "Автообновление ночью: выключено" "Night auto-update: off")"; fi
      local db_item db_hint=""
      if [ "$(db_kind)" = postgres ]; then db_item="$(t "База данных: PostgreSQL" "Database: PostgreSQL")"
      else db_item="$(t "Перейти на PostgreSQL" "Move to PostgreSQL")"
        db_hint="$(t "сейчас SQLite · для большого магазина" "now SQLite · for a bigger shop")"; fi
      choose c \
        -- "$(t "Магазин" "Shop")" "" \
        1 "$(t "Обновить до новой версии" "Update to the latest version")" "$upd_hint" \
        2 "$(t "Состояние и проверка" "Status and checks")" "" \
        3 "$(t "Журнал (выход — Ctrl+C)" "Logs (exit — Ctrl+C)")" "" \
        4 "$(t "Перезапустить" "Restart")" "" \
        -- "$(t "Данные" "Data")" "" \
        5 "$(t "Резервная копия базы" "Database backup")" "" \
        6 "$(t "Восстановить из копии" "Restore from a backup")" "" \
        7 "$(t "Итоги за сутки — прислать в Telegram" "The last 24 hours — send to Telegram")" "" \
        8 "$db_item" "$db_hint" \
        -- "$(t "Настройки" "Settings")" "" \
        9 "$(t "Способы оплаты (карты, крипта, Stars)" "Payment methods (cards, crypto, Stars)")" "" \
        10 "$(t "Сменить адрес (домен / временный)" "Change address (domain / temporary)")" "" \
        11 "$(t "Сменить токен, ID или пароль" "Change token, IDs or password")" "" \
        12 "$auto_label" "" \
        -- "$(t "Прочее" "Other")" "" \
        13 "$(t "Что нового" "What's new")" "" \
        14 "$(t "Очистить место на диске" "Free up disk space")" "" \
        15 "$( [ "$UI" = ru ] && echo English || echo Русский)" "" \
        16 "$(t "Удалить" "Uninstall")" "" \
        0 "$(t "Выход" "Exit")" ""
      case "$c" in
        1) menu_sure "$(t "Обновить магазин сейчас? Сначала будет копия базы, перерыв 1–2 минуты." "Update the shop now? A backup comes first, 1–2 minutes of downtime.")" && cmd_update ;;
        2) cmd_status ;;
        3) trap ':' INT; compose logs -f --tail 100 app || true; trap_int ;;
        4) menu_sure "$(t "Перезапустить магазин? Перерыв — несколько секунд." "Restart the shop? A few seconds of downtime.")" && menu_restart ;;
        5) compose exec -T -u app app python -m app.backup make; ok "$(t "архив в $DIR/data/backups" "archive in $DIR/data/backups")" ;;
        6) menu_restore ;;
        7) menu_report ;;
        8) if [ "$(db_kind)" = postgres ]; then ok "$(t "магазин работает на PostgreSQL" "the shop runs on PostgreSQL")"
           else ( cmd_db_postgres ) || true; fi ;;
        9) cmd_payments ;; 10) cmd_address ;; 11) cmd_settings ;;
        12) menu_auto ;;
        13) menu_news ;;
        14) menu_sure "$(t "Удалить старые образы Docker и копии базы старше 30 дней?" "Remove old Docker images and backups older than 30 days?")" && menu_clean ;;
        15) if [ "$UI" = ru ]; then UI=en; else UI=ru; fi; continue ;;
        16) cmd_uninstall; installed || return 0 ;;
        0) printf '\n'; return 0 ;;
      esac
    else
      say "  $(t "Установит магазин за 5–10 минут. Понадобятся:" "Installs the shop in 5–10 minutes. You will need:")"
      say "   · $(t "токен бота от @BotFather" "a bot token from @BotFather")"
      say "   · $(t "свой Telegram ID (узнаю сам, если напишете боту)" "your Telegram ID (detected if you message the bot)")"
      say "   · $(t "домен — если есть; если нет, дам временный адрес" "a domain — if you have one; otherwise a temporary address")"
      printf '\n'
      choose c \
        1 "$(t "Установить" "Install")" "" \
        2 "$( [ "$UI" = ru ] && echo "English" || echo "Русский")" "" \
        0 "$(t "Выход" "Exit")" ""
      case "$c" in
        1) cmd_install; return 0 ;;
        2) if [ "$UI" = ru ]; then UI=en; else UI=ru; fi; continue ;;
        0) printf '\n'; return 0 ;;
      esac
    fi
    printf '\n'; local _p; ask _p "$(t "Enter — вернуться в меню" "Enter — back to the menu")" ""
  done
}

# ------------------------------------------------------------------ menu pieces
# The card above the menu: is it up, which version, what happened today.
M_LATEST="" M_AUTO="" M_HOUR=""
head_line() {  # head_line "label" "value" — the label padded by characters, not bytes
  local l=$1 w=11; shift
  printf '  %s│%s %s%s%*s%s %s\n' "$A" "$N" "$D" "$l" $(( w > ${#l} ? w - ${#l} : 1 )) '' "$N" "$*"
}
menu_head() {
  local s v state addr free
  s=$(compose exec -T -u app app python -m app.cli summary 2>/dev/null) || s=""
  kv_() { printf '%s\n' "$s" | sed -n "s/^$1=//p" | head -1; }
  M_LATEST=$(kv_ latest) M_AUTO=$(kv_ auto) M_HOUR=$(kv_ hour)
  if [ -n "$s" ] || app_healthy; then state="$G●$N $B$(t "работает" "running")$N"
  else state="$R●$N $B$(t "не отвечает" "not answering")$N"; fi
  v=$(code_version)
  printf '  %s   %s%s%s' "$state" "$D" "$(t "версия" "version") ${v:-?}" "$N"
  [ -n "$M_LATEST" ] && printf '   %s↑ %s %s%s' "$A" "$(t "вышла" "new")" "$M_LATEST" "$N"
  printf '\n'
  addr=$(get_env PUBLIC_URL); [ -n "$addr" ] || addr="https://$(get_env DOMAIN)"
  head_line "$(t "адрес" "address")" "$addr"
  head_line "$(t "база" "database")" "$(db_label "$(db_kind)")"
  if [ -n "$s" ]; then
    head_line "$(t "сегодня" "today")" "$(t "покупок" "purchases"): $(kv_ orders) · $(kv_ revenue) · $(t "новых" "new"): $(kv_ new)"
    head_line "$(t "клиенты" "customers")" "$(t "всего" "in total"): $(kv_ users) · $(t "с подпиской" "subscribed"): $(kv_ active)"
    head_line "$(t "копия базы" "backup")" "$( [ -n "$(kv_ backup)" ] && kv_ backup || t "ещё не было" "none yet")"
  fi
  free=$(df -Ph "$DIR" 2>/dev/null | awk 'NR==2{print $4}' | sed "s/G$/ $(t ГБ GB)/; s/M$/ $(t МБ MB)/; s/T$/ $(t ТБ TB)/")
  [ -n "$free" ] && head_line "$(t "диск" "disk")" "$(t "свободно" "free") $free"
  head_line "$(t "папка" "folder")" "$DIR"
}

# "Go ahead?" with Back as the answer on Enter — every menu item can be left.
menu_sure() {
  local _c
  choose _c 1 "$(t "Да" "Yes")" "$1" 0 "$(t "Назад" "Back")" ""
  [ "$_c" = 1 ]
}

menu_restart() {
  if spin "$(t "Перезапускаю магазин" "Restarting the shop")" compose restart app \
     && spin "$(t "Жду ответа" "Waiting for an answer")" wait_app; then
    ok "$(t "магазин перезапущен и отвечает" "restarted and answering")"
  else
    warn "$(t "магазин не отвечает — посмотрите журнал (пункт 3)" "the shop does not answer — see the logs (item 3)")"
  fi
}

menu_restore() {
  local files=() args=() f c i=1
  mapfile -t files < <(ls -1t "$DIR"/data/backups/backup-*.zip 2>/dev/null | head -8)
  if [ ${#files[@]} -eq 0 ]; then
    warn "$(t "копий пока нет — сначала пункт 5 «Резервная копия базы»" "no backups yet — make one first (item 5)")"
    return 0
  fi
  step "·" "$(t "Из какой копии восстановить" "Which backup to restore")"
  hint "$(t "Текущая база будет заменена. Перед этим сделайте свежую копию, если сомневаетесь." "The current database will be replaced. Make a fresh backup first if unsure.")"
  for f in "${files[@]}"; do
    args+=("$i" "$(date -r "$f" '+%d.%m.%Y %H:%M')" "$(basename "$f") · $(du -h "$f" | cut -f1)")
    i=$((i + 1))
  done
  args+=(0 "$(t "Назад" "Back")" "")
  choose c "${args[@]}"
  [ "$c" = 0 ] && return 0
  cmd_restore "${files[$((c - 1))]}"
}

menu_report() {
  local out
  out=$(compose exec -T -u app app python -m app.cli report 2>/dev/null || true)
  case "$out" in
    *sent=[1-9]*) ok "$(t "итоги за сутки отправлены вам в Telegram" "the results are sent to your Telegram")" ;;
    *) warn "$(t "не отправилось: напишите боту /start с аккаунта владельца и попробуйте снова" "not sent: write /start to the bot from the owner's account and try again")" ;;
  esac
}

menu_auto() {
  local h
  if [ ! -f "$DIR/data/updater.json" ]; then
    install_updater
    [ -f "$DIR/data/updater.json" ] || return 0
  fi
  if [ "$M_AUTO" = 1 ]; then
    menu_sure "$(t "Выключить автообновление?" "Turn the auto-update off?")" || return 0
    compose exec -T -u app app python -m app.cli auto-update off >/dev/null 2>&1 \
      && ok "$(t "автообновление выключено — новые версии ставятся кнопкой" "auto-update off — new versions are installed with the button")" \
      || warn "$(t "магазин не ответил" "the shop did not answer")"
    return 0
  fi
  hint "$(t "Ночью, в выбранный час, магазин сам поставит новую версию (с копией базы и откатом, если не запустится)." \
            "At the chosen hour the shop installs a new version itself (with a backup, and a rollback if it does not start).")"
  menu_sure "$(t "Включить автообновление?" "Turn the auto-update on?")" || return 0
  hint "$(t "Сейчас на сервере" "Server time now") $(date +%H:%M)"
  while :; do
    ask h "$(t "Час (0–23)" "Hour (0–23)")" "${M_HOUR:-1}"
    [[ "$h" =~ ^[0-9]{1,2}$ ]] && [ "$h" -le 23 ] && break
    warn "$(t "число от 0 до 23" "a number from 0 to 23")"
  done
  compose exec -T -u app app python -m app.cli auto-update on "$h" >/dev/null 2>&1 \
    && ok "$(t "автообновление включено, в" "auto-update on, at") $(printf '%02d' "$h"):00" \
    || warn "$(t "магазин не ответил" "the shop did not answer")"
}

menu_news() {
  local f="$DIR/CHANGELOG.md"
  [ "$UI" = en ] && f="$DIR/CHANGELOG.en.md"
  [ -f "$f" ] || { warn "$(t "списка изменений нет" "no list of changes")"; return 0; }
  printf '\n'
  awk -v a="$A$B" -v d="$D" -v n="$N" '
    /^## / { k++; if (k > 3) exit; sub(/^## /, ""); printf "\n  %s%s%s\n", a, $0, n; next }
    k >= 1 && /^- / { sub(/^- /, ""); printf "    %s•%s %s\n", d, n, $0 }' "$f"
}

menu_clean() {
  local before after
  before=$(df -Pk "$DIR" | awk 'NR==2{print $4}')
  spin "$(t "Удаляю старые образы и кэш сборки" "Removing old images and the build cache")" \
    sh -c "docker image prune -af && docker builder prune -af" || true
  find "$DIR/data/backups" -name 'backup-*.zip' -mtime +30 -delete 2>/dev/null || true
  after=$(df -Pk "$DIR" | awk 'NR==2{print $4}')
  ok "$(t "освобождено" "freed") $(( (${after:-0} - ${before:-0}) / 1024 )) $(t "МБ" "MB") · $(t "свободно" "free") $(df -Ph "$DIR" | awk 'NR==2{print $4}')"
  hint "$(t "Копии базы старше 30 дней тоже удалены (свежие на месте)." "Backups older than 30 days were removed too (recent ones stay).")"
}

main() {
  local cmd=""
  case "${1:-}" in install|update|status|logs|backup|restore|address|settings|payments|postgres|uninstall|menu) cmd=$1; shift ;; esac
  while [ $# -gt 0 ]; do
    case "$1" in
      --domain) DOMAIN=$2; ADDR=domain; shift 2 ;;
      --tunnel) ADDR=tunnel; shift ;;
      --token) TOKEN=$2; shift 2 ;;
      --admin-id) ADMIN_ID=$2; shift 2 ;;
      --password) PASSWORD=$2; shift 2 ;;
      --currency) CURRENCY=$2; shift 2 ;;
      --db) DB=$2; shift 2 ;;
      --dir) DIR=$2; shift 2 ;;
      --yes|-y) ASSUME_YES=1; shift ;;
      --from-panel) FROM_PANEL=1; shift ;;
      --en|--ru) shift ;;
      -h|--help) sed -n '2,27p' "$0" 2>/dev/null || true; exit 0 ;;
      *) ARGS+=("$1"); shift ;;
    esac
  done
  if [ "$ASSUME_YES" = 1 ] && [ -z "$cmd" ]; then cmd=install; fi
  { touch "$LOG"; } 2>/dev/null || LOG=/tmp/vpn-shop-install.log
  case "${cmd:-menu}" in
    menu) need_root; menu ;;
    install) [ "$ASSUME_YES" = 1 ] || banner; cmd_install ;;
    update) cmd_update ;;
    status) cmd_status ;;
    logs) compose logs -f --tail 200 app ;;
    backup) compose exec -T -u app app python -m app.backup make ;;
    restore) cmd_restore "${ARGS[0]:-}" ;;
    address) cmd_address ;;
    settings) cmd_settings ;;
    payments) cmd_payments ;;
    postgres) cmd_db_postgres ;;
    uninstall) cmd_uninstall ;;
  esac
}

ARGS=()
CHECK_URL=""
main "$@"
