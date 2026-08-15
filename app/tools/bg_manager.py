# -*- coding: utf-8 -*-
"""
后台任务管理器 — 平台级基础设施（主程序侧，插件热重载不触碰）

从 plugins/system/tools/terminal_tools.py 迁出：BackgroundTaskManager 持有
运行中的后台任务（_tasks/_instance 单例）。若留在插件模块，watcher 全量重扫
（任意插件文件被编辑）会重新 exec 模块 → 单例归零 → 正在运行的任务"消失"
（进程还在跑但 stop/logs/list 全部找不到，无法管理 + 资源泄漏）。

放在 app/tools/（主程序包）后，插件重载不影响后台任务状态。
"""
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from loguru import logger

from app.tools.command_safety import _extract_cmd_name, classify_command, needs_shell, run_safe, run_with_shell
from app.tools.process_job import ProcessJob


def _smart_decode(data: bytes, command: str = "") -> str:
    """
    智能解码：根据命令类型选择正确的编码

    Windows 平台编码规则:
    - Git/npm/Node 等现代工具: 输出 UTF-8
    - Windows 原生命令 (dir, type 等): 输出 GBK/CP936
    - 带管道的命令: 可能是混合编码，用 errors='replace' 容错
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

    cmd_name = _extract_cmd_name(command)
    is_utf8_tool = cmd_name is not None and cmd_name in UTF8_TOOLS

    if is_utf8_tool:
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data.decode("gbk", errors="replace")

    try:
        decoded = data.decode("utf-8")
        printable_ratio = sum(c.isprintable() or c.isspace() for c in decoded) / max(len(decoded), 1)
        if printable_ratio > 0.8:
            return decoded
    except UnicodeDecodeError:
        pass

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
    输出 UTF-8 而非 GBK。如果项目有 .venv，将其 Scripts 目录加入 PATH 前缀。
    """
    if sys.platform != "win32":
        return command

    prefix = "chcp 65001 >nul"

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
