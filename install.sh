#!/usr/bin/env bash
# ============================================================
# fanctl 一键安装（Radxa Zero 3W / RK3566 可配置 PWM 风扇散热）
#
# 用法：
#   sudo ./install.sh              # 装好 + 立即启动 + 自检
#   sudo ./install.sh --no-start   # 只装文件，不启动服务
#   sudo ./install.sh --uninstall  # 卸载（等价于 ./uninstall.sh）
#   sudo ./install.sh --dry-run    # 只打印会做什么，不真做
# ============================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN=/usr/local/bin/fanctl
CONF=/etc/fanctl.toml
UNIT=/etc/systemd/system/fanctl.service

DRY=0
DO_START=1
UNINSTALL=0

for arg in "$@"; do
  case "$arg" in
    --dry-run)    DRY=1 ;;
    --no-start)   DO_START=0 ;;
    --uninstall)  UNINSTALL=1 ;;
    -h|--help)    sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "未知参数：$arg（-h 看用法）"; exit 2 ;;
  esac
done

say()  { printf '\033[1;36m==> \033[0m%s\n' "$*"; }
warn() { printf '\033[1;33m[!] \033[0m%s\n' "$*"; }
die()  { printf '\033[1;31m[x] \033[0m%s\n' "$*" >&2; exit 1; }
run()  { if [ "$DRY" = 1 ]; then echo "    [dry] $*"; else eval "$@"; fi; }

[ "$(id -u)" = 0 ] || [ "$DRY" = 1 ] || die "需要 root：请用 sudo 运行"

if [ "$UNINSTALL" = 1 ]; then
  exec "$HERE/uninstall.sh" $([ "$DRY" = 1 ] && echo --dry-run)
fi

# ---------- 前置检查 ----------
if [ -f /etc/systemd/system/zelux-fand.service ]; then
  die "检测到 Device Image 风扇服务；请使用 Device Image 升级流程，不要并行安装 fanctl.service"
fi
say "环境检查"
miss=0
for f in "$HERE/fanctl" "$HERE/fanctl.toml" "$HERE/fanctl.service"; do
  [ -f "$f" ] || { warn "缺文件：$f"; miss=1; }
done
[ "$miss" = 0 ] || die "安装包不完整（应该和 install.sh 在同一目录）"

PY="$(command -v python3 || true)"
[ -n "$PY" ] || die "找不到 python3：先 apt install python3"
PYV="$("$PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
say "python3 = $PY（$PYV）"
"$PY" - <<'EOF' || die "python3 版本太老：需要 3.9+（代码用了 str | None 等写法；配置文件自带兜底解析器，不强求 3.11）"
import sys
raise SystemExit(0 if sys.version_info >= (3, 9) else 1)
EOF

if [ ! -d /sys/class/pwm ] || [ -z "$(ls -A /sys/class/pwm 2>/dev/null)" ]; then
  warn "/sys/class/pwm 下没有 pwmchip —— 说明设备树的 PWM 通道还没 probe 出来。"
  warn "fanctl 装好后可能报「找不到 PWM 通道」，请先确认板子的 PWM overlay 已启用。"
fi

if [ ! -d /sys/class/thermal ]; then
  warn "/sys/class/thermal 不存在：自动温控读不到温度，会一直按 default_pct 跑。"
fi

# ---------- 装文件 ----------
say "安装 fanctl -> $BIN"
run "install -m 0755 '$HERE/fanctl' '$BIN'"

if [ -f "$CONF" ]; then
  say "已存在 $CONF，不覆盖（要重置请先手动删掉）"
else
  say "写入默认配置 -> $CONF"
  run "install -m 0644 '$HERE/fanctl.toml' '$CONF'"
fi

say "安装 systemd 单元 -> $UNIT"
run "install -m 0644 '$HERE/fanctl.service' '$UNIT'"

# ---------- 有没有 systemd ----------
HAS_SYSTEMD=0
if [ "$DRY" = 1 ]; then
  HAS_SYSTEMD=1
elif command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
  HAS_SYSTEMD=1
fi

# ---------- 启动 ----------
if [ "$HAS_SYSTEMD" = 0 ]; then
  warn "这台机器没有 systemd（或不是 PID1）——不启动服务，改用命令行后台跑"
  say "手动启动：sudo fanctl --auto &"
  say "        或写进你自己的 rc / supervisor；fanctl 是单文件，不依赖 systemd"
elif [ "$DO_START" = 1 ]; then
  say "systemctl daemon-reload"
  run "systemctl daemon-reload"
  say "enable + start fanctl"
  run "systemctl enable --now fanctl.service"
  if [ "$DRY" = 0 ]; then
    sleep 2
    say "服务状态"
    systemctl --no-pager --lines=8 status fanctl.service || true
    echo
    say "风扇状态"
    "$BIN" --status || true
    echo
    say "自检（全速 1 秒 → 读转速 → 回原值）"
    "$BIN" --selftest || true
  fi
else
  say "已跳过启动（--no-start）。手动启动：sudo systemctl enable --now fanctl"
fi

echo
say "完成。常用命令："
if [ "$HAS_SYSTEMD" = 1 ]; then
cat <<'TIP'
    sudo fanctl --status        # 看转速、百分比、通道路径
    sudo fanctl --selftest      # 验风扇转不转
    sudo fanctl --set 0 80      # 第 0 路手动 80%
    sudo systemctl restart fanctl       # 改完 /etc/fanctl.toml 后重启
    sudo journalctl -u fanctl -f        # 看日志
TIP
else
cat <<'TIP'
    sudo fanctl --status        # 看转速、百分比、通道路径
    sudo fanctl --selftest      # 验风扇转不转
    sudo fanctl --set 0 80      # 第 0 路手动 80%
    sudo fanctl --auto          # 前台跑自动温控（Ctrl-C 停）
TIP
fi
