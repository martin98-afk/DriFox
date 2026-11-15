# -*- coding: utf-8 -*-
"""file-tree 吸顶滚动 — 内嵌式（不遮挡内容）

背景：树滚下去之后根目录行被顶出可视区，用户想折叠当前所在目录
必须先滚回顶部找它（或一路折叠上级）。本模块把「顶部可见行的祖先链」
画在树**上方的独立一行区域**里，每层可直接折叠 / 定位 / 右键。

为什么是「布局级内嵌」而不是覆盖层：
- 覆盖层盖住最上面几行，正是用户最想看的内容；用户明确要求不要遮挡。
- 也不能用 ``QAbstractScrollArea.setViewportMargins`` 给树抠顶部留白：
  实测本机 Qt 下对 QTreeView 不生效（reserved=40 时 viewport.geometry
  与 reserved=0 完全相同），配滚动补偿就成了「留白不生效但滚动被推走」，
  表现为往上滚卡住、反复抖动。
- 结论：吸顶条必须是**真实占位**的兄弟控件。由 cards.py 把它和树放进
  同一个垂直布局，条高多少，树就少多少，天然不遮挡。

占位带来一条硬约束：条高变化会同时改变视口高度与滚动范围。为了让内容在
屏幕上「零位移」，条高每变 ``d``，滚动值必须同步变 ``d``（双向对称补偿，
见 ``_apply_height``），否则判据漂移会自激成抖动。滚轮与滚动条拖拽的
输入路径不同，处理方式也不同：
- 滚轮（SmoothScroll 引擎逐帧投递增量）：补偿照做，双方叠加后视觉位移
  等于用户真实滚动量。
- 拖拽滚动条（绝对定位，value 由鼠标位置映射）：补偿一改 value，下一帧
  鼠标移动就把它推翻 → 高频拉扯。所以拖拽期间**冻结刷新**，松手后一次性
  重算（见 ``_is_dragging``）。

行绘制不用 ``QTreeView.drawRow``：它按 ``visualRect`` 的 viewport 绝对坐标
算缩进与分支箭头，且会按 selectionModel 注入选中态 —— 会把某一行画成
琥珀高亮块、缩进错位。这里自绘，但缩进/图标尺寸/字体全部取自树本身，
保证与列表观感一致。
"""

import time
from typing import Dict, List, Optional

from PyQt5.QtCore import QEvent, QModelIndex, QObject, QPoint, QRect, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QPainter, QPen, QPolygon
from PyQt5.QtWidgets import QAbstractItemView, QApplication, QScrollBar, QTreeView, QWidget

from .tree_widget import FileTreeModel

# ── 几何常量 ──────────────────────────────────────────────

MAX_ROWS = 5  # VS Code stickyScroll.maxLineCount 默认值


class StickyScrollOverlay(QWidget):
    """树顶部的祖先链吸顶条（占位控件：条高多少，树就少多少）"""

    # (proxy QModelIndex, 屏幕坐标) — 由宿主映射回 source 后走既有右键链路
    context_menu_requested = pyqtSignal(QModelIndex, object)

    def __init__(self, tree: QTreeView, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("file-tree-sticky")
        # 背景透明：本控件与树同处卡片背景之上，自填任何颜色都会叠成两层
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAutoFillBackground(False)
        self.setFocusPolicy(Qt.NoFocus)
        self.setMouseTracking(True)

        self._tree = tree
        # ★ 缓存 viewport 的 Python 引用：eventFilter 里只做身份比较，
        #   绝不调 ``self._tree.viewport()``。树析构期（先销毁 viewport、
        #   再销毁树体）过滤器仍会被调用一次，此刻任何 C++ 调用都会变成
        #   悬空指针解引用 —— 实测 exit code 3221226505（0xC0000005），无
        #   Python 栈、无 faulthandler 输出，纯原生崩溃。
        self._vp = tree.viewport()
        self._rows: List[QModelIndex] = []
        self._row_h = 0
        self._sig: tuple = ()
        self._hover_row = -1
        self._model: Optional[object] = None
        # 拖拽滚动条期间冻结刷新（见 _is_dragging）
        self._drag = False
        self._drag_tick = 0.0
        self._smooth_bar = None
        self._owed = 0  # 滚动范围未更新时欠下的补偿量（见 _apply_height）

        self.setMinimumHeight(0)
        self.setMaximumHeight(0)

        # 主题（由 cards.py 注入）
        self._border = QColor(255, 255, 255, 26)
        self._fg = QColor(255, 255, 255)
        self._fg_secondary = QColor(170, 170, 170)
        self._hover_bg = QColor(255, 255, 255, 16)

        # 刷新合并：滚动 per-pixel，一条滚动手势能打出几十次 valueChanged，
        # 每次重算祖先链 + 重绘没必要 —— 一律排队到下一轮事件循环。
        self._pending = QTimer(self)
        self._pending.setSingleShot(True)
        self._pending.setInterval(0)
        self._pending.timeout.connect(self._do_refresh)

        self._tree.verticalScrollBar().valueChanged.connect(self.schedule_update)
        self._tree.verticalScrollBar().rangeChanged.connect(self._on_range_changed)
        self._tree.expanded.connect(self.schedule_update)
        self._tree.collapsed.connect(self.schedule_update)
        self._bind_drag_signals(tree)
        if self._vp is not None:
            self._vp.installEventFilter(self)

        self.hide()

    def _bind_drag_signals(self, tree: QTreeView) -> None:
        """接滚动条的拖拽信号（原生 QScrollBar + qfluentwidgets 自绘条）

        ★ 拖拽是**绝对定位**：value 由鼠标位置映射。高度变化一改 max、补偿
          一改 value，下一次鼠标移动就会把它们推翻，形成高频拉扯（用户实测
          「选中滚轮上下拖拽明显抖动」）。所以拖拽期间冻结刷新，松手后一次性
          重算。
        ★ 只靠 duck typing 找条，不 import qfluentwidgets：SmoothScrollDelegate
          会用自绘条接管交互，原生条只被动跟随，两者都要接。
        """
        try:
            children = tree.findChildren(QObject)
        except RuntimeError:
            return
        for obj in children:
            if not hasattr(obj, "sliderPressed"):
                continue
            try:
                if obj.orientation() != Qt.Vertical:
                    continue
                obj.sliderPressed.connect(self._on_slider_pressed)
                obj.sliderReleased.connect(self._on_slider_released)
            except (TypeError, RuntimeError, AttributeError):
                continue
            if not isinstance(obj, QScrollBar):
                self._smooth_bar = obj  # 自绘条（真正的交互对象）

    def _on_slider_pressed(self) -> None:
        self._drag = True
        self._touch_drag()

    def _on_slider_released(self) -> None:
        self._drag = False
        self.schedule_update()

    def _on_range_changed(self, *_args) -> None:
        """滚动范围更新（高度变化的布局副作用已完成）→ 补欠账 + 重算"""
        self._flush_owed()
        self.schedule_update()

    def _touch_drag(self) -> None:
        self._drag_tick = time.monotonic()

    def _is_dragging(self) -> bool:
        """拖拽中？状态位 + 事件续期，**不用** ``isSliderDown()``

        ★ qfluentwidgets 的 ``ScrollBar.setSliderDown(False)`` 内部把
          ``_isPressed`` 恒置 True（源码如此），``isSliderDown()`` 永远返回
          True → 拿它做判据会让刷新永久冻结。
        ★ 拖拽中持续有 valueChanged 进来，据此续期；2 秒无事件自动解除，
          漏收 sliderReleased（窗口失焦 / 热重载）时也能自愈。
        """
        if not self._drag:
            return False
        if time.monotonic() - self._drag_tick > 2.0:
            self._drag = False
            return False
        return True

    def sizeHint(self):  # noqa: N802 (Qt 命名)
        """高度由 maximumHeight 决定（动画操作的就是这个属性）"""
        from PyQt5.QtCore import QSize

        return QSize(super().sizeHint().width(), self.maximumHeight())

    # ── 绑定 / 主题 ──

    def bind_model(self, model) -> None:
        """绑定视图模型（proxy）—— 行增删 / 重置都要重算祖先链

        可重复调用：换 model 时先断开旧连接，避免同一刷新触发多次。
        ★ ``rowsRemoved`` / ``modelReset`` / ``layoutChanged`` 必须**同步**
        清空持有的索引：三者都会让 ``internalPointer`` 指向已销毁节点，
        排队刷新期间只要发生一次重绘，``data()`` 就会解引用野指针（原生
        崩溃，无 Python 栈）。插入不会让既有索引失效，仍走排队。
        """
        old = self._model
        if old is not None:
            for sig_name in ("rowsInserted", "rowsRemoved", "modelReset", "layoutChanged"):
                sig = getattr(old, sig_name, None)
                if sig is None:
                    continue
                for slot in (self.schedule_update, self._drop_rows):
                    try:
                        sig.disconnect(slot)
                    except (TypeError, RuntimeError):
                        pass  # 未连接过 / 底层对象已销毁
        self._model = model
        if model is not None:
            model.rowsInserted.connect(self.schedule_update)
            model.rowsRemoved.connect(self._drop_rows)
            model.modelReset.connect(self._drop_rows)
            model.layoutChanged.connect(self._drop_rows)
        self._drop_rows()

    def _drop_rows(self, *_args) -> None:
        """同步丢弃持有的索引（失效索引不可留到下个事件循环）"""
        self._rows = []
        self._hover_row = -1
        self._sig = ()
        self.setMaximumHeight(0)
        self.hide()
        self.schedule_update()

    def apply_colors(self, colors: Dict[str, object]) -> None:
        """注入主题色（cards.py 的 _colors 字典）

        ★ 不取也不画背景色：条与树同在卡片背景上，背景透明即与列表同色。
        """
        if not colors:
            return
        border = QColor(colors.get("border", QColor(255, 255, 255, 26)))
        self._border = border if border.isValid() else QColor(255, 255, 255, 26)
        fg = QColor(colors.get("text", QColor(255, 255, 255)))
        self._fg = fg if fg.isValid() else QColor(255, 255, 255)
        fg2 = QColor(colors.get("text_secondary", QColor(170, 170, 170)))
        self._fg_secondary = fg2 if fg2.isValid() else QColor(170, 170, 170)
        is_dark = bool(colors.get("is_dark", True))
        self._hover_bg = QColor(255, 255, 255, 16) if is_dark else QColor(0, 0, 0, 10)
        self._sig = ()  # 主题变了必须重绘，签名失效
        self.schedule_update()

    # ── 刷新 ──

    def schedule_update(self, *_args) -> None:
        """排队一次刷新（信号槽可能是任意签名，一律吞掉多余参数）"""
        if self._drag:
            self._touch_drag()  # 拖拽中持续有滚动事件 → 续期（见 _is_dragging）
        if not self._pending.isActive():
            self._pending.start()

    def _do_refresh(self) -> None:
        vp = self._vp
        if vp is None or self._tree is None:
            return
        # ★ 拖拽滚动条期间冻结：拖拽是绝对定位，改 value / 改高度都会与
        #   下一帧的鼠标映射互相推翻，表现为拖不动、来回弹。松手后一次性重算。
        if self._is_dragging():
            return
        # ★ 整段包 RuntimeError：树/视口可能在排队期间被销毁（插件卸载、
        #   热重载），此时任何 C++ 调用都抛 "wrapped C/C++ object has been
        #   deleted"。不捕获就是 Qt 事件循环异常 → 下一轮 paint 解引野指针。
        try:
            self._refresh_now(vp)
        except RuntimeError:
            self._rows = []
            self._sig = ()
            self.setMaximumHeight(0)
            self.hide()

    def _refresh_now(self, vp: QWidget) -> None:
        row_h = self._measure_row_height()
        self._flush_owed()  # 先补上布局延迟造成的补偿欠账（见 _apply_height）
        rows = self._collect_ancestors(MAX_ROWS, row_h)
        # ★ 再加一道高度兜底：吸顶条真实占位，行数过多会把树挤没
        #   （实测视口 90px + 3 行 120px → 树被压成 0 高，内容看不见）。
        #   最多占树的 2/3，至少给列表留一行。
        if row_h > 0 and len(rows) > 1:
            try:
                tree_h = self._tree.height()
            except RuntimeError:
                tree_h = 0
            if tree_h > 0:
                max_rows_fit = max(1, (tree_h * 2 // 3) // row_h)
                if len(rows) > max_rows_fit:
                    rows = rows[-max_rows_fit:]
        # ★ 不按滚动值裁剪行数：裁剪会让 ``_rows`` 缩短，下一次判定又因
        #   滞回把旧行重新纳入，形成 148↔74 的振荡（实测）。滚动值不足时
        #   由 ``_apply_height`` 直接放行收缩（最多一行的收尾位移）。
        sig = (
            tuple(r.data(FileTreeModel.COL_PATH) for r in rows),
            tuple(bool(self._tree.isExpanded(r)) for r in rows),
            row_h,
            self.width(),
        )
        if sig == self._sig:
            return  # 内容没变：滚动高频路径直接返回

        self._sig = sig
        self._rows = rows
        self._row_h = row_h

        height = row_h * len(rows)
        if height <= 0:
            self._hover_row = -1
            self._apply_height(0)
            self.hide()
            return
        self.show()
        self._apply_height(height)
        self.update()

    # ── 高度变更（双向对称补偿） ──

    def _apply_height(self, target: int) -> None:
        """调整条高，并让滚动值等量同步（双向对称补偿）

        判据是 ``visualRect.top() < -行高``（行**完全**滚出视口顶部）。
        条高变 ``d`` 时滚动值同步变 ``d``，``itemY - 滚动值`` 恒定 →
        判据结论在高度变化前后完全一致，不会「收缩 → 阈值放宽 → 又该出现
        → 再长高」。

        ★ 为什么收缩也必须补偿（历史 bug）：只补偿增长时，收缩后阈值放宽，
          刚退出的层在下一帧重新满足条件，高度在 51↔68 之间反复横跳；实测
          滚轮向上 6 格，滚动值只从 268 走到 267（位移全被吃掉）且 27 帧
          内容反向跳动。
        ★ 增长为什么记欠账：``setMaximumHeight`` 后 QTreeView 的滚动范围是
          异步重算的，此时 ``setValue`` 会被**旧 max** 夹取（实测 34 夹到
          34，高度已涨 57）→ ``S - h`` 漂移 → 下一帧误判。这里把未生效的
          量记下来，等 ``rangeChanged`` 或下一次刷新再补。
        """
        target = max(0, int(target))
        current = self.maximumHeight()
        if target == current:
            return
        want = target - current
        before = self._scroll_value()
        bar = self._scroll_bar()
        if bar is None:
            self.setMaximumHeight(target)
            return
        if want < 0:
            # 收缩：只退到最小值，退不动的部分放行（滚动值到 0 时顶部行全部
            # 可见，判据不可能再判出该层，不会来回）
            applied = min(-want, max(0, before))
            self.setMaximumHeight(target)
            if applied:
                self._set_scroll_value(before - applied)
            return

        self.setMaximumHeight(target)
        self._set_scroll_value(before + want)
        eff = self._scroll_value() - before
        if eff < want:
            self._owed += want - eff  # 布局未更新，等 rangeChanged 补

    def _flush_owed(self) -> None:
        """补上「滚动范围未及时更新」欠下的补偿量（保持 S - h 同步）"""
        owed, self._owed = self._owed, 0
        if owed:
            self._set_scroll_value(self._scroll_value() + owed)

    def _scroll_value(self) -> int:
        bar = self._scroll_bar()
        return bar.value() if bar is not None else 0

    def _set_scroll_value(self, value: int) -> None:
        bar = self._scroll_bar()
        if bar is None:
            return
        try:
            bar.setValue(max(bar.minimum(), min(bar.maximum(), int(value))))
        except RuntimeError:
            pass

    def _scroll_bar(self):
        try:
            return self._tree.verticalScrollBar() if self._tree is not None else None
        except RuntimeError:
            return None

    def _collect_ancestors(self, limit: int, row_h: int = 0) -> List[QModelIndex]:
        """视口顶部可见行的祖先目录链（浅→深），只保留已**完全滚出**视口顶部的展开层

        ★ 基线是 ``-row_h``（行完全滚出），不是 ``-条高``。用条高作基线时，
          滞后量会随吸顶行数线性放大：条高 3 行时要滚出 3 行才吸顶第 1 行，
          表现为「行数突然成批冒出」，与 VS Code 的逐行精准对齐差距很大。
        ★ 自洽：双向补偿保证 ``itemY - S`` 在高度变化前后恒定，所以本判据
          是滚动位置的单值函数，不受条高影响，不会自激。
        ★ 滞回带（slack）：已吸顶的行用宽松阈值（难退出），未吸顶的用严格
          阈值（难进入）。滚动值 clamp、行高取整等 ±1~2px 误差都会让临界行
          反复进出，滞回把切换点拉开。
        """
        idx = self._top_index()
        if not idx.isValid():
            return []
        chain: List[QModelIndex] = []
        cur = idx
        while cur.isValid():
            if bool(cur.data(FileTreeModel.COL_IS_DIR)):
                chain.append(cur)
            cur = cur.parent()
        chain.reverse()  # 根在前
        if row_h <= 0:
            row_h = self._row_h or self._measure_row_height()
        base = -max(1, row_h)
        slack = max(2, row_h // 3)
        old_rows = self._rows
        kept: List[QModelIndex] = []
        for depth, i in enumerate(chain):
            try:
                if not self._tree.isExpanded(i):
                    continue
                top = self._tree.visualRect(i).top()
            except RuntimeError:
                continue
            # 已吸顶的行用宽松阈值（难退出），新行用严格阈值（难进入）
            threshold = base + slack if i in old_rows else base - slack
            if top < threshold:
                kept.append(i)
                continue
            # ★ 兄弟接替：新行还没完全滚出，但同深度的旧行（上一层的目录）
            #   已经整行滚出视口。此时直接丢掉旧行会让条高掉一整行再补回来
            #   （实测 d0/a → d0/b 切换瞬间 34→17→34，用户感受为抖动）。
            #   旧行继续站到新行接手为止；向上滚回时旧行会重新进入视口
            #   （top + row_h > 0），自然不满足而退出，不会滞留。
            if depth < len(old_rows):
                prev = old_rows[depth]
                try:
                    if self._tree.isExpanded(prev) and (
                        self._tree.visualRect(prev).top() + row_h <= 0
                    ):
                        kept.append(prev)
                except RuntimeError:
                    pass
        if len(kept) > limit:
            kept = kept[-limit:]  # 截断保留离内容最近的深几层
        return kept

    def _top_index(self) -> QModelIndex:
        """视口顶部第一行"""
        vp_h = self._vp.height() if self._vp is not None else 0
        for dy in range(0, max(1, vp_h)):
            idx = self._tree.indexAt(QPoint(2, dy))
            if idx.isValid():
                return idx
        return QModelIndex()

    def _measure_row_height(self) -> int:
        """取真实行高（高 DPI / 自定义字号下不能写死）"""
        for idx in list(self._rows) + [self._tree.indexAt(QPoint(2, 2))]:
            if idx.isValid():
                rect = self._tree.visualRect(idx)
                if rect.height() > 0:
                    return rect.height()
        h = self._tree.sizeHintForRow(0)
        if h > 0:
            return h
        return max(16, self._tree.fontMetrics().height() + 8)

    # ── 绘制 ──

    def paintEvent(self, event):  # noqa: N802 (Qt 命名)
        if not self._rows or self._row_h <= 0:
            return
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.TextAntialiasing, True)
            painter.setRenderHint(QPainter.Antialiasing, True)
            # 不填背景：与树同在卡片背景上，自填会叠成第二层
            painter.setFont(self._tree.font())

            for row, idx in enumerate(self._rows):
                if not idx.isValid():
                    continue  # 兜底：极端时序下的失效索引不画（也不解引用）
                y = row * self._row_h
                if row == self._hover_row:
                    painter.fillRect(QRect(0, y, self.width(), self._row_h), self._hover_bg)
                self._draw_row(painter, idx, row, y)

            # 与下方内容的分界线（主题边框色，极淡，避免吸顶区「悬空」）
            pen = QPen(self._border)
            pen.setWidth(1)
            painter.setPen(pen)
            painter.drawLine(0, self.height() - 1, self.width(), self.height() - 1)
        finally:
            painter.end()

    def _draw_row(self, painter: QPainter, idx: QModelIndex, row: int, y: int) -> None:
        """画一行吸顶目录（缩进 / 箭头 / 图标 / 文字）

        几何全部对齐原生行：缩进 = ``tree.indentation()``、图标 =
        ``tree.iconSize()``、字体 = ``tree.font()``。层序即缩进级别（最外层
        为 0），因此吸顶区自身的层级相对关系与列表一致。
        """
        tree = self._tree
        try:
            indent = row * tree.indentation()
            arrow_w = tree.indentation()
            icon_size = tree.iconSize()
            icon_w = icon_size.width() if icon_size.width() > 0 else 18
            expanded = bool(tree.isExpanded(idx))
        except RuntimeError:
            return

        row_h = self._row_h
        mid_y = y + row_h // 2
        self._paint_arrow(painter, QRect(indent, y, arrow_w, row_h), expanded)

        icon_x = indent + arrow_w
        icon = idx.data(Qt.DecorationRole)
        if icon is not None and not icon.isNull():
            icon.paint(painter, QRect(icon_x, mid_y - icon_w // 2, icon_w, icon_w))

        text_x = icon_x + icon_w + 6
        avail = self.width() - text_x - 8
        if avail <= 0:
            return
        name = idx.data(Qt.DisplayRole) or ""
        painter.setPen(self._fg)
        painter.drawText(
            QRect(text_x, y, avail, row_h),
            Qt.AlignVCenter | Qt.AlignLeft,
            painter.fontMetrics().elidedText(str(name), Qt.ElideRight, avail),
        )

    def _paint_arrow(self, painter: QPainter, rect: QRect, expanded: bool) -> None:
        """折叠箭头：展开为下三角，收起为右三角（居中于缩进槽）"""
        cx = rect.x() + rect.width() // 2
        cy = rect.y() + rect.height() // 2
        poly = QPolygon()
        if expanded:
            poly.append(QPoint(cx - 3, cy - 2))
            poly.append(QPoint(cx + 3, cy - 2))
            poly.append(QPoint(cx, cy + 2))
        else:
            poly.append(QPoint(cx - 2, cy - 3))
            poly.append(QPoint(cx + 2, cy))
            poly.append(QPoint(cx - 2, cy + 3))
        painter.save()
        painter.setPen(Qt.NoPen)
        painter.setBrush(self._fg_secondary)
        painter.drawPolygon(poly)
        painter.restore()

    # ── 交互 ──

    def _row_at(self, y: int) -> int:
        if self._row_h <= 0:
            return -1
        row = y // self._row_h
        return row if 0 <= row < len(self._rows) else -1

    def _is_arrow_zone(self, row: int, pos: QPoint) -> bool:
        """是否点在分支箭头槽上（与 ``_draw_row`` 的缩进同一口径）"""
        if not (0 <= row < len(self._rows)):
            return False
        try:
            arrow_w = self._tree.indentation()
        except RuntimeError:
            return False
        zone = QRect(row * arrow_w, 0, arrow_w, self._row_h)
        return zone.contains(QPoint(pos.x(), self._row_h // 2))

    def mouseMoveEvent(self, event):  # noqa: N802 (Qt 命名)
        row = self._row_at(event.pos().y())
        if row != self._hover_row:
            self._hover_row = row
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):  # noqa: N802 (Qt 命名)
        if self._hover_row != -1:
            self._hover_row = -1
            self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):  # noqa: N802 (Qt 命名)
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return
        row = self._row_at(event.pos().y())
        if row < 0 or row >= len(self._rows):
            super().mousePressEvent(event)
            return
        idx = self._rows[row]
        if not idx.isValid():
            super().mousePressEvent(event)
            return
        try:
            if self._is_arrow_zone(row, event.pos()):
                self._tree.setExpanded(idx, not self._tree.isExpanded(idx))
                self.schedule_update()
            else:
                self._tree.setCurrentIndex(idx)
                self._tree.scrollTo(idx, QAbstractItemView.EnsureVisible)
        except RuntimeError:
            pass
        event.accept()

    def wheelEvent(self, event):  # noqa: N802 (Qt 命名)
        """滚轮转给树：吸顶条在树上方，鼠标停在条上也要能滚列表"""
        vp = self._vp
        if vp is None:
            event.ignore()
            return
        event.accept()
        QApplication.sendEvent(vp, event)

    def contextMenuEvent(self, event):  # noqa: N802 (Qt 命名)
        row = self._row_at(event.pos().y())
        if row < 0 or row >= len(self._rows) or not self._rows[row].isValid():
            super().contextMenuEvent(event)
            return
        self.context_menu_requested.emit(self._rows[row], event.globalPos())
        event.accept()

    def eventFilter(self, obj, event):  # noqa: N802 (Qt 命名)
        """视口尺寸变化 → 重算宽度与内容（签名含宽度，会触发重绘）

        ★ 只做 Python 侧身份比较，不碰任何 C++ 成员：树析构期本方法仍会
          被调用，此时 ``self._tree.viewport()`` 已是悬空指针。
        """
        if obj is self._vp and event.type() == QEvent.Resize:
            self.schedule_update()
        return False

    # ── 调试 / 测试辅助 ──

    def rows(self) -> List[QModelIndex]:
        """当前吸顶的目录索引（浅→深）"""
        return list(self._rows)

    def row_height(self) -> int:
        return self._row_h

    def rows_height(self) -> int:
        """当前吸顶区目标高度（= 行数 × 行高；动画期间与 height() 不同）"""
        return self.maximumHeight()

    def row_paths(self) -> List[str]:
        """当前吸顶的目录路径（供测试断言，避免跨模块比 index）"""
        paths: List[str] = []
        for idx in self._rows:
            path = idx.data(FileTreeModel.COL_PATH)
            if path:
                paths.append(str(path))
        return paths
