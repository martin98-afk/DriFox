# -*- coding: utf-8 -*-
"""自杀守卫：拒绝会终止 DriFox 自身进程的命令

永久开启：不受沙箱开关（sandbox_enabled）控制、不弹审批、无配置项。
落点：plugins/system-core/tools/terminal_tools.py 的 execute_bash / bg_start。

判定（命中任一即拒）：
  1. 目标 PID 等于当前进程 PID
  2. 目标进程名等于自身镜像名（taskkill /IM、killall、pkill、Stop-Process -Name）
  3. 目标字面量是 $PPID / ${PPID}（POSIX 下工具宿主的父进程，即 DriFox）

刻意不做的事：
  - 不解析运行时构造的 PID（kill $TARGET、python -c "os.kill(...)"）
  - 不做模糊模式匹配（pkill -f <正则> 只按整串取 basename 比较）
  静态解析不到的形态一律放行，避免把「清理相邻进程」误判成自杀。
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Iterable, Optional

try:  # psutil 是主依赖，缺失时退化为只用 sys.executable 判名
    import psutil
except Exception:  # pragma: no cover - 环境异常兜底
    psutil = None

# ============================================================
# 词法层
# ============================================================
# 引号整段保留，其余按空白切（与 app/tools/sandbox.py 同款口径：保留 Windows 反斜杠）
_TOKEN_RE = re.compile(r'"[^"]*"|\'[^\']*\'|\S+')

# shell 连接符，按长度降序保证 || 先于 |
_SEPARATORS = (";;", "&&", "||", "|&", ";", "|", "&")

_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# 命令位前导包装器（真实命令在其后）
_LEAD_WRAPPERS = frozenset(
    {
        "sudo", "doas", "env", "command", "builtin", "exec",
        "nohup", "time", "nice", "xargs", "start",
    }
)

# 包装器的取值型选项（其后紧跟的值不是命令）
_WRAPPER_VALUE_FLAGS = frozenset({"-u", "--user", "-g", "--group", "-n", "--max-args"})

# 可执行任意命令的壳：值 = 载荷起始 flag
_SHELL_PAYLOAD_FLAGS = {
    "cmd": {"/c", "/k"},
    "powershell": {"-command", "-c", "-encodedcommand", "-enc", "-ec"},
    "pwsh": {"-command", "-c", "-encodedcommand", "-enc", "-ec"},
    "bash": {"-c"},
    "sh": {"-c"},
    "zsh": {"-c"},
    "dash": {"-c"},
}

# 快速预筛：整条命令不含这些词就不可能命中
_PREFILTER = re.compile(
    r"(?i)\b(taskkill|tskill|kill|killall|pkill|stop-process|terminate)\b"
)

# PowerShell 管道形态：Get-Process ... | Stop-Process
_PS_PIPE_RE = re.compile(
    r"(?is)get-process\b([^|]*)\|\s*(?:stop-process|kill|taskkill)\b"
)

_PPID_LITERALS = frozenset({"$ppid", "${ppid}", "$$ppid"})


def _strip_quotes(token: str) -> str:
    t = token.strip()
    if len(t) >= 2 and t[0] == t[-1] and t[0] in ("'", '"'):
        return t[1:-1]
    return t


def _cmd_head(token: str) -> str:
    """命令名归一化：去路径前缀、去 .exe 后缀、小写"""
    name = _strip_quotes(token).lower().replace("\\", "/").split("/")[-1]
    return name[:-4] if name.endswith(".exe") else name


def _name_key(token: str) -> str:
    """进程名归一化：取 basename、去 .exe、小写"""
    return _cmd_head(token)


def _tokens(segment: str) -> list[str]:
    return [t for t in (_strip_quotes(x) for x in _TOKEN_RE.findall(segment)) if t]


def _split_commands(command: str) -> list[str]:
    """按 shell 连接符切段（引号内不切）"""
    parts: list[str] = []
    buf: list[str] = []
    quote = ""
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        matched = ""
        for sep in _SEPARATORS:
            if command.startswith(sep, i):
                matched = sep
                break
        if matched:
            parts.append("".join(buf))
            buf = []
            i += len(matched)
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _drop_leading(tokens: list[str]) -> list[str]:
    """去掉环境赋值与前导包装器，返回真实命令起点"""
    idx = 0
    n = len(tokens)
    while idx < n:
        tok = tokens[idx]
        if _ASSIGN_RE.match(tok):
            idx += 1
            continue
        if _cmd_head(tok) not in _LEAD_WRAPPERS:
            break
        idx += 1
        while idx < n:
            nxt = tokens[idx]
            if _ASSIGN_RE.match(nxt):
                idx += 1
                continue
            if not nxt.startswith("-"):
                break
            flag = nxt.lower()
            idx += 1
            if flag in _WRAPPER_VALUE_FLAGS and idx < n:
                idx += 1
    return tokens[idx:]


def _iter_commands(command: str, depth: int = 0) -> Iterable[tuple[str, list[str]]]:
    """产出 (命令头, 参数)；切段后解一层壳，壳载荷递归展开"""
    if depth > 3:
        return
    for segment in _split_commands(command):
        tokens = _drop_leading(_tokens(segment))
        if not tokens:
            continue
        head = _cmd_head(tokens[0])
        payload_flags = _SHELL_PAYLOAD_FLAGS.get(head)
        if payload_flags and depth < 3:
            payload_at = None
            for i, tok in enumerate(tokens[1:], start=1):
                if tok.lower() in payload_flags:
                    payload_at = i + 1
                    break
            if payload_at is not None and payload_at < len(tokens):
                yield from _iter_commands(" ".join(tokens[payload_at:]), depth + 1)
                continue
        yield head, tokens[1:]


# ============================================================
# 目标解析：各命令族的 PID / 名称参数
# ============================================================
def _taskkill_targets(args: list[str]) -> tuple[set[int], set[str]]:
    pids: set[int] = set()
    names: set[str] = set()
    i = 0
    while i < len(args):
        tok = args[i].lower()
        nxt = args[i + 1] if i + 1 < len(args) else ""
        if tok in ("/pid", "-pid"):
            if nxt.isdigit():
                pids.add(int(nxt))
            i += 2
            continue
        if tok in ("/im", "-im"):
            if nxt:
                names.add(nxt)
            i += 2
            continue
        if tok in ("/fi", "-fi"):
            m = re.search(r"pid\s+eq\s+(\d+)", nxt, re.I)
            if m:
                pids.add(int(m.group(1)))
            m = re.search(r"imagename\s+eq\s+([^\s'\"]+)", nxt, re.I)
            if m:
                names.add(m.group(1))
            i += 2
            continue
        i += 1
    return pids, names


def _tskill_targets(args: list[str]) -> tuple[set[int], set[str]]:
    pids: set[int] = set()
    names: set[str] = set()
    for tok in args:
        if tok.startswith(("/", "-")):
            continue
        if tok.isdigit():
            pids.add(int(tok))
        else:
            names.add(tok)
        break
    return pids, names


def _kill_targets(args: list[str]) -> tuple[set[int], set[str], bool]:
    """返回 (PID 集合, 名称集合, 是否命中 $PPID)"""
    pids: set[int] = set()
    ppid_hit = False
    for tok in args:
        if tok.startswith("-"):
            continue
        if tok.lower() in _PPID_LITERALS:
            ppid_hit = True
            continue
        if tok.isdigit():
            pids.add(int(tok))
    return pids, set(), ppid_hit


def _killall_targets(args: list[str]) -> set[str]:
    for tok in args:
        if tok.startswith("-"):
            continue
        return {tok}
    return set()


def _stop_process_targets(args: list[str]) -> tuple[set[int], set[str]]:
    pids: set[int] = set()
    names: set[str] = set()
    i = 0
    while i < len(args):
        tok = args[i].lower()
        nxt = args[i + 1] if i + 1 < len(args) else ""
        if tok in ("-id", "-pid"):
            for part in nxt.split(","):
                part = part.strip()
                if part.isdigit():
                    pids.add(int(part))
            i += 2
            continue
        if tok in ("-name", "-processname"):
            if nxt:
                names.add(nxt)
            i += 2
            continue
        i += 1
    return pids, names


def _wmic_targets(args: list[str]) -> tuple[set[int], set[str]]:
    text = " ".join(args)
    if not re.search(r"(?i)\bcall\s+(terminate|delete)\b", text):
        return set(), set()
    pids: set[int] = set()
    names: set[str] = set()
    m = re.search(r"(?i)processid\s*=\s*['\"]?(\d+)", text)
    if m:
        pids.add(int(m.group(1)))
    m = re.search(r"(?i)\bname\s*=\s*['\"]([^'\"]+)['\"]", text)
    if m:
        names.add(m.group(1))
    return pids, names


def _pipeline_name_targets(command: str) -> set[str]:
    """`Get-Process -Name X | Stop-Process` 的目标名；无目标的全杀形态返回 '*'

    Get-Process 不带目标再管道给 Stop-Process = 杀所有进程，必然包含自己。
    """
    names: set[str] = set()
    for m in _PS_PIPE_RE.finditer(command):
        body = m.group(1)
        name_match = re.search(r"(?i)-name\s+([^\s,;|]+)", body)
        if name_match:
            names.add(name_match.group(1).strip("'\""))
            continue
        positional = [
            tok
            for tok in _tokens(body)
            if not tok.startswith("-") and _cmd_head(tok) not in ("get-process",)
        ]
        if positional:
            names.add(positional[0])
        else:
            names.add("*")
    return names


# ============================================================
# 自身进程信息
# ============================================================
def _self_names() -> set[str]:
    names: set[str] = set()
    try:
        names.add(_name_key(Path(sys.executable).name))
    except Exception:
        pass
    if psutil is not None:
        try:
            names.add(_name_key(psutil.Process().name()))
        except Exception:
            pass
    names.discard("")
    return names


def _reject_message(self_pid: int, self_names: set[str]) -> str:
    name = next(iter(sorted(self_names)), "")
    who = f"PID {self_pid}" + (f" / 进程名 {name}" if name else "")
    return (
        f"已拒绝：该命令会终止当前 DriFox 进程（{who}）。\n"
        "如需清理其它进程，请用精确 PID 指定目标。"
    )


def check_self_kill(command: str) -> Optional[str]:
    """命中「会杀死自己」时返回拒绝原因，否则返回 None"""
    if not command or not command.strip():
        return None
    if not _PREFILTER.search(command):
        return None

    self_pid = os.getpid()
    self_names = _self_names()

    for head, args in _iter_commands(command):
        pids: set[int] = set()
        names: set[str] = set()
        ppid_hit = False

        if head == "taskkill":
            pids, names = _taskkill_targets(args)
        elif head == "tskill":
            pids, names = _tskill_targets(args)
        elif head == "kill":
            pids, _names, ppid_hit = _kill_targets(args)
        elif head in ("pkill", "killall"):
            names = _killall_targets(args)
        elif head == "stop-process":
            pids, names = _stop_process_targets(args)
        elif head == "wmic":
            pids, names = _wmic_targets(args)
        else:
            continue

        if self_pid in pids or ppid_hit:
            return _reject_message(self_pid, self_names)
        if any(_name_key(n) in self_names for n in names):
            return _reject_message(self_pid, self_names)

    # 管道形态单独判：切段后 Stop-Process 段自身无目标
    for name in _pipeline_name_targets(command):
        if name == "*" or _name_key(name) in self_names:
            return _reject_message(self_pid, self_names)

    return None


def guard_tool_call(tool_name: str, args: dict) -> Optional[str]:
    """宿主统一闸门：带 command 参数的工具调用在 ToolExecutor 层统一过守卫

    tool_name 不参与判定：命令来自哪个插件、哪个工具都不影响边界，
    只认 args 里的真实命令文本。新增命令工具无需任何适配。
    """
    if not isinstance(args, dict):
        return None
    command = args.get("command")
    if not isinstance(command, str) or not command.strip():
        return None
    return check_self_kill(command)


def check_self_kill_argv(argv) -> Optional[str]:
    """argv 形态（能力池 process.start 等）的同一套判定"""
    if not argv:
        return None
    try:
        command = " ".join(str(a) for a in argv)
    except Exception:
        return None
    return check_self_kill(command)
