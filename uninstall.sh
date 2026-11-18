#!/usr/bin/env bash
# ============================================================
# fanctl 卸载
#
#   sudo ./uninstall.sh            # 停服务 + 删文件（保留 /etc/fanctl.toml）
#   sudo ./uninstall.sh --purge    # 连配置文件一起删
#   sudo ./uninstall.sh --dry-run  # 只打印会做什么
# ============================================================
set -euo pipefail

BIN=/usr/local/bin/fanctl
CONF=/etc/fanctl.toml
UNIT=/etc/systemd/system/fanctl.service

DRY=0
PURGE=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    --purge)   PURGE=1 ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "未知参数：$arg"; exit 2 ;;
  esac
done

say() { printf '\033[1;36m==> \033[0m%s\n' "$*"; }
run() { if [ "$DRY" = 1 ]; then echo "    [dry] $*"; else eval "$@"; fi; }

[ "$(id -u)" = 0 ] || [ "$DRY" = 1 ] || { echo "需要 root：请用 sudo"; exit 1; }

# 卸载前先给个安全风速，别让风扇停在 0（拔了服务又没人管最热）
if [ "$DRY" = 0 ] && command -v "$BIN" >/dev/null 2>&1; then
  say "卸载前把风扇恢复到默认安全风速"
  "$BIN" --set 0 60 >/dev/null 2>&1 || true
  "$BIN" --set 1 60 >/dev/null 2>&1 || true
fi

HAS_SYSTEMD=0
if [ "$DRY" = 1 ]; then
  HAS_SYSTEMD=1
elif command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
  HAS_SYSTEMD=1
fi

if [ "$HAS_SYSTEMD" = 1 ]; then
  say "停止并禁用 fanctl.service"
  run "systemctl disable --now fanctl.service 2>/dev/null || true"
else
  say "没有 systemd：跳过服务停止（若你在后台跑过，手动 pkill -f 'fanctl --auto'）"
fi

say "删除 $UNIT"
run "rm -f '$UNIT'"

say "删除 $BIN"
run "rm -f '$BIN'"

if [ "$PURGE" = 1 ]; then
  say "删除 $CONF（--purge）"
  run "rm -f '$CONF'"
else
  say "保留 $CONF（要删干净加 --purge）"
fi

if [ "$HAS_SYSTEMD" = 1 ]; then
  run "systemctl daemon-reload 2>/dev/null || true"
fi

say "完成。风扇会保持当前风速（PWM 输出不会因为进程退出而自动停）"
