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
import uuid
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from app.tools.command_safety import _extract_cmd_name, classify_command, needs_shell, run_safe, run_with_shell
from app.tools.process_job import ProcessJob
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
        flag = m.group(2)
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


def _smart_decode(data: bytes, command: str = "") -> str:
    """
    智能解码：根据命令类型选择正确的编码

    Windows 平台编码规则:
    - Git/npm/Node 等现代工具: 输出 UTF-8
    - Windows 原生命令 (dir, type 等): 输出 GBK/CP936
    - 带管道的命令: 可能是混合编码，用 errors='replace' 容错

    Args:
        data: 原始字节数据
        command: 原始命令（用于判断命令类型）

    Returns:
        解码后的字符串
    """
    if not data:
        return ""

    # 常见现代工具（输出 UTF-8）
    UTF8_TOOLS = frozenset(
        {
            "git", "npm", "yarn", "pnpm", "node", "deno", "bun", "python",
            "python3", "pip", "uv", "cargo", "rustc", "go", "java", "javac",
            "mvn", "gradle", "docker", "kubectl", "helm", "terraform", "curl",
            "wget", "gh", "aws", "gcloud", "az", "ruby", "gem", "php", "composer",
            "lua", "perl", "R", "julia", "ruff", "mypy", "pytest", "eslint", "tsc",
            "flutter", "dart", "swift", "make", "cmake", "ninja", "meson", "npx",
            "pip3", "pipx",
        }
    )

    # 使用与 command_safety 一致的命令名提取逻辑，
    # 可处理路径前缀（如 C:\\Python\\python.exe）和裸命令（如 python）
    cmd_name = _extract_cmd_name(command)
    is_utf8_tool = cmd_name is not None and cmd_name in UTF8_TOOLS

    # 如果命令明确是 UTF-8 工具，优先尝试 UTF-8
    if is_utf8_tool:
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            # UTF-8 失败，降级到 GBK
            return data.decode("gbk", errors="replace")

    # 对于其他命令，优先尝试 UTF-8（现代工具越来越多）
    try:
        decoded = data.decode("utf-8")
        # 检查是否包含常见乱码特征（GBK 当作 UTF-8 解码时）
        # 如果结果包含大量不可打印字符，可能是 GBK 被误判为 UTF-8
        printable_ratio = sum(c.isprintable() or c.isspace() for c in decoded) / max(len(decoded), 1)
        if printable_ratio > 0.8:
            return decoded
    except UnicodeDecodeError:
        pass

    # 回退到 GBK
    return data.decode("gbk", errors="replace")


@dataclass
class BackgroundTask:
    """后台任务对象"""

    task_id: str
    command: str
    process: subprocess.Popen
    start_time: float
    output_buffer: list = field(default_factory=list)
    status: str = "running"  # running, stopped, completed
    pid: int = 0
    job: object = None  # S4: Windows Job Object（kill-on-close 杀树），非 Windows 为 None

    def append_output(self, text: str):
        """追加输出到缓冲区"""
        self.output_buffer.append(text)
        # 限制缓冲区大小，最多保留 10000 行
        if len(self.output_buffer) > 10000:
            self.output_buffer = self.output_buffer[-5000:]


def _prepare_windows_encoding(command: str, workdir: Optional[Path] = None) -> str:
    """Windows 上设置 UTF-8 编码前缀（仅当需要 shell=True 路径时使用）

    通过 chcp 65001 将 cmd.exe 代码页切换为 UTF-8，确保内置命令（dir/type 等）
    输出 UTF-8 而非 GBK。

    如果项目有 .venv，将其 Scripts 目录加入 PATH 前缀，解决 Windows App
    Execution Alias（WindowsApps 下的 python.exe）在 cmd.exe 中无法解析的问题。

    注意：只重定向 stdout 到 NUL，保留 stderr。如果 chcp 失败（如无控制台），
    stderr 可被捕获，且 execute_bash 会检查 returncode 报告错误。
    """
    if sys.platform != "win32":
        return command

    prefix = "chcp 65001 >nul"

    # 将项目 .venv/Scripts 加入 PATH 前缀，确保 python/pip 等在 cmd.exe 中可解析
    if workdir:
        venv_scripts = workdir / ".venv" / "Scripts"
        if venv_scripts.is_dir():
            vs = str(venv_scripts).replace("\\", "/")
            prefix += f' && set "PATH={vs};%PATH%"'

    return f"{prefix} && {command}"


class BackgroundTaskManager:
    """全局后台任务管理器（单例）"""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls, owner_getter: Callable = None):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._tasks = {}
                    cls._instance._manager_lock = threading.Lock()
                    cls._instance._workdir = Path.cwd()
                    cls._instance._get_workdir = None
                    # S4: 任务状态事件广播回调（UI 可观测）。
                    # 回调签名：cb(event: str, task_id: str, status: str, detail: str)
                    # event ∈ {"started", "stopped", "completed"}。回调应轻量（可能
                    # 在后台线程触发），线程安全由调用方保证。
                    cls._instance._event_callbacks = []
        if owner_getter:
            # 泄漏修复（6c）：保持强引用（lambda 无其它持有者，弱引用会立即失效），
            # 由窗口关闭路径显式调用 clear_workdir_getter() 解除，避免单例
            # 永久持有最后窗口的 getter → 窗口对象树无法回收。
            cls._instance._get_workdir = owner_getter
        return cls._instance

    @classmethod
    def reset_instance(cls):
        """重置单例（仅用于测试）"""
        cls._instance = None

    @classmethod
    def clear_workdir_getter(cls):
        """解除单例对最后窗口 workdir getter 的持有（泄漏修复 6c）。

        窗口关闭时由 TerminalTools.cleanup() 调用：置 None 后 _effective_workdir
        回退静态缓存 _workdir（由 BuiltinTools.set_workdir 随项目切换更新），
        功能不中断，且已关闭窗口的 getter 闭包不再被单例强引用。
        """
        if cls._instance is not None:
            cls._instance._get_workdir = None

    def set_workdir(self, workdir: Path):
        """设置工作目录"""
        self._workdir = workdir

    # ========== S4: 任务状态事件广播 ==========

    def on_task_event(self, callback: Callable) -> None:
        """注册任务状态事件回调（started/stopped/completed）。

        回调签名：callback(event: str, task_id: str, status: str, detail: str)
        """
        with self._manager_lock:
            self._event_callbacks.append(callback)

    def _emit(self, event: str, task_id: str, status: str, detail: str = "") -> None:
        """广播任务事件（同步调用回调，异常隔离不抛出）"""
        with self._manager_lock:
            callbacks = list(self._event_callbacks)
        for cb in callbacks:
            try:
                cb(event, task_id, status, detail)
            except Exception as e:
                logger.warning(f"[BackgroundTaskManager] event callback error: {e}")

    def _effective_workdir(self) -> Path:
        """获取当前有效工作目录（优先动态获取，其次静态缓存）"""
        if self._get_workdir:
            try:
                return self._get_workdir()
            except Exception:
                pass
        return self._workdir

    def start(self, command: str, cwd: str = None) -> tuple[str, str]:
        """启动后台任务，返回 (task_id, message)"""
        task_id = f"bg_{uuid.uuid4().hex[:8]}"
        workdir = Path(cwd) if cwd else self._effective_workdir()

        classification = classify_command(command)
        if classification == "block":
            return task_id, f"❌ 命令被安全策略拦截: {command}"

        try:
            use_shell = needs_shell(command)
            # S4: 创建 Job Object（Windows）— kill-on-close 兜底杀进程树；
            # assign 失败（进程已在其它 Job）降级为 stop 时的 taskkill/terminate 路径。
            job = ProcessJob() if ProcessJob.is_supported() else None

            if use_shell:
                # Path B: 需要 shell 特性 — 使用 shell=True（后台任务暂不强制审批）
                cmd = _prepare_windows_encoding(command, workdir)
                process = run_with_shell(
                    cmd,
                    job=job,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.PIPE,
                    cwd=str(workdir),
                )
            else:
                # Path A: 安全路径 — shell=False
                process = run_safe(
                    command,
                    job=job,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.PIPE,
                    cwd=str(workdir),
                )

            task = BackgroundTask(
                task_id=task_id,
                command=command,
                process=process,
                start_time=time.time(),
                pid=process.pid,
                job=job,
            )

            with self._manager_lock:
                self._tasks[task_id] = task

            # 启动输出捕获线程
            thread = threading.Thread(target=self._capture_output, args=(task,), daemon=True)
            thread.start()

            self._emit("started", task_id, "running", command)
            return task_id, f"✅ 后台任务已启动\n- 任务ID: {task_id}\n- PID: {process.pid}\n- 命令: {command}"

        except Exception as e:
            return task_id, f"❌ 启动失败: {str(e)}"

    def _capture_output(self, task: BackgroundTask):
        """捕获进程输出（智能解码）"""
        try:
            if task.process.stdout:
                for raw_line in iter(task.process.stdout.readline, b""):
                    if raw_line:
                        # 智能解码每一行
                        line = _smart_decode(raw_line, task.command)
                        task.append_output(line.rstrip("\n"))
                    if task.status != "running":
                        break
        except Exception:
            pass
        finally:
            # 进程结束后更新状态：仅当任务仍为 running 时才置 completed。
            # stop() 已把状态置为 stopped（并广播 stopped 事件），此处不得
            # 覆盖为 completed，否则「已停止任务」会被误标完成并多发 completed 事件。
            if task.status == "running":
                task.status = "completed"
                self._emit("completed", task.task_id, "completed")

    def stop(self, task_id: str) -> tuple[bool, str]:
        """停止指定任务"""
        with self._manager_lock:
            task = self._tasks.get(task_id)

        if not task:
            return False, f"❌ 任务不存在: {task_id}"

        if task.status != "running":
            return False, f"❌ 任务已结束 (状态: {task.status})"

        try:
            task.status = "stopped"

            # S4: 优先用 Job Object 杀进程树（内核级，比 taskkill 更可靠，
            # 不依赖命令行解析；kill-on-close 兜底防止任务对象泄漏）。
            if task.job is not None:
                if task.job.kill():
                    try:
                        task.process.wait(timeout=3)
                    except Exception:
                        pass
                    task.job.close()
                    self._emit("stopped", task_id, "stopped", "job-kill")
                    return True, f"✅ 已终止任务: {task_id} (PID: {task.pid}, Job 杀进程树)"

            # Windows: 使用 taskkill /T 杀死进程树（包括子进程）— 降级路径
            if sys.platform == "win32":
                import subprocess as sp

                # 先尝试用 taskkill /T 杀死整个进程树
                result = sp.run(
                    ["taskkill", "/T", "/F", "/PID", str(task.pid)], capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0:
                    self._emit("stopped", task_id, "stopped", "taskkill")
                    return True, f"✅ 已终止任务: {task_id} (PID: {task.pid}, 含子进程)"
                else:
                    # taskkill 失败，尝试直接 terminate + wait
                    task.process.terminate()
                    try:
                        task.process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        task.process.kill()
                        task.process.wait(timeout=1)
                    self._emit("stopped", task_id, "stopped", "terminate")
                    return True, f"✅ 已终止任务: {task_id} (使用 terminate/kill)"
            else:
                # Unix: 使用 terminate 和 SIGTERM
                task.process.terminate()
                try:
                    task.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    task.process.kill()
                self._emit("stopped", task_id, "stopped", "terminate")
                return True, f"✅ 已终止任务: {task_id}"

        except Exception as e:
            return False, f"❌ 终止失败: {str(e)}"

    def get_logs(self, task_id: str, lines: int = 100) -> str:
        """获取任务日志"""
        with self._manager_lock:
            task = self._tasks.get(task_id)

        if not task:
            return f"❌ 任务不存在: {task_id}"

        output = task.output_buffer[-lines:] if task.output_buffer else []
        output_text = "\n".join(output) if output else "(暂无输出)"

        status_icon = "🟢" if task.status == "running" else ("⏹️ " if task.status == "stopped" else "✅")
        elapsed = time.time() - task.start_time
        elapsed_str = f"{int(elapsed)}s"

        header = f"""📋 任务: {task_id} {status_icon}
状态: {task.status}
PID: {task.pid}
运行时长: {elapsed_str}
命令: {task.command}
---
输出 (最近 {len(output)} 行):
---
{output_text}
---"""

        return header

    def list_tasks(self) -> str:
        """列出所有任务"""
        with self._manager_lock:
            tasks = list(self._tasks.values())

        if not tasks:
            return "📭 暂无后台任务"

        lines = ["📋 后台任务列表:\n"]
        lines.append(f"{'任务ID':<14} {'状态':<10} {'PID':<8} {'运行时长':<10} {'命令'}")
        lines.append("-" * 80)

        for task in sorted(tasks, key=lambda t: t.start_time, reverse=True):
            elapsed = time.time() - task.start_time
            elapsed_str = f"{int(elapsed)}s"
            status_icon = "🟢" if task.status == "running" else ("⏹️ " if task.status == "stopped" else "✅")
            status = f"{status_icon}{task.status}"
            cmd_preview = task.command[:50] + "..." if len(task.command) > 50 else task.command
            lines.append(f"{task.task_id:<14} {status:<10} {task.pid:<8} {elapsed_str:<10} {cmd_preview}")

        return "\n".join(lines)

    def cleanup_completed(self):
        """清理已结束且超过 1 小时的僵尸任务"""
        with self._manager_lock:
            to_remove = []
            for task_id, task in self._tasks.items():
                if task.status in ("stopped", "completed") and (time.time() - task.start_time) > 3600:
                    to_remove.append(task_id)
            for task_id in to_remove:
                del self._tasks[task_id]


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
            },
            "required": ["command"],
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
            },
            "required": ["command"],
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


def register(registry):
    registry.register(
        "bash", _BASH_SCHEMA, impl=_bash_impl,
        danger="dangerous", icon="shell", cn_name="执行命令",
        group=GROUP_TERMINAL, description="执行shell命令",
        aliases=["Bash", "Terminal", "RunCommand", "execute_command", "shell", "Command"],
        render=_render_bash_body,
    )
    registry.register(
        "bg_start", _BG_START_SCHEMA, impl=_bg_start_impl,
        danger="dangerous", icon="shell", cn_name="后台启动",
        group=GROUP_TERMINAL, description="启动后台命令",
        aliases=["BgStart", "bg_start"],
    )
    registry.register(
        "bg_stop", _BG_STOP_SCHEMA, impl=_bg_stop_impl,
        danger="dangerous", icon="shell", cn_name="后台停止",
        group=GROUP_TERMINAL, description="停止后台任务",
        aliases=["BgStop", "bg_stop"],
    )
    registry.register(
        "bg_logs", _BG_LOGS_SCHEMA, impl=_bg_logs_impl,
        danger="safe", icon="shell", cn_name="后台日志",
        group=GROUP_TERMINAL, description="查看后台任务日志",
        aliases=["BgLogs", "bg_logs"],
    )
    registry.register(
        "bg_list", _BG_LIST_SCHEMA, impl=_bg_list_impl,
        danger="safe", icon="shell", cn_name="后台列表",
        group=GROUP_TERMINAL, description="列出后台任务状态",
        aliases=["BgList", "bg_list"],
    )
