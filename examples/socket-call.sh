#!/usr/bin/env bash
# 通过 unix socket 远程控制风扇的例子（给脚本/网页/别的程序调用）
#
# 协议：一行一个 JSON 请求，服务端回一行 JSON。字段与人话见 README。
# 需要先在 /etc/fanctl.toml 里 socket_enable = true，
# 并且把 socket_group 设成你有权访问的组（否则只有 root 能连）。

SOCK="${FAN_SOCK:-/run/fanctl/fan.sock}"

send() { printf '%s\n' "$1" | socat - "UNIX-CONNECT:$SOCK"; }

# 1) 探活
send '{"op":"ping"}'

# 2) 看状态（转速 / 百分比 / 通道路径 / 温度）
send '{"op":"status"}'

# 3) 第 0 路设 80%，并且 30 秒后自动交回温控
send '{"op":"set","index":0,"pct":80,"hold":30}'

# 4) 读当前温度
send '{"op":"temp"}'

# 5) 临时改曲线（只在内存里，重启服务恢复配置文件的值）
send '{"op":"curve","points":[[60,50],[75,100]]}'

# 6) 交回自动温控
send '{"op":"auto"}'

# ---- 没有 socat 的话，用 python 也行 ----
# python3 - <<'EOF'
# import socket, json
# s = socket.socket(socket.AF_UNIX); s.connect("/run/fanctl/fan.sock")
# s.sendall(b'{"op":"status"}\n')
# print(s.recv(65536).decode())
# EOF
