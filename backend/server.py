#!/usr/bin/env python3
"""Hermes 行为树监控：只读采集 + HTTP 服务（纯标准库，零依赖）。

数据来源（全部只读，不要求 Hermes 做任何配合）：
  1. 每个 Hermes 实例的 state.db      → 会话 / 对话轮次 / 工具调用（谁派了哪个 claude、结果如何）
  2. /proc 进程表                      → 当前真正在跑的 claude 进程（用来确认“进行中”）
  3. ~/.claude*/projects/*/*.jsonl     → Claude Code 自己的转录，读出“它此刻在干什么”
  4. 各实例 gateway_state.json / pid   → 实例是否存活、正在处理几个会话

输出一份 JSON 快照：instances -> tasks -> leaves（claude code），前端轮询渲染。
"""
import glob
import json
import os
import re
import shlex
import socket
import sqlite3
import sys
import threading
import time
import hashlib
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _load_env():
    """读取仓库根目录（或 backend/）的 .env；已存在的环境变量优先。"""
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

def _path(val, default):
    val = os.path.expanduser(val) if val else default
    return val if os.path.isabs(val) else os.path.join(ROOT, val)


DATA_DIR = _path(os.environ.get("MONITOR_DATA_DIR"), os.path.join(ROOT, "data"))
FRONT_DIR = _path(os.environ.get("MONITOR_FRONT_DIR"), os.path.join(ROOT, "front"))
INDEX_FILE = os.path.join(FRONT_DIR, "index.html")
os.makedirs(DATA_DIR, exist_ok=True)

BOT_ROOT = _path(os.environ.get("HERMES_BOT_ROOT"), os.path.expanduser("~/bot"))
WRAPPER_DIR = _path(os.environ.get("HERMES_WRAPPER_DIR"), os.path.expanduser("~/.local/bin"))
CLAUDE_HOMES = [os.path.expanduser(p) for p in
                os.environ.get("CLAUDE_HOMES", "~/.claude:~/.claude_real:~/.claude_personal").split(":")]
WINDOW_H = float(os.environ.get("WINDOW_HOURS", "24"))      # 采集窗口
RUNNING_FRESH_S = 15 * 60       # 末轮无最终回复且 15 分钟内有动静 → 进行中
REFRESH_S = 2.0

# ---------------------------------------------------------------- 脱敏
_SECRET = [
    (re.compile(r"sk-[A-Za-z0-9_\-]{12,}"), "sk-***"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{12,}"), r"\1***"),
    (re.compile(r"(?i)((?:api[_-]?key|token|secret|passw(?:or)?d|authorization)\s*[=:]\s*)[^\s\"',;]+"), r"\1***"),
    (re.compile(r"\b(?=[A-Za-z0-9]*\d)(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{40,}\b"), "***"),
]


def redact(s):
    s = s or ""
    for rx, rep in _SECRET:
        s = rx.sub(rep, s)
    return s


def clip(s, n):
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


# ---------------------------------------------------------------- 进程表
def _read(path, mode="r"):
    try:
        with open(path, mode, errors="replace") if mode == "r" else open(path, mode) as f:
            return f.read()
    except Exception:
        return None


_CLK = os.sysconf("SC_CLK_TCK")
_BOOT = None


def boot_time():
    global _BOOT
    if _BOOT is None:
        for line in (_read("/proc/stat") or "").splitlines():
            if line.startswith("btime"):
                _BOOT = float(line.split()[1])
    return _BOOT or 0


def proc_table():
    """pid -> {ppid, cmd(list), cwd, start(epoch)}"""
    out = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        pid = int(d)
        stat = _read(f"/proc/{pid}/stat")
        if not stat:
            continue
        try:
            rest = stat[stat.rindex(")") + 2:].split()
            ppid = int(rest[1])
            start = boot_time() + int(rest[19]) / _CLK
            cpu_s = (int(rest[11]) + int(rest[12])) / _CLK
        except Exception:
            continue
        raw = _read(f"/proc/{pid}/cmdline", "rb")
        cmd = [x.decode("utf-8", "replace") for x in (raw or b"").split(b"\0") if x]
        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except Exception:
            cwd = ""
        out[pid] = {"pid": pid, "ppid": ppid, "cmd": cmd, "cwd": cwd, "start": start, "cpu_s": cpu_s}
    return out


_HAPPY_PROFILE_RE = re.compile(r"-p\s+(hermes\d*)\b")


def is_service_proc(p):
    """基础设施进程：模型网关 shim、happy 桥、opencode serve/acp —— 不算工人。"""
    cmd = " ".join(p.get("cmd") or [])
    if "shim.js" in cmd:
        return True
    if "/happy" in cmd and " acp" in cmd:
        return True
    if re.search(r"\bopencode\s+(acp|serve)\b", cmd) or re.search(r"\bopencode\.exe\s+serve\b", cmd):
        return True
    return False


def service_owner(p, inst_ids):
    """happy acp -- hermes -p hermesN 这类桥，按 -p 参数或 cwd 归到对应实例。"""
    cmd = " ".join(p.get("cmd") or [])
    m = _HAPPY_PROFILE_RE.search(cmd)
    if m:
        name = (m.group(1) or "hermes")
        return name if name in inst_ids else ("hermes-default" if name == "hermes" else None)
    mm = re.search(r"/bot/(hermes\d*)/?$", p.get("cwd") or "")
    if mm:
        name = mm.group(1) or "hermes"
        return name if name in inst_ids else ("hermes-default" if name == "hermes" else None)
    return None


def is_idle_worker(p, now, min_age_s=7200, cpu_ratio=0.01):
    """老进程 + CPU 时间占比 <1% → 休眠（挂着的 TUI 空转）。"""
    age = now - (p.get("start") or now)
    if age < min_age_s:
        return False
    cpu = p.get("cpu_s") or 0.0
    return (cpu / max(age, 1.0)) < cpu_ratio


_SESSION_META = {}


def proc_session_meta(p):
    """判断一个进程是不是「人手动开的终端会话」：有 /dev/pts 控制终端或在 tmux 里。"""
    key = (p["pid"], round(p.get("start") or 0, 1))
    if key in _SESSION_META:
        return _SESSION_META[key]
    meta = {"tty": "", "tmux": False}
    try:
        meta["tty"] = os.readlink(f"/proc/{p['pid']}/fd/0")
    except Exception:
        pass
    try:
        with open(f"/proc/{p['pid']}/environ", "rb") as f:
            meta["tmux"] = b"TMUX=" in f.read()
    except Exception:
        pass
    if len(_SESSION_META) > 4000:
        _SESSION_META.clear()
    _SESSION_META[key] = meta
    return meta


def is_human_session(p):
    """有 pts 控制终端或跑在 tmux 里 → 用户自己开的交互会话，不是 Hermes 派发的工人。"""
    m = proc_session_meta(p)
    return m["tmux"] or m["tty"].startswith("/dev/pts/")


def is_harness_payload(p, procs):
    """wrapper/守护进程剔除：shim.js、以及「父进程本身就是工人」的子进程（opencode serve/acp）。"""
    base = os.path.basename((p.get("cmd") or [""])[0]) if p.get("cmd") else ""
    if base in ("node", "nodejs") and any("shim.js" in c for c in (p.get("cmd") or [])):
        return False
    for a in ancestors(p["pid"], procs)[1:]:
        if a in procs and is_harness_proc(procs[a]):
            return False
    return True


def ancestors(pid, table):
    seen = []
    while pid and pid in table and pid not in seen and len(seen) < 40:
        seen.append(pid)
        pid = table[pid]["ppid"]
    return seen


HARNESS_NAMES = {"claude": "Claude Code", "opencode": "opencode", "antigravity": "Antigravity"}
_CLAUDE_TOKS = {"claude", "claude-real", "claude-native", "claude-gw", "ccd"}
_OPENCODE_TOKS = {"opencode", "ocd"}
_AGY_TOKS = {"antigravity", "agy"}


def harness_of_proc(p):
    """判断一个进程是哪种工人：claude / opencode / antigravity；不是则 None。"""
    if not p["cmd"]:
        return None
    exe = os.path.basename(p["cmd"][0])
    if exe in _CLAUDE_TOKS:
        return "claude"
    if exe in _OPENCODE_TOKS:
        return "opencode"
    if exe in _AGY_TOKS:
        return "antigravity"
    if exe == "node":
        head = " ".join(p["cmd"][1:3])
        if "claude-code" in head or any(a.endswith("/claude") for a in p["cmd"][1:3]):
            return "claude"
        if "opencode" in head:
            return "opencode"
        if "antigravity" in head:
            return "antigravity"
    return None


def is_harness_proc(p):
    return harness_of_proc(p) is not None


def is_claude_proc(p):
    return harness_of_proc(p) == "claude"


# ---------------------------------------------------------------- Hermes 实例发现
def discover_instances():
    homes = {}
    for h in sorted(glob.glob(os.path.join(BOT_ROOT, "hermes*"))):
        if os.path.isfile(os.path.join(h, "state.db")):
            homes[h] = {"home": h, "id": os.path.basename(h)}
    # 默认的 ~/.hermes 只有在 gateway 真在跑时才算一个实例
    dflt = os.path.expanduser("~/.hermes")
    st = _read(os.path.join(dflt, "gateway_state.json"))
    if st and os.path.isfile(os.path.join(dflt, "state.db")):
        try:
            if json.loads(st).get("gateway_state") == "running":
                homes.setdefault(dflt, {"home": dflt, "id": "hermes-default"})
        except Exception:
            pass
    # 用 wrapper 脚本里的注释当友好名（如“开发机2助手”）
    for w in glob.glob(os.path.join(WRAPPER_DIR, "hermes*")):
        txt = _read(w) or ""
        m = re.search(r"HERMES_HOME=(\S+)", txt)
        c = re.search(r"^#.*?\(([^)]*)\)", txt, re.M)
        if m and m.group(1) in homes and c:
            nm = re.search(r"Feishu app:\s*([^/]+?)\s*/", c.group(1))
            homes[m.group(1)]["label"] = (nm.group(1) if nm else c.group(1)).strip()
    return list(homes.values())


def _proc_stat_start_time(pid):
    """读 /proc/<pid>/stat 的第 22 列（进程启动时刻，单位 jiffies）。"""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            data = f.read().decode("utf-8", "ignore")
        # comm 里可能有空格/括号，按最后一个 ')' 之后切
        tail = data[data.rfind(")") + 2:].split()
        return int(tail[19])
    except Exception:
        return None


def _gateway_lock_valid(pid, start_time, home):
    """lock 可能是崩溃残留：pid 必须还活着，且（若记录了 start_time）必须是同一个进程。"""
    if not os.path.isdir(f"/proc/{pid}"):
        return False
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmdline = f.read()
    except Exception:
        return False
    if not cmdline.strip():
        return False
    if start_time:
        st = _proc_stat_start_time(pid)
        if st is not None and int(st) != int(start_time):
            return False          # pid 被复用，lock 已过期
    return True


def gateway_pid(home):
    raw = _read(os.path.join(home, "gateway.pid"))
    if raw:
        try:
            return int(json.loads(raw)["pid"])
        except Exception:
            try:
                return int(raw.strip())
            except Exception:
                pass
    # 新版 hermes 把 pid 写在 gateway.lock（JSON）：{"pid":..., "kind":"hermes-gateway",
    # "argv":[...], "start_time":..., "hermes_home":...}
    lock = _read(os.path.join(home, "gateway.lock"))
    if lock:
        try:
            o = json.loads(lock)
            pid = int(o.get("pid") or 0)
            if pid > 0 and _gateway_lock_valid(pid, o.get("start_time"), home):
                return pid
        except Exception:
            pass
    # 退而求其次：扫 /proc 找 HERMES_HOME 对得上的 gateway
    for d in os.listdir("/proc"):
        if d.isdigit():
            cmd = _read(f"/proc/{d}/cmdline", "rb") or b""
            if b"gateway" in cmd and b"hermes" in cmd:
                env = _read(f"/proc/{d}/environ", "rb") or b""
                if f"HERMES_HOME={home}".encode() in env.split(b"\0"):
                    return int(d)
    return None


# ---------------------------------------------------------------- 工人调用识别（claude / opencode / antigravity）
_NON_RUN = {"plugin", "plugins", "auth", "doctor", "update", "upgrade", "mcp", "config",
            "--version", "-v", "install", "setup-token", "--help", "-h", "agents", "migrate-installer"}
_WRAPPERS = {"timeout", "time", "nohup", "setsid", "exec", "env", "stdbuf", "nice", "ionice", "sudo", "command", "script"}
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1.*?\n.*?\n\s*\2\b", re.S)
_OPT_ARG = {"-m", "--model", "-s", "--session", "--title", "--format", "--agent", "--mode", "-c", "--continue"}
_NON_RUN_SUB = {"--version", "-v", "--help", "-h", "help", "models", "auth", "update", "upgrade",
                "acp", "plugin", "plugins", "config", "doctor", "mcp", "login", "logout", "install"}


def _is_claude_tok(t):
    return os.path.basename(t) in _CLAUDE_TOKS


def _strip_prefix(toks):
    """跳过 VAR=val、timeout 120、time、nohup 等前缀，返回真正命令的起点。"""
    i = 0
    while i < len(toks):
        t = toks[i]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t):
            i += 1
        elif os.path.basename(t) in _WRAPPERS:
            i += 1
            while i < len(toks) and (toks[i].startswith("-") or re.match(r"^\d+[smhd]?$", toks[i])):
                i += 1
        else:
            break
    return i


def _opt(toks, names):
    for j, t in enumerate(toks):
        if t in names and j + 1 < len(toks):
            return toks[j + 1]
    return ""


def _positionals(toks, skip=0):
    out, j = [], skip
    while j < len(toks):
        t = toks[j]
        if t in _OPT_ARG:
            j += 2
            continue
        if re.match(r"^\d*[<>]&?$", t):
            j += 2
            continue
        if re.match(r"^\d*[<>]", t) or t.startswith("-"):
            j += 1
            continue
        out.append(t)
        j += 1
    return out


def split_shell(text):
    """按未被引号包住的 && || ; | 换行 ( ` { 切分命令段，引号内的内容原样保留。"""
    segs, buf, q, i, n = [], [], None, 0, len(text)
    while i < n:
        ch = text[i]
        if q:
            buf.append(ch)
            if ch == "\\" and q == '"' and i + 1 < n:
                buf.append(text[i + 1]); i += 1
            elif ch == q:
                q = None
        elif ch in "'\"":
            q = ch; buf.append(ch)
        elif ch == "\\" and i + 1 < n:
            buf.append(ch); buf.append(text[i + 1]); i += 1
        elif ch in ";\n|&(`{":
            segs.append("".join(buf)); buf = []
        else:
            buf.append(ch)
        i += 1
    segs.append("".join(buf))
    return segs


def _find_harness_invocation_inner(command):
    """命令里若真的在*启动*某个工人（而不是 echo/grep 提到它），返回
    {harness, mode, prompt, model, session?, title?, conversation?}，否则 None。"""
    if not command or not any(k in command for k in ("claude", "opencode", "antigravity", "agy", "ocd", "ccd")):
        return None
    hd = re.search(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n(.*?)\n\s*\2\b", command, re.S)
    heredoc_text = (hd.group(3) or "").strip() if hd else ""
    body = _HEREDOC.sub(" ", command)                      # 去掉 heredoc 正文
    def _inv(d):
        if not (d.get("prompt") or "").strip() and heredoc_text:
            d["prompt"] = heredoc_text
        return d

    for seg in split_shell(body):
        seg = seg.strip()
        if not seg:
            continue
        # 代码片段（python/js 源码里提到 opencode、claude 等词）不算启动工人
        if re.search(r"==|\)\s*\.get\(|\bdef\s|\bimport\s|\bprint\(|\blambda\b|\bjson\.", seg):
            continue
        try:
            toks = shlex.split(seg, posix=True)
        except Exception:
            toks = seg.split()
        i = _strip_prefix(toks)
        if i >= len(toks):
            continue
        head = toks[i]
        rest = toks[i + 1:]
        base = os.path.basename(head)
        model = _opt(rest, ("-m", "--model"))

        if base == "tmux" and "send-keys" in rest:
            keys = " ".join(rest[rest.index("send-keys") + 1:])
            for h, tok in (("claude", "claude"), ("opencode", "opencode"), ("antigravity", "antigravity")):
                if re.search(rf"(?:^|[\s;&|'\"`]){tok}(?:-real)?(?:\s|$)", keys):
                    return {"harness": h, "mode": "tmux", "prompt": "", "model": model}
            continue

        if base in _CLAUDE_TOKS:
            if rest and rest[0] in _NON_RUN:
                continue
            mode, prompt = "interactive", ""
            for j, t in enumerate(rest):
                if t in ("-p", "--print"):
                    mode = "print"
                    if j + 1 < len(rest) and not rest[j + 1].startswith("-"):
                        prompt = rest[j + 1]
                    break
            if mode == "print" and not prompt:
                pos = _positionals(rest)
                prompt = pos[-1] if pos else ""
            return {"harness": "claude", "mode": mode, "prompt": prompt, "model": model}

        if base in _OPENCODE_TOKS:
            sub = rest[0] if rest else ""
            if sub in _NON_RUN_SUB:
                continue
            if sub in ("-p", "--print"):
                pos = _positionals(rest, 1)
                return {"harness": "opencode", "mode": "run", "prompt": pos[0] if pos else "",
                        "model": model, "session": _opt(rest, ("-s", "--session"))}
            if sub in ("run", "exec"):
                pos = _positionals(rest, 1)
                return {"harness": "opencode", "mode": "run", "prompt": pos[0] if pos else "",
                        "model": model, "session": _opt(rest, ("-s", "--session")),
                        "title": _opt(rest, ("--title",))}
            # ocd -m model "..." / ocd "$(cat brief)"：给了任务文本就是一次性任务
            #   （位置参数可能被 shell 拆成多段，比如 "$(cat x.md)" → ["$(cat", "x.md)"]，这里拼回去）
            pos = _positionals(rest, 0)
            if pos:
                return {"harness": "opencode", "mode": "run", "prompt": " ".join(pos),
                        "model": model, "session": _opt(rest, ("-s", "--session"))}

            return {"harness": "opencode", "mode": "tui", "prompt": "", "model": model,
                    "session": _opt(rest, ("-s", "--session"))}

        if base in _AGY_TOKS:
            if rest and rest[0] in _NON_RUN_SUB:
                continue
            mode, prompt = "interactive", ""
            for j, t in enumerate(rest):
                if t in ("-p", "--print"):
                    mode = "print"
                    if j + 1 < len(rest) and not rest[j + 1].startswith("-"):
                        prompt = rest[j + 1]
                    break
            if _opt(rest, ("--mode",)) == "auto":
                mode = "auto"
            if mode == "print" and not prompt:
                pos = _positionals(rest)
                prompt = pos[-1] if pos else ""
            return {"harness": "antigravity", "mode": mode, "prompt": prompt, "model": model,
                    "conversation": _opt(rest, ("--conversation",))}
    return None



def find_harness_invocation(command):
    """对外入口：解析命令，并用 heredoc 正文兜底空 prompt。"""
    inv = _find_harness_invocation_inner(command)
    if inv and not (inv.get("prompt") or "").strip():
        hd = re.search(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n(.*?)\n\s*\2\b", command or "", re.S)
        if hd:
            inv["prompt"] = (hd.group(3) or "").strip()
    return inv

def find_claude_invocation(command):
    """兼容旧调用：只返回 claude 的识别结果。"""
    inv = find_harness_invocation(command)
    return inv if inv and inv["harness"] == "claude" else None


def resolve_prompt(prompt, cwd, command=""):
    """把 `$(cat ticket.md)` / `$(<file)` 这类 prompt 还原成真正的指令正文。"""
    src = prompt or command or ""
    m = (re.search(r"\$\(\s*cat\s+([^)]+?)\s*\)", src)
         or re.search(r"\$\(\s*<\s*([^)]+?)\s*\)", src)
         or re.search(r"\$\{\s*cat\s+([^}]+?)\s*\}", src)
         or re.search(r"(?:^|[\s;&|])<\s*([^\s;&|<>]+)", src)
         or re.search(r"--(?:prompt-)?file[= ]\s*([^\s;&|]+)", src)
         or re.search(r"(?:^|\s)-f\s+([^\s;&|]+\.(?:md|txt|json))", src))
    # prompt 本身就是个指令文件（ocd /tmp/brief.md）→ 读出来
    cand = (prompt or "").strip().strip("'\"")
    if cand and not cand.startswith("$") and len(cand) < 300:
        fp = cand if os.path.isabs(cand) else os.path.join(cwd or "", cand)
        if os.path.isfile(fp):
            txt = _read(fp)
            if txt:
                return txt
    if not m:
        return prompt or ""
    p = m.group(1).strip().strip("'\"")
    if not os.path.isabs(p):
        p = os.path.join(cwd or "", p)
    txt = _read(p)
    if txt:
        return txt
    base = os.path.basename(p)
    for cand in (os.path.join(cwd or "", base), os.path.join(cwd or "", "tickets", base)):
        txt = _read(cand)
        if txt:
            return txt
    shown = prompt or src
    m2 = re.match(r"^\$\(\s*(?:cat|<)\s+(.*?)\s*\)$", shown)
    if m2:
        return "（原指令文件已不存在：%s）" % m2.group(1)
    return shown


def cwd_from(command, args):
    wd = args.get("workdir") or args.get("cwd")
    if wd:
        return wd
    m = re.search(r"\bcd\s+(?:-P\s+)?(?:--\s+)?(['\"]?)([^\s;&|'\"]+)\1", command or "")
    return m.group(2) if m else ""


# ---------------------------------------------------------------- Claude 转录
def enc_cwd(cwd):
    return re.sub(r"[^A-Za-z0-9]", "-", cwd or "")


def transcript_for(cwd, since):
    """在 cwd 对应的 projects 目录里找 mtime>=since 的最新 jsonl。"""
    best = None
    if not cwd:
        return None
    for ch in CLAUDE_HOMES:
        d = os.path.join(ch, "projects", enc_cwd(cwd))
        try:
            for fn in os.listdir(d):
                if fn.endswith(".jsonl"):
                    fp = os.path.join(d, fn)
                    mt = os.path.getmtime(fp)
                    if mt >= since and (best is None or mt > best[0]):
                        best = (mt, fp)
        except Exception:
            pass
    return best


def first_user_text(fp):
    """claude 转录里的第一条用户消息（用来给 `$(cat file)` / `$Q` 这类 prompt 起个像样的标题）。"""
    try:
        with open(fp, "rb") as f:
            head = f.read(96 * 1024).decode("utf-8", "replace").splitlines()
    except Exception:
        return ""
    for line in head:
        try:
            o = json.loads(line)
        except Exception:
            continue
        if o.get("type") == "user":
            c = (o.get("message") or {}).get("content")
            if isinstance(c, list):
                c = " ".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
            if isinstance(c, str) and c.strip():
                return c
    return ""


def doing_from_transcript(fp):
    """从 jsonl 尾部读出最后一次工具调用 / 文本，作为“正在干什么”。"""
    try:
        size = os.path.getsize(fp)
        with open(fp, "rb") as f:
            f.seek(max(0, size - 96 * 1024))
            tail = f.read().decode("utf-8", "replace").splitlines()
    except Exception:
        return None
    steps, tools = [], 0
    for line in reversed(tail):
        try:
            o = json.loads(line)
        except Exception:
            continue
        msg = o.get("message") or {}
        if o.get("type") != "assistant" or not isinstance(msg.get("content"), list):
            continue
        for blk in reversed(msg["content"]):
            if blk.get("type") == "tool_use":
                inp = blk.get("input") or {}
                arg = (inp.get("command") or inp.get("file_path") or inp.get("pattern")
                       or inp.get("path") or inp.get("description") or inp.get("url") or "")
                steps.append(f"{blk.get('name')} {clip(redact(str(arg)), 90)}")
                tools += 1
            elif blk.get("type") == "text" and blk.get("text", "").strip():
                steps.append("💬 " + clip(redact(blk["text"]), 120))
            if len(steps) >= 5:
                break
        if len(steps) >= 5:
            break
    if not steps:
        return None
    return {"now": steps[0], "recent": steps[:5]}



# ---------------------------------------------------------------- opencode 转录
OC_DB = os.path.expanduser(os.environ.get("OPENCODE_DB", "~/.local/share/opencode/opencode.db"))
_oc_cache = {}


def _jload(s):
    try:
        return json.loads(s) if isinstance(s, str) else (s or {})
    except Exception:
        return {}


def opencode_info(session="", cwd="", since=0, title_hint=""):
    """从 opencode 的 SQLite 里读会话：标题、第一条用户指令、最后一个工具调用。"""
    if not os.path.isfile(OC_DB):
        return None
    key = session or ("cwd:" + cwd)
    hit = _oc_cache.get(key)
    if hit and time.time() - hit[0] < 4:
        return hit[1]
    try:
        c = sqlite3.connect(f"file:{OC_DB}?mode=ro", uri=True, timeout=1)
        c.row_factory = sqlite3.Row
        row = None
        if session:
            row = c.execute("select * from session where id=?", (session,)).fetchone()
        if row is None and cwd:
            ms = int(max(0, since - 30) * 1000)
            if title_hint:
                row = c.execute("select * from session where directory=? and title=? and time_created>=? "
                                "order by time_created desc limit 1", (cwd, title_hint, ms)).fetchone()
            if row is None:
                row = c.execute("select * from session where directory=? and time_created>=? "
                                "order by time_updated desc limit 1", (cwd, ms)).fetchone()
        if row is None:
            return None
        sid = row["id"]
        prompt = ""
        for r in c.execute("select data from part where session_id=? order by time_created limit 40", (sid,)):
            d = _jload(r["data"])
            if d.get("type") == "text" and (d.get("text") or "").strip():
                prompt = d["text"]
                break
        steps = []
        for r in c.execute("select data from part where session_id=? order by time_created desc limit 24", (sid,)):
            d = _jload(r["data"])
            t = d.get("type")
            if t == "tool":
                st = d.get("state") or {}
                inp = st.get("input") or {}
                arg = inp.get("command") or inp.get("filePath") or inp.get("path") or inp.get("description") or ""
                mark = "▶ " if st.get("status") == "running" else ""
                steps.append(f"{mark}{d.get('tool')} {clip(redact(str(arg)), 100)}".strip())
            elif t == "text" and (d.get("text") or "").strip():
                steps.append("💬 " + clip(redact(d["text"]), 140))
            elif t == "reasoning" and (d.get("text") or "").strip():
                steps.append("💭 " + clip(redact(d["text"]), 120))
            if len(steps) >= 5:
                break
        doing = {"now": steps[0], "recent": steps[:5]} if steps else None
        info = {
            "session": sid, "title": row["title"] or title_hint or "",
            "prompt": prompt or "", "doing": doing,
            "model": (_jload(row["model"]).get("id") or "") if row["model"] else "",
            "startedAt": (row["time_created"] or 0) / 1000.0,
            "lastActivity": (row["time_updated"] or 0) / 1000.0,
            "tokens": (row["tokens_input"] or 0) + (row["tokens_output"] or 0),
        }
        _oc_cache[key] = (time.time(), info)
        return info
    except Exception:
        return None


# ---------------------------------------------------------------- antigravity 转录
AGY_DIR = os.path.expanduser(os.environ.get("ANTIGRAVITY_DIR", "~/.gemini/antigravity-cli"))
AGY_DB = os.path.join(AGY_DIR, "conversation_summaries.db")
_agy_cache = {}


def _ts(s):
    """'2026-11-25 06:25:39.2921071+00:00' → epoch 秒。"""
    try:
        from datetime import datetime, timezone
        s = re.sub(r"\.(\d+)", lambda m: "." + m.group(1)[:6], str(s))
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _agy_ws(uris, cwd):
    for u in _jload(uris) or []:
        p = u[7:] if str(u).startswith("file://") else u
        if cwd and (p.rstrip("/") == cwd.rstrip("/")):
            return True
    return False


def agy_transcript(conv):
    return os.path.join(AGY_DIR, "brain", conv, ".system_generated", "logs", "transcript.jsonl")


def agy_info(cwd="", since=0, conv=""):
    """从 antigravity 的 conversation_summaries.db + brain 转录里读会话。"""
    if not os.path.isfile(AGY_DB):
        return None
    key = conv or ("cwd:" + cwd)
    hit = _agy_cache.get(key)
    if hit and time.time() - hit[0] < 4:
        return hit[1]
    try:
        c = sqlite3.connect(f"file:{AGY_DB}?mode=ro", uri=True, timeout=1)
        c.row_factory = sqlite3.Row
        row = None
        if conv:
            row = c.execute("select * from conversation_summaries where conversation_id=?", (conv,)).fetchone()
        if row is None:
            best = None
            for r in c.execute("select * from conversation_summaries order by last_modified_time desc limit 80"):
                ts = _ts(r["last_modified_time"])
                if ts and ts >= since - 60 and _agy_ws(r["workspace_uris"], cwd):
                    best = r
                    break
            row = best
        if row is None:
            return None
        cid = row["conversation_id"]
        prompt, doing, model = "", None, ""
        fp = agy_transcript(cid)
        try:
            steps = []
            with open(fp, errors="replace") as f:
                for line in f:
                    o = _jload(line)
                    if not o:
                        continue
                    if o.get("type") == "USER_INPUT" and not prompt:
                        t = o.get("content") or ""
                        m = re.search(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", t, re.S)
                        prompt = m.group(1) if m else t
                    if o.get("model"):
                        model = o["model"]
                    if o.get("type") == "PLANNER_RESPONSE":
                        for tc in (o.get("tool_calls") or [])[:3]:
                            a = tc.get("args") or {}
                            arg = str(a.get("CommandLine") or a.get("Path") or a.get("toolSummary") or "").strip("\"")
                            steps.append(f"{tc.get('name')} {clip(redact(arg), 100)}".strip())
                        if (o.get("content") or "").strip():
                            steps.append("💬 " + clip(redact(o["content"]), 120))
            if steps:
                doing = {"now": steps[-1], "recent": list(reversed(steps[-5:]))}
        except Exception:
            pass
        info = {
            "conversation": cid, "title": (row["preview"] or "")[:120],
            "prompt": prompt or "", "doing": doing, "model": model,
            "status": row["status"] or "", "steps": row["step_count"],
            "startedAt": _ts(row["last_user_input_time"]) or _ts(row["last_modified_time"]),
            "lastActivity": _ts(row["last_modified_time"]),
        }
        _agy_cache[key] = (time.time(), info)
        return info
    except Exception:
        return None


# ---------------------------------------------------------------- 用户消息归一化
def normalize_user(content):
    """返回 (kind, text)：kind = system(后台进程通知) | oob(中途追加) | user(正常新任务)"""
    t = (content or "").strip()
    if t.startswith("[IMPORTANT:") and "ackground process" in t[:160]:
        return "system", t
    kind = "user"
    if t.startswith("[OUT-OF-BAND USER MESSAGE"):
        kind = "oob"
        t = t.split("\n", 1)[1] if "\n" in t else ""
    if t.startswith("Gateway message origin"):
        parts = t.split("\n", 2)
        t = parts[2] if len(parts) > 2 and parts[1].lstrip().startswith("{") else (parts[1] if len(parts) > 1 else "")
    m = re.match(r'^\[Replying to: ".*?"\]\s*', t, re.S)
    if m:
        t = t[m.end():] or "（回复了一条消息）"
    t = re.sub(r"\\([\\\-_*#.!()\[\]])", r"\1", t)      # 飞书 markdown 的转义：\- → -
    return kind, t.strip()


# ---------------------------------------------------------------- 飞书应用名（如 roi-老李）
NAMES_FILE = os.path.join(DATA_DIR, "names.json")
_names = {}          # home -> {"name":..., "at":...}
_names_lock = threading.Lock()


def _load_names():
    try:
        with open(NAMES_FILE) as f:
            _names.update(json.load(f))
    except Exception:
        pass


def _env_vals(home):
    d = {}
    try:
        for line in open(os.path.join(home, ".env"), errors="replace"):
            m = re.match(r"^(FEISHU_APP_ID|FEISHU_APP_SECRET|FEISHU_DOMAIN)=(.*)$", line.strip())
            if m:
                d[m.group(1)] = m.group(2).strip().strip("\"'")
    except Exception:
        pass
    return d


def fetch_app_name(home):
    """和 Hermes 自己一样调用飞书 /bot/v3/info 取应用名。凭证只在内存里用，不落盘、不打印。"""
    import urllib.request
    e = _env_vals(home)
    if not (e.get("FEISHU_APP_ID") and e.get("FEISHU_APP_SECRET")):
        return None
    base = "https://open.larksuite.com" if e.get("FEISHU_DOMAIN") == "lark" else "https://open.feishu.cn"
    r = urllib.request.Request(base + "/open-apis/auth/v3/tenant_access_token/internal",
                               json.dumps({"app_id": e["FEISHU_APP_ID"], "app_secret": e["FEISHU_APP_SECRET"]}).encode(),
                               {"Content-Type": "application/json"})
    tok = json.load(urllib.request.urlopen(r, timeout=8)).get("tenant_access_token")
    if not tok:
        return None
    r2 = urllib.request.Request(base + "/open-apis/bot/v3/info", headers={"Authorization": "Bearer " + tok})
    return ((json.load(urllib.request.urlopen(r2, timeout=8)).get("bot") or {}).get("app_name") or "").strip() or None


def app_name_for(inst):
    """缓存的名字；后台线程负责刷新（每 6 小时一次，失败 5 分钟后重试）。"""
    with _names_lock:
        return (_names.get(inst["home"]) or {}).get("name")


def names_loop():
    while True:
        wait = 6 * 3600
        for inst in discover_instances():
            with _names_lock:
                cur = _names.get(inst["home"]) or {}
            if cur.get("name") and time.time() - cur.get("at", 0) < 6 * 3600:
                continue
            try:
                nm = fetch_app_name(inst["home"])
            except Exception as ex:
                nm = None
                print("fetch app name failed:", type(ex).__name__, file=sys.stderr, flush=True)
            if nm:
                with _names_lock:
                    _names[inst["home"]] = {"name": nm, "at": time.time()}
                    try:
                        with open(NAMES_FILE, "w") as f:
                            json.dump(_names, f, ensure_ascii=False)
                    except Exception:
                        pass
            elif not cur.get("name"):
                wait = min(wait, 300)
        time.sleep(wait)

# ---------------------------------------------------------------- 采集一个实例
def load_state(home):
    st = _read(os.path.join(home, "gateway_state.json"))
    try:
        return json.loads(st) if st else {}
    except Exception:
        return {}


def _leaf_proc_match(leaf, harness, procs, used_procs, now):
    """在全部存活进程里找这个叶子对应的那一个（不依赖 cwd 是否在实例目录下）。"""
    prompt = (leaf.get("prompt") or "").strip()
    literal = prompt[:40] if len(prompt) >= 10 and not prompt.startswith("$") else ""
    cwd = (leaf.get("cwd") or "").rstrip("/")
    for p in procs:
        if p["pid"] in used_procs or harness_of_proc(p) != harness:
            continue
        pj = " ".join(p["cmd"])
        hit = False
        if harness == "opencode" and leaf.get("session") and leaf["session"] in pj:
            hit = True
        elif literal and literal in pj:
            hit = True
        elif cwd and p["cwd"].rstrip("/") == cwd and \
                leaf["startedAt"] - 20 <= p["start"] <= (leaf.get("endedAt") or now) + 120:
            hit = True
        if hit:
            used_procs.add(p["pid"])
            return p
    return None


def enrich_leaf(leaf, all_procs, used_procs, now):
    """给叶子补上：存活进程、harness 转录里的完整指令 / 正在做什么 / 模型。"""
    h = leaf.get("harness") or "claude"
    cwd = leaf.get("cwd") or ""
    p = _leaf_proc_match(leaf, h, all_procs, used_procs, now)
    if p:
        leaf["pid"] = p["pid"]
        leaf["procStart"] = p["start"]
        leaf["status"] = "running"
    if h == "opencode":
        info = opencode_info(leaf.get("session") or "", cwd, leaf["startedAt"], leaf.get("titleHint") or "")
        if info:
            if info.get("prompt") and (not leaf.get("prompt") or leaf["prompt"].lstrip().startswith("$(")):
                leaf["prompt"] = redact(info["prompt"])[:PROMPT_MAX]
            if leaf.get("titleHint"):
                leaf["title"] = clip(redact(leaf["titleHint"]), 80)
            elif not leaf.get("prompt") and info.get("title") and \
                    not info["title"].lower().startswith("new session"):
                leaf["title"] = clip(redact(info["title"]), 80)
            if info.get("doing"):
                leaf["doing"] = info["doing"]
            leaf["model"] = info.get("model") or leaf.get("model", "")
            leaf["session"] = info.get("session") or leaf.get("session", "")
            leaf["lastActivity"] = max(leaf.get("lastActivity") or 0, info.get("lastActivity") or 0)
            leaf["tokens"] = info.get("tokens") or 0
            if p and info.get("lastActivity") and now - info["lastActivity"] > 20 * 60:
                leaf["status"] = "stalled"
    elif h == "antigravity":
        info = agy_info(cwd, leaf["startedAt"], leaf.get("conversation") or "")
        if info:
            if info.get("prompt") and not leaf.get("prompt"):
                leaf["prompt"] = redact(info["prompt"])[:PROMPT_MAX]
            if info.get("title") and not leaf.get("prompt"):
                leaf["title"] = clip(redact(info["title"]), 80)
            if info.get("doing"):
                leaf["doing"] = info["doing"]
            leaf["model"] = info.get("model") or leaf.get("model", "")
            leaf["conversation"] = info.get("conversation") or leaf.get("conversation", "")
            leaf["lastActivity"] = max(leaf.get("lastActivity") or 0, info.get("lastActivity") or 0)
            if p and info.get("lastActivity") and now - info["lastActivity"] > 20 * 60:
                leaf["status"] = "stalled"
    else:
        tr = transcript_for(cwd, leaf["startedAt"] - 10)
        if tr:
            leaf["doing"] = doing_from_transcript(tr[1])
            leaf["lastActivity"] = tr[0]
            leaf["transcript"] = os.path.basename(tr[1])
            if (leaf.get("prompt") or "").lstrip().startswith("$") or not leaf.get("prompt"):
                ft = first_user_text(tr[1])
                if ft:
                    leaf["title"] = clip(redact(ft), 80)
                    leaf["prompt"] = redact(ft)[:PROMPT_MAX]
            if leaf["status"] == "running" and now - tr[0] > 20 * 60 and not leaf["pid"]:
                leaf["status"] = "stalled"
    if leaf.get("prompt") and re.match(r"^\s*\$\(\s*(cat|<)", leaf["prompt"]):
        leaf["prompt"] = redact(resolve_prompt(leaf["prompt"], cwd, leaf.get("command", "")))[:PROMPT_MAX]
        if not leaf.get("title") or str(leaf["title"]).lstrip().startswith("$("):
            first = (leaf["prompt"].strip().splitlines() or [""])[0]
            if first:
                leaf["title"] = clip(first, 80)
    return leaf


def collect_instance(inst, procs, now, used_procs=None):
    home = inst["home"]
    gpid = gateway_pid(home)
    gst = load_state(home)
    alive = bool(gpid and (gpid in procs or os.path.isdir(f"/proc/{gpid}")))
    out = {
        "id": inst["id"], "name": app_name_for(inst) or inst.get("label") or inst["id"], "home": home,
        "alive": alive, "pid": gpid, "gateway_state": gst.get("gateway_state"),
        "active_agents": gst.get("active_agents", 0), "tasks": [], "error": None,
    }
    db = os.path.join(home, "state.db")
    since = now - WINDOW_H * 3600
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        c.row_factory = sqlite3.Row
        sessions = c.execute(
            "select id,source,chat_type,display_name,started_at,ended_at,last_activity_at,cwd from sessions "
            "where coalesce(last_activity_at,started_at)>=? order by coalesce(last_activity_at,started_at) desc",
            (since,)).fetchall()
    except Exception as e:
        out["error"] = f"读取 state.db 失败: {e}"
        return out

    # 该实例名下、当前存活的工人进程（先按祖先链，再按 cwd 落在实例目录下兜底）
    all_procs = [p for p in procs.values() if is_harness_proc(p)]
    my_procs = []
    for p in all_procs:
        if gpid and gpid in ancestors(p["pid"], procs):
            my_procs.append(p)
        elif p["cwd"].startswith(home + "/") or p["cwd"].startswith(home):
            my_procs.append(p)
    if used_procs is None:
        used_procs = set()

    tasks = []
    for s in sessions:
        sid = s["id"]
        msgs = c.execute(
            "select id,role,content,tool_calls,tool_call_id,tool_name,timestamp from messages "
            "where session_id=? and timestamp>=? order by timestamp,id", (sid, since - 3600)).fetchall()
        # 切分对话轮次
        turns, cur = [], None
        for m in msgs:
            if m["role"] == "user":
                kind, text = normalize_user(m["content"])
                if kind in ("system", "oob") and cur is not None:
                    cur["msgs"].append(m)            # 并入当前任务（后台通知 / 中途追加指令）
                    if kind == "oob":
                        cur["followups"] += 1
                    continue
                cur = {"user": m, "text": text, "followups": 0, "msgs": []}
                turns.append(cur)
            elif cur is not None:
                cur["msgs"].append(m)
        for ti, t in enumerate(turns):
            um = t["user"]
            if um["timestamp"] < since:
                continue
            ms = [m for m in t["msgs"] if m["role"] != "user"]
            last_ts = ms[-1]["timestamp"] if ms else um["timestamp"]
            final = None
            for m in reversed(ms):
                if m["role"] == "assistant":
                    if not (m["tool_calls"] or "").strip() or m["tool_calls"] in ("[]", "null"):
                        if (m["content"] or "").strip():
                            final = m
                    break
                if m["role"] == "tool":
                    continue
            is_last = ti == len(turns) - 1
            ended_session = s["ended_at"] is not None
            if final is not None:
                status = "done"
            elif is_last and not ended_session and now - last_ts < RUNNING_FRESH_S:
                status = "running"
            elif is_last and not ended_session and out["active_agents"] and now - last_ts < 3600:
                status = "running"
            else:
                status = "stalled"
            # 工具调用 → claude 叶子
            results = {m["tool_call_id"]: m for m in ms if m["role"] == "tool" and m["tool_call_id"]}
            leaves = []
            for m in ms:
                if m["role"] != "assistant" or not m["tool_calls"]:
                    continue
                try:
                    calls = json.loads(m["tool_calls"])
                except Exception:
                    continue
                for call in calls:
                    fn = call.get("function") or {}
                    name = fn.get("name", "")
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except Exception:
                        args = {}
                    # 只认 shell 类工具的命令；execute_code 里出现的 opencode/claude 是源码字符串，不是启动工人
                    if name not in ("terminal", "process_manage", "shell", "bash"):
                        continue
                    cmd = args.get("command") or ""
                    inv = find_harness_invocation(cmd)
                    if not inv:
                        continue
                    if re.match(r"^\d*[<>]", inv.get("prompt") or ""):
                        inv["prompt"] = ""
                    res = results.get(call.get("id") or call.get("call_id"))
                    rj = {}
                    if res and res["content"]:
                        try:
                            rj = json.loads(res["content"])
                        except Exception:
                            rj = {}
                    cwd = cwd_from(cmd, args) or s["cwd"] or ""
                    h = inv["harness"]
                    leaf = {
                        "id": f"{sid}:{m['id']}:{call.get('id', '')[-6:]}",
                        "kind": h, "harness": h, "mode": inv["mode"],
                        "title": clip(redact(inv.get("title") or inv["prompt"] or cmd), 80),
                        "prompt": redact(inv["prompt"])[:PROMPT_MAX],
                        "command": redact(cmd)[:CMD_MAX],
                        "cwd": cwd, "startedAt": m["timestamp"],
                        "endedAt": res["timestamp"] if res else None,
                        "status": "running", "doing": None, "pid": None,
                        "model": inv.get("model", ""), "session": inv.get("session", ""),
                        "conversation": inv.get("conversation", ""), "titleHint": inv.get("title", ""),
                    }
                    bg = bool(args.get("background")) or rj.get("session_id") or "Background" in str(rj.get("output", ""))[:40]
                    if res is None:
                        leaf["status"] = "running" if (status == "running" or now - m["timestamp"] < RUNNING_FRESH_S) else "stalled"
                    else:
                        ec = rj.get("exit_code")
                        if bg and ec in (0, None):
                            leaf["status"] = "running"      # 后台启动，是否结束要看进程
                            leaf["bg"] = True
                        elif ec not in (0, None):
                            leaf["status"] = "failed"
                            leaf["error"] = clip(redact(str(rj.get("error") or rj.get("output") or "")[-300:]), 200)
                        else:
                            leaf["status"] = "done"
                        leaf["result"] = clip(redact(str(rj.get("output", "") if rj else res["content"])[-600:]), 400)
                    enrich_leaf(leaf, all_procs, used_procs, now)
                    if leaf.get("bg") and leaf["status"] == "running" and not leaf.get("pid"):
                        leaf["status"] = "done"            # 后台进程已不在 → 视为已结束
                    leaves.append(leaf)
            # 任务状态：有 claude 在跑则整轮视为进行中
            if any(l["status"] == "running" for l in leaves) and status != "done":
                status = "running"
            if status == "running" and final is None and not leaves and now - last_ts > RUNNING_FRESH_S:
                status = "stalled"
            tasks.append({
                "id": f"{sid}:{um['id']}",
                "title": clip(redact(t["text"]), 90),
                "text": redact(t["text"])[:1500],
                "followups": t["followups"],
                "session": sid,
                "chat": s["display_name"] or s["source"],
                "chatType": s["chat_type"],
                "source": s["source"],
                "status": status,
                "startedAt": um["timestamp"],
                "endedAt": final["timestamp"] if final else None,
                "lastTs": last_ts,
                "toolCalls": sum(1 for m in ms if m["role"] == "tool"),
                "final": clip(redact(final["content"]), 300) if final else None,
                "leaves": leaves,
            })

    # hermes 自带的异步子代理（如有）
    try:
        for r in c.execute("select delegation_id,origin_session,state,dispatched_at,completed_at,task_json from async_delegations "
                           "where dispatched_at>=?", (since,)):
            tj = {}
            try:
                tj = json.loads(r["task_json"] or "{}")
            except Exception:
                pass
            cand = [t for t in tasks if t["session"] == r["origin_session"] and t["startedAt"] <= r["dispatched_at"]]
            if cand:
                tgt = max(cand, key=lambda t: t["startedAt"])
                st = {"running": "running", "completed": "done", "done": "done", "failed": "failed"}.get(r["state"], "stalled")
                tgt["leaves"].append({
                    "id": r["delegation_id"], "kind": "subagent", "mode": "hermes-subagent",
                    "title": clip(redact(str(tj.get("goal") or tj.get("task") or r["delegation_id"])), 80),
                    "prompt": redact(str(tj))[:800], "command": "", "cwd": "", "startedAt": r["dispatched_at"],
                    "endedAt": r["completed_at"], "status": st, "doing": None, "pid": None})
    except Exception:
        pass

    # 找不到对应任务的存活工人进程 → 归到“后台/未归属”
    orphan = [p for p in my_procs if p["pid"] not in used_procs]
    if orphan:
        leaves = []
        for p in orphan:
            h = harness_of_proc(p) or "claude"
            inv = find_harness_invocation(" ".join(p["cmd"])) or {"mode": "process", "prompt": ""}
            leaf = {
                "id": f"proc:{p['pid']}", "kind": h, "harness": h, "mode": inv["mode"],
                "title": clip(redact(inv.get("title") or inv.get("prompt") or " ".join(p["cmd"][1:]) or h), 80),
                "prompt": redact(inv.get("prompt") or "")[:PROMPT_MAX],
                "command": redact(" ".join(p["cmd"]))[:CMD_MAX], "cwd": p["cwd"],
                "startedAt": p["start"], "endedAt": None,
                "status": "stalled" if is_idle_worker(p, now) else "running", "pid": p["pid"],
                "doing": None, "lastActivity": None,
                "model": inv.get("model", ""), "session": inv.get("session", ""),
                "conversation": inv.get("conversation", ""), "titleHint": inv.get("title", ""),
            }
            enrich_leaf(leaf, [], set(), now)
            leaf.update(pid=p["pid"], status="running", procStart=p["start"])
            used_procs.add(p["pid"])
            leaves.append(leaf)
        target = next((t for t in tasks if t.get("status") == "running"), None)
        if target is not None:
            # 有正在进行的对话轮次 → 把没人认领的工人挂到这一轮，便于看出“这一轮派了什么活”
            target["leaves"] = (target.get("leaves") or []) + leaves
            target["lastTs"] = now
        else:
            tasks.append({"id": f"{inst['id']}:orphan", "title": "后台 / 未归属的工人进程", "text": "",
                      "session": "", "chat": "", "chatType": "", "source": "process", "status": "running",
                          "startedAt": min(l["startedAt"] for l in leaves), "endedAt": None,
                          "lastTs": now, "toolCalls": 0, "final": None, "leaves": leaves})

    merge_reports(inst["id"], tasks, now)
    # 只要还有工人进程在跑，这一轮就不算“已完成”（否则会出现 leaves 全 running、任务却显示 done）
    for t in tasks:
        live = [l for l in (t.get("leaves") or []) if l.get("status") in ("running", "stalled")]
        if live and t.get("status") == "done":
            t["status"] = "running"
            t["lastTs"] = now
    tasks.sort(key=lambda t: t["startedAt"], reverse=True)
    out["tasks"] = tasks
    # 忙 / 休息：有任务或 Claude 在跑 = 忙，忙了多久从最早那个在跑的任务算起；
    # 否则休息，休息了多久从最近一次活动结束算起（查全库，不受时间窗口限制）
    running = [t for t in tasks if t["status"] == "running"]
    try:
        last_msg = c.execute("select max(timestamp) from messages").fetchone()[0] or 0
    except Exception:
        last_msg = 0
    last_end = max([t.get("lastTs") or 0 for t in tasks if t["status"] != "running"] + [last_msg])
    out["state"] = "offline" if not alive else ("busy" if running else "rest")
    out["busy_since"] = min(t["startedAt"] for t in running) if running else None
    out["rest_since"] = last_end or None
    return out



# ---------------------------------------------------------------- 主动上报（push）：让 Hermes 自己改网页状态
# 自动采集（pull）负责“兜底和真相”，上报（push）负责“语义”：任务叫什么、Claude 此刻在干嘛、给出明确的开始/结束。
# 两者合并显示：上报的任务/叶子带 reported=True；上报的 Claude 可以挂到自动识别出的任务下。
import secrets
import uuid

REPORTS_FILE = os.path.join(DATA_DIR, "reports.json")
TOKEN_FILE = os.environ.get("MONITOR_TOKEN_FILE", os.path.join(DATA_DIR, ".token"))
DEFAULT_TTL = int(os.environ.get("REPORT_TTL_S", "1800"))      # 上报为“进行中”的条目，超过这么久没更新 → 视为中断
KEEP_FINISHED_S = 7 * 86400
_rep = {"tasks": {}, "leaves": {}}
_rep_lock = threading.RLock()


def load_token():
    try:
        t = open(TOKEN_FILE).read().strip()
        if t:
            return t
    except Exception:
        pass
    t = secrets.token_urlsafe(24)
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(t)
    return t


TOKEN = None


def _rep_load():
    try:
        with open(REPORTS_FILE) as f:
            d = json.load(f)
        _rep["tasks"], _rep["leaves"] = d.get("tasks", {}), d.get("leaves", {})
    except Exception:
        pass


def _rep_save():
    now = time.time()
    for kind in ("tasks", "leaves"):
        for k, v in list(_rep[kind].items()):
            if v["status"] != "running" and now - v["updatedAt"] > KEEP_FINISHED_S:
                del _rep[kind][k]
    tmp = REPORTS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(_rep, f, ensure_ascii=False)
    os.replace(tmp, REPORTS_FILE)


_TASK_ST = {"running": "running", "done": "done", "failed": "failed", "interrupted": "stalled", "stalled": "stalled"}


def resolve_hermes(x):
    """接受实例 id（hermes2）、飞书应用名（roi-老李）或目录名；大小写不敏感。"""
    insts = discover_instances()
    x = (x or "").strip()
    names = {}
    for i in insts:
        names[i["id"].lower()] = i["id"]
        nm = app_name_for(i)
        if nm:
            names[nm.lower()] = i["id"]
        if i.get("label"):
            names[i["label"].lower()] = i["id"]
        names[os.path.basename(i["home"]).lower()] = i["id"]
    if x.lower() in names:
        return names[x.lower()]
    if not x and len(insts) == 1:
        return insts[0]["id"]
    raise ValueError(f"未知的 hermes: {x!r}；可用: {sorted(set(names))}")


def _need(args, *keys):
    for k in keys:
        if args.get(k) in (None, ""):
            raise ValueError(f"缺少参数 {k}")


def apply_op(op, a):
    """所有写操作的唯一入口（REST 和 MCP 共用）。返回可 JSON 化的结果。"""
    now = time.time()
    if op.startswith("harness_"):
        a = dict(a)
        a["harness"] = a.get("harness") or "claude"
        op = "claude_" + op[len("harness_"):]
    with _rep_lock:
        T, L = _rep["tasks"], _rep["leaves"]
        if op == "task_start":
            _need(a, "title")
            hid = resolve_hermes(a.get("hermes"))
            tid = a.get("task_id") or "rt_" + uuid.uuid4().hex[:8]
            old = T.get(tid, {})
            T[tid] = {"id": tid, "hermes": hid, "title": clip(redact(a["title"]), 200), "text": redact(a.get("detail") or a["title"])[:1500],
                      "chat": clip(a.get("chat") or old.get("chat") or "上报", 40), "status": "running",
                      "startedAt": old.get("startedAt") or now, "endedAt": None, "updatedAt": now,
                      "note": clip(redact(a.get("note") or ""), 160), "ttl": int(a.get("ttl_s") or old.get("ttl") or DEFAULT_TTL)}
            res = {"task_id": tid}
        elif op == "task_update":
            _need(a, "task_id")
            t = T.get(a["task_id"]) or (_ for _ in ()).throw(ValueError("找不到 task_id"))
            if a.get("note") is not None:
                t["note"] = clip(redact(a["note"]), 160)
            if a.get("title"):
                t["title"] = clip(redact(a["title"]), 200)
            if a.get("status"):
                t["status"] = _TASK_ST.get(a["status"], "running")
            t["updatedAt"] = now
            res = {"ok": True}
        elif op == "task_finish":
            _need(a, "task_id")
            t = T.get(a["task_id"]) or (_ for _ in ()).throw(ValueError("找不到 task_id"))
            t["status"] = _TASK_ST.get(a.get("status") or "done", "done")
            t["endedAt"] = now
            t["updatedAt"] = now
            if a.get("summary"):
                t["summary"] = clip(redact(a["summary"]), 600)
            res = {"ok": True}
        elif op == "claude_start":
            _need(a, "title")
            hid = resolve_hermes(a.get("hermes")) if (a.get("hermes") or not a.get("task_id")) else None
            tid = a.get("task_id")
            if tid and tid in T:
                hid = T[tid]["hermes"]
            elif not hid:
                hid = resolve_hermes(a.get("hermes"))
            lid = a.get("leaf_id") or "rl_" + uuid.uuid4().hex[:8]
            old = L.get(lid, {})
            L[lid] = {"id": lid, "task_id": tid, "hermes": hid, "title": clip(redact(a["title"]), 200), "prompt": redact(a.get("prompt") or "")[:PROMPT_MAX],
                      "cwd": a.get("cwd") or "", "status": "running", "startedAt": old.get("startedAt") or now, "endedAt": None,
                      "updatedAt": now, "doing": None, "result": "", "error": "",
                      "harness": a.get("harness") or "claude", "model": a.get("model") or "",
                      "ttl": int(a.get("ttl_s") or old.get("ttl") or DEFAULT_TTL)}
            if tid in T:
                T[tid]["updatedAt"] = now
            res = {"leaf_id": lid}
        elif op in ("claude_update", "claude_finish"):
            _need(a, "leaf_id")
            l = L.get(a["leaf_id"]) or (_ for _ in ()).throw(ValueError("找不到 leaf_id"))
            if a.get("doing"):
                d = clip(redact(a["doing"]), 200)
                recent = [d] + [x for x in ((l.get("doing") or {}).get("recent") or []) if x != d]
                l["doing"] = {"now": d, "recent": recent[:5]}
            if a.get("harness"):
                l["harness"] = a["harness"]
            if a.get("model"):
                l["model"] = a["model"]
            if op == "claude_finish":
                l["status"] = {"done": "done", "failed": "failed", "interrupted": "stalled"}.get(a.get("status") or "done", "done")
                l["endedAt"] = now
                if a.get("result"):
                    l["result"] = clip(redact(a["result"]), 800)
                if a.get("error"):
                    l["error"] = clip(redact(a["error"]), 300)
            elif a.get("status"):
                l["status"] = {"running": "running", "done": "done", "failed": "failed", "interrupted": "stalled"}.get(a["status"], "running")
            l["updatedAt"] = now
            if l.get("task_id") in T:
                T[l["task_id"]]["updatedAt"] = now
            res = {"ok": True}
        elif op == "remove":
            _need(a, "id")
            res = {"removed": bool(T.pop(a["id"], None) or L.pop(a["id"], None))}
            if a["id"].startswith("rt_") or a["id"] in {v["task_id"] for v in L.values() if v.get("task_id")}:
                for k in [k for k, v in L.items() if v.get("task_id") == a["id"]]:
                    del L[k]
        elif op == "clear":
            hid = resolve_hermes(a["hermes"]) if a.get("hermes") else None
            only_fin = bool(a.get("only_finished", True))
            n = 0
            for kind in ("tasks", "leaves"):
                for k, v in list(_rep[kind].items()):
                    if (hid is None or v["hermes"] == hid) and (not only_fin or v["status"] != "running"):
                        del _rep[kind][k]
                        n += 1
            res = {"cleared": n}
        else:
            raise ValueError(f"未知操作 {op}")
        _rep_save()
    return res


def eff_status(r, now):
    if r["status"] == "running" and now - r["updatedAt"] > r.get("ttl", DEFAULT_TTL):
        return "stalled"
    return r["status"]


def merge_reports(inst_id, tasks, now):
    with _rep_lock:
        rt = [dict(v) for v in _rep["tasks"].values() if v["hermes"] == inst_id]
        rl = [dict(v) for v in _rep["leaves"].values() if v["hermes"] == inst_id]
    if not rt and not rl:
        return
    by_id = {t["id"]: t for t in tasks}
    for r in rt:
        t = {"id": r["id"], "title": r["title"], "text": r.get("text") or r["title"], "session": "", "chat": r.get("chat") or "上报",
             "chatType": "", "source": "reported", "status": eff_status(r, now), "startedAt": r["startedAt"], "endedAt": r.get("endedAt"),
             "lastTs": r["updatedAt"], "toolCalls": 0, "final": r.get("summary"), "leaves": [], "reported": True, "note": r.get("note")}
        tasks.append(t)
        by_id[t["id"]] = t
    orphan = None
    for r in rl:
        parent = by_id.get(r.get("task_id") or "")
        if parent is None:
            if orphan is None:
                orphan = {"id": f"{inst_id}:reported-orphan", "title": "（未归属任务的上报工人）", "text": "", "session": "", "chat": "上报",
                          "chatType": "", "source": "reported", "status": "done", "startedAt": r["startedAt"], "endedAt": None, "lastTs": r["updatedAt"],
                          "toolCalls": 0, "final": None, "leaves": [], "reported": True, "note": ""}
                tasks.append(orphan)
            parent = orphan
        st = eff_status(r, now)
        h = r.get("harness") or "claude"
        parent["leaves"].append({
            "id": r["id"], "kind": h, "harness": h, "mode": "reported", "title": r["title"], "prompt": r.get("prompt", ""), "command": "",
            "cwd": r.get("cwd", ""), "startedAt": r["startedAt"], "endedAt": r.get("endedAt"), "status": st, "doing": r.get("doing"),
            "pid": None, "result": r.get("result", ""), "error": r.get("error", ""), "lastActivity": r["updatedAt"], "reported": True,
            "model": r.get("model", "")})
        parent["startedAt"] = min(parent["startedAt"], r["startedAt"]) if parent is orphan else parent["startedAt"]
        parent["lastTs"] = max(parent.get("lastTs") or 0, r["updatedAt"])
    # 任何有 Claude 在跑的任务都算进行中；孤儿容器按叶子状态定
    for t in tasks:
        if any(l["status"] == "running" for l in t["leaves"]) and t["status"] != "running":
            t["status"] = "running"
            t["endedAt"] = None
        if t["id"].endswith(":reported-orphan") and t["leaves"] and not any(l["status"] == "running" for l in t["leaves"]):
            t["status"] = "done"


# ---------------------------------------------------------------- MCP（JSON-RPC 2.0，stdio 代理与 /mcp 共用）
_H = {"type": "string", "description": "哪个 Hermes：实例目录名(hermes2)或飞书应用名(roi-老李)。通过本机 stdio 代理调用时可省略，会按 HERMES_HOME 自动填。"}
_HARNESS = {"type": "string", "enum": ["claude", "opencode", "antigravity"], "description": "哪个工人，默认 claude"}
MCP_TOOLS = [
    ("task_start", "【开始处理用户请求时调用】在监控网页上新建一个任务节点（状态=进行中）。返回 task_id，后续 update/finish 和 claude_start 都要带上它。",
     {"title": {"type": "string", "description": "任务名，一句话（≤40字），如“修登录 bug”"}, "hermes": _H,
      "detail": {"type": "string", "description": "可选：用户原话/更详细的描述"}, "chat": {"type": "string", "description": "可选：来自哪个会话/群"},
      "note": {"type": "string", "description": "可选：此刻进展的一句话"}, "ttl_s": {"type": "integer", "description": "可选：多久没更新就判为被中断，默认1800秒"}}, ["title"]),
    ("task_update", "【阶段性进展时调用】更新任务的进展说明(note)，同时刷新心跳（长任务至少每几分钟调一次，否则超时会被标为被中断）。",
     {"task_id": {"type": "string"}, "note": {"type": "string", "description": "当前进展，一句话"}, "title": {"type": "string"},
      "status": {"type": "string", "enum": ["running", "done", "failed", "interrupted"]}}, ["task_id"]),
    ("task_finish", "【任务结束时调用】把任务标记为完成/失败/被中断。完成的任务会进入网页的“已完成”分组。",
     {"task_id": {"type": "string"}, "status": {"type": "string", "enum": ["done", "failed", "interrupted"], "description": "默认 done"},
      "summary": {"type": "string", "description": "一两句话的结果摘要"}}, ["task_id"]),
    ("harness_start", "【派出一个工人时调用】在任务下新建一个工人叶子节点（状态=进行中），支持 claude / opencode / antigravity。返回 leaf_id。",
     {"title": {"type": "string", "description": "这个工人负责什么，一句话"}, "task_id": {"type": "string", "description": "挂在哪个任务下（task_start 返回的）"},
      "hermes": _H, "harness": _HARNESS, "model": {"type": "string", "description": "可选：模型名"},
      "prompt": {"type": "string", "description": "派给它的完整指令（建议贴上）"}, "cwd": {"type": "string"},
      "ttl_s": {"type": "integer"}}, ["title"]),
    ("harness_update", "【工人运行期间调用】更新它此刻在做什么(doing)，同时刷新心跳。",
     {"leaf_id": {"type": "string"}, "doing": {"type": "string", "description": "此刻在做什么，一句话"},
      "status": {"type": "string", "enum": ["running", "done", "failed", "interrupted"]}, "harness": _HARNESS,
      "model": {"type": "string"}}, ["leaf_id"]),
    ("harness_finish", "【工人结束时调用】标记完成/失败/被中断，并附结果。",
     {"leaf_id": {"type": "string"}, "status": {"type": "string", "enum": ["done", "failed", "interrupted"]},
      "result": {"type": "string", "description": "结果摘要"}, "error": {"type": "string", "description": "失败原因"}}, ["leaf_id"]),
    ("claude_start", "【旧名，等同 harness_start】派出一个 Claude Code 时调用。",
     {"title": {"type": "string", "description": "这个 Claude Code 负责什么，一句话"}, "task_id": {"type": "string", "description": "挂在哪个任务下（task_start 返回的）"},
      "hermes": _H, "harness": _HARNESS, "model": {"type": "string"},
      "prompt": {"type": "string", "description": "派给它的完整指令（可选）"}, "cwd": {"type": "string"},
      "ttl_s": {"type": "integer"}}, ["title"]),
    ("claude_update", "【旧名，等同 harness_update】工人运行期间调用。",
     {"leaf_id": {"type": "string"}, "doing": {"type": "string", "description": "此刻在做什么，一句话"},
      "status": {"type": "string", "enum": ["running", "done", "failed", "interrupted"]}}, ["leaf_id"]),
    ("claude_finish", "【旧名，等同 harness_finish】工人结束时调用。",
     {"leaf_id": {"type": "string"}, "status": {"type": "string", "enum": ["done", "failed", "interrupted"]},
      "result": {"type": "string", "description": "结果摘要"}, "error": {"type": "string", "description": "失败原因"}}, ["leaf_id"]),
    ("list", "读取网页上当前的树（含自动识别 + 上报的），返回文字版，用来确认自己报的状态是否生效。",
     {"hermes": _H, "only_running": {"type": "boolean"}}, []),
    ("remove", "删除一个上报的任务或 Claude 节点（填错了/想撤销时用）。只能删上报的，不能删自动识别的。",
     {"id": {"type": "string", "description": "task_id 或 leaf_id"}}, ["id"]),
    ("clear", "清空上报的条目。默认只清已结束的。", {"hermes": _H, "only_finished": {"type": "boolean", "description": "默认 true"}}, []),
]


def _fmt_list(args):
    with _lock:
        snap = _snapshot
    hid = resolve_hermes(args["hermes"]) if args.get("hermes") else None
    out = []
    for i in snap.get("instances", []):
        if hid and i["id"] != hid:
            continue
        out.append(f"## {i['name']} [{i['id']}] {i.get('state')}")
        for t in i["tasks"]:
            if args.get("only_running") and t["status"] != "running" and not any(l["status"] == "running" for l in t["leaves"]):
                continue
            out.append(f"- [{t['status']}] {t['title']}  (id={t['id']}{', 上报' if t.get('reported') else ''})")
            for l in t["leaves"]:
                h = l.get("harness") or "claude"
                out.append(f"    · [{l['status']}] [{h}] {l['title']}  (id={l['id']}){' — ' + l['doing']['now'] if l.get('doing') else ''}")
        if len(out) > 150:
            out.append("…（已截断）")
            break
    return "\n".join(out) or "（空）"


def mcp_handle(req):
    """处理一条 JSON-RPC 请求，返回响应 dict；通知返回 None。"""
    method, rid, params = req.get("method"), req.get("id"), req.get("params") or {}

    def ok(r):
        return {"jsonrpc": "2.0", "id": rid, "result": r}

    def err(code, msg):
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": msg}}
    if rid is None:
        return None
    if method == "initialize":
        return ok({"protocolVersion": params.get("protocolVersion") or "2024-11-05", "capabilities": {"tools": {}},
                   "serverInfo": {"name": "hermes-monitor", "version": "1.0.0"},
                   "instructions": "用来在 Hermes 行为树网页上主动汇报状态：开始任务 task_start → 派 Claude 时 claude_start → 期间 *_update → 结束 *_finish。"})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": [{"name": n, "description": d, "inputSchema": {"type": "object", "properties": pr, "required": rq}}
                             for n, d, pr, rq in MCP_TOOLS]})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        try:
            if name == "list":
                text = _fmt_list(args)
            elif name in {t[0] for t in MCP_TOOLS}:
                text = json.dumps(apply_op(name, args), ensure_ascii=False)
            else:
                return err(-32602, f"未知工具 {name}")
            return ok({"content": [{"type": "text", "text": text}], "isError": False})
        except Exception as e:
            return ok({"content": [{"type": "text", "text": f"错误: {e}"}], "isError": True})
    return err(-32601, f"不支持的方法 {method}")


# ---------------------------------------------------------------- 快照循环
_snapshot = {"ts": 0, "instances": [], "window_hours": WINDOW_H}
_lock = threading.Lock()

# ---------------------------------------------------------------- 后台巡检摘要
# 巡检节奏：只在“树发生变化（新节点/节点信息变化）”时才调 LLM；
# 无变化时只做本地指纹比对，不消耗 API。
INSPECT_POLL_S = float(os.environ.get("INSPECT_POLL_S", "30"))          # 本地比对周期
INSPECT_DEBOUNCE_S = float(os.environ.get("INSPECT_DEBOUNCE_S", "15"))  # 变化后合并抖动
INSPECT_MIN_INTERVAL_S = float(os.environ.get("INSPECT_MIN_INTERVAL_S", "300"))  # 两次 LLM 调用最小间隔
INSPECT_IDLE_REFRESH_S = float(os.environ.get("INSPECT_IDLE_REFRESH_S", "0"))    # >0 时按此周期做空闲兜底刷新
INSPECT_S = float(os.environ.get("INSPECT_S", "600"))
# 详情页展示完整派活指令 / 命令（用户要求看全部，不截断）
PROMPT_MAX = int(os.environ.get("PROMPT_MAX", "20000"))
CMD_MAX = int(os.environ.get("CMD_MAX", "4000"))
# 是否在树里显示“常驻服务/基础设施/人工终端会话”这些非任务节点（默认 0 = 不显示）
SHOW_INFRA = os.environ.get("SHOW_INFRA", "0") not in ("0", "", "false", "False")
INSPECT_FILE = os.path.join(DATA_DIR, "inspect.json")
INSPECT_SIG_FILE = os.path.join(DATA_DIR, "inspect_sig.json")
LLM_URL = os.environ.get("LLM_URL", "http://127.0.0.1:4000/v1/chat/completions")
LLM_FALLBACK_MODEL = os.environ.get("LLM_FALLBACK_MODEL", "gemini-4-argon")
LLM_MODEL = os.environ.get("LLM_MODEL", "ali-deepseek-v4.1-flash-nothink")
LLM_KEY = os.environ.get("LLM_KEY", "")  # 只从 .env 读，源码里不留任何字面量 key
_inspect = {"ts": 0, "text": "", "model": LLM_MODEL, "error": None}
_inspect_lock = threading.Lock()

TITLES_FILE = os.path.join(DATA_DIR, "task_titles.json")
LEAF_FILE = os.path.join(DATA_DIR, "leaf_notes.json")
TITLE_S = float(os.environ.get("TITLE_S", "60"))
# 标题/工人摘要的 LLM 也只在「出现新节点」时调用：记住见过的节点指纹，
# 没有新节点就完全不打模型；有但距上次不足 TITLE_MIN_INTERVAL_S 时也只等不调。
TITLE_MIN_INTERVAL_S = float(os.environ.get("TITLE_MIN_INTERVAL_S", "120"))
SEEN_NODES_FILE = os.path.join(DATA_DIR, "seen_nodes.json")
_seen_nodes = set()
_seen_lock = threading.Lock()

_titles = {}
_titles_lock = threading.Lock()
_leaf_notes = {}
_leaf_lock = threading.Lock()


def llm_chat(prompt, max_tokens=1200, temperature=0.3, timeout=90, model=None):
    if not LLM_KEY:
        raise RuntimeError("LLM_KEY 未配置：请在 .env 里设置 LLM_KEY")
    body = json.dumps({
        "model": model or LLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature, "max_tokens": max_tokens, "reasoning_effort": "none",
    }).encode()
    req = urllib.request.Request(LLM_URL, data=body, headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + LLM_KEY})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        o = json.loads(r.read())
    msg = ((o.get("choices") or [{}])[0].get("message") or {})
    text = msg.get("content") or ""
    if not text.strip():
        # 推理模型（deepseek 等）可能把预算全用在 reasoning 上：退而用 reasoning 里的 JSON 片段
        text = msg.get("reasoning_content") or msg.get("reasoning") or ""
        m = re.search(r"\[[\s\S]*\]|\{[\s\S]*\}", text)
        text = m.group(0) if m else ""
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    if not text.strip():
        raise ValueError("模型返回空内容")
    return text.strip()


def _title_hash(raw):
    return hashlib.sha1((raw or "").encode("utf-8")).hexdigest()[:12]


def load_titles():
    try:
        with open(TITLES_FILE) as f:
            o = json.load(f)
        if isinstance(o, dict):
            with _titles_lock:
                _titles.clear()
                _titles.update(o)
    except Exception:
        pass


def save_titles():
    tmp = TITLES_FILE + ".tmp"
    try:
        with _titles_lock:
            data = dict(_titles)
        with open(tmp, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=0)
        os.replace(tmp, TITLES_FILE)
    except Exception as e:
        print("titles save error:", e, file=sys.stderr, flush=True)


def load_leaf_notes():
    try:
        with open(LEAF_FILE) as f:
            o = json.load(f)
        if isinstance(o, dict):
            with _leaf_lock:
                _leaf_notes.clear()
                _leaf_notes.update(o)
    except Exception:
        pass


def save_leaf_notes():
    tmp = LEAF_FILE + ".tmp"
    try:
        with _leaf_lock:
            data = dict(_leaf_notes)
        with open(tmp, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=0)
        os.replace(tmp, LEAF_FILE)
    except Exception as e:
        print("leaf notes save error:", e, file=sys.stderr, flush=True)


def _clean_expect(t):
    t = re.sub(r"\s+", " ", str(t or "")).strip().strip("\"'“”‘’《》")
    return t[:48]


def _parse_pairs(text, n):
    t = (text or "").replace("【", "[").replace("】", "]").replace("“", "\"").replace("”", "\"")
    m = re.search(r"\[.*\]", t, re.S)
    if m:
        try:
            arr = json.loads(m.group(0))
            if isinstance(arr, list) and len(arr) >= n:
                out = []
                for a in arr[:n]:
                    if isinstance(a, (list, tuple)) and len(a) >= 2:
                        out.append((_clean_title(a[0]), _clean_expect(a[1])))
                    elif isinstance(a, dict):
                        out.append((_clean_title(a.get("t") or a.get("title")),
                                    _clean_expect(a.get("e") or a.get("expect"))))
                    else:
                        out.append((_clean_title(a), ""))
                return out
        except Exception:
            pass
    pairs = re.findall(r'"([^"\n]{1,24})"\s*[,:：]\s*"([^"\n]{1,60})"', t)
    if len(pairs) >= n:
        return [(_clean_title(a), _clean_expect(b)) for a, b in pairs[:n]]
    return []


def need_leaf_note(raw):
    t = (raw or "").strip()
    if not t or t.startswith("后台 / 未归属") or t.startswith("（未归属"):
        return False
    return True


def summarize_leaf_notes():
    with _lock:
        snap = _snapshot
    leaves = []
    for i in snap.get("instances", []):
        for t in i.get("tasks", []):
            for l in t.get("leaves", []):
                leaves.append(l)
    if not leaves:
        return 0
    leaves.sort(key=lambda l: (l.get("status") != "running", -(l.get("startedAt") or 0)))
    with _leaf_lock:
        cache = dict(_leaf_notes)
    groups = {}
    meta = {}
    for l in leaves:
        raw = (l.get("prompt") or l.get("title") or "").strip()
        if not need_leaf_note(raw):
            continue
        h = _title_hash(raw)
        if cache.get(l["id"], {}).get("h") == h:
            continue
        groups.setdefault(raw, []).append(l["id"])
        meta.setdefault(raw, l.get("harness") or l.get("kind") or "claude")
    if not groups:
        return 0
    raws = list(groups)[:4]
    listing = "\n".join(f"{i+1}. [{meta[r]}] {clip(r, 400)}" for i, r in enumerate(raws))
    prompt = ("下面是派给 AI 工人的任务原文（编号 + 工人类型 + 原文）。请对每条做结构化解析：\n"
              "1) title：任务名，≤14 个汉字，动宾结构，突出对象和动作；\n"
              "2) context：任务背景/前提（≤40 汉字，原文没写就写\"未说明\"）；\n"
              "3) what：要做什么（≤40 汉字）；\n"
              "4) how：大概怎么做（≤40 汉字，只要方式/思路，不写具体命令）；\n"
              "5) result：期望的结果/交付物及存放位置（≤40 汉字）；\n"
              "6) expect：验收标准（≤32 汉字，原文没写就写合理标准）。\n"
              "用中文，不要照抄长句，不要 @人名/引号，不要任何解释或思考过程。\n"
              "输出格式（严格一行一条，用 | 分隔 6 个字段，顺序与输入一致）：\n"
              "序号|任务名|上下文|要做什么|大概怎么做|期望结果|验收标准\n"
              "例如：1|修登录超时|线上偶发登录失败|定位并修复超时|先看日志再改重试|超时率降到 0|p99<2s\n\n" + listing)
    try:
        text = llm_chat(prompt, max_tokens=2600, temperature=0.2, timeout=120)
    except Exception as e:
        print("leaf llm error:", e, file=sys.stderr, flush=True)
        return 0
    got = _parse_pipe(text, len(raws)) or _parse_objs(text, len(raws))
    if len(got) < len(raws) and LLM_FALLBACK_MODEL:
        try:
            text2 = llm_chat(prompt, max_tokens=1800, temperature=0.0, timeout=120,
                             model=LLM_FALLBACK_MODEL)
            got2 = _parse_pipe(text2, len(raws)) or _parse_objs(text2, len(raws))
            if len(got2) > len(got):
                got, text = got2, text2
        except Exception as e:
            print("leaf fallback llm error:", e, file=sys.stderr, flush=True)
    if len(got) < len(raws):
        print("leaf parse short:", len(got), "/", len(raws), repr(text[:200]), file=sys.stderr, flush=True)
    n = 0
    with _leaf_lock:
        for raw, title in zip(raws, got):
            if not (isinstance(title, dict) and title.get("title")):
                continue
            for lid in groups[raw]:
                _leaf_notes[lid] = {"h": _title_hash(raw), "t": (title.get("title") or "").strip(),
                                    "e": (title.get("expect") or "").strip(), "p": title,
                                    "ts": int(time.time())}
                n += 1
    if n:
        save_leaf_notes()
    return n


def _parse_pipe(text, n):
    """解析 "序号|任务名|上下文|要做什么|大概怎么做|期望结果|验收标准" 行。"""
    out = []
    for line in (text or "").splitlines():
        line = line.strip().strip("`").strip()
        if "|" not in line:
            continue
        parts = [x.strip().strip('"') for x in line.split("|")]
        if len(parts) < 6:
            continue
        if parts[0].isdigit() or (parts[0].startswith("[") and parts[0].endswith("]")):
            parts = parts[1:]
        if len(parts) < 5:
            continue
        parts = parts[:6]
        keys = ("title", "context", "what", "how", "result", "expect")
        d = {k: _clean_title(v) for k, v in zip(keys, parts)}
        out.append(d)
        if len(out) >= n:
            break
    return out


def _parse_objs(text, n):
    """解析 [{title, context, what, how, result, expect}, ...]；失败则退化成 [title, expect] 对。"""
    t = (text or "").replace("【", "[").replace("】", "]").replace("“", '"').replace("”", '"')
    m = re.search(r"\[.*\]", t, re.S)
    if m:
        try:
            arr = json.loads(m.group(0))
            if isinstance(arr, list) and arr and isinstance(arr[0], dict):
                out = []
                for o in arr[:n]:
                    out.append({k: _clean_title(o.get(k, "")) for k in ("title", "context", "what", "how", "result", "expect")})
                return out
        except Exception:
            pass
    pairs = _parse_pairs(text, n)
    return [{"title": a, "expect": b, "context": "", "what": "", "how": "", "result": ""} for a, b in pairs]


def _need_title(raw):
    t = (raw or "").strip()
    if not t or t.startswith("后台 / 未归属") or t.startswith("（未归属"):
        return False
    if len(t) > 120:
        return True
    wide = sum(1 for ch in t if ord(ch) > 0x2E80)
    return (wide + len(t)) > 30 or "\n" in t or "\r" in t or "@" in t or "http" in t.lower()


def _clean_title(t):
    t = re.sub(r"\s+", " ", str(t or "")).strip().strip("\"'“”‘’《》")
    t = re.sub(r"^[0-9０-９]+[.、)）]\s*", "", t)
    return t[:24]


def _parse_titles(text, n):
    t = (text or "").replace("【", "[").replace("】", "]").replace("“", "\"").replace("”", "\"")
    m = re.search(r"\[.*\]", t, re.S)
    if m:
        try:
            arr = json.loads(m.group(0))
            if isinstance(arr, list) and len(arr) >= n:
                return [_clean_title(x) for x in arr[:n]]
        except Exception:
            pass
    quoted = re.findall(r'"([^"\n]{1,40})"', t)
    if len(quoted) >= n:
        return [_clean_title(x) for x in quoted[:n]]
    out = []
    for line in t.splitlines():
        m2 = re.match(r"^\s*(?:[0-9１-９]+[.、)）]|[-*•])\s*(.+)$", line)
        if m2:
            out.append(_clean_title(m2.group(1)))
    if len(out) < n:
        out = [_clean_title(x) for x in t.splitlines() if x.strip() and not x.strip().startswith(("{", "}", "[", "]"))]
    return out[:n]


def summarize_titles():
    with _lock:
        snap = _snapshot
    tasks = []
    for i in snap.get("instances", []):
        for t in i.get("tasks", []):
            tasks.append(t)
    if not tasks:
        return 0
    tasks.sort(key=lambda t: (t.get("status") != "running", -(t.get("lastTs") or t.get("startedAt") or 0)))
    groups = {}
    with _titles_lock:
        cache = dict(_titles)
    for t in tasks:
        if t.get("noTitle"):
            continue
        raw = t.get("rawTitle") or t.get("title") or ""
        if not _need_title(raw):
            continue
        h = _title_hash(raw)
        if cache.get(t["id"], {}).get("h") == h:
            continue
        groups.setdefault(raw.strip(), []).append(t["id"])
    if not groups:
        return 0
    raws = list(groups)[:20]
    listing = "\n".join(f"{i+1}. {clip(r, 300)}" for i, r in enumerate(raws))
    prompt = ("你是任务命名助手。下面是用户交给 AI 助手的任务原文（可能很长、带 @人名 和编号）。\n"
              "请为每一条起一个中文短标题：不超过 16 个汉字，动宾结构，突出动作和对象，"
              "去掉 @人名、寒暄和编号，不要引号、不要句号、不要解释。\n"
              "只输出一个 JSON 数组，长度与任务条数相同，例如 [\"修复登录超时\", \"评估特征重要性\"]。\n\n"
              + listing)
    try:
        _LLM_STATS["titles"] += 1
        _LLM_STATS["calls"] += 1
        _LLM_STATS["last_call_ts"] = time.time()
        text = llm_chat(prompt, max_tokens=1500, temperature=0.2, timeout=90)
    except Exception as e:
        print("title llm error:", e, file=sys.stderr, flush=True)
        return 0
    got = _parse_titles(text, len(raws))
    if len(got) < len(raws):
        print("title parse short:", len(got), "/", len(raws), repr(text[:200]), file=sys.stderr, flush=True)
    n = 0
    with _titles_lock:
        for raw, title in zip(raws, got):
            if not title:
                continue
            for tid in groups[raw]:
                _titles[tid] = {"h": _title_hash(raw), "t": title, "ts": int(time.time())}
                n += 1
    if n:
        save_titles()
    return n


def pending_leaf_notes(snap):
    """还有哪些工人的「结构化解析」没做过（缓存缺失或原文变了）。"""
    with _leaf_lock:
        cache = dict(_leaf_notes)
    n = 0
    for i in (snap or {}).get("instances", []):
        for t in i.get("tasks", []):
            for l in t.get("leaves", []):
                raw = (l.get("prompt") or l.get("title") or "").strip()
                if not need_leaf_note(raw):
                    continue
                c = cache.get(l.get("id"), {})
                if c.get("h") != _title_hash(raw) or not c.get("p"):
                    n += 1
    return n


def title_loop():
    """标题 / 工人摘要：只在出现新节点时调 LLM，其余轮次纯本地扫表。

    触发条件（全部满足才调）：
      1) 相比上次见过的节点集合，出现了新节点（或同一节点的原文变了 → 指纹变）；
      2) 距上次标题调用 ≥ TITLE_MIN_INTERVAL_S（默认 120s），防止一波新节点连发。
    """
    load_seen_nodes()
    time.sleep(8)
    last_call = float(_LLM_STATS.get("title_call_ts") or 0.0)
    while True:
        sleep_s = max(20.0, TITLE_S)
        try:
            with _lock:
                snap = _snapshot
            keys = node_keys(snap)
            with _seen_lock:
                new_keys = keys - _seen_nodes
            now = time.time()
            pending = pending_leaf_notes(snap)
            if not new_keys and pending == 0:
                _LLM_STATS["title_skipped"] += 1
                _LLM_STATS["title_reason"] = "no-new-node"
            elif now - last_call < TITLE_MIN_INTERVAL_S:
                _LLM_STATS["title_skipped"] += 1
                _LLM_STATS["title_reason"] = "new-node-cooling"
            else:
                n = 0
                try:
                    n = summarize_titles()
                except Exception as e:
                    print("title loop error:", e, file=sys.stderr, flush=True)
                try:
                    n += summarize_leaf_notes()
                except Exception as e:
                    print("leaf loop error:", e, file=sys.stderr, flush=True)
                last_call = _LLM_STATS["title_call_ts"] = time.time()
                _LLM_STATS["title_calls"] += 1
                _LLM_STATS["title_reason"] = ("new-node(%d)" % len(new_keys)) if new_keys else ("pending(%d)" % pending)
                with _seen_lock:
                    _seen_nodes.update(keys)
                save_seen_nodes()
                print("title call: %s (title_calls=%d skipped=%d)"
                      % (_LLM_STATS["title_reason"], _LLM_STATS["title_calls"],
                         _LLM_STATS["title_skipped"]), file=sys.stderr, flush=True)
                if n >= 8:
                    sleep_s = 5.0
        except Exception as e:
            print("title loop error:", e, file=sys.stderr, flush=True)
        time.sleep(sleep_s)


_LLM_STATS = {"calls": 0, "skipped": 0, "titles": 0, "last_reason": "", "last_call_ts": 0.0,
              "title_calls": 0, "title_skipped": 0, "title_reason": "", "title_call_ts": 0.0}
_last_inspected_sig = None


def node_keys(snap):
    """当前树上所有节点的指纹（任务 id+原文 hash、工人 id+指令 hash）。

    用于「只对新节点调 LLM」：指纹集合没有新增元素时，标题/工人摘要一律不调用。
    """
    keys = set()
    for i in (snap or {}).get("instances", []):
        for t in (i.get("tasks") or []):
            keys.add("T:%s:%s" % (t.get("id"), _title_hash(t.get("rawTitle") or t.get("title") or "")))
            for l in (t.get("leaves") or []):
                keys.add("L:%s:%s" % (l.get("id"), _title_hash(l.get("prompt") or l.get("title") or "")))
    return keys


def load_seen_nodes():
    try:
        with open(SEEN_NODES_FILE) as f:
            o = json.load(f)
        if isinstance(o, list):
            with _seen_lock:
                _seen_nodes.clear()
                _seen_nodes.update(str(x) for x in o)
    except Exception:
        pass


def save_seen_nodes():
    try:
        with _seen_lock:
            data = list(_seen_nodes)[-20000:]
        tmp = SEEN_NODES_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, SEEN_NODES_FILE)
    except Exception:
        pass


def tree_signature(snap):
    """树的内容指纹：实例/任务/工人的 id + 状态 + 文案。变了才值得花一次 LLM。"""
    parts = []
    for i in (snap or {}).get("instances", []):
        parts.append("I%s:%d" % (i.get("id"), 1 if i.get("alive") else 0))
        for t in i.get("tasks", []):
            parts.append("T%s:%s:%s" % (t.get("id"), t.get("status"), (t.get("rawTitle") or "")[:60]))
            for l in t.get("leaves", []):
                doing = ((l.get("doing") or {}).get("now") or "")[:80]
                parts.append("L%s:%s:%s" % (l.get("id"), l.get("status"), doing))
    return hashlib.sha1("\n".join(parts).encode("utf-8", "ignore")).hexdigest()


def has_live_work(snap):
    for i in (snap or {}).get("instances", []):
        for t in i.get("tasks", []):
            if t.get("status") == "running" or any(l.get("status") == "running" for l in t.get("leaves", [])):
                return True
    return False


def llm_stats():
    return dict(_LLM_STATS)


def inspect_digest():
    with _lock:
        snap = _snapshot
    lines = []
    for i in snap.get("instances", []):
        lines.append(f"## {i.get('name')} ({i['id']}) {'在线' if i.get('alive') else '离线'}")
        for t in i.get("tasks", [])[:8]:
            lines.append(f"- 任务[{t['status']}] {t['title']}")
            for l in t.get("leaves", [])[:6]:
                d = (l.get("doing") or {}).get("now") or ""
                lines.append(f"  - 工人[{l.get('harness', 'claude')}/{l['status']}] {l['title']}"
                             f" | 指令: {clip(l.get('prompt') or '', 160)} | 在做: {clip(d, 120)}")
    return "\n".join(lines)[:12000]


def run_inspect():
    digest = inspect_digest()
    if not digest.strip():
        return _inspect
    prompt = ("下面是各 Hermes 智能体当前的行为树（任务 / 工人 / 派给工人的指令 / 此刻在做）。\n"
              "请用中文写一份简洁的巡检摘要，分三点：\n"
              "1) 整体进展：哪些任务在推进、哪些已完成；\n"
              "2) 风险：卡住/中断/失败/长时间无动静的，点名到具体工人；\n"
              "3) 建议：下一步该盯什么。\n"
              "只依据数据，不要编造；总长 200-400 字。\n\n" + digest)
    _LLM_STATS["calls"] += 1
    _LLM_STATS["last_call_ts"] = time.time()
    try:
        text = llm_chat(prompt, max_tokens=1200, temperature=0.3, timeout=90)
        with _inspect_lock:
            _inspect.update(ts=time.time(), text=text.strip(), model=LLM_MODEL, error=None)
    except Exception as e:
        with _inspect_lock:
            _inspect.update(ts=time.time(), error=f"{type(e).__name__}: {e}")
        print("inspect error:", e, file=sys.stderr, flush=True)
    try:
        with _inspect_lock:
            with open(INSPECT_FILE, "w") as f:
                json.dump(_inspect, f, ensure_ascii=False)
    except Exception:
        pass
    return _inspect


def inspect_loop():
    try:
        with open(INSPECT_FILE) as f:
            o = json.load(f)
        with _inspect_lock:
            _inspect.update({k: o[k] for k in ("ts", "text", "error") if k in o})
    except Exception:
        pass
    global _last_inspected_sig
    # 重启后不要因为内存里没有指纹就白打一次 LLM：从磁盘恢复上次巡检的指纹
    try:
        with open(INSPECT_SIG_FILE) as f:
            sig_state = json.load(f)
        _last_inspected_sig = sig_state.get("sig") or None
        if sig_state.get("ts"):
            _LLM_STATS["last_call_ts"] = float(sig_state["ts"])
    except Exception:
        pass
    time.sleep(20)
    while True:
        reason = ""
        try:
            with _lock:
                snap = _snapshot
            sig = tree_signature(snap)
            now = time.time()
            since_call = now - (_LLM_STATS["last_call_ts"] or 0.0)
            if sig != _last_inspected_sig:
                if since_call < INSPECT_MIN_INTERVAL_S:
                    reason = "changed-but-cooling"
                else:
                    reason = "tree-changed"
            elif INSPECT_IDLE_REFRESH_S > 0 and has_live_work(snap) and since_call >= INSPECT_IDLE_REFRESH_S:
                reason = "idle-refresh"
            if reason in ("tree-changed", "idle-refresh"):
                time.sleep(INSPECT_DEBOUNCE_S)      # 合并同一批变化，避免连发
                with _lock:
                    snap = _snapshot
                _last_inspected_sig = tree_signature(snap)
                try:
                    with open(INSPECT_SIG_FILE, "w") as f:
                        json.dump({"sig": _last_inspected_sig, "ts": time.time()}, f)
                except Exception:
                    pass
                _LLM_STATS["last_reason"] = reason
                print("inspect call: %s (calls=%d skipped=%d)" % (reason, _LLM_STATS["calls"], _LLM_STATS["skipped"]),
                      file=sys.stderr, flush=True)
                run_inspect()
            else:
                _LLM_STATS["skipped"] += 1
                _LLM_STATS["last_reason"] = reason or "no-change"
        except Exception as e:
            print("inspect loop error:", e, file=sys.stderr, flush=True)
        time.sleep(max(5.0, INSPECT_POLL_S))


def build():
    now = time.time()
    procs = proc_table()
    insts = []
    used = set()
    for inst in discover_instances():
        try:
            insts.append(collect_instance(inst, procs, now, used))
        except Exception as e:  # 单个实例出错不影响其它
            insts.append({"id": inst["id"], "name": inst.get("label") or inst["id"], "home": inst["home"],
                          "alive": False, "tasks": [], "error": f"{type(e).__name__}: {e}"})
    inst_ids = {i["id"] for i in insts}
    if not SHOW_INFRA:
        # 用户只关心派给 Hermes 的任务：基础设施/人工会话不展示
        with _titles_lock:
            tc = dict(_titles)
        for i in insts:
            for t in i.get("tasks", []):
                raw = t.get("title") or ""
                t["rawTitle"] = raw
                if t.get("noTitle"):
                    continue
                c = tc.get(t["id"])
                if c and c.get("h") == _title_hash(raw) and c.get("t"):
                    t["title"] = c["t"]
                    t["titleSource"] = "llm"
        with _leaf_lock:
            lc = dict(_leaf_notes)
        for i in insts:
            for t in i.get("tasks", []):
                for leaf in t.get("leaves", []):
                    raw = (leaf.get("prompt") or leaf.get("title") or "").strip()
                    leaf["rawTitle"] = leaf.get("title") or ""
                    c = lc.get(leaf["id"])
                    if c and c.get("h") == _title_hash(raw):
                        leaf["title"] = c["t"]
                        leaf["expect"] = c["e"]
                        leaf["aiNote"] = True
                        leaf["plan"] = c.get("p")
        with _inspect_lock:
            ins = dict(_inspect)
        return {"ts": now, "window_hours": WINDOW_H, "instances": insts, "inspect": ins,
                "stats": llm_stats()}

    def _service_leaf(p):
        cmd = " ".join(p.get("cmd") or [])
        return {
            "id": f"proc:{p['pid']}", "kind": "service", "harness": "service", "mode": "service",
            "title": clip(redact(cmd), 80), "prompt": "", "command": redact(cmd)[:CMD_MAX],
            "cwd": p.get("cwd") or "", "startedAt": p.get("start"), "endedAt": None,
            "status": "running", "pid": p["pid"], "doing": None, "lastActivity": None,
            "model": "", "session": "", "conversation": "", "titleHint": "",
        }

    # 基础设施进程（happy 桥 / 模型网关 / opencode serve、acp）：能按 -p / cwd 归到实例的就归过去
    svc_free = []
    svc_pids = [q["pid"] for q in procs.values() if is_service_proc(q)]
    for p in procs.values():
        if p["pid"] in used or not is_service_proc(p):
            continue
        if any(a in svc_pids and a != p["pid"] for a in ancestors(p["pid"], procs)[1:]):
            used.add(p["pid"])
            continue
        used.add(p["pid"])
        owner = service_owner(p, inst_ids)
        if owner:
            for i in insts:
                if i["id"] == owner:
                    for t in i.get("tasks", []):
                        if t.get("id") == f"{owner}:service":
                            t["leaves"].append(_service_leaf(p))
                            break
                    else:
                        i.setdefault("tasks", []).append({
                            "id": f"{owner}:service", "noTitle": True,
                            "title": "常驻服务（非任务：happy 桥 / 模型网关）", "text": "",
                            "session": "", "chat": "", "chatType": "", "source": "process", "status": "running",
                            "startedAt": p.get("start"), "endedAt": None, "lastTs": now, "toolCalls": 0,
                            "final": None, "leaves": [_service_leaf(p)]})
                    i["active_agents"] = (i.get("active_agents") or 0) + 1
                    break
        else:
            svc_free.append(p)
    if svc_free:
        leaves = [_service_leaf(p) for p in svc_free]
        insts.append({
            "id": "services", "name": "基础设施进程", "home": "", "alive": True, "pid": None,
            "gateway_state": "running", "active_agents": len(leaves), "error": None, "state": "rest",
            "busy_since": None, "rest_since": min(l["startedAt"] for l in leaves),
            "tasks": [{"id": "services:all", "noTitle": True,
                       "title": "常驻服务（非任务：模型网关 / happy 桥 / serve）",
                       "text": "", "session": "", "chat": "", "chatType": "", "source": "process",
                       "status": "done", "startedAt": min(l["startedAt"] for l in leaves), "endedAt": None,
                       "lastTs": now, "toolCalls": 0, "final": None, "leaves": leaves}],
        })

    # 任何实例都没认领的存活工人进程（手工在 shell / tmux 里起的 opencode、claude…）
    leftovers = [p for p in procs.values()
                 if is_harness_proc(p) and p["pid"] not in used and not is_service_proc(p)
                 and is_harness_payload(p, procs)]
    human = [p for p in leftovers if is_human_session(p)]
    leftovers = [p for p in leftovers if p not in human]

    def _mk_node(node_id, name, title, procs_list):
        leaves = []
        for p in procs_list:
            h = harness_of_proc(p) or "claude"
            inv = find_harness_invocation(" ".join(p["cmd"])) or {"mode": "process", "prompt": ""}
            leaves.append({
                "id": f"proc:{p['pid']}", "kind": h, "harness": h, "mode": inv.get("mode", "process"),
                "title": clip(redact(inv.get("title") or inv.get("prompt") or " ".join(p["cmd"][1:]) or h), 80),
                "prompt": redact(inv.get("prompt") or "")[:PROMPT_MAX],
                "command": redact(" ".join(p["cmd"]))[:CMD_MAX], "cwd": p["cwd"],
                "startedAt": p["start"], "endedAt": None,
                "status": "stalled" if is_idle_worker(p, now) else "running", "pid": p["pid"],
                "doing": None, "lastActivity": None, "model": inv.get("model", ""),
                "session": inv.get("session", ""), "conversation": "", "titleHint": inv.get("title", ""),
            })
            used.add(p["pid"])
        if not leaves:
            return
        insts.append({
            "id": node_id, "name": name, "home": "", "alive": True, "pid": None,
            "gateway_state": "running", "active_agents": len(leaves), "error": None,
            "state": "busy", "busy_since": min(l["startedAt"] for l in leaves), "rest_since": None,
            "tasks": [{"id": f"{node_id}:orphan", "title": title, "text": "", "session": "", "chat": "",
                       "chatType": "", "source": "process", "status": "running",
                       "startedAt": min(l["startedAt"] for l in leaves), "endedAt": None,
                       "lastTs": now, "toolCalls": 0, "final": None, "leaves": leaves}],
        })

    if human:
        _mk_node("human-sessions", "人工终端会话",
                 "人工终端会话（你自己在 pts / tmux 里开的 opencode / claude / agy，不是 Hermes 派发）", human)
    if leftovers:
        _mk_node("unattributed", "未归属工人",
                 "后台 / 未归属的工人进程（无终端、非 tmux，可能是 Hermes 派发但没认领到）", leftovers)
    with _titles_lock:
        tc = dict(_titles)
    for i in insts:
        for t in i.get("tasks", []):
            raw = t.get("title") or ""
            t["rawTitle"] = raw
            if t.get("noTitle"):
                continue
            c = tc.get(t["id"])
            if c and c.get("h") == _title_hash(raw) and c.get("t"):
                t["title"] = c["t"]
                t["titleSource"] = "llm"
    with _leaf_lock:
        lc = dict(_leaf_notes)
    for i in insts:
        for t in i.get("tasks", []):
            for leaf in t.get("leaves", []):
                raw = (leaf.get("prompt") or leaf.get("title") or "").strip()
                leaf["rawTitle"] = leaf.get("title") or ""
                c = lc.get(leaf["id"])
                if c and c.get("h") == _title_hash(raw):
                    leaf["title"] = c["t"]
                    leaf["expect"] = c["e"]
                    leaf["aiNote"] = True
                    leaf["plan"] = c.get("p")
                elif not leaf.get("reported"):
                    if leaf.get("harness") == "service":
                        leaf.setdefault("title", "服务进程")
                    else:
                        nw = (leaf.get("doing") or {}).get("now")
                        leaf["title"] = nw or ("%s 工人" % (leaf.get("harness") or leaf.get("kind") or "Claude"))
    with _inspect_lock:
        ins = dict(_inspect)
    return {"ts": now, "window_hours": WINDOW_H, "instances": insts, "inspect": ins,
            "stats": llm_stats()}


def loop():
    global _snapshot
    while True:
        try:
            snap = build()
            with _lock:
                _snapshot = snap
        except Exception as e:
            print("collect error:", e, file=sys.stderr, flush=True)
        time.sleep(REFRESH_S)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _authed(self):
        got = self.headers.get("X-Monitor-Token") or (self.headers.get("Authorization") or "").replace("Bearer ", "", 1)
        return bool(TOKEN) and secrets.compare_digest(got or "", TOKEN)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self._send(200, open(INDEX_FILE, "rb").read(), "text/html; charset=utf-8")
        elif path == "/api/snapshot":
            with _lock:
                body = json.dumps(_snapshot, ensure_ascii=False).encode()
            self._send(200, body, "application/json; charset=utf-8")
        elif path == "/api/reports":
            with _rep_lock:
                self._json(200, _rep)
        elif path == "/api/inspect":
            with _inspect_lock:
                self._json(200, dict(_inspect))
        elif path == "/api/tools":
            self._json(200, [{"name": n, "description": d, "properties": pr, "required": rq} for n, d, pr, rq in MCP_TOOLS])
        elif path == "/healthz":
            self._send(200, b"ok", "text/plain")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        path = self.path.split("?")[0]
        if path not in ("/api/report", "/mcp", "/api/inspect"):
            return self._send(404, b"not found", "text/plain")
        if not self._authed():
            return self._json(401, {"error": "需要 X-Monitor-Token（见 data/.token）"})
        if path == "/api/inspect":
            print("inspect call: http-manual (calls=%d)" % (_LLM_STATS["calls"] + 1),
                  file=sys.stderr, flush=True)
            return self._json(200, run_inspect())
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 256 * 1024:
                return self._json(413, {"error": "请求体过大"})
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            return self._json(400, {"error": f"JSON 解析失败: {e}"})
        if path == "/mcp":
            if isinstance(body, list):
                outs = [r for r in (mcp_handle(b) for b in body) if r is not None]
                return self._json(200, outs) if outs else self._send(202, b"", "text/plain")
            r = mcp_handle(body)
            return self._json(200, r) if r is not None else self._send(202, b"", "text/plain")
        op = body.pop("op", None)
        try:
            self._json(200, apply_op(op, body))
        except Exception as e:
            self._json(400, {"error": str(e)})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "9010"))
    host = os.environ.get("HOST", "127.0.0.1")
    if "--once" in sys.argv:
        _load_names()
        _rep_load()
        load_titles()
        load_leaf_notes()
        print(json.dumps(build(), ensure_ascii=False, indent=1))
        sys.exit(0)
    _load_names()
    TOKEN = load_token()
    _rep_load()
    load_titles()
    load_leaf_notes()
    threading.Thread(target=names_loop, daemon=True).start()
    threading.Thread(target=loop, daemon=True).start()
    threading.Thread(target=inspect_loop, daemon=True).start()
    threading.Thread(target=title_loop, daemon=True).start()
    print(f"hermes-monitor on http://{host}:{port}", flush=True)
    class Srv(ThreadingHTTPServer):
        address_family = socket.AF_INET6 if ":" in host else socket.AF_INET
        daemon_threads = True
    Srv((host, port), H).serve_forever()
