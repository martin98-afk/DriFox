# -*- coding: utf-8 -*-
"""QSplitter 角点联动 + 悬浮延迟高亮（VSCode 式交互）

两项能力都挂在 QApplication 级事件过滤器上，对任何 QSplitter 生效
（主程序与插件里的 splitter 一视同仁，无需各自注册）：

1. **角点联动**：横向与纵向 splitter 的 handle 在几何上交叉（或隔着容器
   留白相邻）时，交叉处构成一个角点。悬停变对角光标，按住拖动可同时调整
   两个 splitter 的尺寸 —— 等价 VSCode 里拖拽左右分栏与上下分栏交汇点的
   行为。角点命中方块覆盖两条线的交点及之间的留白，因此鼠标不必精确压在
   handle 上。
2. **延迟高亮**：handle 悬停 ``HOVER_DELAY_MS`` 后才点亮（设置动态属性
   ``splitterHot=True``），移出立即熄灭，避免鼠标快速划过时分割线闪蓝。
   视觉由样式表里的 ``QSplitterHandle[splitterHot="true"]`` 规则决定，
   本模块只负责属性的时序。

设计约束：
- 只在「QSplitterHandle 或其子树内的控件」上做角点检测，普通鼠标移动一次
  isinstance 后放行，零额外开销；
- 不改 QSplitter 的既有拖拽：非角点区域一律放行，由 handle 自己处理；
- 不 import 任何窗口/容器类，纯 Qt 实现，插件侧同样可用。
"""

from typing import Dict, List, Optional, Tuple

from PyQt5.QtCore import QEvent, QObject, QPoint, QRect, Qt, QTimer
from PyQt5.QtGui import QCursor
from PyQt5.QtWidgets import QApplication, QSplitter, QSplitterHandle, QWidget

# 点亮判定的样式表属性名（样式表作者用 QSplitterHandle[splitterHot="true"]）
PROP_HOT = "splitterHot"

# _find_corner 的返回：(handle_a, 索引_a, splitter_a, handle_b, 索引_b, splitter_b)
Corner = Tuple[QSplitterHandle, int, QSplitter, QSplitterHandle, int, QSplitter]


def _handle_rect(handle: QSplitterHandle) -> QRect:
    """handle 的全局矩形（mapToGlobal + size）"""
    return QRect(handle.mapToGlobal(QPoint(0, 0)), handle.size())


def _min_extent(widget: QWidget, orientation: Qt.Orientation) -> int:
    """窗格在该轴上的最小尺寸（与 QSplitter 排序约束同口径）"""
    if widget is None or widget.isHidden():
        return 0
    hint = widget.minimumSizeHint()
    if orientation == Qt.Horizontal:
        return max(0, widget.minimumWidth(), hint.width())
    return max(0, widget.minimumHeight(), hint.height())


def _has_splitter_ancestor(widget: QWidget, depth: int = 8) -> bool:
    """控件是否位于某个 splitter 子树内（鼠标事件才有角点意义）"""
    current = widget
    for _ in range(depth):
        if isinstance(current, QSplitter):
            return True
        current = current.parentWidget() if current is not None else None
        if current is None:
            return False
    return False


class SplitterCornerController(QObject):
    """全局角点拖动 + handle 延迟高亮控制器（每进程一个）"""

    HOVER_DELAY_MS = 140
    HIT_PAD = 5  # 角点命中方块的外扩（像素）
    EDGE_PAD = 10  # 两条线端点相距多远仍算「相邻」（容器留白）
    NEAR_PAD = 24  # 交点落在另一条线范围外的最大容忍（超过即平行错开，不组角点）

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._app = QApplication.instance()
        self._drag: Optional[Dict] = None
        self._pending_handle: Optional[QSplitterHandle] = None
        self._hot_handle: Optional[QSplitterHandle] = None
        self._cursor_widget: Optional[QWidget] = None
        self._cursor_shape: Optional[Qt.CursorShape] = None
        self._splitter_cache: Optional[Tuple[QWidget, List[QSplitter]]] = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(self.HOVER_DELAY_MS)
        self._timer.timeout.connect(self._flush_hot)
        if self._app is not None:
            self._app.installEventFilter(self)

    # ── 生命周期 ──

    def stop(self) -> None:
        """卸载（窗口销毁 / 插件热重载）"""
        self._timer.stop()
        self._end_drag()
        self._restore_cursor()
        self._pending_handle = None
        self._hot_handle = None
        self._splitter_cache = None
        if self._app is not None:
            try:
                self._app.removeEventFilter(self)
            except RuntimeError:
                pass
            self._app = None

    # ── 事件入口 ──

    def eventFilter(self, obj, event):  # noqa: N802 (Qt 命名)
        et = event.type()

        # 拖动中：全局接管鼠标（grabMouse 保证事件都落到抓取控件上）
        if self._drag is not None:
            if et == QEvent.MouseMove:
                self._drag_to(event.globalPos())
                return True
            if et == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
                self._end_drag()
                return True

        if et == QEvent.ChildAdded:
            # 子控件增减会让 splitter 缓存失真（工作台 / 卡片随页面创建销毁）
            if self._splitter_cache is not None:
                self._splitter_cache = None
            return False

        if not isinstance(obj, QWidget):
            return False

        if et == QEvent.Enter and isinstance(obj, QSplitterHandle):
            self._pending_handle = obj
            self._timer.start()
        elif et == QEvent.Leave and isinstance(obj, QSplitterHandle):
            self._on_leave(obj)
        elif et == QEvent.MouseMove:
            is_handle = isinstance(obj, QSplitterHandle)
            if is_handle or _has_splitter_ancestor(obj):
                # Enter 事件在部分窗口类型上会丢（合成器/浮层），用移动事件兼作兜底
                if is_handle:
                    self._ensure_pending(obj)
                self._sync_cursor(obj, event.globalPos())
        elif et == QEvent.MouseButtonPress:
            if event.button() == Qt.LeftButton and self._begin_drag(obj, event.globalPos()):
                return True
        return False

    # ── 延迟高亮 ──

    def _on_leave(self, handle: QSplitterHandle) -> None:
        if self._pending_handle is handle:
            self._timer.stop()
            self._pending_handle = None
        if self._hot_handle is handle:
            self._set_hot(handle, False)
            self._hot_handle = None

    def _ensure_pending(self, handle: QSplitterHandle) -> None:
        """鼠标已在 handle 上但未进入 pending/hot 态时补启动延迟计时"""
        if handle is self._hot_handle or handle is self._pending_handle:
            return
        if self._hot_handle is not None:
            self._set_hot(self._hot_handle, False)
            self._hot_handle = None
        self._pending_handle = handle
        self._timer.start()

    def _flush_hot(self) -> None:
        handle = self._pending_handle
        self._pending_handle = None
        if handle is None:
            return
        try:
            rect = QRect(handle.mapToGlobal(QPoint(0, 0)), handle.size())
        except RuntimeError:
            return
        # macOS 上 underMouse() 在部分窗口类型下不可靠，改按光标位置判定
        if not rect.contains(QCursor.pos()):
            return
        self._set_hot(handle, True)
        self._hot_handle = handle

    def _set_hot(self, handle: QSplitterHandle, hot: bool) -> None:
        """切换 splitterHot 属性并触发样式重算（样式表按属性选择器上色）"""
        try:
            if bool(handle.property(PROP_HOT)) == bool(hot):
                return
            handle.setProperty(PROP_HOT, bool(hot))
            style = handle.style()
            style.unpolish(handle)
            style.polish(handle)
            handle.update()
        except RuntimeError:
            pass  # C++ 对象已销毁

    # ── 角点检测 ──

    def _splitters_for_window(self, window: QWidget) -> List[QSplitter]:
        """同一顶层窗口内的全部 splitter（按子控件树缓存，ChildAdded 时失效）"""
        cache = self._splitter_cache
        if cache is not None and cache[0] is window:
            return cache[1]
        splitters = window.findChildren(QSplitter) if window is not None else []
        self._splitter_cache = (window, splitters)
        return splitters

    def _find_corner(self, window: QWidget, global_pos: QPoint) -> Optional[Corner]:
        """global_pos 命中的角点（两条正交 handle 的交叉/相邻处）"""
        if window is None:
            return None
        splitters = self._splitters_for_window(window)
        for index_a, splitter_a in enumerate(splitters):
            try:
                if not splitter_a.isVisible():
                    continue
                orient_a = splitter_a.orientation()
                for splitter_b in splitters[index_a + 1 :]:
                    if not splitter_b.isVisible() or splitter_b.orientation() == orient_a:
                        continue
                    for idx_a in range(1, splitter_a.count()):
                        handle_a = splitter_a.handle(idx_a)
                        if handle_a is None or not handle_a.isVisible():
                            continue
                        for idx_b in range(1, splitter_b.count()):
                            handle_b = splitter_b.handle(idx_b)
                            if handle_b is None or not handle_b.isVisible():
                                continue
                            if self._hit(
                                handle_a, orient_a, handle_b, splitter_b.orientation(), global_pos
                            ):
                                return (handle_a, idx_a, splitter_a, handle_b, idx_b, splitter_b)
            except RuntimeError:
                continue
        return None

    def _hit(
        self,
        handle_a: QSplitterHandle,
        orient_a: Qt.Orientation,
        handle_b: QSplitterHandle,
        orient_b: Qt.Orientation,
        global_pos: QPoint,
    ) -> bool:
        """鼠标是否落在两条正交 handle 构成的角点方块内

        方块 = 竖线的宽度方向 × 横线的高度方向，两者各外扩「端点间距 + HIT_PAD」，
        因此容器留白（frame padding / 卡片边距）不会把角点顶到用户点不到的位置。
        """
        rect_a = _handle_rect(handle_a)
        rect_b = _handle_rect(handle_b)
        if orient_a == Qt.Horizontal:
            vertical_rect, horizontal_rect = rect_a, rect_b
        else:
            vertical_rect, horizontal_rect = rect_b, rect_a
        # 交点必须落在两条线的可视范围内（±NEAR_PAD），排除两条平行错开的线
        if not (
            vertical_rect.top() - self.NEAR_PAD
            <= horizontal_rect.center().y()
            <= vertical_rect.bottom() + self.NEAR_PAD
        ):
            return False
        if not (
            horizontal_rect.left() - self.NEAR_PAD
            <= vertical_rect.center().x()
            <= horizontal_rect.right() + self.NEAR_PAD
        ):
            return False
        # 水平方向的允许距离：竖线到横线最近端点的间距（容器留白）
        center_x = vertical_rect.center().x()
        if horizontal_rect.left() > center_x:
            gap_x = horizontal_rect.left() - center_x
        elif center_x > horizontal_rect.right():
            gap_x = center_x - horizontal_rect.right()
        else:
            gap_x = 0
        # 垂直方向同理（横线到竖线最近端点）
        center_y = horizontal_rect.center().y()
        if vertical_rect.top() > center_y:
            gap_y = vertical_rect.top() - center_y
        elif center_y > vertical_rect.bottom():
            gap_y = center_y - vertical_rect.bottom()
        else:
            gap_y = 0
        limit_x = max(vertical_rect.width() / 2.0, gap_x) + self.HIT_PAD
        limit_y = max(horizontal_rect.height() / 2.0, gap_y) + self.HIT_PAD
        return abs(global_pos.x() - center_x) <= limit_x and abs(global_pos.y() - center_y) <= limit_y

    def _corner_cursor(self, corner: Corner) -> Qt.CursorShape:
        """角点的对角光标方向：与 VSCode 的 nwse / nesw 同语义

        竖线与横线的相对位置决定拖拽时两条线的运动方向：
        竖线在左、横线在上（或反之）→ NWSE，否则 NESW。
        """
        handle_a, _, splitter_a, handle_b, _, _ = corner
        rect_a = _handle_rect(handle_a)
        rect_b = _handle_rect(handle_b)
        if splitter_a.orientation() == Qt.Horizontal:
            vertical_rect, horizontal_rect = rect_a, rect_b
        else:
            vertical_rect, horizontal_rect = rect_b, rect_a
        left_side = vertical_rect.center().x() <= horizontal_rect.center().x()
        top_side = vertical_rect.center().y() <= horizontal_rect.center().y()
        return Qt.SizeFDiagCursor if left_side == top_side else Qt.SizeBDiagCursor

    def _sync_cursor(self, obj: QWidget, global_pos: QPoint) -> None:
        """角点用对角光标；离开角点把光标还给原控件"""
        try:
            corner = self._find_corner(obj.window(), global_pos)
        except RuntimeError:
            corner = None
        if corner is None:
            if self._cursor_widget is not None:
                self._restore_cursor()
            return
        try:
            shape = self._corner_cursor(corner)
        except RuntimeError:
            return
        if obj is self._cursor_widget and shape == self._cursor_shape:
            return
        self._restore_cursor()
        try:
            obj.setCursor(shape)
            self._cursor_widget = obj
            self._cursor_shape = shape
        except RuntimeError:
            pass

    def _restore_cursor(self) -> None:
        widget = self._cursor_widget
        self._cursor_widget = None
        self._cursor_shape = None
        if widget is None:
            return
        try:
            if isinstance(widget, QSplitterHandle):
                splitter = widget.splitter()
                horizontal = splitter is not None and splitter.orientation() == Qt.Horizontal
                widget.setCursor(Qt.SplitHCursor if horizontal else Qt.SplitVCursor)
            else:
                widget.unsetCursor()
        except RuntimeError:
            pass

    # ── 角点拖动 ──

    def _begin_drag(self, obj: QWidget, global_pos: QPoint) -> bool:
        try:
            corner = self._find_corner(obj.window(), global_pos)
        except RuntimeError:
            return False
        if corner is None:
            return False
        try:
            handle_a, idx_a, splitter_a, handle_b, idx_b, splitter_b = corner
            if splitter_a.orientation() == Qt.Horizontal:
                horizontal, h_idx = splitter_a, idx_a
                vertical, v_idx = splitter_b, idx_b
            else:
                horizontal, h_idx = splitter_b, idx_b
                vertical, v_idx = splitter_a, idx_a
            self._drag = {
                "grab": obj,
                "handle_a": handle_a,
                "handle_b": handle_b,
                "origin": global_pos,
                "h_sp": horizontal,
                "h_idx": h_idx,
                "h_sizes": list(horizontal.sizes()),
                "v_sp": vertical,
                "v_idx": v_idx,
                "v_sizes": list(vertical.sizes()),
            }
            obj.grabMouse()
        except RuntimeError:
            self._drag = None
            return False
        # 拖动期间两条分割线一起点亮（用户能看到正在联动哪两条）
        self._set_hot(handle_a, True)
        self._set_hot(handle_b, True)
        self._hot_handle = handle_a
        return True

    def _drag_to(self, global_pos: QPoint) -> None:
        drag = self._drag
        if drag is None:
            return
        delta = global_pos - drag["origin"]
        try:
            self._shift_pair(drag["h_sp"], drag["h_idx"], drag["h_sizes"], delta.x())
            self._shift_pair(drag["v_sp"], drag["v_idx"], drag["v_sizes"], delta.y())
        except RuntimeError:
            self._end_drag()

    @staticmethod
    def _shift_pair(splitter: QSplitter, index: int, base_sizes: List[int], delta: int) -> None:
        """平移 handle(index) 的两侧窗格（窗格 = index-1 与 index），以拖动起始尺寸为基准"""
        if index <= 0 or index >= splitter.count():
            return
        total = base_sizes[index - 1] + base_sizes[index]
        if total <= 0:
            return
        orientation = splitter.orientation()
        min1 = _min_extent(splitter.widget(index - 1), orientation)
        min2 = _min_extent(splitter.widget(index), orientation)
        upper = max(min1, total - min2)
        target = min(max(base_sizes[index - 1] + delta, min1), upper)
        if target == base_sizes[index - 1]:
            return
        sizes = list(base_sizes)
        sizes[index - 1] = target
        sizes[index] = total - target
        splitter.setSizes(sizes)

    def _end_drag(self) -> None:
        drag = self._drag
        if drag is None:
            return
        self._drag = None
        grab = drag.get("grab")
        try:
            if grab is not None:
                grab.releaseMouse()
        except RuntimeError:
            pass
        for handle in (drag.get("handle_a"), drag.get("handle_b")):
            if handle is None:
                continue
            try:
                if not handle.underMouse():
                    self._set_hot(handle, False)
            except RuntimeError:
                pass
        self._restore_cursor()
