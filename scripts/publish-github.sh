#!/usr/bin/env bash
# =============================================================================
#  Выложить эту копию VPN Seller в СВОЙ репозиторий GitHub (через deploy key).
#  Publish this copy of VPN Seller to YOUR GitHub repository (with a deploy key).
#
#  1. На GitHub: New repository → имя vpn-seller → Private → БЕЗ README → Create.
#  2. На сервере, в папке проекта:   bash scripts/publish-github.sh
#
#  Скрипт сделает ключ (~/.ssh/vpnshop_deploy, НЕ в папке проекта), покажет
#  его для GitHub → Settings → Deploy keys, пропишет адрес репозитория в
#  install.sh и README, покажет список файлов и отправит код. Секреты (.env,
#  data/, ключи, базы) в репозиторий не попадут — это проверяется дважды.
#  Повторный запуск просто отправляет новые изменения.
# =============================================================================
set -Eeuo pipefail

KEY="${PUBLISH_KEY:-$HOME/.ssh/vpnshop_deploy}"
ROOT=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
SSH_OPTS="-i $KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"

if [ -t 1 ]; then A=$'\e[38;5;214m' B=$'\e[1m' D=$'\e[2m' G=$'\e[32m' Y=$'\e[33m' R=$'\e[31m' N=$'\e[0m'
else A="" B="" D="" G="" Y="" R="" N=""; fi
ok()   { printf '  %s✓%s %s\n' "$G" "$N" "$*"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$*"; }
die()  { printf '\n  %s✗ %s%s\n\n' "$R" "$*" "$N" >&2; exit 1; }
step() { printf '\n  %s%s%s  %s%s%s\n' "$A" "$1" "$N" "$B" "$2" "$N"; }
IN=/dev/tty; { : < /dev/tty; } 2>/dev/null || IN=/dev/stdin
ask() {  # ask var "Question" [default]
  local _v
  if [ -n "${3:-}" ]; then printf '  %s›%s %s %s[%s]%s ' "$A" "$N" "$2" "$D" "$3" "$N"; else printf '  %s›%s %s ' "$A" "$N" "$2"; fi
  IFS= read -r _v < "$IN" || die "нет ответа"
  printf -v "$1" '%s' "${_v:-${3:-}}"
}
yes_no() {  # yes_no "Question" [y|n]
  local _a; ask _a "$1 (д/н)" "$( [ "${2:-y}" = y ] && echo д || echo н)"
  case "$_a" in y|Y|yes|д|Д|да|Да) return 0 ;; *) return 1 ;; esac
}

cd "$ROOT"
[ -f install.sh ] && [ -d app ] && [ -f docker-compose.yml ] || die "запускайте из папки VPN Seller (нет install.sh / app)"
command -v git >/dev/null || die "нужен git: apt-get install -y git"

# ------------------------------------------------------------ 1. repository
step "1/4" "Адрес репозитория"
printf '    %sНапример: https://github.com/ivan/vpn-seller  или  ivan/vpn-seller%s\n' "$D" "$N"
url=$(git remote get-url origin 2>/dev/null || true)
while :; do
  ask REPO "Репозиторий" "${url:-}"
  REPO=${REPO%.git}; REPO=${REPO%/}
  case "$REPO" in
    git@github.com:*) SLUG=${REPO#git@github.com:} ;;
    https://github.com/*) SLUG=${REPO#https://github.com/} ;;
    github.com/*) SLUG=${REPO#github.com/} ;;
    /*|file://*) SLUG="local/test"; REMOTE=$REPO; break ;;     # a local path (tests)
    *) SLUG=$REPO ;;
  esac
  if [[ "$SLUG" =~ ^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$ ]]; then REMOTE="git@github.com:$SLUG.git"; break; fi
  warn "не понял адрес — нужен вид ник/имя, например ivan/vpn-seller"
done
ok "$SLUG"

# ------------------------------------------------------------ 2. deploy key
if [[ "$REMOTE" == git@github.com:* ]]; then
  step "2/4" "Ключ доступа (deploy key)"
  command -v ssh-keygen >/dev/null || die "нужен ssh: apt-get install -y openssh-client"
  mkdir -p "$(dirname "$KEY")"; chmod 700 "$(dirname "$KEY")"
  if [ ! -f "$KEY" ]; then
    ssh-keygen -t ed25519 -N "" -C "vpn-seller deploy $(hostname)" -f "$KEY" -q
    ok "ключ создан: $KEY (хранится только на этом сервере)"
  else
    ok "ключ уже есть: $KEY"
  fi
  chmod 600 "$KEY"
  if ssh $SSH_OPTS -T git@github.com 2>&1 | grep -q "successfully authenticated"; then
    ok "GitHub уже принимает этот ключ"
  else
    printf '\n    Откройте: %shttps://github.com/%s/settings/keys/new%s\n' "$B" "$SLUG" "$N"
    printf '    Title: vpn-seller-server · Key: строка ниже целиком · %sотметьте Allow write access%s → Add key\n\n' "$B" "$N"
    printf '    %s\n\n' "$(cat "$KEY.pub")"
    while :; do
      ask _ "Добавили? Нажмите Enter" ""
      if ssh $SSH_OPTS -T git@github.com 2>&1 | grep -q "successfully authenticated"; then ok "GitHub принял ключ"; break; fi
      warn "GitHub пока не принимает ключ. Проверьте, что вставили строку целиком, в нужный репозиторий, с галочкой Allow write access."
    done
  fi
fi

# ------------------------------------------------------------ 3. what goes out
step "3/4" "Что уйдёт в репозиторий"
[ -d .git ] || { git init -q -b main 2>/dev/null || { git init -q && git checkout -q -b main; }; ok "git init"; }
git config user.name >/dev/null 2>&1 || git config user.name "${SLUG%%/*}"
git config user.email >/dev/null 2>&1 || git config user.email "${SLUG%%/*}@users.noreply.github.com"
[[ "$REMOTE" == git@github.com:* ]] && git config core.sshCommand "ssh $SSH_OPTS"
if git remote get-url origin >/dev/null 2>&1; then git remote set-url origin "$REMOTE"; else git remote add origin "$REMOTE"; fi

# The one-command install must point at this repository.
# The shop looks for new versions in the same repository (app/release.py).
# (The address already written there — OWNER/vpn-seller in a fresh copy, the
# original author's in a fork — is replaced with this one.)
was=$(sed -n 's/^GITHUB_REPO="${VPN_SHOP_GITHUB_REPO:-\([^}]*\)}".*/\1/p' install.sh | head -1)
[ -n "$was" ] || was=$(sed -n 's/^GITHUB_REPO="\([^"]*\)".*/\1/p' install.sh | head -1)
if [ -n "$was" ] && [ "$was" != "$SLUG" ] && [ "$SLUG" != local/test ]; then
  sed -i "s#$was#$SLUG#g" install.sh README.md README.en.md app/release.py
  ok "адрес $SLUG вписан в install.sh, README и app/release.py (оттуда магазин узнаёт о новых версиях)"
fi

git add -A
# Secrets never leave the server, even if .gitignore was edited.
bad=$(git diff --cached --name-only | grep -E '(^|/)\.env$|(^|/)\.env\.bak$|^data/|\.db$|\.db-|(^|/)id_(rsa|ed25519)|\.pem$|\.key$|deploy_key|docker-compose\.override\.yml$|\.vpnshop\.bak$' || true)
if [ -n "$bad" ]; then
  git reset -q
  printf '%s\n' "$bad" | sed 's/^/      /'
  die "эти файлы не должны попасть на GitHub (секреты или база). Ничего не отправлено."
fi
n=$(git diff --cached --name-only | wc -l)
if [ "$n" = 0 ] && git rev-parse -q --verify HEAD >/dev/null; then
  ok "новых изменений нет"
else
  git diff --cached --name-only | awk -F/ '{print $1}' | sort | uniq -c | awk '{printf "      %-28s %s файл(ов)\n", $2, $1}'
  printf '    %sвсего: %s · .env, data/, ключи и базы — не отправляются%s\n' "$D" "$n" "$N"
  yes_no "Отправить это на GitHub?" || { git reset -q; die "остановлено, ничего не отправлено"; }
  msg="VPN Seller $(date +%Y-%m-%d)"
  git rev-parse -q --verify HEAD >/dev/null || msg="VPN Seller: first version"
  git commit -qm "$msg"
  ok "коммит: $msg"
fi

# ------------------------------------------------------------ 4. push
step "4/4" "Отправка"
branch=$(git rev-parse --abbrev-ref HEAD)
if out=$(git push -u origin "$branch" 2>&1); then
  ok "отправлено в $SLUG ($branch)"
else
  if printf '%s' "$out" | grep -qiE 'rejected|fetch first|non-fast-forward'; then
    warn "в репозитории на GitHub уже есть файлы (например, README, созданный при создании репозитория)."
    if yes_no "Заменить содержимое репозитория этой копией?" n; then
      git push -u --force-with-lease origin "$branch" >/dev/null 2>&1 || git push -u --force origin "$branch"
      ok "отправлено (содержимое GitHub заменено)"
    else
      die "не отправлено. Проще всего — создать репозиторий заново, пустым (без README)."
    fi
  else
    printf '%s\n' "$out" | tail -5 | sed 's/^/      /'
    die "GitHub не принял отправку"
  fi
fi

printf '\n  %s✓ Готово%s\n\n' "$G$B" "$N"
if [ "$SLUG" != local/test ]; then
  printf '  Репозиторий:  https://github.com/%s\n' "$SLUG"
  printf '  Установка (когда репозиторий станет публичным):\n'
  printf '    wget -qO install.sh https://raw.githubusercontent.com/%s/main/install.sh && sudo bash install.sh\n' "$SLUG"
  printf '  Пока он приватный — ставьте из этой папки:  sudo bash %s/install.sh\n' "$ROOT"
  printf '  %s(обновления «vpn-seller → Обновить» будут приходить из GitHub через этот же ключ)%s\n\n' "$D" "$N"
  printf '  В следующий раз — просто: bash %s/scripts/publish-github.sh\n\n' "$ROOT"
fi
