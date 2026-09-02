# -*- coding: utf-8 -*-
"""
系统工具插件 — 终端与后台任务（bash/bg_* 完整实现）

从主程序 app/tools/terminal_tools.py 整体迁移（工具插件化）：
- BackgroundTask / BackgroundTaskManager / TerminalTools 类（命令执行/后台任务管理）
- 命令安全拦截（command_safety）、进程树（process_job）为主程序共享基础设施
- 输出压缩走同目录 _shell_compressor.py
- 单例模式保留（跨窗口共享），workdir 由 tool_ctx 注入

依赖（主程序共享基础设施，保留在 app/tools/）：
  app.tools.command_safety — 命令安全分类/执行
  app.tools.process_job    — Windows Job Object 进程树
"""
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

from app.tools.command_safety import classify_command, needs_shell, run_safe, run_with_shell
from app.tools.bg_manager import (
    BackgroundTaskManager,
    _prepare_windows_encoding,
    _smart_decode,
)
from app.tools.result import ToolResult

# ============================================================
# bash 输出压缩模块（同目录 _shell_compressor.py，下划线前缀 → loader 跳过）
# ============================================================
_shell_compressor_module = None


def _get_shell_compressor():
    """加载 bash 输出压缩函数（进程级缓存一次；缺失回退原样输出）"""
    global _shell_compressor_module
    if _shell_compressor_module is not None:
        return _shell_compressor_module
    import importlib.util

    plugin_path = Path(__file__).resolve().parent / "_shell_compressor.py"
    if not plugin_path.exists():
        import loguru

        loguru.logger.warning(f"[TerminalTools] 压缩模块缺失: {plugin_path}")
        return lambda command, output: output
    spec = importlib.util.spec_from_file_location("_plugin_shell_compressor", plugin_path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
        _shell_compressor_module = mod.compress
        return _shell_compressor_module
    except Exception as e:
        import loguru

        loguru.logger.warning(f"[TerminalTools] 压缩模块加载失败: {e}")
        return lambda command, output: output


# ============================================================
# 共享 description 参数约定（同目录 _tool_desc.py，下划线前缀 → loader 跳过）
# ============================================================
_tool_desc_module = None


def _tool_desc_loader():
    """加载共享的 description 参数约定（进程级缓存一次；失败返回 None，调用方走原预览）"""
    global _tool_desc_module
    if _tool_desc_module is not None:
        return _tool_desc_module
    import importlib.util

    plugin_path = Path(__file__).resolve().parent / "_tool_desc.py"
    if not plugin_path.exists():
        _tool_desc_module = False
        return _tool_desc_module
    spec = importlib.util.spec_from_file_location("_plugin_tool_desc", plugin_path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
        _tool_desc_module = mod
    except Exception:
        _tool_desc_module = False
    return _tool_desc_module


def _desc_param(example: str) -> dict:
    """生成 description 参数的 schema 片段（模块缺失时返回空 dict，schema 退化为原样）"""
    mod = _tool_desc_loader()
    if not mod:
        return {}
    return mod.description_param(example)


def _desc_preview(preview_fn):
    """包装 preview 闭包：优先展示大模型填写的 description（模块缺失时原样返回）"""
    mod = _tool_desc_loader()
    if not mod:
        return preview_fn
    return mod.prefer_description(preview_fn)


from loguru import logger

# ── findstr 管道符修复 ────────────────────────────────────────────────
# Windows cmd.exe 即使在双引号内也会把 | 当管道解析，导致
# findstr /n "pattern1|pattern2" 静默失败。自动转换为 /c: 语法。


def _fix_findstr_pipe(command: str) -> str:
    """将 findstr 正则中的 | 转换为 /c: 语法，避免被 cmd.exe 当管道。

    findstr /n "mousePressEvent|mouseReleaseEvent" file
    → findstr /n /c:"mousePressEvent" /c:"mouseReleaseEvent" file

    注意：re.search 而非 re.match，以支持管道和复合命令中的 findstr。
    正则支持 \" 转义引号，避免遇到 \"elapsed\" 时提前截断。
    """
    if sys.platform != "win32":
        return command

    # 如果命令已包含 /c: 语法，跳过避免双重转换
    if "/c:" in command:
        return command

    # 匹配 findstr [flags] "pattern" — 支持 \" 转义引号
    FINDSTR_RE = re.compile(r'(findstr\s+(?:\S+\s+)*)"((?:[^"\\]|\\.)*)"', re.IGNORECASE)
    m = FINDSTR_RE.search(command)
    if not m:
        return command

    prefix = command[: m.start(1)] + m.group(1)  # 含 findstr 标志
    pattern = m.group(2)                          # "error|warning"
    suffix = command[m.end():]                    # 剩余部分

    if "|" not in pattern:
        return command

    parts = pattern.split("|")
    # cmd.exe 中双引号内 " 需要写成 "" ，重建 /c:"..." 时做转义
    def _cmd_escape(s: str) -> str:
        return s.replace('"', '""')

    rebuilt = prefix.rstrip() + " " + " ".join(f'/c:"{_cmd_escape(p)}"' for p in parts) + suffix
    logger.debug(f"[Bash] findstr pipe fix: {command[:80]}... → {rebuilt[:80]}...")
    return rebuilt


# ── 内联脚本自动转临时文件 ──────────────────────────────────────────────
# Windows cmd 无法可靠处理多行/嵌套引号的 python -c "..." 等内联脚本，
# 自动将其写入临时文件再执行，对所有解释器通用。

_INTERPRETERS = frozenset({"python", "python3", "node", "ruby", "perl", "php"})
_SCRIPT_FLAGS = frozenset({"-c", "-e"})
_SCRIPT_EXT = {
    "python": ".py",
    "python3": ".py",
    "node": ".js",
    "ruby": ".rb",
    "perl": ".pl",
    "php": ".php",
}


def _parse_inline_script(command: str) -> Optional[dict]:
    """
    解析内联脚本命令，返回 {interpreter, flag, script, rest}。

    逐字符扫描寻找匹配的引号，正确处理 \" 转义。
    仅当命令格式为：解释器 -c/-e "脚本内容" [剩余参数] 时匹配。
    """
    cmd = command.strip()
    # 1. 提取解释器
    parts = cmd.split(None, 2)
    if len(parts) < 3:
        return None
    interpreter, flag, rest = parts[0], parts[1], parts[2]
    if interpreter not in _INTERPRETERS or flag not in _SCRIPT_FLAGS:
        return None

    # 2. 找到第一个引号
    quote_char = None
    script_start = -1
    for i, ch in enumerate(rest):
        if ch in ('"', "'"):
            quote_char = ch
            script_start = i + 1
            break
    if quote_char is None or script_start >= len(rest):
        return None

    # 3. 逐字符扫描找匹配的结束引号（处理 \" 转义）
    script_end = -1
    i = script_start
    while i < len(rest):
        ch = rest[i]
        if ch == "\\":
            i += 2  # 跳过转义序列
            continue
        if ch == quote_char:
            script_end = i
            break
        i += 1

    if script_end == -1:
        return None  # 没有匹配的结束引号

    script = rest[script_start:script_end]
    rest_after = rest[script_end + 1 :].strip()

    return {
        "interpreter": interpreter,
        "flag": flag,
        "script": script,
        "rest": rest_after,
        "outer_quote": quote_char,
    }


def _rewrite_inline_script(command: str) -> tuple[str, Optional[str]]:
    """
    检测内联脚本命令，有多行/引号嵌套时自动写入临时文件。

    支持两种场景：
    1. 命令以解释器开头: python -c "多行脚本"
    2. 链式命令中包含: cd xxx && python -c "多行脚本"

    Args:
        command: 原始命令

    Returns:
        (最终命令, 临时文件路径) — 无需改写则后者为 None
    """
    parsed = _parse_inline_script(command)
    if not parsed:
        # 命令不是以解释器开头，尝试扫描整个命令找内联脚本
        command, tmp = _scan_and_rewrite_chain(command)
        return command, tmp

    script = parsed["script"]

    # 只有脚本含换行才改写为临时文件。
    # 简单的引号嵌套（has_same_quote）无需改写——Path A (shell=False)
    # 通过 shlex.split 已能正确处理，走 rewrite 反而引入转义问题。
    has_newline = "\n" in script
    if not has_newline:
        return command, None

    # 反转义 shell 转义序列：\\ → \ , \" → "
    # 原始脚本中的 \" 是为 shell 准备的转义，Python 源码文件不认
    script = script.replace("\\\\", "\\").replace('\\"', '"')

    ext = _SCRIPT_EXT.get(parsed["interpreter"], ".py")
    fd, tmp_path = tempfile.mkstemp(suffix=ext, prefix="drifox_inline_", text=True)
    try:
        os.write(fd, script.encode("utf-8"))
    finally:
        os.close(fd)

    # 使用正斜杠避免 Windows 路径转义问题
    # 路径加引号：避免临时目录含空格时 shlex.split 切碎路径 → FileNotFoundError
    safe_path = tmp_path.replace("\\", "/")
    new_cmd = f'{parsed["interpreter"]} "{safe_path}"'
    if parsed["rest"]:
        new_cmd += f" {parsed['rest']}"
    return new_cmd, tmp_path


def _scan_and_rewrite_chain(command: str) -> tuple[str, Optional[str]]:
    """扫描链式命令中的内联脚本并改写为临时文件。

    例如: cd xxx && python -c "多行脚本" → cd xxx && python "temp.py"
    """
    if sys.platform != "win32":
        return command, None

    # 构建解释器+标志的搜索模式: python -c, node -e, 等等
    interp_pattern = "|".join(_INTERPRETERS)
    flag_pattern = "|".join(_SCRIPT_FLAGS)
    # 找到命令中任意位置的 解释器 标志 组合
    pattern = re.compile(rf"\b({interp_pattern})\s+({flag_pattern})\s+", re.IGNORECASE)

    result_cmd = command
    any_rewritten = False
    tmp_files = []

    # 从后往前替换，避免偏移问题
    matches = list(pattern.finditer(result_cmd))
    for m in reversed(matches):
        interp = m.group(1)
        start = m.end()  # 引号开始位置

        # 找引号内的脚本
        rest = result_cmd[start:]
        quote_char = None
        script_start = -1
        for i, ch in enumerate(rest):
            if ch in ('"', "'"):
                quote_char = ch
                script_start = i + 1
                break
        if quote_char is None or script_start >= len(rest):
            continue

        # 找匹配的结束引号
        script_end = -1
        i = script_start
        while i < len(rest):
            ch = rest[i]
            if ch == "\\":
                i += 2
                continue
            if ch == quote_char:
                script_end = i
                break
            i += 1

        if script_end == -1:
            continue

        script = rest[script_start:script_end]
        if "\n" not in script:
            continue  # 无换行，不需要改写

        # 改写为临时文件
        script = script.replace("\\\\", "\\").replace('\\"', '"')
        ext = _SCRIPT_EXT.get(interp.lower(), ".py")
        fd, tmp_path = tempfile.mkstemp(suffix=ext, prefix="drifox_inline_", text=True)
        try:
            os.write(fd, script.encode("utf-8"))
        finally:
            os.close(fd)
        tmp_files.append(tmp_path)

        safe_path = tmp_path.replace("\\", "/")
        # 替换原文中的内联脚本部分（去掉 -c/-e 标志，直接用脚本文件）
        # m.start() = "python" 的起始位置，m.end() = "-c " 之后的引号前位置
        before = result_cmd[: m.start()]  # "cd xxx && " 部分
        after = result_cmd[start + script_end + 1:]  # 跳过结束引号
        result_cmd = f'{before}{interp} "{safe_path}"{after}'
        any_rewritten = True
        logger.debug(f"[Bash] chain inline script → temp: {tmp_path}")

    if any_rewritten:
        return result_cmd, tmp_files[0] if len(tmp_files) == 1 else tmp_files[0]
    return command, None


def _cleanup_script_temp(path: Optional[str]) -> None:
    """安全删除临时脚本文件"""
    if path:
        try:
            os.unlink(path)
        except Exception:
            pass

class TerminalTools:
    def __init__(self, owner):
        self._owner = owner
        # 注册动态获取 workdir 的回调给 BackgroundTaskManager
        BackgroundTaskManager(lambda: self.workdir)

    def cleanup(self):
        """窗口关闭时解除 BackgroundTaskManager 单例对 workdir getter 的持有（泄漏修复 6c）。

        BackgroundTaskManager 是全局单例，__init__ 注册的 lambda 捕获 self
        （TerminalTools），单例强引用它 → 窗口对象树无法回收。窗口关闭链
        （backend.cleanup → tool_executor.cleanup）调用本方法后，getter 置空，
        _effective_workdir 回退静态缓存，功能不中断。
        """
        BackgroundTaskManager.clear_workdir_getter()

    @property
    def workdir(self) -> Path:
        return self._owner.workdir

    def execute_bash(self, command: str, timeout: int = 120) -> ToolResult:
        """执行 shell 命令，支持可靠的 timeout

        使用 communicate(timeout) 避免管道死锁，同时在超时时杀死进程。

        安全说明:
          - Path A (shell=False): 无 shell 元字符的命令，用 argv 数组直接执行
          - Path B (shell=True):  含管道/重定向的命令，保留 shell 解释

        编码说明:
          - Windows 原生命令 (dir, type 等): 输出 GBK
          - Git/npm 等现代工具: 输出 UTF-8
          - 使用智能解码自动选择正确编码
        """
        try:
            tmp_script = None

            # 安全分类
            classification = classify_command(command)
            if classification == "block":
                return ToolResult(False, error=f"命令被安全策略拦截: {command}")

            # ── 内联脚本自动转临时文件（Windows cmd 无法可靠处理多行/嵌套引号）──
            command, tmp_script = _rewrite_inline_script(command)

            # ── findstr 管道符修复（cmd.exe 把引号内 | 当管道）──
            command = _fix_findstr_pipe(command)

            use_shell = needs_shell(command)

            if use_shell:
                # Path B: 需要 shell 特性（管道、重定向等）
                cmd = _prepare_windows_encoding(command, self.workdir)
                process = run_with_shell(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=str(self.workdir),
                )
            else:
                # Path A: 安全路径 — 无 shell 注入风险
                process = run_safe(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=str(self.workdir),
                )

            start_time = time.time()

            # 使用子线程执行 communicate，避免阻塞主线程
            result_holder = {"stdout": None, "stderr": None, "error": None}

            def communicate_in_thread():
                """在子线程中执行 communicate，同时读取 stdout 和 stderr"""
                try:
                    stdout, stderr = process.communicate()
                    result_holder["stdout"] = stdout
                    result_holder["stderr"] = stderr
                except Exception as e:
                    result_holder["error"] = str(e)

            comm_thread = threading.Thread(target=communicate_in_thread, daemon=True)
            comm_thread.start()

            # 等待线程完成或超时
            comm_thread.join(timeout=timeout)

            if comm_thread.is_alive():
                # 超时：杀死进程
                try:
                    process.kill()
                    # 等待线程结束（进程被杀后 communicate 会立即返回）
                    comm_thread.join(timeout=5)
                except Exception:
                    pass

                elapsed = time.time() - start_time
                return ToolResult(False, error=f"Command timeout after {elapsed:.1f}s (killed)")

            # 检查是否有错误
            if result_holder["error"]:
                return ToolResult(False, error=f"Execution error: {result_holder['error']}")

            # 进程正常完成：使用智能解码
            stdout_bytes = result_holder["stdout"] or b""
            stderr_bytes = result_holder["stderr"] or b""

            # 智能解码（自动选择 UTF-8 或 GBK）
            stdout = _smart_decode(stdout_bytes, command).strip()
            stderr = _smart_decode(stderr_bytes, command).strip()

            combined = "\n".join(filter(None, [stdout, stderr]))

            # 检查进程退出码
            if process.returncode != 0:
                detail = combined if combined else "(no output)"
                return ToolResult(
                    False,
                    error=f"Command exited with code {process.returncode}:\n{detail}",
                )

            # Shell 输出压缩（减少 token 消耗）— 压缩模块随工具插件走
            # （plugins/system/tools/_shell_compressor.py，bash 工具实现的一部分）
            compress = _get_shell_compressor()
            compressed = compress(command, combined if combined else "(command completed with no output)")

            return ToolResult(True, content=compressed)

        except Exception as e:
            return ToolResult(False, error=f"Execution error: {str(e)}")
        finally:
            _cleanup_script_temp(tmp_script)

    def bg_start(self, command: str, cwd: str = None) -> ToolResult:
        """启动后台命令

        参数:
            command: 要执行的 shell 命令
            cwd: 工作目录（可选）
        返回:
            ToolResult
        """
        manager = BackgroundTaskManager()
        task_id, message = manager.start(command, cwd)
        return ToolResult(True, content=message)

    def bg_stop(self, task_id: str) -> ToolResult:
        """停止后台任务

        参数:
            task_id: 任务 ID
        返回:
            ToolResult
        """
        manager = BackgroundTaskManager()
        success, message = manager.stop(task_id)
        return ToolResult(success, content=message)

    def bg_logs(self, task_id: str, lines: int = 100) -> ToolResult:
        """获取后台任务日志

        参数:
            task_id: 任务 ID
            lines: 返回最近 N 行（默认 100）
        返回:
            ToolResult
        """
        manager = BackgroundTaskManager()
        content = manager.get_logs(task_id, lines)
        return ToolResult(True, content=content)

    def bg_list(self) -> ToolResult:
        """列出所有后台任务

        返回:
            ToolResult
        """
        manager = BackgroundTaskManager()
        content = manager.list_tasks()
        return ToolResult(True, content=content)

# ============================================================
# 工具插件化：owner 包装 / 单例 / impl / register
# ============================================================

class _OwnerShim:
    """TerminalTools 的 owner 最小实现（仅 workdir，由 tool_ctx 注入）"""

    def __init__(self, workdir):
        self.workdir = Path(workdir)


_terminal_instance = None


def _get_terminal(workdir):
    """获取 TerminalTools 单例并更新当前 workdir（跨窗口安全）"""
    global _terminal_instance
    if _terminal_instance is None:
        _terminal_instance = TerminalTools(_OwnerShim(workdir))
    else:
        _terminal_instance._owner.workdir = Path(workdir)
    return _terminal_instance


def _bash_impl(tool_ctx, **kwargs):
    terminal = _get_terminal(tool_ctx.get("workdir") or Path.cwd())
    return terminal.execute_bash(kwargs.get("command", ""), kwargs.get("timeout", 120))


def _bg_start_impl(tool_ctx, **kwargs):
    terminal = _get_terminal(tool_ctx.get("workdir") or Path.cwd())
    return terminal.bg_start(kwargs.get("command", ""), kwargs.get("cwd"))


def _bg_stop_impl(tool_ctx, **kwargs):
    terminal = _get_terminal(tool_ctx.get("workdir") or Path.cwd())
    return terminal.bg_stop(kwargs.get("task_id", ""))


def _bg_logs_impl(tool_ctx, **kwargs):
    terminal = _get_terminal(tool_ctx.get("workdir") or Path.cwd())
    return terminal.bg_logs(kwargs.get("task_id", ""), kwargs.get("lines", 100))


def _bg_list_impl(tool_ctx, **kwargs):
    terminal = _get_terminal(tool_ctx.get("workdir") or Path.cwd())
    return terminal.bg_list()


GROUP_TERMINAL = "终端与后台"

_BASH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "bash",
        "description": "执行shell命令。仅内置工具不够用时用：构建(pytest/ruff/build)、git(status/diff/log/add/commit)、进程(ps/kill/lsof)、管道(cat|grep|awk)、环境探测(which/env)。禁止替代: read/write/edit/multi_edit/list/glob/grep/get_diagnostics/lsp/bg_*/screenshot/mouse/keyboard/websearch/webfetch。调用前自检：有专用工具？有则用它。",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "命令"},
                "timeout": {"type": "integer", "description": "超时秒数"},
                **_desc_param("运行全量单元测试"),
            },
            "required": ["command", "description"],
        },
    },
}

_BG_START_SCHEMA = {
    "type": "function",
    "function": {
        "name": "bg_start",
        "description": "后台启动命令，不阻塞对话。用于持续服务(如开发服务器)。",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的命令"},
                "cwd": {"type": "string", "description": "工作目录（可选，默认为项目根目录）"},
                **_desc_param("启动前端开发服务器"),
            },
            "required": ["command", "description"],
        },
    },
}

_BG_STOP_SCHEMA = {
    "type": "function",
    "function": {
        "name": "bg_stop",
        "description": "停止后台任务",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "任务ID，格式bg_xxxxxxxx"},
            },
            "required": ["task_id"],
        },
    },
}

_BG_LOGS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "bg_logs",
        "description": "获取后台任务日志",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "任务 ID"},
                "lines": {"type": "integer", "description": "返回最近N行(默认100)"},
            },
            "required": ["task_id"],
        },
    },
}

_BG_LIST_SCHEMA = {
    "type": "function",
    "function": {
        "name": "bg_list",
        "description": "列出所有后台任务状态",
        "parameters": {"type": "object", "properties": {}},
    },
}


def _render_bash_body(result, tool_name, tool_args, success):
    """bash 完成框渲染闭包：终端风格（命令头 + 输出体，从主程序 render_helpers 迁出）"""
    from app.widgets.render_helpers import (
        _get_global_font,
        escape,
        scale_font_size,
    )

    _gf = _get_global_font()
    raw = getattr(result, "content", "") or ""
    tool_args = tool_args or {}
    cmd = tool_args.get("command", "")
    cmd_display = escape(cmd[:120]) if cmd else "(no command)"
    return f"""
    <div class="terminal-block" style="background:rgba(13,17,23,0.40);border:1px solid rgba(48,54,61,0.25);border-radius:8px;overflow:hidden;margin:0;">
        <div style="padding:6px 12px;background:rgba(22,27,34,0.40);border-bottom:1px solid rgba(48,54,61,0.25);color:#8b949e;font-family:'{_gf}',Consolas,monospace;font-size:{scale_font_size(12)}px;">
            $ <span style="color:#c9d1d9;">{cmd_display}</span>
        </div>
        <pre style="margin:0;padding:10px 12px;background:rgba(13,17,23,0.40);color:#c9d1d9;font-family:'{_gf}',Consolas,monospace;font-size:{scale_font_size(13)}px;line-height:1.5;white-space:pre-wrap;word-break:break-all;overflow-x:auto;">{escape(raw)}</pre>
    </div>"""


def _render_bg_body(result, tool_name, tool_args, success):
    """bg_start/bg_stop/bg_logs 完成框渲染闭包：终端风格（任务标识头 + 状态体）"""
    from app.widgets.render_helpers import (
        _get_global_font,
        escape,
        scale_font_size,
    )

    _gf = _get_global_font()
    raw = getattr(result, "content", "") or ""
    tool_args = tool_args or {}
    if tool_name == "bg_start":
        cmd = tool_args.get("command", "")
        header = f"bg_start command={escape(cmd[:120])}" if cmd else "bg_start"
    elif tool_name == "bg_stop":
        task_id = tool_args.get("task_id", "")
        header = f"bg_stop task_id={task_id}" if task_id else "bg_stop"
    elif tool_name == "bg_logs":
        task_id = tool_args.get("task_id", "")
        lines = tool_args.get("lines", 100)
        header = f"bg_logs task_id={task_id} lines={lines}"
    else:
        header = tool_name
    return f"""
    <div class="terminal-block" style="background:rgba(13,17,23,0.40);border:1px solid rgba(48,54,61,0.25);border-radius:8px;overflow:hidden;margin:0;">
        <div style="padding:6px 12px;background:rgba(22,27,34,0.40);border-bottom:1px solid rgba(48,54,61,0.25);color:#8b949e;font-family:'{_gf}',Consolas,monospace;font-size:{scale_font_size(12)}px;">
            $ <span style="color:#c9d1d9;">{escape(header)}</span>
        </div>
        <pre style="margin:0;padding:10px 12px;background:rgba(13,17,23,0.40);color:#c9d1d9;font-family:'{_gf}',Consolas,monospace;font-size:{scale_font_size(13)}px;line-height:1.5;white-space:pre-wrap;word-break:break-all;overflow-x:auto;">{escape(raw)}</pre>
    </div>"""


def _preview_bash(tool_args: dict) -> str:
    """bash 预览：命令片段兜底（有 description 时由 _desc_preview 优先接管）"""
    tool_args = tool_args or {}
    cmd = tool_args.get("command", "")
    return f'执行 "{cmd[:60]}"' if cmd else "执行命令"


def _make_bg_preview(tool_name: str):
    """生成 bg_* 工具的自然语言预览闭包（绑定工具名，避免模块级状态）"""

    def _preview(tool_args: dict) -> str:
        tool_args = tool_args or {}
        if tool_name == "bg_start":
            cmd = tool_args.get("command", "")
            return f'后台启动 "{cmd[:40]}"' if cmd else "后台启动"
        if tool_name == "bg_stop":
            task_id = tool_args.get("task_id", "")
            return f"停止后台任务 {task_id}" if task_id else "停止后台任务"
        if tool_name == "bg_logs":
            task_id = tool_args.get("task_id", "")
            lines = tool_args.get("lines", 100)
            return f"后台日志 {task_id} (前 {lines} 行)" if task_id else "后台日志"
        return "后台任务列表"

    return _preview


def _summarize_bash(tool_name, tool_args, tool_content):
    """bash 压缩摘要：命令 + exit code + 输出行数（从 history_compactor 迁出）"""
    import re as _re

    args = tool_args or {}
    content = tool_content or ""
    cmd = args.get("command", "")
    if len(cmd) > 80:
        cmd = cmd[:77] + "..."
    exit_match = _re.search(r'"exit_code"\s*:\s*(-?\d+)', content)
    exit_code = exit_match.group(1) if exit_match else "?"
    line_count = content.count("\n") + 1 if content.strip() else 0
    # 有自然语言描述时以描述为主，命令作为事实留档（压缩后仍需知道到底跑了什么）
    _desc_mod = _tool_desc_loader()
    desc = _desc_mod.get_description(args) if _desc_mod else ""
    label = f"{desc} (`{cmd}`)" if desc and cmd else (desc or f"`{cmd}`")
    return f"[{tool_name}] ran {label} -> exit {exit_code}, {line_count} lines output"


def _make_bg_summarize(preview_fn):
    """bg_* 通用压缩摘要：预览 + 内容长度"""

    def _summarize(tool_name, tool_args, tool_content):
        label = preview_fn(tool_args or {}) if preview_fn else ""
        content_len = len(tool_content or "")
        return f"[{tool_name}] {label} ({content_len:,} chars)"

    return _summarize


def register(registry):
    # bg_start 的预览/摘要都走带 description 优先的版本
    _bg_start_preview = _desc_preview(_make_bg_preview("bg_start"))
    registry.register(
        "bash", _BASH_SCHEMA, impl=_bash_impl,
        danger="dangerous", icon="shell", cn_name="执行命令",
        group=GROUP_TERMINAL, description="执行shell命令",
        aliases=["Bash", "Terminal", "RunCommand", "execute_command", "shell", "Command"],
        render=_render_bash_body,
        preview=_desc_preview(_preview_bash),
        summarize=_summarize_bash,
        metadata={"permission_arg": "command"},
    )
    registry.register(
        "bg_start", _BG_START_SCHEMA, impl=_bg_start_impl,
        danger="dangerous", icon="shell", cn_name="后台启动",
        group=GROUP_TERMINAL, description="启动后台命令",
        aliases=["BgStart", "bg_start"],
        render=_render_bg_body,
        preview=_bg_start_preview,
        summarize=_make_bg_summarize(_bg_start_preview),
    )
    registry.register(
        "bg_stop", _BG_STOP_SCHEMA, impl=_bg_stop_impl,
        danger="dangerous", icon="shell", cn_name="后台停止",
        group=GROUP_TERMINAL, description="停止后台任务",
        aliases=["BgStop", "bg_stop"],
        render=_render_bg_body,
        preview=_make_bg_preview("bg_stop"),
        summarize=_make_bg_summarize(_make_bg_preview("bg_stop")),
    )
    registry.register(
        "bg_logs", _BG_LOGS_SCHEMA, impl=_bg_logs_impl,
        danger="safe", icon="shell", cn_name="后台日志",
        group=GROUP_TERMINAL, description="查看后台任务日志",
        aliases=["BgLogs", "bg_logs"],
        render=_render_bg_body,
        preview=_make_bg_preview("bg_logs"),
        summarize=_make_bg_summarize(_make_bg_preview("bg_logs")),
    )
    registry.register(
        "bg_list", _BG_LIST_SCHEMA, impl=_bg_list_impl,
        danger="safe", icon="shell", cn_name="后台列表",
        group=GROUP_TERMINAL, description="列出后台任务状态",
        aliases=["BgList", "bg_list"],
        preview=_make_bg_preview("bg_list"),
        summarize=_make_bg_summarize(_make_bg_preview("bg_list")),
    )