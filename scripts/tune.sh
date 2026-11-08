#!/usr/bin/env bash
# VPN Seller — speed up a VPN server with kernel network settings.
#
#   bash tune.sh           apply (shows the values before and after)
#   bash tune.sh --status  show the current values
#   bash tune.sh --undo    remove the settings (the kernel defaults return after a reboot)
#   add --en for English
#
# What it changes and why:
#   * BBR + fq — congestion control that measures the real bandwidth instead
#     of backing off at every lost packet; much faster on mobile networks.
#   * TCP buffers up to 16 MB — long distances need big windows to fill the line.
#   * bigger connection queues — survive bursts of reconnects after a network drop.
#   * MTU probing — no hangs when a hop drops big packets ("black holes").
#   * TCP Fast Open, no slow start after idle — quicker first bytes.
#   * a million open files — Xray with many clients never hits "too many open files".
# Nothing here touches your panel or Xray settings. Safe to run again.
set -euo pipefail

NAME=vpnshop-tune
ETC=${VPNSHOP_ETC:-/etc}
SYSCTL="$ETC/sysctl.d/99-$NAME.conf"
LIMITS="$ETC/security/limits.d/99-$NAME.conf"
SYSTEMD="$ETC/systemd/system.conf.d/99-$NAME.conf"

EN=0
for a in "$@"; do [ "$a" = --en ] && EN=1; done
t() { if [ "$EN" = 1 ]; then printf '%s' "$2"; else printf '%s' "$1"; fi; }
if [ -t 1 ]; then G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; B=$'\e[1m'; D=$'\e[2m'; N=$'\e[0m'
else G=""; Y=""; R=""; B=""; D=""; N=""; fi
ok()   { printf '  %s✓%s %s\n' "$G" "$N" "$*"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$*"; }
die()  { printf '  %s✗%s %s\n' "$R" "$N" "$*" >&2; exit 1; }

KEYS="net.ipv4.tcp_congestion_control net.core.default_qdisc net.core.rmem_max net.core.wmem_max
net.ipv4.tcp_rmem net.ipv4.tcp_wmem net.core.somaxconn net.ipv4.tcp_max_syn_backlog
net.core.netdev_max_backlog net.ipv4.tcp_mtu_probing net.ipv4.tcp_fastopen
net.ipv4.tcp_slow_start_after_idle net.ipv4.ip_local_port_range fs.file-max"

show() {
  local k v
  for k in $KEYS; do
    v=$(sysctl -n "$k" 2>/dev/null | tr '\t' ' ' || true)
    printf '    %-40s %s\n' "$k" "${v:-—}"
  done
  printf '    %-40s %s\n' "ulimit -n" "$(ulimit -n)"
}

settings() {  # the file, with BBR only when the kernel has it
  local bbr=$1
  cat <<EOF
# Written by VPN Seller tune.sh — network settings for a VPN server.
# Remove with: bash tune.sh --undo
EOF
  if [ "$bbr" = 1 ]; then
    printf 'net.core.default_qdisc = fq\nnet.ipv4.tcp_congestion_control = bbr\n'
  fi
  cat <<'EOF'
net.core.rmem_max = 16777216
net.core.wmem_max = 16777216
net.ipv4.tcp_rmem = 4096 87380 16777216
net.ipv4.tcp_wmem = 4096 65536 16777216
net.core.somaxconn = 8192
net.ipv4.tcp_max_syn_backlog = 8192
net.core.netdev_max_backlog = 10000
net.ipv4.tcp_mtu_probing = 1
net.ipv4.tcp_fastopen = 3
net.ipv4.tcp_slow_start_after_idle = 0
net.ipv4.ip_local_port_range = 10000 65000
fs.file-max = 1048576
EOF
}

cmd_apply() {
  [ "$(id -u)" = 0 ] || [ -n "${VPNSHOP_ETC:-}" ] || die "$(t "Нужен root: sudo bash tune.sh" "Run as root: sudo bash tune.sh")"
  printf '\n  %s%s%s\n\n  %s\n' "$B" "$(t "Ускорение VPN-сервера" "Speeding up the VPN server")" "$N" "$(t "Сейчас:" "Now:")"
  show

  local bbr=0
  modprobe tcp_bbr 2>/dev/null || true
  if sysctl -n net.ipv4.tcp_available_congestion_control 2>/dev/null | grep -qw bbr; then bbr=1
  else warn "$(t "ядро без BBR — остальное всё равно применяется" "the kernel has no BBR — the rest is applied anyway")"; fi
  if [ "$bbr" = 1 ] && [ -d "$ETC/modules-load.d" ]; then printf 'tcp_bbr\n' > "$ETC/modules-load.d/$NAME.conf"; fi

  mkdir -p "$(dirname "$SYSCTL")" "$(dirname "$LIMITS")" "$(dirname "$SYSTEMD")"
  settings "$bbr" > "$SYSCTL"
  # Apply key by key: in a container some keys are read-only; the others still count.
  local line key val failed=""
  while IFS= read -r line; do
    case "$line" in ""|\#*) continue ;; esac
    key=${line%% =*}; val=${line#*= }
    sysctl -q -w "$key=$val" >/dev/null 2>&1 || failed+="$key "
  done < "$SYSCTL"
  if [ -n "$failed" ]; then
    warn "$(t "не применились (сервер в контейнере или старое ядро):" "not applied (a container or an old kernel):") $failed"
  fi
  ok "$(t "сетевые настройки ядра" "kernel network settings") → $SYSCTL"

  printf '* soft nofile 1048576\n* hard nofile 1048576\nroot soft nofile 1048576\nroot hard nofile 1048576\n' > "$LIMITS"
  printf '[Manager]\nDefaultLimitNOFILE=1048576\n' > "$SYSTEMD"
  if command -v systemctl >/dev/null 2>&1 && [ -z "${VPNSHOP_NO_SYSTEMD:-}" ]; then
    systemctl daemon-reexec 2>/dev/null || true
  fi
  ok "$(t "лимит открытых файлов: 1 048 576" "open files limit: 1 048 576")"

  printf '\n  %s\n' "$(t "Стало:" "After:")"
  show
  printf '\n  %s\n' "$D$(t "Службы получат новый лимит файлов после перезапуска, например: systemctl restart x-ui (или перезагрузите сервер)." "Services get the new file limit after a restart, e.g. systemctl restart x-ui (or reboot).")$N"
  printf '  %s\n\n' "$D$(t "Вернуть как было: bash tune.sh --undo" "Undo: bash tune.sh --undo")$N"
}

cmd_undo() {
  [ "$(id -u)" = 0 ] || [ -n "${VPNSHOP_ETC:-}" ] || die "$(t "Нужен root: sudo bash tune.sh --undo" "Run as root: sudo bash tune.sh --undo")"
  rm -f "$SYSCTL" "$LIMITS" "$SYSTEMD" "$ETC/modules-load.d/$NAME.conf"
  sysctl -q --system >/dev/null 2>&1 || true
  if command -v systemctl >/dev/null 2>&1 && [ -z "${VPNSHOP_NO_SYSTEMD:-}" ]; then systemctl daemon-reexec 2>/dev/null || true; fi
  ok "$(t "Настройки удалены. Значения ядра по умолчанию вернутся полностью после перезагрузки." "Settings removed. The kernel defaults fully return after a reboot.")"
}

args=()
for a in "$@"; do [ "$a" = --en ] || args+=("$a"); done
set -- "${args[@]+"${args[@]}"}"
case "${1:-}" in
  "") cmd_apply ;;
  --status) show ;;
  --undo) cmd_undo ;;
  *) printf '%s\n' "bash tune.sh [--status | --undo] [--en]"; exit 1 ;;
esac
