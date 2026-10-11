# -*- coding: utf-8 -*-
"""排队消息卡片 —— 智能体运行时用户排队的待发送消息

行为契约
--------
- 主程序通过 CardManager 显隐本卡片；队列空时主程序 hide_card；
- 视觉：卡片自绘表面（底色 + 1px 中性边框 + 圆角），页眉条独立底色，
  条目区用「序号 + 文本 + 三个图标按钮」紧凑单行；
- 每条排队消息一行：「插入」「编辑」「删除」三个图标按钮；
- 「插入」→ insertRequested(msg_id)：立即注入当前对话流（不停 worker）；
- 「编辑」→ 行内编辑，回车/失焦保存 emit editRequested(msg_id, new_text)，Esc 取消；
- 「删除」→ removeRequested(msg_id)：移出队列；
- 卡片不自管数据，set_entries 按 msg_id 增量刷新（单一数据源在 main_widget）；
- 高度精确跟随内容：提供 heightForWidth + heightChanged（见下方"高度契约"）；
- 无 TTL（区别于 UndoDeleteCard）。

设计说明（2026-09-12 视觉重做）
旧版用 ``REALTIME_BG`` 作底 + 1px 饱和蓝（``REALTIME_BORDER`` #2563eb）做全宽
行分隔线。在浅色主题下 ``realtime_bg`` 与对话区底色几乎同色（lumia: #f5f5f5 vs
#ffffff），卡片没有任何可见边界，整块区域只剩两根高饱和蓝线 —— 实测像素对比度
2.47，是画面里最抢眼的元素，于是观感就是"一堆悬浮的文字 + 两根蓝线"。

重做后：
1. 卡片自绘表面（``CARD_BG`` + ``BORDER`` + 圆角），与对话区形成明确边界；
2. 页眉独立底色（``CARD_BG_DIM``）+ 下边框，建立"卡片起止"认知；
3. 去掉饱和蓝分隔线，改用序号列 + 行 hover 底色表达条目结构；
4. 层级重排：标题 13px/600 → 计数 pill → hint 降为 11px 次要色；
   条目文本 14px 常规字重（旧版 15px bold，比标题还重）。
"""

from __future__ import annotations

from PyQt5.QtCore import Qt, QSize, pyqtSignal
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.utils.design_tokens import Colors, font_size_css
from app.utils.utils import get_font_family_css, get_icon
from app.widgets.cards.card_container import CardContainer
from app.widgets.elided_label import _ElidedLabel
from qfluentwidgets import TransparentToolButton


def summarize_entry_text(text: str, limit: int = 80) -> str:
    """排队条目摘要：压缩空白 + 单行截断"""
    collapsed = " ".join((text or "").split())
    if len(collapsed) > limit:
        return collapsed[: limit - 1] + "…"
    return collapsed


class QueueMessageCard(QWidget):
    """排队消息卡片（页眉 + 多条排队消息）"""

    # ── 视觉常量（改版集中处，便于与 UndoDeleteCard 保持同族）──
    RADIUS = 10  # 卡片圆角
    HEADER_H = 34  # 页眉行高
    ROW_H = 32  # 条目行高
    BTN_SIZE = 24  # 图标按钮边长

    insertRequested = pyqtSignal(str)  # msg_id：立即注入该条（不停 worker）
    removeRequested = pyqtSignal(str)  # msg_id：移出队列
    editRequested = pyqtSignal(str, str)  # msg_id, new_text：编辑保存
    # 内容高度变化：容器据此重算高度（add_card 已通用接线 → _schedule_expand）。
    # 不能只依赖卡片 Resize 事件——展开/折叠动画窗口内 Resize 会被容器守卫
    # 丢弃（防自激），窗口过后几何不再变化 → 容器永久锁在旧高度（多条时
    # 第 2 行起被裁切的根因）。
    heightChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._editor: QLineEdit | None = None  # 当前行内编辑器
        self._editing_id: str | None = None
        # 高度严格跟随内容：出入栈行数变化时容器高度同步收缩/展开
        # （否则 dock 模式下容器锁在首次展开高度，出栈后底部留白）
        self.setProperty(CardContainer.FOLLOW_CONTENT_PROP, True)
        # 自定义 QWidget 子类的 QSS 背景/边框需要显式开启，否则不绘制
        self.setAttribute(Qt.WA_StyledBackground, True)
        # 容器动画豁免：条目增删走 snap 直落终值。容器 200ms 高度动画在真实环境
        # （流式输出挤压 BOTTOM 停靠区 + 动画中途 stop/restart）下中间态暴露为
        # 视觉跳闪；条目变化是高频小操作，直接落位观感更稳（与 CommandCard 同款）。
        self.setProperty(CardContainer.NO_ANIMATION_PROP, True)
        self.setVisible(False)
        self._setup_ui()

    # ───────────────────────────────────────────────────────────
    # 样式
    # ───────────────────────────────────────────────────────────

    def refresh_style(self):
        """刷新样式（主题切换 / 字号变更时调用）"""
        Colors.refresh()
        # 卡片表面：自绘底色 + 中性边框 + 圆角（旧版靠 REALTIME_BG，浅色主题下不可见）
        self.setStyleSheet(f"""
            QueueMessageCard {{
                background-color: {Colors.CARD_BG.format(alpha=250)};
                border: 1px solid {Colors.BORDER};
                border-radius: {self.RADIUS}px;
            }}
        """)
        self._icon_label.setPixmap(get_icon("todo").pixmap(16, 16))
        # 页眉：浅色分区条 + 下边框，建立"卡片起止"认知
        self._header.setStyleSheet(f"""
            QWidget#queueHeader {{
                background: {Colors.CARD_BG_DIM};
                border-bottom: 1px solid {Colors.BORDER};
                border-top-left-radius: {self.RADIUS - 1}px;
                border-top-right-radius: {self.RADIUS - 1}px;
            }}
        """)
        self._title_label.setStyleSheet(f"""
            QLabel {{
                color: {Colors.TEXT_PRIMARY};
                {get_font_family_css()} {font_size_css(13)};
                font-weight: 600;
                background: transparent;
            }}
        """)
        # 计数 pill：主题色底 + 描边，替代旧版与标题同色的 "· 2 条"
        self._count_label.setStyleSheet(f"""
            QLabel#queueCount {{
                color: {Colors.TAG_ACCENT_TEXT};
                {get_font_family_css()} {font_size_css(11)};
                background: {Colors.REALTIME_TAG_BG};
                border: 1px solid {Colors.REALTIME_TAG_BORDER};
                border-radius: 8px;
                padding: 1px 6px;
            }}
        """)
        # hint 降级：11px 次要色（旧版 13px 与标题同色，主次不分）
        self._hint_label.setStyleSheet(f"""
            QLabel {{
                color: {Colors.TEXT_SECONDARY};
                {get_font_family_css()} {font_size_css(11)};
                background: transparent;
            }}
        """)
        self._refresh_rows_style()

    def _refresh_rows_style(self):
        """条目行样式（主题切换 / 字号变更时全量补刷）"""
        for row in self._rows():
            self._style_row(row)

    def _style_row(self, row: QWidget):
        """单行样式应用（新建行即时调用；复用行不重刷，避免整表 setStyleSheet 开销）"""
        # 去线：只用 hover 底色表达行结构（旧版全宽饱和蓝分隔线是画面最抢眼元素）
        row.setStyleSheet(f"""
            QWidget#queueRow {{
                background: transparent;
                border-radius: 6px;
            }}
            QWidget#queueRow:hover {{
                background: {Colors.HOVER_BG};
            }}
        """)
        idx = row.findChild(QLabel, "queueRowIndex")
        if idx is not None:
            idx.setStyleSheet(f"""
                QLabel#queueRowIndex {{
                    color: {Colors.TEXT_SECONDARY};
                    {get_font_family_css()} {font_size_css(11)};
                    background: transparent;
                }}
            """)
        for label in row.findChildren(QLabel):
            if label.objectName() == "queueRowText":
                label.setStyleSheet(f"""
                    QLabel#queueRowText {{
                        color: {Colors.TEXT_PRIMARY};
                        {get_font_family_css()} {font_size_css(14)};
                        background: transparent;
                    }}
                """)
        for edit in row.findChildren(QLineEdit):
            edit.setStyleSheet(f"""
                QLineEdit {{
                    background: {Colors.HOVER_BG};
                    border: 1px solid {Colors.TAG_ACCENT};
                    border-radius: 6px;
                    padding: 3px 8px;
                    {get_font_family_css()} {font_size_css(14)};
                    color: {Colors.TEXT_PRIMARY};
                }}
                QLineEdit:focus {{
                    border: 1px solid {Colors.TAG_ACCENT};
                }}
            """)

    def _rows(self) -> list:
        """按布局顺序返回条目行（顺序 = 发送顺序，勿用 findChildren 的任意序）"""
        rows = []
        for i in range(self._list_layout.count()):
            w = self._list_layout.itemAt(i).widget()
            if w is not None and w.objectName() == "queueRow":
                rows.append(w)
        return rows

    # ───────────────────────────────────────────────────────────
    # 构建
    # ───────────────────────────────────────────────────────────

    def _setup_ui(self):
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 页眉行
        self._header = QWidget(self)
        self._header.setObjectName("queueHeader")
        self._header.setAttribute(Qt.WA_StyledBackground, True)
        self._header.setFixedHeight(self.HEADER_H)
        header_layout = QHBoxLayout(self._header)
        header_layout.setContentsMargins(12, 0, 12, 0)
        header_layout.setSpacing(8)

        self._icon_label = QLabel(self._header)
        self._icon_label.setFixedSize(16, 16)
        self._icon_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        header_layout.addWidget(self._icon_label)

        self._title_label = QLabel("排队消息", self._header)
        self._title_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        header_layout.addWidget(self._title_label)

        self._count_label = QLabel("", self._header)
        self._count_label.setObjectName("queueCount")
        self._count_label.setFixedHeight(22)  # 胶囊独立高度，避免被页眉拉伸上下贴满
        self._count_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._count_label.setVisible(False)
        header_layout.addWidget(self._count_label)

        header_layout.addStretch()

        self._hint_label = QLabel("结束后自动发送", self._header)
        self._hint_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._hint_label.setToolTip("智能体结束后按顺序自动依次发送")
        header_layout.addWidget(self._hint_label)
        root.addWidget(self._header)

        # 条目区
        self._list_container = QWidget(self)
        self._list_layout = QVBoxLayout(self._list_container)
        self._list_layout.setContentsMargins(6, 4, 6, 6)
        self._list_layout.setSpacing(1)
        root.addWidget(self._list_container)

        # 样式必须在子控件创建后应用（refresh_style 直接引用这些属性）
        self.refresh_style()

    # ───────────────────────────────────────────────────────────
    # 高度契约（容器 follow_content 分支取高接口）
    # ───────────────────────────────────────────────────────────

    def _content_height(self) -> int:
        """按当前行数直算精确卡片高度（不读 Qt 布局缓存）

        布局口径（与 _setup_ui 一致）：页眉 HEADER_H + 条目区上下 margin +
        n 行 × ROW_H + 行间距。实测 1/2/3/5 行 = 76/109/142/208，与
        sizeHint 收敛值一致；不读 sizeHint 是刻意的——它带布局缓存，
        set_entries 同 tick 内是残值（实测 44 vs 真值 109）。
        """
        n = self._list_layout.count()
        m = self._list_layout.contentsMargins()
        spacing = self._list_layout.spacing() * max(0, n - 1)
        return self.HEADER_H + m.top() + m.bottom() + n * self.ROW_H + spacing

    def hasHeightForWidth(self):
        # 与 CommandCard 同款：供容器 _follow_content_natural_h 精确取高
        return True

    def heightForWidth(self, w):
        """按行数返回卡片目标高度（容器 follow_content 分支的直接输入）"""
        return self._content_height()

    # ───────────────────────────────────────────────────────────
    # 对外 API
    # ───────────────────────────────────────────────────────────

    def set_entries(self, entries: list):
        """按 msg_id 增量刷新条目列表：已有行复用不重建，只增删变化行

        旧实现全量销毁重建，每次入队整卡重刷（hover 态丢失 + 视觉突变）。
        现复用行控件：追加/移除时已有行零变化，编辑中的行不被打断。

        Args:
            entries: [{"id": str, "text": str}]，顺序即发送顺序
        """
        desired_ids = [str(e.get("id", "")) for e in entries]
        # 编辑中的行：仍在列表则保留编辑态；被移除才拆编辑器
        if self._editor is not None and self._editing_id not in desired_ids:
            self._teardown_editor()

        by_id: dict = {}
        for row in self._rows():
            by_id[row.property("msg_id") or ""] = row
        # 全部行先摘出布局（不销毁），随后按 entries 顺序放回
        for row in by_id.values():
            self._list_layout.removeWidget(row)

        for i, entry in enumerate(entries, 1):
            mid = str(entry.get("id", ""))
            row = by_id.pop(mid, None)
            if row is None:
                row = self._build_row(entry, i)
                self._style_row(row)
            else:
                self._update_row(row, entry, i)
            self._list_layout.addWidget(row)

        # 剩余 = 不在新列表的行，销毁
        for row in by_id.values():
            row.deleteLater()

        # 计数 pill：仅多条目时显示（单条时 "1 条" 属冗余信息）
        self._count_label.setText(f"{len(entries)} 条" if len(entries) > 1 else "")
        self._count_label.setVisible(len(entries) > 1)
        # 布局缓存失效，再主动通知容器重算：容器读 heightForWidth（按行数直算），
        # 不读 sizeHint 缓存，故本 tick 内 emit 也拿到正确新高度。
        self.updateGeometry()
        self.adjustSize()
        self.heightChanged.emit()

    def _update_row(self, row: QWidget, entry: dict, index: int):
        """复用行更新序号与文本（控件不重建，视觉零变化）"""
        row.setProperty("msg_id", str(entry.get("id", "")))
        idx_label = row.findChild(QLabel, "queueRowIndex")
        if idx_label is not None:
            idx_label.setText(str(index))
        text_label = row.findChild(QLabel, "queueRowText")
        if text_label is not None:
            raw_text = str(entry.get("text", ""))
            text_label.setText(summarize_entry_text(raw_text))  # _ElidedLabel 自动同步 tooltip
            text_label.setToolTip(raw_text)  # 覆盖摘要 tooltip，悬浮看完整原文（与 _build_row 同口径）

    def _teardown_editor(self):
        """拆除行内编辑器并还原文本 label（保留行控件，供增量复用）"""
        if self._editor is None:
            return
        editor = self._editor
        self._editor = None
        self._editing_id = None
        parent = editor.parentWidget()
        if parent is not None:
            layout = parent.layout()
            if layout is not None:
                layout.removeWidget(editor)
            label = parent.findChild(QLabel, "queueRowText")
            if label is not None:
                label.show()
        editor.deleteLater()

    def _build_row(self, entry: dict, index: int) -> QWidget:
        row = QWidget(self._list_container)
        row.setObjectName("queueRow")
        row.setAttribute(Qt.WA_StyledBackground, True)
        row.setFixedHeight(self.ROW_H)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(6, 0, 2, 0)
        layout.setSpacing(6)
        msg_id = str(entry.get("id", ""))
        raw_text = str(entry.get("text", ""))
        row.setProperty("msg_id", msg_id)

        # 序号列：替代旧版分隔线承担"条目结构"的表达
        index_label = QLabel(str(index), row)
        index_label.setObjectName("queueRowIndex")
        index_label.setFixedWidth(14)
        index_label.setAlignment(Qt.AlignCenter)
        index_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        layout.addWidget(index_label)

        text_label = _ElidedLabel(summarize_entry_text(raw_text), row)
        text_label.setObjectName("queueRowText")
        text_label.setToolTip(raw_text)  # 覆盖 _ElidedLabel 自动设的摘要 tooltip，悬浮看完整原文
        layout.addWidget(text_label, stretch=1)

        # 插入：立即注入当前对话流（不停 worker）
        insert_btn = TransparentToolButton(get_icon("邮件-发送"), row)
        insert_btn.setFixedSize(self.BTN_SIZE, self.BTN_SIZE)
        insert_btn.setIconSize(QSize(15, 15))
        insert_btn.setToolTip("立即插入当前对话流（不打断智能体）")
        insert_btn.clicked.connect(lambda _=False, mid=msg_id: self.insertRequested.emit(mid))
        layout.addWidget(insert_btn)

        # 编辑：行内编辑待发送文本
        edit_btn = TransparentToolButton(get_icon("编辑"), row)
        edit_btn.setFixedSize(self.BTN_SIZE, self.BTN_SIZE)
        edit_btn.setIconSize(QSize(15, 15))
        edit_btn.setToolTip("编辑消息")
        edit_btn.clicked.connect(lambda _=False, r=row, mid=msg_id, t=raw_text: self._begin_edit(r, mid, t))
        layout.addWidget(edit_btn)

        # 删除：移出队列
        delete_btn = TransparentToolButton(get_icon("删除"), row)
        delete_btn.setFixedSize(self.BTN_SIZE, self.BTN_SIZE)
        delete_btn.setIconSize(QSize(15, 15))
        delete_btn.setToolTip("移出队列")
        delete_btn.clicked.connect(lambda _=False, mid=msg_id: self.removeRequested.emit(mid))
        layout.addWidget(delete_btn)
        return row

    # ───────────────────────────────────────────────────────────
    # 行内编辑
    # ───────────────────────────────────────────────────────────

    def _begin_edit(self, row: QWidget, msg_id: str, current_text: str):
        """把该行文本切换为行内编辑器（回车/失焦保存，Esc 取消）"""
        if self._editor is not None:
            return
        layout = row.layout()
        label = row.findChild(QLabel, "queueRowText")
        if layout is None or label is None:
            return
        editor = QLineEdit(current_text, row)
        editor.returnPressed.connect(lambda: self._commit_edit(editor))
        editor.installEventFilter(self)
        index = layout.indexOf(label)
        layout.insertWidget(index, editor, stretch=1)
        label.hide()
        self._editor = editor
        self._editing_id = msg_id
        self._refresh_rows_style()
        editor.setFocus()
        editor.selectAll()

    def _commit_edit(self, editor: QLineEdit):
        """保存编辑（回车 / 失焦）"""
        if editor is not self._editor:
            return
        new_text = editor.text().strip()
        msg_id = self._editing_id or ""
        # 先拆编辑器还原行（行控件会被 set_entries 复用），再 emit
        self._teardown_editor()
        if new_text:
            self.editRequested.emit(msg_id, new_text)
        else:
            # 空文本视为取消：全量重建还原显示
            self.set_entries(self._entries_snapshot())

    def _cancel_edit(self):
        """取消编辑（Esc）：丢弃修改，还原显示"""
        if self._editor is None:
            return
        self._teardown_editor()
        self.set_entries(self._entries_snapshot())

    def _entries_snapshot(self) -> list:
        """从当前行还原条目快照（取消编辑时用；编辑中的行保留原文）"""
        entries = []
        for row in self._rows():
            label = row.findChild(QLabel, "queueRowText")
            if label is None:
                continue
            entries.append({"id": row.property("msg_id") or "", "text": label.toolTip()})
        return entries

    def eventFilter(self, watched, event):
        from PyQt5.QtCore import QEvent

        if event.type() == QEvent.KeyPress and watched is self._editor:
            from PyQt5.QtGui import QKeyEvent

            if event.key() == Qt.Key_Escape:
                self._cancel_edit()
                return True
        elif event.type() == QEvent.FocusOut and watched is self._editor:
            self._commit_edit(self._editor)
            return True
        return super().eventFilter(watched, event)

    # ───────────────────────────────────────────────────────────
    # 显隐（由 CardManager 调用）
    # ───────────────────────────────────────────────────────────

    def show_card(self):
        """显示卡片（容器默认隐藏，显示动作自负责）"""
        self.setVisible(True)

    def hide_card(self):
        """隐藏卡片"""
        self.setVisible(False)
