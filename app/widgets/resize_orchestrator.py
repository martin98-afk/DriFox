"""多对话（多页）resize 编排器。

实测结论（QStackedWidget + offscreen 探针）
------------------------------------------
* 主窗口 resize **不会**向隐藏页派发 resizeEvent；
* 但**切页会给新页派发一次完整 resizeEvent**，且此刻新页 ``isVisible()`` 仍为
  ``False`` —— 所以任何「后台页裁剪」守卫都**不能**写成
  ``if not self.isVisible(): return``，否则切页同步会被永久跳过。

即便 Qt 本身不给隐藏页派发 resize，仍有两个「多对话放大效应」需要处理：

1. **后台页的恢复链仍在跑**。QTimer 不受可见性约束：用户在 100ms 内切走，
   原页的 80/100ms 防抖定时器和「20 张 / 30ms」的离屏恢复链会继续推进，
   与新页的恢复链争抢主线程 → 前台页收尾被拖慢、卡片高度分批到达更明显。
2. **切页有约 180ms 的空窗**。新页先被 resizeEvent 置成占位（WebView 隐藏），
   再等 80ms + 100ms 定时器才恢复真实内容，用户会看到一段空白/占位。

做法
----
* 由 ``TabManagerWindow`` 通过 :meth:`register_stack` 注册内容区 QStackedWidget；
* 各页推进恢复链前询问 :meth:`is_current`，非当前页一律**挂起**（标 dirty）；
* 页被激活时（``currentChanged``）同步补跑一次不分批的收尾，消除空窗。

未注册 stack 时 :meth:`is_current` 恒返回 ``True``（保守：绝不阻塞既有行为）。
"""

import contextlib
from typing import Any, Callable, Dict, Optional

try:  # 仅用于剔除已销毁的页面引用，缺失时退化为不清理
    import sip
except Exception:  # pragma: no cover
    sip = None


class ResizeOrchestrator:
    """进程级单例：仲裁「哪一页有资格推进 resize 恢复链」。"""

    _instance: Optional["ResizeOrchestrator"] = None

    def __init__(self) -> None:
        self._stack: Any = None
        self._dirty: Dict[int, Any] = {}
        self._paused: Dict[int, Any] = {}

    @classmethod
    def get_instance(cls) -> "ResizeOrchestrator":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── 注册 ────────────────────────────────────────────────────────

    def register_stack(self, stack: Any) -> None:
        """注册内容区 QStackedWidget（幂等）。

        同时接管其 ``currentChanged`` 信号：页被激活时补跑被挂起的同步。
        """
        if stack is None or self._stack is stack:
            return
        self._stack = stack
        with contextlib.suppress(Exception):
            stack.currentChanged.connect(self._on_stack_changed)

    def set_current_provider(self, provider: Callable[[], Any]) -> None:
        """自定义「当前页」取值器（无 stack 时使用）。"""
        self._custom_provider = provider

    # ── 查询 ────────────────────────────────────────────────────────

    def current_widget(self) -> Any:
        provider = getattr(self, "_custom_provider", None)
        if provider is not None:
            with contextlib.suppress(RuntimeError):
                return provider()
        if self._stack is not None:
            with contextlib.suppress(RuntimeError):
                return self._stack.currentWidget()
        return None

    def is_current(self, widget: Any) -> bool:
        """widget 是否为当前可见页。未注册 stack 时恒 True（不改变既有行为）。"""
        if self._stack is None and getattr(self, "_custom_provider", None) is None:
            return True
        current = self.current_widget()
        if current is None:
            return True
        return current is widget

    # ── 挂起 / 激活 ─────────────────────────────────────────────────

    def mark_dirty(self, widget: Any) -> None:
        """标记「该页有未完成的 resize 同步」，等激活时补跑。"""
        if widget is None:
            return
        self._prune()
        self._dirty[id(widget)] = widget

    def _prune(self) -> None:
        """剔除已被销毁的页面引用，避免长期运行下 dict 无界增长。"""
        if sip is None:
            return
        for store in (self._dirty, self._paused):
            for key in [k for k, w in store.items() if sip.isdeleted(w)]:
                store.pop(key, None)

    def mark_paused(self, widget: Any) -> None:
        """标记「该页的恢复链被挂起」（区别于 dirty：链停在半途）。"""
        if widget is None:
            return
        self._paused[id(widget)] = widget
        self.mark_dirty(widget)

    def clear_dirty(self, widget: Any) -> None:
        self._dirty.pop(id(widget), None)
        self._paused.pop(id(widget), None)

    def on_activated(self, widget: Any) -> None:
        """页被激活：若此前被挂起/标记，同步补跑一次宽度+高度同步。"""
        if widget is None:
            return
        key = id(widget)
        if key not in self._dirty:
            return
        self._dirty.pop(key, None)
        self._paused.pop(key, None)
        sync = getattr(widget, "_sync_all_cards_width", None)
        if sync is None:
            return
        try:
            was_enabled = widget.updatesEnabled()
        except RuntimeError:
            return
        try:
            if was_enabled:
                widget.setUpdatesEnabled(False)
            try:
                sync()
            finally:
                if was_enabled:
                    widget.setUpdatesEnabled(True)
        except RuntimeError:
            pass

    # ── 内部 ────────────────────────────────────────────────────────

    def _on_stack_changed(self, index: int) -> None:
        if self._stack is None or index < 0:
            return
        with contextlib.suppress(RuntimeError):
            self.on_activated(self._stack.widget(index))
