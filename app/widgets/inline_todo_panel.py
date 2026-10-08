# -*- coding: utf-8 -*-
"""卡片内嵌任务看板（InlineTodoPanel）

从右侧工作台任务区（原 ``workbench_panel.TasksPage``）迁移而来，挂载点改为
消息卡片气泡内（正文下方、页脚上方）。与旧位置的三点差异：

1. **默认折叠**：卡片空间宝贵，常态只占「分隔条 + 进度条 + 进行中常驻条」；
   用户点击分隔条（或右侧折叠按钮）展开完整清单。
2. **展开全量**：列表不限高、不内滚，展开即完整展示全部待办。
3. **无任务整区隐藏**：会话无待办时不占任何高度（与旧实现一致）。

视觉与数据契约保持原样：分隔条（图标 + 标题 + 完成统计 + 折叠按钮）、
4px 细进度条、进行中置顶常驻条（折叠态仍可见）、行式任务清单（单行省略，
悬浮看全文）。新任务到达不强制展开，不打断用户的折叠意愿。

数据入口 ``update_todos(todos)``，接 todowrite 回传的列表
（``[{status, content, priority}, ...]``）。高度变化经 ``heightChanged``
上报宿主（MessageCard 据此做外层滚动锚定补偿）。
"""

import json
from typing import Any, Dict, List, Optional

from PyQt5.QtCore import QEvent, Qt, pyqtSignal
from PyQt5.QtWidgets import QFrame, QHBoxLayout, QLabel, QProgressBar, QVBoxLayout, QWidget
from qfluentwidgets import ScrollArea, TransparentToolButton

from app.utils.design_tokens import BorderRadius, Colors, font_size_css, get_unified_scrollbar_style
from app.utils.motion import LoopTimer
from app.utils.utils import _is_current_theme_light, get_font_family_css, get_icon
from app.widgets._workbench_helpers import _SectionHeader
from app.widgets.cards.floating.sub_agent_compact_widget import _RotatingIcon
from app.widgets.elided_label import _ElidedLabel


class InlineTodoPanel(QWidget):
    """任务看板：分隔条 + 进度条 + 进行中常驻条 + 行式清单。

    视觉取舍沿用旧实现：条目用**行式（无边框）**而非卡片堆叠——任务清单通常
    条目多，每条目加边框会形成密集的"框中框"，视觉噪声重。改为默认透明、
    hover 淡背景，靠左侧状态符号的颜色区分状态；全部条目单行省略
    （_ElidedLabel，悬浮看全文），长任务不再换行撑高、把"进行中"挤出可视区。
    """

    # 面板高度变化（折叠切换 / 任务增减）→ 宿主据此重算卡片高度与外层锚定
    heightChanged = pyqtSignal()

    _PRI_LABELS = {"high": "高", "medium": "中", "low": "低"}
    # 状态 → (符号, 颜色)；pending 圆点恒为中性灰（优先级着色易误读为出错状态）
    _STATUS_META = {
        "completed": ("✓", "#3fb950"),
        "in_progress": ("◐", "#f59e0b"),
        "pending": ("●", "#6b7280"),
    }

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._collapsed = True  # 默认折叠（卡片内嵌场景）
        # 任务列表内容签名（数据未变时跳过全量重建）
        self._last_todos_sig: Optional[str] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(4)

        # ── 头部：图标 + 标题 + 统计 + 折叠按钮 ──
        self._header = _SectionHeader("任务列表", "todo", self)
        self._collapse_btn = TransparentToolButton(get_icon("展开"), self)
        self._collapse_btn.setFixedSize(22, 22)
        self._collapse_btn.setToolTip("展开任务列表")
        self._collapse_btn.clicked.connect(self._on_collapse_clicked)
        # 折叠按钮插入到 header 末尾（统计之后）
        hdr_layout = self._header.layout()
        hdr_layout.insertWidget(hdr_layout.count(), self._collapse_btn)
        # 整行 header 可点折叠（按钮自身消费点击，不会重复触发 eventFilter）
        self._header.installEventFilter(self)
        self._header.setCursor(Qt.PointingHandCursor)
        self._header.setToolTip("点击展开任务列表")
        layout.addWidget(self._header)

        # ── 进度条：细横条，显示完成比例 ──
        self._progress = QProgressBar(self)
        self._progress.setObjectName("taskProgressBar")
        self._progress.setFixedHeight(4)
        self._progress.setTextVisible(False)
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        layout.addWidget(self._progress)

        # ── 进行中常驻条：脱离列表置顶，折叠态也可见 ──
        self._running_wrap = QFrame(self)
        self._running_wrap.setObjectName("taskRunningWrap")
        self._running_wrap.setStyleSheet("background: transparent;")
        self._running_layout = QVBoxLayout(self._running_wrap)
        self._running_layout.setContentsMargins(2, 2, 2, 2)
        self._running_layout.setSpacing(1)  # 与列表行距一致（支持多条 in_progress）
        self._running_wrap.hide()
        layout.addWidget(self._running_wrap)

        # ── 任务列表（展开态可见，限高内滚）──
        self._scroll = ScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFocusPolicy(Qt.NoFocus)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)  # 展开全量展示，不内滚
        self._scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }\n" + get_unified_scrollbar_style(6)
        )
        self._list_wrap = QWidget()
        self._list_wrap.setStyleSheet("background: transparent;")
        self._scroll.setWidget(self._list_wrap)
        self._list_layout = QVBoxLayout(self._list_wrap)
        self._list_layout.setContentsMargins(0, 0, 2, 0)
        self._list_layout.setSpacing(1)  # 行式条目：紧凑行距
        self._list_layout.addStretch(1)
        self._scroll.setVisible(False)  # 默认折叠
        layout.addWidget(self._scroll)

        self.hide()  # 无任务整区隐藏
        self.refresh_style()

    # ── 折叠 ──

    def is_collapsed(self) -> bool:
        return self._collapsed

    def _set_collapsed(self, collapsed: bool) -> None:
        """内部：设置折叠态（不触发高度回调，避免递归）

        只收起任务列表；进行中常驻条不随折叠隐藏（折叠态保持可见执行中任务）。
        """
        self._collapsed = collapsed
        self._scroll.setVisible(not collapsed)
        icon = "展开" if collapsed else "折叠"
        self._collapse_btn.setIcon(get_icon(icon))
        self._collapse_btn.setToolTip("展开任务列表" if collapsed else "折叠任务列表")
        self._header.setToolTip("点击展开任务列表" if collapsed else "点击折叠任务列表")

    def eventFilter(self, obj, event) -> bool:
        """header 整行左键点击 = 折叠/展开切换（按钮自身消费点击，不冒泡到这）"""
        if obj is self._header and event.type() == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
            self._on_collapse_clicked()
            return True
        return super().eventFilter(obj, event)

    def _on_collapse_clicked(self) -> None:
        self._set_collapsed(not self._collapsed)
        self.heightChanged.emit()

    # ── 数据 ──

    def _item_fields(self, t: Any) -> "tuple[str, str, str]":
        """单条任务数据归一化：(status, content, priority)，兜存量脏数据"""
        status = (t.get("status") or "pending") if isinstance(t, dict) else "pending"
        raw = (t.get("content") if isinstance(t, dict) else t) or ""
        # 兜存量脏数据：content 非 str 时 dict/list 转 JSON 文本（QLabel 只收 str）
        content = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
        priority = ((t.get("priority") or "medium") if isinstance(t, dict) else "medium") or "medium"
        return status, content, priority

    def _clear_items(self) -> None:
        """清空列表项（其余彻底销毁）

        ★ ``deleteLater()`` 只是"预约删除"——在事件循环真正处理前，widget 仍挂在
        parent 的 children 链上并继续绘制。此前只 deleteLater 不 setParent(None)，
        旧条目残影会盖在新列表第一项上（现象：第一项重复显示上一次的最后一项）。
        """
        while self._list_layout.count():
            item = self._list_layout.takeAt(0)
            w = item.widget()
            if w is None:
                continue
            w.setParent(None)  # ★ 先断开父子关系，立即停止绘制
            w.deleteLater()
        while self._running_layout.count():
            item = self._running_layout.takeAt(0)
            w = item.widget()
            if w is None:
                continue
            w.setParent(None)
            w.deleteLater()

    def update_todos(self, todos: List[Dict[str, Any]]) -> None:
        """刷新任务列表（无任务时整区 hide；非空时显示，折叠态保持不打断）

        ★ 数据未变时跳过重建（内容签名比对）：卡片重建补推等路径会重复推送，
        任务多时逐条重建 widget 成本可观；签名相同 → 渲染结果相同，直接跳过。
        """
        todos = list(todos or [])
        sig = json.dumps(todos, ensure_ascii=False, sort_keys=True, default=str)
        if sig == self._last_todos_sig:
            return
        self._last_todos_sig = sig
        self._clear_items()
        done = sum(1 for t in todos if (t.get("status") or "pending") == "completed")
        total = len(todos)
        pct = int(round(done * 100 / total)) if total else 0

        # 头部统计 + 进度条 + 折叠按钮
        self._header.set_extra(f"{pct}% · {done}/{total}" if todos else "")
        self._progress.setValue(pct)
        self._progress.setVisible(bool(todos))
        self._collapse_btn.setVisible(bool(todos))
        self.setVisible(bool(todos))

        # 进行中置顶常驻条（折叠态也可见）；下面列表保持完整原序，不受置顶影响
        running = [t for t in todos if self._item_fields(t)[0] == "in_progress"]
        for t in running:
            _status, content, priority = self._item_fields(t)
            self._running_layout.addWidget(
                self._make_item("in_progress", content, priority, self._running_wrap, pinned=True)
            )
        self._running_wrap.setVisible(bool(running))

        # 重建列表：全部任务按原序 → 底部 stretch
        for t in todos:
            status, content, priority = self._item_fields(t)
            self._list_layout.addWidget(self._make_item(status, content, priority))
        self._list_layout.addStretch(1)

        self.heightChanged.emit()

    def _make_item(
        self, status: str, content: str, priority: str, parent: Optional[QWidget] = None, *, pinned: bool = False
    ) -> QFrame:
        """单条任务：左状态符号 + 中内容（单行省略），右侧不放任何标签

        置顶区条目（pinned=True）带琥珀竖条 + 600 字重重点样式；列表条目一律
        普通行样式（completed 划线弱化、pending 中性灰圆点），不随状态强调。
        优先级与全文合并进单一 tooltip（分设会弹两个气泡）。
        parent 缺省挂列表，进行中常驻条传自身容器。
        """
        frame = QFrame(parent or self._list_wrap)
        frame.setObjectName("taskItem")
        frame.setProperty("pinned", pinned)
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(8)

        mark, _color = self._STATUS_META.get(status, self._STATUS_META["pending"])
        if status == "in_progress":
            # 进行中：子智能体运行中同款旋转 SVG 图标（QPainter 原地旋转，无抖动）
            mark_widget: QWidget = _RotatingIcon(":/icons/执行中.svg", size=16, parent=frame)
            # 浅色主题叠加半透明黑色，避免亮背景下图标不可见（与子智能体悬浮框一致）
            mark_widget.set_tint("#88000000" if _is_current_theme_light() else None)
            # _RotatingIcon 无自驱动定时器，条目自带循环驱动。用 LoopTimer 门控：
            # 条目不可见（面板折叠 / 卡片离屏）或系统「减少动态效果」时自动跳过
            # 回调，不再在隐藏状态下空转重绘 SVG。80ms/32° 与子智能体悬浮框
            # 旋转参数一致（400°/s）；宿主取 mark_widget，条目销毁即自动停。
            _angle = 0

            def _spin_tick() -> None:
                nonlocal _angle
                _angle = (_angle + 32) % 360
                mark_widget.set_angle(_angle)

            spin_timer = LoopTimer(mark_widget, 80, _spin_tick)
            spin_timer.start()
            layout.addWidget(mark_widget)
        else:
            mark_label = QLabel(mark, frame)
            mark_label.setObjectName("taskMark")
            mark_label.setFixedWidth(16)
            mark_label.setAlignment(Qt.AlignCenter)
            layout.addWidget(mark_label)

        # 单行省略（_ElidedLabel 随宽度自动重算）：任务清单的价值是看进度而非读全文
        content_label = _ElidedLabel(content, frame)
        content_label.setObjectName("taskContent")
        # 单一 tooltip：优先级 + 全文合一（优先级设 frame、全文设 label 会先后弹两个气泡）
        content_label.setToolTip(f"优先级：{self._PRI_LABELS.get(priority, '中')}\n{content}")
        layout.addWidget(content_label, 1)

        frame.setProperty("status", status)
        frame.setProperty("priority", priority)
        self._apply_item_style(frame)
        return frame

    def _apply_item_style(self, frame: QFrame) -> None:
        """应用条目样式（行式：默认透明，hover 淡背景；靠状态符号着色）"""
        status = frame.property("status") or "pending"
        _mark, color = self._STATUS_META.get(status, self._STATUS_META["pending"])

        frame.setStyleSheet(
            "QFrame#taskItem { background: transparent; border: none;"
            f" border-left: 3px solid transparent; border-radius: {BorderRadius.SM}; }}"
            "QFrame#taskItem:hover {"
            f" background: {Colors.HOVER_BG}; }}"
            # 置顶区条目：左琥珀竖条；淡琥珀底由置顶容器承担，列表行不带强调
            'QFrame#taskItem[pinned="true"] {'
            f" border-left-color: {Colors.ACCENT_WARM}; }}"
        )

        mark_label = frame.findChild(QLabel, "taskMark")
        if mark_label is not None:
            mark_label.setStyleSheet(
                f"color: {color}; background: transparent; font-weight: 700;"
                f" {get_font_family_css()} {font_size_css(13)};"
            )

        content_label = frame.findChild(QLabel, "taskContent")
        if content_label is not None:
            if status == "completed":
                line, c, weight = "text-decoration: line-through;", Colors.TEXT_MUTED, "normal"
            elif frame.property("pinned"):
                line, c, weight = "", Colors.TEXT_PRIMARY, "600"
            else:
                line, c, weight = "", Colors.TEXT_PRIMARY, "500"
            content_label.setStyleSheet(
                f"color: {c}; background: transparent; {line}"
                f" font-weight: {weight};"
                f" {get_font_family_css()} {font_size_css(12)};"
            )

    def refresh_style(self) -> None:
        """主题刷新（宿主在主题切换时调用）"""
        self._header.refresh_style()
        # 置顶常驻区：淡琥珀底卡，与下方普通列表视觉区分
        self._running_wrap.setStyleSheet(
            "QFrame#taskRunningWrap { background: rgba(245, 158, 11, 0.10);"
            f" border-radius: {BorderRadius.SM}; }}"
        )
        # 进度条：轨道 = BORDER 色，已完成块 = completed 绿
        self._progress.setStyleSheet(
            "QProgressBar#taskProgressBar {"
            f" background: {Colors.BORDER};"
            " border: none;"
            " border-radius: 2px;"
            " }"
            "QProgressBar#taskProgressBar::chunk {"
            f" background: {self._STATUS_META['completed'][1]};"
            " border-radius: 2px;"
            " }"
        )
        for lay in (self._running_layout, self._list_layout):
            for i in range(lay.count()):
                w = lay.itemAt(i).widget()
                if isinstance(w, QFrame) and w.objectName() == "taskItem":
                    self._apply_item_style(w)
