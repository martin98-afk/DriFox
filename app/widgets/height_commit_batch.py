"""resize 恢复期的高度提交协调器（Height Commit Batch）。

背景
----
每张消息卡片的高度由内嵌 ``QWebEngineView`` 通过
``console.log('pywebview_height:<h>|...')`` **异步**回传。一次窗口 resize 会让
N 张卡片各自 relayout 并各自回传（甚至多次：显式 ``reportHeight`` +
ResizeObserver + 解锁补报）。旧链路是「谁先回来谁立刻 ``setFixedHeight``」：

* ``chat_container`` 的总高在数百毫秒内被改动 N 次；
* 每次改动都让 ``QScrollArea`` 的 scrollbar maximum 变化；
* 滚动位置靠「每张卡的 ``_last_height_delta`` 逐次累加」补偿 —— **顺序敏感**，
  且 resize 占位恢复路径还把这个 delta 显式清零 → 恢复期全程零补偿。

表现就是「一 resize，画面内部 WebView 同步得非常乱」。

本模块做两件事
--------------
1. **合并**：极短时间内到达的多次上报按卡片去重，只应用最后一次，避免同一张卡
   连续 ``setFixedHeight`` 抖动。
2. **锚定**：整轮恢复的滚动修正改成「锚点卡片 + 相对视口偏移」，与到达顺序、
   到达次数**完全无关**；期间所有 per-card 增量补偿一律关闭（delta 清零）。

用法::

    batch = HeightCommitBatch(scroll_area, container, follow_bottom_fn)
    batch.begin()                 # resize 恢复开始
    batch.submit(card, height)    # 卡片高度上报的统一出口
    batch.end()                   # 收尾（自动 flush + 复位锚点）
"""

import contextlib
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Tuple

from PyQt5.QtCore import QPoint, QTimer
from PyQt5.QtWidgets import QWidget

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查
    from app.widgets.message_card import MessageCard

# 空闲关闭间隔：最后一次 submit 之后这么久没有新提交，就认为本轮恢复结束。
# 离屏卡片分批恢复是 20 张 / 30ms，150ms 足够覆盖两批之间的空档。
_IDLE_CLOSE_MS = 150


class HeightCommitBatch:
    """把一轮 resize 恢复中的 N 次高度提交收敛为一次锚定修正。

    Args:
        scroll_area: 聊天区的 ``QScrollArea``。
        container: ``scroll_area.widget()``，卡片的直接父容器。
        follow_bottom_fn: 返回当前视口是否处于「跟随底部」状态的回调。
    """

    def __init__(self, scroll_area, container: QWidget, follow_bottom_fn: Callable[[], bool]):
        self._sa = scroll_area
        self._container = container
        self._follow_bottom = follow_bottom_fn
        self._pending: Dict[int, Tuple["MessageCard", int]] = {}
        self._scheduled = False
        self._active = False
        self._anchor_card: Optional[QWidget] = None
        self._anchor_offset = 0
        # 父对象挂 scroll_area：随聊天区一起销毁，避免定时器泄漏到已释放对象
        self._idle_timer = QTimer(scroll_area)
        self._idle_timer.setSingleShot(True)
        self._idle_timer.timeout.connect(self.end)

    # ── 生命周期 ────────────────────────────────────────────────────

    def begin(self) -> None:
        """开启一轮批量提交（幂等）。"""
        if self._active:
            self._idle_timer.start(_IDLE_CLOSE_MS)
            return
        self._active = True
        self._anchor_card = None
        self._anchor_offset = 0
        self._idle_timer.start(_IDLE_CLOSE_MS)

    def end(self) -> None:
        """收尾：先 flush 掉余量，再复位锚点与激活标志。"""
        self._idle_timer.stop()
        self.flush()
        self._active = False
        self._anchor_card = None
        self._anchor_offset = 0

    @property
    def active(self) -> bool:
        return self._active

    # ── 提交 ────────────────────────────────────────────────────────

    def submit(self, card: "MessageCard", height: int) -> None:
        """登记一次卡片高度。真正的 ``setFixedHeight`` 推迟到本轮事件循环末尾。"""
        if not self._active:
            return
        # 每次提交都续期空闲计时器：离屏卡片是分批恢复的，中途空档不能提前收尾
        self._idle_timer.start(_IDLE_CLOSE_MS)
        if self._anchor_card is None:
            self._capture_anchor()
        self._pending[id(card)] = (card, height)
        # 与同步路径保持一致的去重语义：同一高度不会被重复提交/重复应用
        card._last_applied_viewer_height = height
        if not self._scheduled:
            self._scheduled = True
            QTimer.singleShot(0, self.flush)

    def flush(self) -> None:
        """应用缓冲中的所有高度，并在末尾做**一次**锚定修正。"""
        self._scheduled = False
        if not self._pending:
            return
        items: List[Tuple["MessageCard", int]] = list(self._pending.values())
        self._pending.clear()

        with contextlib.suppress(RuntimeError):
            sb = self._sa.verticalScrollBar()
            follow = bool(self._follow_bottom())
            # 冻结绘制：期间 N 次布局只会在解冻后 paint 一次，避免逐张闪跳
            was_enabled = self._sa.updatesEnabled()
            if was_enabled:
                self._sa.setUpdatesEnabled(False)
            try:
                for card, height in items:
                    with contextlib.suppress(RuntimeError):
                        # 本轮统一走锚定，关闭 per-card 增量补偿
                        card._last_height_delta = 0
                        card.viewer.setFixedHeight(height)
                        card.heightChanged.emit(height)
            finally:
                if follow:
                    sb.setValue(sb.maximum())
                elif self._anchor_card is not None:
                    top = self._anchor_card.mapTo(self._container, QPoint(0, 0)).y()
                    sb.setValue(max(0, top - self._anchor_offset))
                if was_enabled:
                    self._sa.setUpdatesEnabled(True)

    # ── 内部 ────────────────────────────────────────────────────────

    def _capture_anchor(self) -> None:
        """记录锚点：视口顶部所在的那张卡 + 它相对视口顶部的偏移。

        之后无论卡片高度怎么变、按什么顺序变，都把这张卡拉回同一偏移，
        等价于浏览器原生的 scroll anchoring。
        """
        try:
            top = self._sa.verticalScrollBar().value()
            layout = self._container.layout()
            if layout is None:
                return
            for i in range(layout.count()):
                item = layout.itemAt(i)
                widget = item.widget() if item is not None else None
                if widget is None:
                    continue
                y = widget.mapTo(self._container, QPoint(0, 0)).y()
                if y + widget.height() > top:
                    self._anchor_card = widget
                    self._anchor_offset = y - top
                    return
        except RuntimeError:
            self._anchor_card = None
