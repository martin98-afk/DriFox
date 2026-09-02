# -*- coding: utf-8 -*-
"""
持久 Shell 会话（S5）— Windows ConPTY via pywinpty

提供跨调用保留 shell 状态（cwd / env / 函数）的交互式会话：
- exec(command, timeout) 执行命令并返回输出
- 超时可配置（默认 300s，替代 shell_task.py 的固定 120s）
- close() 终止会话并杀进程树
- 非 Windows / 未安装 pywinpty 时 is_supported()=False（安全降级）

实现要点：
- 非阻塞读：select 轮询 fd + read，避免 pywinpty 阻塞读导致死锁
- 结束标记：命令后追加 `echo <token>`，按「行内容 == token」精确匹配
  （cmd 命令回显是 `echo <token>`，不会误触发）
- 超时恢复：杀 cmd 子进程 + 探测会话 → 失败重建（保证可用性）

用法::

    sess = PtyShellSession()
    ok, out = sess.exec("cd C:\\Windows")
    ok, out = sess.exec("cd")          # 输出 C:\\Windows（状态保留）
    sess.close()
"""
from __future__ import annotations

import re
import select
import sys
import time
import uuid
from typing import Optional, Tuple

from loguru import logger

from app.tools.process_job import ProcessJob

DEFAULT_TIMEOUT = 300.0  # 单条命令默认超时（秒）
_IDLE_POLL = 0.05        # 读循环轮询间隔（秒）


def _strip_ansi(text: str) -> str:
    """去除终端 ANSI 转义序列（颜色/光标定位/窗口标题等），保留可读文本"""
    # CSI 序列: ESC [ ... letter（颜色、光标定位、模式切换）
    text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)
    # OSC 序列: ESC ] ... (BEL | ESC \) — 如窗口标题 \x1b]0;...\x1b\\
    text = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", text)
    # 单字符引入序列（如 ESC ( B 字符集切换）
    text = re.sub(r"\x1b[()][0-9A-Za-z]", "", text)
    return text


class PtyShellSession:
    """持久 shell 会话（Windows cmd.exe via ConPTY）。

    线程安全：exec 应在单线程内串行调用（PTY 本质是串行协议）。
    """

    def __init__(self, timeout: Optional[float] = None):
        self.timeout = DEFAULT_TIMEOUT if timeout is None else float(timeout)
        self._proc = None
        self._closed = False
        if not self.is_supported():
            raise RuntimeError("PtyShellSession 仅支持 Windows 且需安装 pywinpty")

        self._spawn_session()
        # 等待 cmd 启动就绪（横幅延迟输出，读至连续 0.8s 无数据视为就绪）
        self._wait_idle(0.8, total_timeout=6.0)

    def _spawn_session(self) -> None:
        """spawn 新 cmd 会话并挂靠 ProcessJob（生命周期统一管理）"""
        import winpty

        # /Q 关闭命令回显，减少输出噪音；ConPTY 保证状态（cwd/env）跨调用保留
        self._proc = winpty.PtyProcess.spawn(["cmd.exe", "/Q"])
        # 生命周期挂靠 S3 ProcessJob：cmd 进程树入 Job，close 时 kill-on-close
        # 统一杀灭（与 BackgroundTaskManager 同一进程管理基建）
        self._job = ProcessJob() if ProcessJob.is_supported() else None
        if self._job is not None:
            self._job.assign(self._proc.pid)

    # ========== 能力探测 ==========

    @staticmethod
    def is_supported() -> bool:
        """当前平台/环境是否支持（Windows + pywinpty 可用）"""
        if sys.platform != "win32":
            return False
        try:
            import winpty  # noqa: F401

            return True
        except ImportError:
            return False

    # ========== 核心执行 ==========

    def exec(
        self,
        command: str,
        timeout: Optional[float] = None,
        strip_ansi: bool = True,
    ) -> Tuple[bool, str]:
        """执行单条命令，返回 (success, output)。

        - success=True：命令执行完成（收到结束标记）
        - success=False：超时（已尝试恢复） / 会话已关闭 / 执行异常
        - 状态（cwd/env/函数）跨调用保留；超时恢复失败会重建会话（cwd/env 重置）
        """
        if self._closed or self._proc is None:
            return False, "[错误] 会话已关闭"
        limit = self.timeout if timeout is None else float(timeout)

        # 先排空旧输出，确保结果归属本次命令
        self._wait_idle(0.15, total_timeout=1.0)
        token = f"__DRIFOX_END_{uuid.uuid4().hex[:6]}__"
        self._proc.write(f"{command}\r\necho {token}\r")

        buf = ""
        deadline = time.monotonic() + max(limit, 0.5)
        try:
            while time.monotonic() < deadline:
                data = self._read_available(_IDLE_POLL)
                if data:
                    buf += data
                    if self._has_token(_strip_ansi(buf), token):
                        return self._finalize(buf, token, command, strip_ansi)
        except Exception as e:
            return False, f"[错误] 执行异常: {e}"

        # 超时：尝试恢复会话（杀子进程/探测/重建），返回超时信息
        self._interrupt_pending()
        return False, "[超时] 命令执行超过时限，已中断，部分输出如下:\n" + self._clean(buf, strip_ansi).strip()

    # ========== 超时恢复 ==========

    def _interrupt_pending(self) -> None:
        """超时恢复：杀 cmd 子进程 → 探测会话 → 失败重建。

        实测结论（Windows ConPTY + cmd）：
        - 向 ConPTY 发送 Ctrl+C（sendintr）会破坏 cmd 输入状态（不再响应）
        - 杀外部命令子进程（如 ping）后 cmd 不一定恢复（进程树关系异常，
          psutil children 为空）
        因此恢复 = 尽力杀子进程 + 探测；探测失败则重建会话保证可用性
        （重建会重置 cwd/env）。
        """
        if self._closed or self._proc is None:
            return
        try:
            import psutil

            parent = psutil.Process(self._proc.pid)
            for child in parent.children(recursive=True):
                try:
                    child.kill()
                except Exception:
                    pass
        except Exception as e:
            logger.debug(f"[PtyShellSession] child kill: {e}")
        if not self._probe(1.5):
            logger.warning("[PtyShellSession] 超时后会话未恢复，重建会话")
            self._rebuild()
        else:
            # 恢复成功：排空探测残留输出
            self._wait_idle(0.3, total_timeout=1.0)

    def _probe(self, timeout: float) -> bool:
        """探测会话是否可交互（写 echo 探测标记，等行级精确匹配）"""
        if self._closed or self._proc is None:
            return False
        token = f"__DRIFOX_PROBE_{uuid.uuid4().hex[:6]}__"
        try:
            self._proc.write(f"echo {token}\r")
        except Exception:
            return False
        buf = ""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            data = self._read_available(0.1)
            if data:
                buf += data
                if any(l.strip() == token for l in _strip_ansi(buf).splitlines()):
                    return True
        return False

    def _rebuild(self) -> None:
        """重建会话（超时恢复兜底）：杀旧 cmd，spawn 新 cmd"""
        self.close()
        if not self.is_supported():
            return
        self._spawn_session()
        self._closed = False
        self._wait_idle(0.8, total_timeout=6.0)

    # ========== 内部工具 ==========

    def _read_available(self, timeout: float) -> str:
        """select 轮询 + 非阻塞读，无数据返回空串（不阻塞）"""
        if self._proc is None:
            return ""
        try:
            fd = self._proc.fileno()
        except Exception:
            return ""
        r, _, _ = select.select([fd], [], [], timeout)
        if not r:
            return ""
        try:
            data = self._proc.read(4096)
        except Exception:
            return ""
        return data if isinstance(data, str) else data.decode("utf-8", "replace")

    def _wait_idle(self, idle_seconds: float, total_timeout: float) -> None:
        """读输出直到连续 idle_seconds 无新数据（或总超时）"""
        idle = 0.0
        deadline = time.monotonic() + total_timeout
        while time.monotonic() < deadline:
            data = self._read_available(_IDLE_POLL)
            if data:
                idle = 0.0
            else:
                idle += _IDLE_POLL
                if idle >= idle_seconds:
                    return

    @staticmethod
    def _has_token(text: str, token: str) -> bool:
        """行级精确匹配结束标记（cmd 回显 `echo <token>` 不会误触发）"""
        return any(line.strip() == token for line in text.splitlines())

    def _finalize(self, buf: str, token: str, command: str, strip_ansi: bool) -> Tuple[bool, str]:
        """提取 token 行之前的命令输出，剔除命令回显/提示符噪音"""
        lines = _strip_ansi(buf).splitlines()
        token_idx = next((i for i, l in enumerate(lines) if l.strip() == token), None)
        result_lines = lines[:token_idx] if token_idx is not None else lines
        # 过滤噪音：空行、命令回显、cmd 横幅、提示符行
        filtered = []
        for l in result_lines:
            s = l.strip()
            if not s:
                continue
            if s.startswith("Microsoft Windows") or "保留所有权利" in s:
                continue
            if s == command.strip() or s.startswith(command.strip()[:40]):
                continue
            if re.match(r"^[A-Za-z]:\\[^>]*>\S*", s):  # 提示符行/提示符+命令回显粘连（如 D:\path>echo x）
                continue
            if "echo " in s and token in s:
                continue
            filtered.append(s)
        cleaned = "\n".join(filtered)
        if strip_ansi:
            cleaned = _strip_ansi(cleaned)
        cleaned = cleaned.strip()
        return True, cleaned if cleaned else "(命令执行完成，无输出)"

    @staticmethod
    def _clean(text: str, strip_ansi: bool) -> str:
        return _strip_ansi(text) if strip_ansi else text

    # ========== 生命周期 ==========

    def close(self) -> None:
        """终止会话并杀进程树（幂等）"""
        if self._closed:
            return
        self._closed = True
        proc, self._proc = self._proc, None
        job, self._job = getattr(self, "_job", None), None
        if job is not None:
            # kill-on-close：连同 cmd 进程树一起杀灭
            try:
                job.close()
            except Exception as e:
                logger.debug(f"[PtyShellSession] job close: {e}")
        if proc is None:
            return
        try:
            proc.terminate(force=True)
        except Exception as e:
            logger.debug(f"[PtyShellSession] terminate: {e}")
        try:
            proc.close(force=True)
        except Exception as e:
            logger.debug(f"[PtyShellSession] close: {e}")

    def __enter__(self) -> "PtyShellSession":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
