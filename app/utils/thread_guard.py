# -*- coding: utf-8 -*-
"""
全局 QThread 安全守卫 — 系统级单点防护

安装方式：在 app/__init__.py 中导入一次：
    from app.utils.thread_guard import install_guard
    install_guard()

原理：
    Monkey-patch QThread.__init__，对所有 QThread 实例自动执行：
    1. 重设 parent 为全局隐藏 QObject — 即使卡片创建了 QThread(self)，
       widget 销毁时 QThread 也不会被 Qt 父链级联销毁。
    2. 全局强引用跟踪 — Python GC 无法回收仍在运行中的 QThread，
       即使卡片代码执行了 self._worker_thread = None。

适用场景：
    - 热重载卸载卡片
    - 关闭聊天窗口
    - QThread 引用丢失 + Python GC
    - 任何 QThread 先于底层 OS 线程被销毁的路径
"""

from typing import Set
from PyQt5.QtCore import QObject, QThread

# ── 全局隐藏 QObject ──────────────────────────────────
# 所有 QThread 的 parent 被重定向到此对象，生命周期 = 应用进程。
# 任何 widget 销毁链都无法波及此对象下的 QThread。
_thread_anchor: QObject = QObject()

# ── 全局强引用集合 ────────────────────────────────────
# 保持对 ALL 运行中 QThread 的强引用，防止 Python GC 提前回收。
_running_threads: Set[QThread] = set()


def _on_thread_finished(thread: QThread) -> None:
    """线程正常结束后从墓地移除"""
    _running_threads.discard(thread)


def _on_thread_destroyed(thread: QThread) -> None:
    """线程被销毁后从墓地移除"""
    _running_threads.discard(thread)


def install_guard() -> None:
    """安装 QThread 安全守卫（monkey-patch QThread.__init__）

    此函数可多次调用，幂等。
    """
    # 检查是否已安装
    if getattr(QThread, "__init__", None) is getattr(
        install_guard, "_patched", None
    ):
        return

    original_init = QThread.__init__

    def _safe_init(self, parent=None):
        # ── 1. 重设 parent → 全局隐藏锚点 ──
        # 不管调用方传了什么 parent（哪怕是 widget），
        # 都改为 _thread_anchor，防止 widget 销毁时连带销毁运行中的 QThread
        original_init(self, _thread_anchor)

        # ── 2. 全局强引用跟踪 ──
        # 即使卡片代码执行 self._worker_thread = None，
        # Python GC 也无法回收此 QThread，因为 _running_threads 持有引用
        _running_threads.add(self)
        self.finished.connect(lambda t=self: _on_thread_finished(t))
        self.destroyed.connect(lambda t=self: _on_thread_destroyed(t))

    QThread.__init__ = _safe_init
    # 标记已安装
    install_guard._patched = _safe_init  # type: ignore[attr-defined]
