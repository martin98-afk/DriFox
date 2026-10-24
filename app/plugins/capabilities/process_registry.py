# -*- coding: utf-8 -*-
"""``process.start`` 进程登记表 —— 插件后台进程的唯一台账。

语义（与 docs/plugins/capabilities.md 对齐）：
- **宿主存续期绑定**：每个进程入专属 kill-on-close Job（Windows），宿主进程
  退出（正常退出 / 崩溃 / 被任务管理器杀）时 OS 关闭 Job 句柄自动树杀，
  插件进程不可能残留成孤儿。进程存活期 ≤ 宿主存活期，由内核保证；
- **插件禁用/卸载回收**：`kill_plugin` 主动树杀该插件名下全部进程，
  由 plugin_manager.disable_plugin 与 installer.uninstall/disable 挂接；
- **限额防滥用**：per-plugin 与全局双层上限，超限拒绝启动；
- **env 白名单**：进程只继承宿主环境的白名单子集 + 插件显式增量，
  宿主密钥类环境变量（API key 等）不再泄漏给插件启动的进程。

非 Windows 平台：Job 不可用（is_supported() False），靠持有 Popen 引用
proc.kill() 终止；宿主退出时非 Windows 的子进程不保证被杀（POSIX 无
kill-on-close 等价物，接受该平台差异，日志明示）。
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

from loguru import logger

from app.tools.process_job import ProcessJob

# 限额：per-plugin 并发进程数 / 全局进程总数
MAX_PER_PLUGIN = 8
MAX_GLOBAL = 32

# env 白名单：进程可见的宿主环境变量基座。缺失的 key 直接跳过（跨平台取交集式）。
# 刻意不含任何密钥/令牌类变量；进程需要额外变量时由插件 env 参数显式传入。
ENV_WHITELIST: tuple[str, ...] = (
    # 路径与系统
    "PATH",
    "PATHEXT",
    "HOME",
    "TEMP",
    "TMP",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    # 用户与目录
    "USERNAME",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMFILES",
    "PROGRAMDATA",
    # 本地化与其他
    "LANG",
    "LC_ALL",
    "TZ",
    "TERM",
    "PYTHONUNBUFFERED",
    "PYTHONIOENCODING",
)


@dataclass
class ProcessRecord:
    """一条插件启动的进程记录。"""

    plugin: str
    name: str
    pid: int
    argv0: str
    started_at: float
    proc: Any  # subprocess.Popen（类型宽松避免测试环境 import 负担）
    job: Optional[ProcessJob] = None  # Job 不可用或 assign 失败时 None


class ProcessRegistry:
    """插件后台进程台账（进程级单例，线程安全）。

    首启确认状态也在此管理（会话级：approve / deny 后本会话内不再询问，
    重启主程序即重置——被误拒后重开主程序即可重新授权）。
    """

    _instance: Optional["ProcessRegistry"] = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._records: dict[str, list[ProcessRecord]] = {}
        self._guard = threading.Lock()
        self._approved: set[str] = set()
        self._denied: set[str] = set()

    @classmethod
    def get_instance(cls) -> "ProcessRegistry":
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """测试专用：重置单例（不负责杀进程，测试自清理）。"""
        with cls._instance_lock:
            cls._instance = None

    # ── 登记 ──

    def check_limit(self, plugin: str) -> None:
        """启动前限额检查，超限抛 RuntimeError（调用方直接透传给插件）。"""
        self._prune()
        with self._guard:
            per = len(self._records.get(plugin, ()))
            total = sum(len(v) for v in self._records.values())
        if per >= MAX_PER_PLUGIN:
            raise RuntimeError(
                f"process.start 超出限额：插件 {plugin} 已有 {per} 个后台进程"
                f"（上限 {MAX_PER_PLUGIN}），请先复用或等待退出"
            )
        if total >= MAX_GLOBAL:
            raise RuntimeError(f"process.start 超出限额：全部插件共 {total} 个后台进程（全局上限 {MAX_GLOBAL}）")

    def register(self, record: ProcessRecord) -> None:
        with self._guard:
            self._records.setdefault(record.plugin, []).append(record)
        logger.info(
            f"[process.start] 登记插件={record.plugin} name={record.name} "
            f"pid={record.pid} 当前该插件进程数={self.count_for(record.plugin)}"
        )

    def count_for(self, plugin: str) -> int:
        with self._guard:
            return sum(1 for r in self._records.get(plugin, ()) if r.proc.poll() is None)

    def count_global(self) -> int:
        with self._guard:
            return sum(1 for rs in self._records.values() for r in rs if r.proc.poll() is None)

    # ── 首启确认状态 ──

    def approval_state(self, plugin: str) -> str:
        """返回 "approved" / "denied" / "pending"（会话级）。"""
        with self._guard:
            if plugin in self._approved:
                return "approved"
            if plugin in self._denied:
                return "denied"
            return "pending"

    def approve(self, plugin: str) -> None:
        with self._guard:
            self._approved.add(plugin)

    def deny(self, plugin: str) -> None:
        """拒绝同样入会话记忆：防恶意插件反复弹窗骚扰。重启主程序可重置。"""
        with self._guard:
            self._denied.add(plugin)

    # ── 回收 ──

    def kill_plugin(self, plugin: str) -> int:
        """树杀该插件名下全部进程（禁用/卸载挂接点）。返回成功终止数。

        优先 Job 树杀（覆盖孙进程）；Job 不可用时退回 Popen.kill()（只杀
        直接子进程）。幂等：无记录返回 0。"""
        with self._guard:
            records = self._records.pop(plugin, [])
        killed = 0
        for r in records:
            if self._terminate(r):
                killed += 1
        if records:
            logger.info(f"[process.start] 已回收插件 {plugin} 的 {killed}/{len(records)} 个进程")
        return killed

    def kill_all(self) -> int:
        """终止全部插件进程（宿主退出兜底；Windows 上 OS 兜底已足够，此为显式保险）。"""
        with self._guard:
            plugins = list(self._records)
        return sum(self.kill_plugin(p) for p in plugins)

    def snapshot(self) -> list[dict]:
        """存活进程快照（观测/审计用）。"""
        self._prune()
        with self._guard:
            return [
                {
                    "plugin": r.plugin,
                    "name": r.name,
                    "pid": r.pid,
                    "argv0": r.argv0,
                    "started_at": r.started_at,
                }
                for rs in self._records.values()
                for r in rs
                if r.proc.poll() is None
            ]

    # ── 内部 ──

    @staticmethod
    def _terminate(r: ProcessRecord) -> bool:
        """终止单条记录对应的进程树。幂等，失败仅告警。"""
        try:
            if r.proc.poll() is not None:
                return False  # 已退出
            if r.job is not None:
                r.job.kill()
            else:
                r.proc.kill()
            try:
                r.proc.wait(timeout=5)
            except Exception:
                pass
            return True
        except Exception as e:
            logger.warning(f"[process.start] 终止进程失败 plugin={r.plugin} pid={r.pid}: {e}")
            return False
        finally:
            if r.job is not None:
                r.job.close()

    def _prune(self) -> None:
        """惰性清理已退出进程的记录（读路径顺带执行，避免台账虚涨）。"""
        with self._guard:
            for plugin in list(self._records):
                alive = [r for r in self._records[plugin] if r.proc.poll() is None]
                if alive:
                    self._records[plugin] = alive
                else:
                    self._records.pop(plugin, None)


def build_process_env(extra: Optional[dict]) -> dict[str, str]:
    """构造子进程环境：白名单基座 + 插件增量。

    必须始终返回显式 dict（绝不返回 None）——Popen(env=None) 会继承宿主
    全量环境，等于把密钥全量交给插件进程。
    """
    env = {k: os.environ[k] for k in ENV_WHITELIST if k in os.environ}
    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    return env
