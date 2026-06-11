# -*- coding: utf-8 -*-
"""
终端工具集 - 提供命令行执行功能

支持：
- execute_bash: 执行 shell 命令，支持超时控制和输出捕获
- 后台任务管理：start_bg_task, stop_bg_task, get_bg_output

提供同步和后台两种执行模式。
"""
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Optional, Tuple, Union
from dataclasses import dataclass, field

from app.tools.result import ToolResult
from app.tools.command_safety import needs_shell, classify_command, run_safe, run_with_shell


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
    UTF8_TOOLS = frozenset({
        'git', 'npm', 'yarn', 'pnpm', 'node', 'deno', 'bun',
        'python', 'python3', 'pip', 'uv', 'cargo', 'rustc',
        'go', 'java', 'javac', 'mvn', 'gradle',
        'docker', 'kubectl', 'helm', 'terraform',
        'curl', 'wget', 'gh', 'aws', 'gcloud', 'az',
        'ruby', 'gem', 'php', 'composer',
        'lua', 'perl', 'R', 'julia',
        'ruff', 'mypy', 'pytest', 'eslint', 'tsc',
        'flutter', 'dart', 'swift', 'cargo',
        'make', 'cmake', 'ninja', 'meson',
        'npx', 'pip3', 'pipx',
    })
    
    cmd_lower = command.lower().strip()
    
    # 判断是否是已知输出 UTF-8 的工具
    is_utf8_tool = False
    for tool in UTF8_TOOLS:
        if cmd_lower.startswith(tool + ' ') or cmd_lower.startswith(tool + '.exe '):
            is_utf8_tool = True
            break
    
    # 如果命令明确是 UTF-8 工具，优先尝试 UTF-8
    if is_utf8_tool:
        try:
            return data.decode('utf-8')
        except UnicodeDecodeError:
            # UTF-8 失败，降级到 GBK
            return data.decode('gbk', errors='replace')
    
    # 对于其他命令，优先尝试 UTF-8（现代工具越来越多）
    try:
        decoded = data.decode('utf-8')
        # 检查是否包含常见乱码特征（GBK 当作 UTF-8 解码时）
        # 如果结果包含大量不可打印字符，可能是 GBK 被误判为 UTF-8
        printable_ratio = sum(c.isprintable() or c.isspace() for c in decoded) / max(len(decoded), 1)
        if printable_ratio > 0.8:
            return decoded
    except UnicodeDecodeError:
        pass
    
    # 回退到 GBK
    return data.decode('gbk', errors='replace')


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

    def append_output(self, text: str):
        """追加输出到缓冲区"""
        self.output_buffer.append(text)
        # 限制缓冲区大小，最多保留 10000 行
        if len(self.output_buffer) > 10000:
            self.output_buffer = self.output_buffer[-5000:]


def _prepare_windows_encoding(command: str) -> str:
    """Windows 上设置 UTF-8 编码前缀（仅当需要 shell=True 路径时使用）

    替代方案：通过 Python 的 env 传递编码，避免嵌入 chcp 命令。
    但某些 Windows 程序仍需要控制台代码页，所以保留 chcp 前缀。
    """
    if sys.platform == "win32":
        return f"chcp 65001 >nul 2>&1 && {command}"
    return command


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
        if owner_getter:
            cls._instance._get_workdir = owner_getter
        return cls._instance

    @classmethod
    def reset_instance(cls):
        """重置单例（仅用于测试）"""
        cls._instance = None

    def set_workdir(self, workdir: Path):
        """设置工作目录"""
        self._workdir = workdir

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
        if classification == 'block':
            return task_id, f"❌ 命令被安全策略拦截: {command}"

        try:
            use_shell = needs_shell(command)

            if use_shell:
                # Path B: 需要 shell 特性 — 使用 shell=True（后台任务暂不强制审批）
                cmd = _prepare_windows_encoding(command)
                process = run_with_shell(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.PIPE,
                    cwd=str(workdir),
                )
            else:
                # Path A: 安全路径 — shell=False
                process = run_safe(
                    command,
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
            )

            with self._manager_lock:
                self._tasks[task_id] = task

            # 启动输出捕获线程
            thread = threading.Thread(target=self._capture_output, args=(task,), daemon=True)
            thread.start()

            return task_id, f"✅ 后台任务已启动\n- 任务ID: {task_id}\n- PID: {process.pid}\n- 命令: {command}"

        except Exception as e:
            return task_id, f"❌ 启动失败: {str(e)}"

    def _capture_output(self, task: BackgroundTask):
        """捕获进程输出（智能解码）"""
        try:
            if task.process.stdout:
                for raw_line in iter(task.process.stdout.readline, b''):
                    if raw_line:
                        # 智能解码每一行
                        line = _smart_decode(raw_line, task.command)
                        task.append_output(line.rstrip('\n'))
                    if task.status != "running":
                        break
        except Exception:
            pass
        finally:
            # 进程结束后更新状态
            task.status = "completed"

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

            # Windows: 使用 taskkill /T 杀死进程树（包括子进程）
            if sys.platform == "win32":
                import subprocess as sp
                # 先尝试用 taskkill /T 杀死整个进程树
                result = sp.run(
                    ["taskkill", "/T", "/F", "/PID", str(task.pid)],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if result.returncode == 0:
                    return True, f"✅ 已终止任务: {task_id} (PID: {task.pid}, 含子进程)"
                else:
                    # taskkill 失败，尝试直接 terminate + wait
                    task.process.terminate()
                    try:
                        task.process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        task.process.kill()
                        task.process.wait(timeout=1)
                    return True, f"✅ 已终止任务: {task_id} (使用 terminate/kill)"
            else:
                # Unix: 使用 terminate 和 SIGTERM
                task.process.terminate()
                try:
                    task.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    task.process.kill()
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
        output_text = '\n'.join(output) if output else "(暂无输出)"

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

        return '\n'.join(lines)

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
            # 安全分类
            classification = classify_command(command)
            if classification == 'block':
                return ToolResult(False, error=f"命令被安全策略拦截: {command}")

            use_shell = needs_shell(command)

            if use_shell:
                # Path B: 需要 shell 特性（管道、重定向等）
                cmd = _prepare_windows_encoding(command)
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

            # Shell 输出压缩（减少 token 消耗）
            from app.tools.shell_compressor import compress
            compressed = compress(command, combined if combined else "(command completed with no output)")

            return ToolResult(True, content=compressed)

        except Exception as e:
            return ToolResult(False, error=f"Execution error: {str(e)}")

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
