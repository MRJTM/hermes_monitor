#!/usr/bin/env python3
"""Hermes 行为树监控 —— MCP stdio 服务（纯标准库，零依赖）。

它只是个薄代理：把 Hermes 通过 stdio 发来的 MCP 请求转发给监控网页的 /mcp 接口，
所以网页服务（server.py）必须在运行。工具逻辑全部在 server.py 里，只维护一份。

环境变量：
  MONITOR_URL         网页服务地址，默认 http://127.0.0.1:9010
  MONITOR_TOKEN_FILE  写接口口令文件，默认 与本脚本同目录的 .token
  MONITOR_TOKEN       直接给口令（优先于文件）
  HERMES_HOME         Hermes 启动时已有；本代理用它的目录名（如 hermes2）自动填 `hermes` 参数，
                      所以 Hermes 调工具时不用再自己说“我是谁”。
"""
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _load_env():
    for base in (ROOT, HERE):
        path = os.path.join(base, ".env")
        if not os.path.exists(path):
            continue
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip()
                    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                        v = v[1:-1]
                    if k and k not in os.environ:
                        os.environ[k] = v
        except OSError:
            pass


_load_env()

def _data_dir():
    val = os.path.expanduser(os.environ.get("MONITOR_DATA_DIR") or "")
    if not val:
        val = os.path.join(ROOT, "data")
    return val if os.path.isabs(val) else os.path.join(ROOT, val)


DATA_DIR = _data_dir()
URL = os.environ.get("MONITOR_URL", "http://127.0.0.1:9010").rstrip("/") + "/mcp"
ME = os.path.basename((os.environ.get("HERMES_HOME") or "").rstrip("/"))


def token():
    t = os.environ.get("MONITOR_TOKEN")
    if t:
        return t.strip()
    try:
        return open(os.environ.get("MONITOR_TOKEN_FILE", os.path.join(DATA_DIR, ".token"))).read().strip()
    except Exception:
        return ""


def forward(req):
    if req.get("method") == "tools/call" and ME:
        a = req.setdefault("params", {}).setdefault("arguments", {})
        if isinstance(a, dict) and not a.get("hermes"):
            a["hermes"] = ME
    data = json.dumps(req, ensure_ascii=False).encode()
    r = urllib.request.Request(URL, data, {"Content-Type": "application/json", "X-Monitor-Token": token()})
    with urllib.request.urlopen(r, timeout=10) as resp:
        body = resp.read()
    return json.loads(body) if body else None


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        try:
            out = forward(req)
        except Exception as e:
            if req.get("id") is None:
                continue
            out = {"jsonrpc": "2.0", "id": req["id"],
                   "error": {"code": -32000, "message": f"监控网页服务不可用（{URL}）：{type(e).__name__}: {e}"}}
        if out is not None:
            sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
