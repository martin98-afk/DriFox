# -*- coding: utf-8 -*-
"""
跨平台 Shell 上下文管理器

提供持久化 shell 进程管理、Marker+Base64 输出捕获、超时熔断。

架构:
    ShellContextManager (抽象基类)
    ├── WindowsShellManager  — PowerShell + ConvertTo-Json + ToBase64String
    └── UnixShellManager     — Bash/Zsh + python3 + base64
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any


# 项目根目录（用于相对路径解析）
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# 默认超时
_DEFAULT_TIMEOUT = 30

# Marker 前缀
_MARKER_PREFIX = "__DRIFOX_B64_"


class ShellContextManager:
    """跨平台 shell 执行引擎（抽象基类）"""

    # ---- 子类必须实现 ----

    @property
    def _shell_cmd(self) -> list[str]:
        """平台差异①：启动 shell 的可执行文件和参数"""
        raise NotImplementedError

    def _build_capture_script(self, command: str, marker: str) -> str:
        """平台差异②：用 shell 语法包裹命令，输出 marker + base64(json) 格式"""
        raise NotImplementedError

    # ---- 通用逻辑 ----

    def __init__(self):
        self._contexts: dict[str, subprocess.Popen] = {}
        self._context_io_locks: dict[str, threading.Lock] = {}
        self._lock = threading.Lock()

    def close_all(self):
        """关闭所有持久化上下文进程"""
        with self._lock:
            contexts = list(self._contexts.values())
            self._contexts.clear()
            self._context_io_locks.clear()
        for process in contexts:
            self._close_process(process)

    def _create_process(self, cwd: str | None = None) -> subprocess.Popen:
        """创建新的 shell 子进程（跨平台）

        参数:
            cwd: 工作目录，None 则用 BASE_DIR
        """
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            # 防止弹出黑色控制台窗口
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        return subprocess.Popen(
            self._shell_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            cwd=cwd or str(BASE_DIR),
            **kwargs,
        )

    @staticmethod
    def _close_process(process: subprocess.Popen):
        """安全关闭进程（关闭 IO、终止进程、等待结束）"""
        if process is None:
            return
        # 先关闭 stdin，让进程感知 EOF
        try:
            if process.stdin:
                process.stdin.close()
        except Exception:
            pass
        # 在 Windows 上用 taskkill 强制杀进程树
        if sys.platform == "win32":
            try:
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                    capture_output=True,
                    timeout=5,
                )
            except Exception:
                pass
        else:
            try:
                process.terminate()
            except Exception:
                pass
        # 等待进程结束
        try:
            process.wait(timeout=3)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass
        # 最后清理 stdout/stderr
        for stream_name in ("stdout", "stderr"):
            try:
                stream = getattr(process, stream_name, None)
                if stream is not None:
                    stream.close()
            except Exception:
                pass

    @staticmethod
    def _create_marker() -> str:
        """创建唯一的输出边界 Marker"""
        return f"{_MARKER_PREFIX}{uuid.uuid4().hex}__"

    @staticmethod
    def _decode_payload(payload: str | None) -> tuple[str, int | None]:
        """解码 payload

        支持两种格式:
        1. Windows: Base64(JSON) — {"output": "...", "exit_code": N}
        2. Unix:     <exit_code>:<Base64(output)>  (无 python3 依赖)
        """
        if not payload:
            return "", None
        # 格式①: Windows JSON 格式
        try:
            decoded = base64.b64decode(payload).decode("utf-8")
            data = json.loads(decoded)
            if isinstance(data, dict):
                return str(data.get("output", "")).strip(), data.get("exit_code")
            return decoded.strip(), None
        except Exception:
            pass
        # 格式②: Unix 分隔符格式 <exit_code>:<Base64(output)>
        # 用正则严格匹配，避免普通 base64 或含冒号的文本误入
        if re.match(r"^\d+:[A-Za-z0-9+/=]*$", payload):
            try:
                exit_code_str, b64_output = payload.split(":", 1)
                exit_code = int(exit_code_str.strip())
                output = base64.b64decode(b64_output.strip()).decode("utf-8")
                return output.strip(), exit_code
            except Exception:
                pass
        return payload.strip(), None

    def _read_until_marker(
        self,
        process: subprocess.Popen,
        marker: str,
        timeout_seconds: int,
        timeout_closer=None,
    ) -> tuple[str, bool, int | None]:
        """读取 stdout 直到遇到 Marker，支持超时"""
        timed_out = threading.Event()

        def on_timeout():
            timed_out.set()
            if timeout_closer:
                timeout_closer()
            else:
                self._close_process(process)

        timer = threading.Timer(timeout_seconds, on_timeout)
        timer.daemon = True
        timer.start()
        raw_lines: list[str] = []
        payload: str | None = None
        try:
            assert process.stdout is not None
            while True:
                line = process.stdout.readline()
                if not line:
                    break
                line = line.rstrip("\r\n")
                if line.startswith(marker):
                    payload = line[len(marker):]
                    break
                raw_lines.append(line)
        finally:
            timer.cancel()

        if timed_out.is_set():
            return "", True, None

        output, exit_code = self._decode_payload(payload)
        if not output and raw_lines:
            output = "\n".join(raw_lines).strip()
        return output, False, exit_code

    def run_once(self, command: str, timeout_seconds: int | None = None, cwd: str | None = None) -> tuple[str, bool, int | None]:
        """执行一次性命令（无持久上下文）

        参数:
            command: 要执行的命令
            timeout_seconds: 超时秒数
            cwd: 工作目录（None 用 BASE_DIR）
        返回:
            (output, timed_out, exit_code)
        """
        effective_timeout = timeout_seconds if timeout_seconds is not None else _DEFAULT_TIMEOUT
        marker = self._create_marker()
        process = self._create_process(cwd=cwd)
        try:
            assert process.stdin is not None
            process.stdin.write(self._build_capture_script(command, marker) + "\n")
            process.stdin.flush()
            decoded, timed_out, exit_code = self._read_until_marker(process, marker, effective_timeout)
        finally:
            self._close_process(process)

        if timed_out:
            timeout_text = (
                f"命令执行超过 {effective_timeout} 秒，当前进程已关闭。"
                " 如果这个命令需要交互输入，请改用更明确、不会阻塞的命令。"
            )
            return timeout_text, True, None

        return decoded, False, exit_code

    def run_detailed(
        self,
        command: str,
        context_id: str | None,
        timeout_seconds: int | None = None,
        cwd: str | None = None,
    ) -> tuple[str, str, bool, bool, int | None]:
        """在持久化上下文中执行命令

        参数:
            command: 要执行的命令
            context_id: 上下文 ID（None 自动创建）
            timeout_seconds: 超时秒数
            cwd: 工作目录（None 用 BASE_DIR）
        返回:
            (context_id, output, created, timed_out, exit_code)
        """
        with self._lock:
            created = False
            if not context_id:
                context_id = uuid.uuid4().hex[:8]
                created = True
            elif context_id not in self._contexts:
                created = True
            else:
                created = False

            if created:
                self._contexts[context_id] = self._create_process(cwd=cwd)
                self._context_io_locks[context_id] = threading.Lock()

            process = self._contexts[context_id]
            context_lock = self._context_io_locks[context_id]

        with context_lock:
            marker = self._create_marker()
            script = self._build_capture_script(command, marker)
            try:
                assert process.stdin is not None
                process.stdin.write(script + "\n")
                process.stdin.flush()
            except Exception as exc:
                # 进程已死，自动重建
                with self._lock:
                    self._contexts.pop(context_id, None)
                self._close_process(process)
                process = self._create_process(cwd=cwd)
                with self._lock:
                    self._contexts[context_id] = process
                assert process.stdin is not None
                process.stdin.write(script + "\n")
                process.stdin.flush()

            effective_timeout = timeout_seconds if timeout_seconds is not None else _DEFAULT_TIMEOUT
            output, timed_out, exit_code = self._read_until_marker(
                process,
                marker,
                effective_timeout,
                timeout_closer=lambda: self._close_context_process(context_id, process),
            )

        if timed_out:
            timeout_text = (
                f"命令执行超过 {effective_timeout} 秒，上下文进程已关闭。"
                " 如果这个命令需要交互输入，请改用更明确、不会阻塞的命令。"
            )
            return context_id, timeout_text, created, True, None

        return context_id, output, created, False, exit_code

    def run(self, command: str, context_id: str | None, timeout_seconds: int | None = None, cwd: str | None = None) -> tuple[str, str, bool, bool]:
        """简化版：不返回 exit_code
        
        参数:
            command: 要执行的命令
            context_id: 上下文 ID
            timeout_seconds: 超时秒数
            cwd: 工作目录（None 用 BASE_DIR）
        返回:
            (context_id, output, created, timed_out)
        """
        ctx_id, output, created, timed_out, _ = self.run_detailed(command, context_id, timeout_seconds, cwd=cwd)
        return ctx_id, output, created, timed_out

    def close_context(self, context_id: str):
        """关闭指定上下文进程"""
        with self._lock:
            process = self._contexts.pop(context_id, None)
            self._context_io_locks.pop(context_id, None)
        if process:
            self._close_process(process)

    @staticmethod
    def _resolve_path(path_str: str) -> Path:
        """解析路径（相对路径以 BASE_DIR 为基）"""
        path = Path(os.path.expandvars(os.path.expanduser(path_str.strip())))
        if not path.is_absolute():
            path = BASE_DIR / path
        return path.resolve()

    @staticmethod
    def get_shell_manager() -> "ShellContextManager":
        """工厂方法：根据当前平台返回对应的 ShellManager 实例"""
        if sys.platform == "win32":
            return WindowsShellManager()
        return UnixShellManager()


class WindowsShellManager(ShellContextManager):
    """Windows PowerShell 实现"""

    @property
    def _shell_cmd(self) -> list[str]:
        return ["powershell", "-NoLogo", "-NoProfile", "-Command", "-"]

    def _build_capture_script(self, command: str, marker: str) -> str:
        """PowerShell: Base64 编码命令 → 执行 → ConvertTo-Json → ToBase64String"""
        command_literal = base64.b64encode(command.encode("utf-8")).decode("ascii")
        return (
            "$ProgressPreference='SilentlyContinue'\n"
            "$ErrorActionPreference='Continue'\n"
            "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)\n"
            "$OutputEncoding = [Console]::OutputEncoding\n"
            f"$__drCB64 = '{command_literal}'\n"
            f"$__drMarker = '{marker}'\n"
            "$__drSource = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($__drCB64))\n"
            "$__drExitCode = 0\n"
            "try {\n"
            "    $__drOutput = & ([ScriptBlock]::Create($__drSource)) *>&1 | Out-String -Width 4096\n"
            "    if ($LASTEXITCODE -is [int]) { $__drExitCode = $LASTEXITCODE }\n"
            "} catch {\n"
            "    $__drOutput = ($_ | Out-String -Width 4096)\n"
            "    $__drExitCode = 1\n"
            "}\n"
            "$__drResult = @{ output = $__drOutput; exit_code = $__drExitCode } | ConvertTo-Json -Compress -Depth 4\n"
            "$__drBytes = [System.Text.Encoding]::UTF8.GetBytes($__drResult)\n"
            "$__drPayload = [Convert]::ToBase64String($__drBytes)\n"
            "Write-Output $__drMarker$__drPayload\n"
        )


class UnixShellManager(ShellContextManager):
    """Linux/macOS Bash/Zsh 实现"""

    @property
    def _shell_cmd(self) -> list[str]:
        shell = os.environ.get("SHELL", "/bin/bash")
        return [shell, "--norc", "--noprofile", "-i"]

    def _build_capture_script(self, command: str, marker: str) -> str:
        """Bash/Zsh: Base64 命令 → eval → exit_code:Base64(output)

        不需要 python3，纯 bash + base64 + tr，兼容 Linux/macOS。
        输出格式: <marker><exit_code>:<Base64(output)>
        """
        cmd_b64 = base64.b64encode(command.encode("utf-8")).decode("ascii")
        return (
            f"__dr_cmd=$(printf '%s' '{cmd_b64}' | base64 -d 2>/dev/null); "
            f"__dr_output=$(eval \"$__dr_cmd\" 2>&1); "
            f"__dr_exit=$?; "
            f"__dr_b64out=$(printf '%s' \"$__dr_output\" | base64 | tr -d '\\n'); "
            f"echo '{marker}$__dr_exit:$__dr_b64out'"
        )
