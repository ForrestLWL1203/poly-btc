#!/usr/bin/env bash
# ── poly-btc 部署运维台 · 双击启动 ──────────────────────────────
# 在 Finder 里双击此文件即可。会自动打开浏览器到 http://127.0.0.1:8799
# 关闭:在弹出的终端窗口里按 Ctrl+C。
cd "$(dirname "$0")/../.." || exit 1

PORT=8799
echo "┌─ poly-btc launcher ─────────────────────────────"
echo "│  启动中…浏览器将自动打开 http://127.0.0.1:$PORT"
echo "│  关闭:按 Ctrl+C"
echo "└─────────────────────────────────────────────────"

# 清掉上一次残留的实例(避免端口占用),再干净启动
lsof -ti:$PORT 2>/dev/null | xargs kill -9 2>/dev/null

python3 -m hyper.launcher.launcher --port $PORT

echo
echo "launcher 已退出。可关闭此窗口(或按任意键)。"
read -n 1 -s
