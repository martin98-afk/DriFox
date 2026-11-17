# -*- coding: utf-8 -*-
"""QSplitter 角点联动 + 悬浮延迟高亮（VSCode 式交互）

两项能力都挂在 QApplication 级事件过滤器上，对任何 QSplitter 生效
（主程序与插件里的 splitter 一视同仁，无需各自注册）：

1. **角点联动**：横向与纵向 splitter 的 handle 在几何上交叉时，交叉处构成
   一个角点。悬停变对角光标，按住拖动可同时调整两个 splitter 的尺寸 ——
   等价 VSCode 里拖拽左右分栏与上下分栏交汇点的行为。
2. **延迟高亮**：handle 悬停 ``HOVER_DELAY_MS`` 后才点亮（设置动态属性
   ``splitterHot=True``），移出立即熄灭，避免鼠标快速划过时分割线闪蓝。
   视觉由样式表里的 ``QSplitterHandle[splitterHot="true"]`` 规则决定，
   本模块只负责属性的时序。

设计约束：
- 只在 ``QSplitterHandle`` 上工作：其它对象的事件一次 isinstance 后放行，
  普通鼠标移动零额外开销；
- 不改 QSplitter 的既有拖拽：非角点区域一律放行，由 handle 自己处理；
- 不 import 任何窗口/容器类，纯 Qt 实现，插件侧同样可用。
"""

from typing import Dict, List, Optional, Tuple

from PyQt5.QtCore import QEvent, QObject, QPoint, QRect, Qt, QTimer
from PyQt5.QtWidgets import QApplication, QSplitter, QSplitterHandle, QWidget

# 点亮判定的样式表属性名（样式表作者用 QSplitterHandle[splitterHot="true"]）
PROP_HOT = "splitterHot"


def _handle_rect(handle: QSplitterHandle) -> QRect:
    """handle 的全局矩形（mapToGlobal + size）"""
    return QRect(handle.mapToGlobal(QPoint(0, 0)), handle.size())


def _min_extent(widget, orientation: Qt.Orientation) -> int:
    """窗格在该轴上的最小尺寸（与 QSplitter 排序约束同口径）"""
    if widget is None or widget.isHidden():
        return 0
    hint = widget.minimumSizeHint()
    if orientation == Qt.Horizontal:
        return max(0, widget.minimumWidth(), hint.width())
    return max(0, widget.minimumHeight(), hint.height())


class SplitterCornerController(QObject):
    """全局角点拖动 + handle 延迟高亮控制器（每进程一个）"""

    HOVER_DELAY_MS = 140
    HIT_PAD = 5  # 角点命中半径（像素，handle 很细，需要外扩）
    EDGE_PAD = 6  # 两个 handle 视觉相邻但留白的容忍间距

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._app = QApplication.instance()
        self._drag: Optional[Dict] = None
        self._pending_handle: Optional[QSplitterHandle] = None
        self._hot_handle: Optional[QSplitterHandle] = None
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
        if not isinstance(obj, QSplitterHandle):
            # 子控件增减会让 splitter 缓存失真（工作台 / 卡片随页面创建销毁）
            if event.type() == QEvent.ChildAdded and self._splitter_cache is not None:
                self._splitter_cache = None
            return False
        et = event.type()
        if et == QEvent.Enter:
            self._on_enter(obj)
        elif et == QEvent.Leave:
            self._on_leave(obj)
        elif et == QEvent.MouseButtonPress:
            if event.button() == Qt.LeftButton and self._begin_drag(obj, event.globalPos()):
                return True
        elif et == QEvent.MouseMove:
            if self._drag is not None:
                self._drag_to(event.globalPos())
                return True
            self._sync_cursor(obj, event.globalPos())
        elif et == QEvent.MouseButtonRelease:
            if self._drag is not None and event.button() == Qt.LeftButton:
                self._end_drag()
                return True
        return False

    # ── 延迟高亮 ──

    def _on_enter(self, handle: QSplitterHandle) -> None:
        self._pending_handle = handle
        self._timer.start()

    def _on_leave(self, handle: QSplitterHandle) -> None:
        if self._pending_handle is handle:
            self._timer.stop()
            self._pending_handle = None
        if self._hot_handle is handle:
            self._set_hot(handle, False)
            self._hot_handle = None

    def _flush_hot(self) -> None:
        handle = self._pending_handle
        self._pending_handle = None
        if handle is None:
            return
        try:
            if not handle.underMouse():
                return
        except RuntimeError:
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

    def _splitters_for(self, handle: QSplitterHandle) -> List[QSplitter]:
        """同一顶层窗口内的全部 splitter（按子控件树缓存，ChildAdded 时失效）"""
        window = handle.window()
        cache = self._splitter_cache
        if cache is not None and cache[0] is window:
            return cache[1]
        splitters = window.findChildren(QSplitter) if window is not None else []
        self._splitter_cache = (window, splitters)
        return splitters

    def _corner_at(
        self, handle: QSplitterHandle, global_pos: QPoint
    ) -> Optional[Tuple[QSplitterHandle, int, QSplitter]]:
        """global_pos 落在 handle 的角点区域时返回 (另一 handle, 其索引, 其 splitter)"""
        try:
            splitter = handle.splitter()
            if splitter is None:
                return None
            index = splitter.indexOf(handle)
            # QSplitterHandle 索引 0..N：0 与 N 是两端隐藏占位，1..N-1 才是分隔条
            if index <= 0 or index >= splitter.count():
                return None
            rect = _handle_rect(handle)
        except RuntimeError:
            return None
        orientation = splitter.orientation()
        for other_splitter in self._splitters_for(handle):
            try:
                if other_splitter is splitter or not other_splitter.isVisible():
                    continue
                if other_splitter.orientation() == orientation:
                    continue  # 只与正交 splitter 组成角点
                for other_index in range(1, other_splitter.count()):
                    other = other_splitter.handle(other_index)
                    if other is None or not other.isVisible():
                        continue
                    other_rect = _handle_rect(other)
                    inter = rect.intersected(other_rect)
                    if inter.isEmpty():
                        # 视觉相邻但无像素重叠（frame padding 留白）→ 容忍 EDGE_PAD
                        inter = rect.adjusted(
                            -self.EDGE_PAD, -self.EDGE_PAD, self.EDGE_PAD, self.EDGE_PAD
                        ).intersected(other_rect)
                    if inter.isEmpty():
                        continue
                    hit = inter.adjusted(-self.HIT_PAD, -self.HIT_PAD, self.HIT_PAD, self.HIT_PAD)
                    if hit.contains(global_pos):
                        return other, other_index, other_splitter
            except RuntimeError:
                continue
        return None

    def _sync_cursor(self, handle: QSplitterHandle, global_pos: QPoint) -> None:
        """角点用对角光标，普通 handle 还原该轴向的拖拽光标"""
        try:
            corner = self._corner_at(handle, global_pos)
            if corner is not None:
                want = Qt.SizeFDiagCursor
            elif handle.splitter() is not None and handle.splitter().orientation() == Qt.Horizontal:
                want = Qt.SplitHCursor
            else:
                want = Qt.SplitVCursor
            if handle.cursor().shape() != want:
                handle.setCursor(want)
        except RuntimeError:
            pass

    # ── 角点拖动 ──

    def _begin_drag(self, handle: QSplitterHandle, global_pos: QPoint) -> bool:
        corner = self._corner_at(handle, global_pos)
        if corner is None:
            return False
        try:
            splitter = handle.splitter()
            index = splitter.indexOf(handle)
            other, other_index, other_splitter = corner
            horizontal, vertical = (
                (splitter, other_splitter)
                if splitter.orientation() == Qt.Horizontal
                else (other_splitter, splitter)
            )
            h_index = index if horizontal is splitter else other_index
            v_index = index if vertical is splitter else other_index
            self._drag = {
                "handle": handle,
                "other": other,
                "origin": global_pos,
                "h_sp": horizontal,
                "h_idx": h_index,
                "h_sizes": list(horizontal.sizes()),
                "v_sp": vertical,
                "v_idx": v_index,
                "v_sizes": list(vertical.sizes()),
            }
            handle.grabMouse()
        except RuntimeError:
            self._drag = None
            return False
        # 拖动期间两条分割线一起点亮（用户能看到正在联动哪两条）
        self._set_hot(handle, True)
        self._set_hot(other, True)
        self._hot_handle = handle
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
        handle = drag["handle"]
        other = drag["other"]
        try:
            handle.releaseMouse()
        except RuntimeError:
            pass
        for h in (handle, other):
            try:
                if not h.underMouse():
                    self._set_hot(h, False)
            except RuntimeError:
                pass
        self._hot_handle = handle if _safe_under_mouse(handle) else None


def _safe_under_mouse(widget) -> bool:
    try:
        return bool(widget.underMouse())
    except RuntimeError:
        return False
