#!/usr/bin/env python3
"""poly-btc 部署与运维 launcher — 一键把跟单系统部署到远程 VPS 或本地,并做长期运维(启停/更新/日志)。

用法:
  python3 launcher.py                 # 起本地服务并自动打开浏览器
  python3 launcher.py --port 8799     # 指定端口
  python3 launcher.py --no-browser    # 不自动开浏览器(远程/无头环境)

依赖:paramiko(远程 VPS 部署需要;本地部署不需要)。装:pip install -r launcher/requirements.txt
"""
import argparse
import os
import sys
import threading
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # so `from core import ...` works

from server import serve  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    url = f"http://{a.host}:{a.port}"
    print("┌─ poly-btc launcher ──────────────────────────")
    if not a.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        serve(a.port, a.host)
    except KeyboardInterrupt:
        print("\n  bye")


if __name__ == "__main__":
    main()
