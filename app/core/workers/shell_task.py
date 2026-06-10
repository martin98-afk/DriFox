# -*- coding: utf-8 -*-
"""
Shell 执行任务 - 异步执行系统命令

安全说明:
  Path A (shell=False): 无 shell 元字符的命令，直接 argv 执行
  Path B (shell=True):  含管道/重定向的命令
"""

from PyQt5.QtCore import QRunnable, pyqtSlot
from app.tools.command_safety import needs_shell, classify_command, run_safe


class ShellExecutionTask(QRunnable):
    """异步执行Shell命令任务"""

    def __init__(self, command: str, callback):
        super().__init__()
        self.command = command
        self.callback = callback
        self.setAutoDelete(True)

    @pyqtSlot()
    def run(self):
        import subprocess
        import sys

        try:
            classification = classify_command(self.command)
            if classification == 'block':
                self.callback("[安全拦截] 命令被安全策略禁止执行")
                return

            use_shell = needs_shell(self.command)

            if use_shell:
                # Path B: 需要 shell 特性
                cmd = self.command
                if sys.platform == "win32":
                    cmd = f"chcp 65001 >nul 2>&1 && {cmd}"
                res = subprocess.run(
                    cmd,
                    shell=True,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="ignore",
                    timeout=120,
                )
            else:
                # Path A: 安全路径, shell=False
                import shlex
                args = shlex.split(self.command)
                res = subprocess.run(
                    args,
                    shell=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="ignore",
                    timeout=120,
                )

            output = res.stdout.strip() if res.stdout else ""
            error_out = res.stderr.strip() if res.stderr else ""
            combined = "\n".join(filter(None, [output, error_out]))
            result_text = combined if combined else "(命令执行完成，无输出)"
        except subprocess.TimeoutExpired:
            result_text = "[错误] 命令执行超时"
        except Exception as e:
            result_text = f"[错误] {str(e)}"

        self.callback(result_text)