#!/bin/sh
# 启动 Hermes 行为树监控（默认端口 9010）。在任意目录执行都可以，重复执行安全。
cd "$(dirname "$0")"
[ -f .env ] || echo "提示：未找到 .env，将使用默认配置。可先 cp .env.example .env 再填写。"
PORT=$(sed -n 's/^PORT=//p' .env 2>/dev/null | tail -1 | tr -d '"'"'"' ')
PORT=${PORT:-9010}
PID=$(ss -ltnp 2>/dev/null | grep ":$PORT " | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2)
if [ -n "$PID" ] && tr "\0" " " < "/proc/$PID/cmdline" 2>/dev/null | grep -q "server.py"; then
  kill "$PID" 2>/dev/null; sleep 1
fi
setsid nohup python3 -I backend/server.py > server.log 2>&1 < /dev/null &
sleep 2
if command -v mlx >/dev/null 2>&1; then
  mlx export --port "$PORT"
else
  echo "已启动：http://127.0.0.1:$PORT （未找到 mlx，跳过公网导出）"
fi
