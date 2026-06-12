# -*- coding: utf-8 -*-
"""
命令安全分类器 — 消除 shell=True 注入风险的辅助模块

策略（三分法）:
  Path A — 安全路径 (shell=False): 不含 shell 元字符的命令，直接用 argv 数组执行
  Path B — 受控路径 (shell=True + 审批): 需要管道/重定向等 shell 特性的命令，需用户确认
  Path C — 拒绝: 黑名单命令直接拦截
"""
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Optional

from loguru import logger

# ============================================================
# Shell 元字符检测
# ============================================================
# 这些字符需要 shell 解释器来处理
SHELL_META = re.compile(r'[|;&`$()<>]')

# Windows 上的额外 cmd.exe 元字符
WINDOWS_SHELL_META = re.compile(r'[|;&`$()<>\^@!%]')

# ============================================================
# Windows Shell 内置命令（必须通过 shell 执行）
# ============================================================
# 这些命令是 cmd.exe 的内置命令，不是可执行文件，
# 必须通过 shell=True 执行，否则会 FileNotFoundError
WINDOWS_BUILTIN_COMMANDS = frozenset({
    # 文件/目录操作
    'dir', 'cd', 'chdir', 'md', 'mkdir', 'rd', 'rmdir',
    'copy', 'move', 'ren', 'rename', 'del', 'erase', 'type',
    'deltree', 'xcopy', 'robocopy', 'replace',
    # I/O 命令
    'echo', 'echo.',
    # 系统命令
    'cls', 'ver', 'date', 'time', 'prompt', 'set', 'path',
    'append', 'assign', 'backcup', 'call', 'cdd', 'choice',
    'cmd', 'color', 'comp', 'compact', 'convert', 'ctty',
    'diskcomp', 'diskcopy', 'doskey', 'edit', 'edlin',
    'expand', 'extract', 'fasthelp', 'fc', 'fdisk',
    'find', 'findstr', 'fixfat', 'fonttool', 'format',
    'graftabl', 'graphics', 'join', 'keyb', 'label',
    'loadfix', 'loadhigh', 'lock', 'mem', 'mirror',
    'mkdir', 'mode', 'more', 'msbackup', 'msd', 'nlsfunc',
    'ntbackup', 'pathping', 'pause', 'ping', 'power',
    'print', 'qbasic', 'rabios', 'recover', 'rem',
    'restore', 'rsh', 'runas', 'setver', 'share',
    'shift', 'smartdrv', 'sort', 'start', 'subst',
    'sys', 'telnet', 'tftp', 'tint', 'title',
    'tlntadmn', 'tracert', 'tree', 'undelete', 'unformat',
    'unlock', 'verify', 'vol', 'wingding', 'win', 'wmic',
    'xwizard', 'dos', 'hh',
})

# ============================================================
# 命令白名单/黑名单
# ============================================================
# 自动允许的安全命令（只读/无害操作）
SAFE_COMMANDS = frozenset({
    # 文件查看
    'echo', 'cat', 'type', 'ls', 'dir', 'more', 'less',
    'head', 'tail', 'grep', 'findstr', 'find', 'sort', 'wc',
    'uniq', 'cut', 'tee',
    # 系统信息
    'pwd', 'date', 'time', 'whoami', 'hostname', 'uname',
    'id', 'uptime', 'env', 'printenv', 'set',
    # 开发工具
    'python', 'python3', 'node', 'deno', 'bun',
    'npm', 'pip', 'pip3', 'uv', 'cargo',
    'git', 'curl', 'wget', 'make',
    # 文件操作（只读）
    'stat', 'file', 'du', 'df', 'which', 'where',
})

# 需要用户确认的危险命令
CONFIRM_COMMANDS = frozenset({
    # 文件删除/修改
    'rm', 'del', 'erase', 'rmdir', 'rd', 'deltree',
    'mv', 'move', 'rename', 'ren', 'cp', 'copy', 'xcopy', 'robocopy',
    'chmod', 'chown', 'attrib',
    # 进程管理
    'kill', 'taskkill', 'pkill',
    # 系统管理
    'sudo', 'su', 'doas',
    'shutdown', 'reboot', 'halt', 'poweroff',
    'mount', 'umount',
    'diskpart', 'fdisk', 'mkfs', 'format',
    'dd', 'parted',
    # 注册表
    'reg', 'regedit', 'regedt32',
    # 包管理（修改系统）
    'apt', 'apt-get', 'dnf', 'yum', 'pacman', 'zypper',
    'brew', 'port', 'choco', 'scoop', 'winget',
})

# 禁止执行的命令
BLOCK_COMMANDS = frozenset({
    'cacls', 'icacls', 'takeown',         # 权限篡改
    'sc', 'services', 'net', 'net1',       # 服务控制（Windows）
    'nbtstat', 'nslookup',                # 网络诊断（可能泄露信息）
})


def _remove_quoted(text: str) -> str:
    """去除引号内的内容，避免引号内的元字符被误判"""
    # 去除单引号、双引号内的内容
    result = re.sub(r"'[^']*'", '', text)
    result = re.sub(r'"[^"]*"', '', result)
    return result


def needs_shell(command: str) -> bool:
    """检测命令是否需要 shell 解释器

    如果命令包含管道、重定向、命令链等 shell 特性，返回 True。
    Windows 上还需要考虑 shell 内置命令（如 dir, type, copy 等）。
    """
    if not command or not command.strip():
        return False

    cleaned = _remove_quoted(command)

    if sys.platform == "win32":
        # 1. 检查是否有 shell 元字符
        if WINDOWS_SHELL_META.search(cleaned):
            return True
        # 2. Windows 上：检查是否是 shell 内置命令
        cmd_name = _extract_cmd_name(command)
        if cmd_name and cmd_name in WINDOWS_BUILTIN_COMMANDS:
            return True
        return False
    return bool(SHELL_META.search(cleaned))


def _extract_cmd_name(command: str) -> Optional[str]:
    """提取命令名称（去掉路径前缀）"""
    try:
        parts = shlex.split(command)
        if not parts:
            return None
        raw_cmd = parts[0].lower()
        cmd_name = raw_cmd.replace('\\', '/').split('/')[-1]
        return cmd_name
    except ValueError:
        return None


def classify_command(command: str) -> str:
    """分类命令: 'safe' | 'confirm' | 'block'

    Returns:
        'safe':   可安全自动执行
        'confirm': 需要用户确认
        'block':  禁止执行
    """
    if not command or not command.strip():
        return 'block'

    try:
        parts = shlex.split(command)
        if not parts:
            return 'block'
    except ValueError:
        # shlex 解析失败（如引号不配对），说明是复杂 shell 命令
        return 'confirm'

    # 提取命令名（去掉路径前缀）
    raw_cmd = parts[0].lower()
    cmd_name = raw_cmd.replace('\\', '/').split('/')[-1]

    if cmd_name in BLOCK_COMMANDS:
        logger.warning(f"Blocked command: {command}")
        return 'block'

    if cmd_name in CONFIRM_COMMANDS:
        return 'confirm'

    return 'safe'


def split_command(command: str) -> list[str]:
    """安全地将命令字符串拆分为 argv 数组

    供 shell=False 路径使用。如果拆分失败返回空列表。
    """
    try:
        return shlex.split(command)
    except ValueError as e:
        logger.warning(f"shlex.split failed for command '{command}': {e}")
        return []


def _ensure_no_window(kwargs: dict) -> dict:
    """Windows 上自动添加 CREATE_NO_WINDOW 标志，避免弹出 cmd.exe 黑框

    如果调用方已传 creationflags，则尊重调用方设置不覆盖。
    """
    if sys.platform == "win32" and "creationflags" not in kwargs:
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kwargs


def run_safe(command: str, **kwargs) -> "subprocess.Popen":
    """Path A: 使用 shell=False 执行安全命令

    参数与 subprocess.Popen 一致。
    返回 Popen 对象，调用方负责 communicate/wait。
    """
    args = split_command(command)
    if not args:
        raise ValueError(f"Cannot split command: {command}")
    return subprocess.Popen(
        args,
        shell=False,
        **_ensure_no_window(kwargs),
    )


def run_with_shell(command: str, **kwargs) -> "subprocess.Popen":
    """Path B: 使用 shell=True 执行复杂命令（需外部审批保障）

    此路径仅在命令包含 shell 元字符时使用。
    调用方必须确保命令已通过用户审批。
    """
    return subprocess.Popen(
        command,
        shell=True,
        **_ensure_no_window(kwargs),
    )
