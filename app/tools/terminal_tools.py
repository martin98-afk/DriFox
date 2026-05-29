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
from typing import Callable, Optional
from dataclasses import dataclass, field

from app.tools.result import ToolResult


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
        
        try:
            # Windows: 设置代码页避免编码问题
            if sys.platform == "win32":
                cmd = f"chcp 65001 >nul 2>&1 && {command}"
            else:
                cmd = command

            # 使用 ShellContextManager 的平台适配参数
            kwargs = {}
            if sys.platform == "win32":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = subprocess.SW_HIDE
                kwargs["startupinfo"] = startupinfo
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            
            process = subprocess.Popen(
                cmd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(workdir),
                **kwargs,
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
        """捕获进程输出"""
        try:
            if task.process.stdout:
                for line in iter(task.process.stdout.readline, ''):
                    if line:
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
        # ShellContextManager 实例（与 TerminalTools 同生命周期，保证 context 持久化）
        from app.tools.shell_context_manager import ShellContextManager
        self._shell_mgr = ShellContextManager.get_shell_manager()
        # 注册动态获取 workdir 的回调给 BackgroundTaskManager
        BackgroundTaskManager(lambda: self.workdir)

    @property
    def workdir(self) -> Path:
        return self._owner.workdir

    def execute_bash(self, command: str, timeout: int = 120, context: str = None) -> ToolResult:
        """执行 shell 命令，支持可靠 timeout 和持久上下文

        使用 ShellContextManager 的 marker + base64 机制，
        避免管道死锁和编码问题。

        参数:
            command: shell 命令
            timeout: 超时秒数（默认 120）
            context: 持久化上下文 ID（可选）。传入已有 ID 则复用该 shell 进程，
                     传入 None 或空字符串则每次新建进程。
        """
        try:
            mgr = self._shell_mgr

            if context:
                # 持久化上下文模式
                ctx_id, output, created, timed_out, exit_code = mgr.run_detailed(
                    command, context, timeout
                )
                if timed_out:
                    return ToolResult(False, error=output)

                combined = output if output else "(no output)"
                if exit_code is not None and exit_code != 0:
                    combined = f"exit_code: {exit_code}\n{combined}"

                from app.tools.shell_compressor import compress
                compressed = compress(command, combined)
                return ToolResult(True, content=compressed)
            else:
                # 一次性执行（原有行为）
                output, timed_out, exit_code = mgr.run_once(command, timeout)
                if timed_out:
                    return ToolResult(False, error=output)

                combined = output if output else "(command completed with no output)"
                if exit_code is not None and exit_code != 0:
                    combined = f"exit_code: {exit_code}\n{combined}"

                from app.tools.shell_compressor import compress
                compressed = compress(command, combined)
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
