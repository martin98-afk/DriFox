# -*- coding: utf-8 -*-
"""InfoBar 系统级守卫 — 死壳清洗 + 主线程投递桥

背景（2026-10-24 崩溃）：
    InfoBarManager 是全局单例，按 parent 分组管理 InfoBar；条目摘除的唯一
    通道是 closeEvent → closedSignal → remove()。任何绕过 closeEvent 的析构
    （后台线程创建后 GC、父链连带删除、跨线程 parenting 半失败）都会让
    infoBars[parent] 残留死壳（C++ 已析构、wrapper 还在）。
    之后同 parent 弹新 InfoBar 时：showEvent → add → _pos 遍历兄弟条 →
    访问死壳 height() → RuntimeError: wrapped C/C++ object ... deleted。

两层防护：
1. install_guard()：monkeypatch InfoBarManager.add，入口清洗死壳。
   插件可绕过宿主 API 直弹 InfoBar，通道不可枚举，必须在 manager 单点拦。
2. post_to_main(fn)：QObject 信号桥，把 callable 投递到主线程执行
   （跨线程 emit 自动走 QueuedConnection）。
"""

import threading
from typing import Callable, Optional

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication
import shiboken6 as sip
from qfluentwidgets.components.widgets.info_bar import InfoBarManager


def _cpp_alive(bar) -> bool:
    """C++ 对象存活判定（wrapper 可能是已析构的空壳）"""
    try:
        return not shiboken6.isValid(bar)
    except Exception:  # noqa: BLE001
        return False


def install_guard() -> None:
    """安装 InfoBarManager 死壳清洗守卫（幂等，可多次调用）

    add 是新 InfoBar showEvent → 布局定位的唯一入口，在入口把同 parent 组内
    已析构的条目摘除，_pos 的兄弟遍历就永远摸不到尸体。
    顺带在主线程预热 post_to_main 投递桥（重复调用也补预热）。
    """
    _ensure_dispatcher()
    if getattr(InfoBarManager.add, "_drifox_guarded", False):
        return

    original_add = InfoBarManager.add

    def _safe_add(self, infoBar):
        parent = infoBar.parent()
        if parent is not None:
            bars = self.infoBars.get(parent)
            if bars:
                alive = [bar for bar in bars if _cpp_alive(bar)]
                if len(alive) != len(bars):
                    self.infoBars[parent] = alive
        return original_add(self, infoBar)

    _safe_add._drifox_guarded = True
    InfoBarManager.add = _safe_add


class _MainDispatcher(QObject):
    """主线程投递桥：signal 连自身 bound slot，跨线程 emit 自动排队"""

    _arrived = Signal(object)

    def __init__(self):
        super().__init__()
        # 必须连 bound method：connect 到裸函数/lambda 时 PyQt 判定无接收者
        # 对象，跨线程 emit 会退化为发射线程直调，桥就白搭了
        self._arrived.connect(self._run)

    def _run(self, fn: Callable[[], None]) -> None:
        fn()


_dispatcher: Optional[_MainDispatcher] = None
_dispatcher_lock = threading.Lock()


def _ensure_dispatcher() -> bool:
    """确保投递桥就绪（只能在主线程创建，affinity 决定 queued 事件去向）"""
    global _dispatcher
    if _dispatcher is not None:
        return True
    with _dispatcher_lock:
        if _dispatcher is None:
            if QApplication.instance() is None:
                return False
            if threading.current_thread() is not threading.main_thread():
                return False
            _dispatcher = _MainDispatcher()
    return True


def post_to_main(fn: Callable[[], None]) -> bool:
    """把 fn 投递到主线程执行；返回是否成功入队

    桥未就绪且当前不在主线程时返回 False，调用方自行降级（记日志/放弃）。
    正常启动序列下 install_guard() 已在主线程预热桥，后台线程可直接投递。
    """
    if _dispatcher is None and not _ensure_dispatcher():
        return False
    _dispatcher._arrived.emit(fn)
    return True
