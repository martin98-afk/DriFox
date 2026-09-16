# -*- coding: utf-8 -*-
"""renderer 崩溃自愈错峰队列。

背景（2026-09-16 0xC0000409 连锁）
----------------------------------
单 renderer 承载全部卡片（``--renderer-process-limit``）时，renderer 崩溃会让
**所有**卡片同帧收到 ``renderCrashed``。旧实现每张卡即刻 ``_try_restore_context``
（重载骨架 + 全量补渲），N 张卡的重载请求挤在同一个事件循环批次里齐发，
把刚重启的 Chromium 线程再次压垮 → renderer 二次自杀 → 进程级死亡
（STATUS_STACK_BUFFER_OVERRUN）。

本队列把「同帧齐发」改成「错峰串行」：全局单例登记待自愈 viewer，
QTimer 每 ``_MIN_INTERVAL_MS`` 拍只恢复一张，可见者优先（用户先看到内容）。

设计要点
--------
* **弱引用持有**：队列作为进程级单例不该拖住 viewer 生命周期。viewer 被销毁
  后条目在下一拍被剔除；已恢复（``_context_lost=False``）的对象同样剔除。
* **去重**：同一 viewer 重复入队只登记一次（崩溃可能连发多拍）。
* **与复用池协同**：viewer 归还 ``WebViewPool`` 前 ``discard``（``reset_for_reuse``
  已重置页面，无需再自愈）。

线程约束（[T29 A5]）
-------------------
**仅限主线程调用**：``enqueue`` / ``discard`` / ``clear`` / ``get_instance`` 以及内部
的 QTimer 节拍都操作 Qt 对象（QTimer / viewer），且 ``_pending`` 不自带锁。
崩溃信号（``renderCrashed``）与池化归还（``reset_for_reuse``）都发生在主线程，
无需跨线程保护；若未来从工作线程调用，须自行 marshal 到主线程
（``QMetaObject.invokeMethod`` 或信号桥接），不得直调。
"""

import weakref
from collections import OrderedDict
from typing import Any, Optional

from loguru import logger
from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QApplication

# 每拍最小间隔：500ms 给 Chromium 侧 renderer 重启留出喘息窗口。
# 与既有 ``_schedule_context_restore`` 的 500ms 恢复延迟同节奏。
_MIN_INTERVAL_MS = 500


class RenderCrashQueue:
    """进程级 renderer 崩溃自愈队列（单例）。

    用法：崩溃回调里 ``RenderCrashQueue.get_instance().enqueue(viewer)``；
    viewer 归还复用池时 ``discard(viewer)``。
    """

    _instance: Optional["RenderCrashQueue"] = None

    @classmethod
    def get_instance(cls) -> "RenderCrashQueue":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self) -> None:
        self._pending: "OrderedDict[int, weakref.ReferenceType[Any]]" = OrderedDict()
        self._timer: Optional[QTimer] = None
        # [T29 A2] 进程退出时清空队列并停表：退出期事件循环可能再跑一拍，
        # 若队列残留已析构 viewer 会触发 AV（_drain_one 的 try 挡不住
        # 访问已释放 C++ 对象的崩溃）。aboutToQuit 在 QApplication 存在时才可连。
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.clear)

    # ── 入队 / 出队 ────────────────────────────────────────────────

    def enqueue(self, viewer) -> bool:
        """登记一个待自愈 viewer；已在队列中则不重复登记。

        Returns:
            True = 新登记并已排期；False = 重复（已忽略）。
        """
        key = id(viewer)
        if key in self._pending:
            return False
        self._pending[key] = weakref.ref(viewer)
        self._ensure_timer()
        return True

    def discard(self, viewer) -> None:
        """剔除指定 viewer（池化归还 / 主动销毁场景）。"""
        self._pending.pop(id(viewer), None)

    def clear(self) -> None:
        """清空队列并停表（测试隔离 / 全局重置）。"""
        self._pending.clear()
        if self._timer is not None:
            self._timer.stop()

    def pending_count(self) -> int:
        return len(self._pending)

    # ── 节拍 ───────────────────────────────────────────────────────

    def _ensure_timer(self) -> None:
        """懒创建单发定时器；未排期则启动。

        [T29 A1] 定时器挂到 QApplication 上：进程退出时 Qt 销毁对象树，
        未挂 parent 的裸 QTimer 可能抢在对象析构后触发 timeout → 回调里
        访问已析构 viewer 触发原生崩溃（AV）。挂 parent + aboutToQuit 清表
        （见 __init__）双保险。QApplication 不存在时（无 GUI 的极端测试环境）
        退化为无 parent，行为与旧版一致。
        """
        timer = self._timer
        if timer is None:
            timer = QTimer(QApplication.instance())
            timer.setSingleShot(True)
            timer.timeout.connect(self._drain_one)
            self._timer = timer
        if not timer.isActive():
            timer.start(_MIN_INTERVAL_MS)

    def _drain_one(self) -> None:
        """恢复一张 viewer；仍有待办则排下一拍。"""
        viewer = self._pick_next()
        if viewer is not None:
            try:
                viewer._try_restore_context()
            except Exception:
                # [T29 A3] 恢复失败时显式置 ``_context_lost = False``：否则该
                # viewer 保持「上下文已丢」状态，任何后续触发点（如崩溃重入队 /
                # webgl 集中上报）都会把它重新入队 → 同一张卡每拍循环恢复失败，
                # 形成 CPU/日志风暴。置 False = 放弃本次自愈，交由 needRecreate
                # 链（达阈值后重建）或用户手动操作兜底。
                try:
                    viewer._context_lost = False
                except Exception:
                    pass
                logger.warning("RenderCrashQueue: 自愈恢复失败（对象可能已销毁），已出队")
        # [T29 A4] 仅当未排期时才 start：_drain_one 执行期间若有新 viewer
        # 入队（enqueue → _ensure_timer 已 start），此处再 start 会重置计时器，
        # 使已等待的那一拍被推迟（持续入队时恢复节奏被无限推后）。
        if self._pending:
            timer = self._timer
            if timer is not None and not timer.isActive():
                timer.start(_MIN_INTERVAL_MS)

    def _pick_next(self):
        """取下一张待恢复 viewer：可见优先，剔除死引用与已恢复对象。"""
        # 先剔除：weakref 失效（viewer 已销毁）或 _context_lost=False（已恢复/已复位）
        for key in list(self._pending.keys()):
            ref = self._pending.get(key)
            obj = ref() if ref is not None else None
            if obj is None or not getattr(obj, "_context_lost", False):
                self._pending.pop(key, None)
        # 可见优先：用户眼前的卡片先恢复；不可见者按入队顺序（先崩先恢复）
        best_key = None
        for key in self._pending:
            ref = self._pending.get(key)
            obj = ref() if ref is not None else None
            if obj is None:
                continue
            try:
                if obj.isVisible():
                    best_key = key
                    break
            except Exception:
                continue
        if best_key is None:
            best_key = next(iter(self._pending), None)
        if best_key is None:
            return None
        entry = self._pending.pop(best_key, None)
        return None if entry is None else entry()
