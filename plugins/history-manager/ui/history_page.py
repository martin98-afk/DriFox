# -*- coding: utf-8 -*-
"""历史会话页（workbench_tab：``page_id="history-manager"``）

原为 ``app/widgets/workbench_panel.HistoryPage``（宿主内置页），现随
``history-manager`` 插件迁出：形态不变（历史会话 / 归档 子页签 + 列表上方
搜索框 + 右端导入按钮），但**自带** ``HistoryCard``（会话列表本体），不再由
宿主窗口 attach。

数据与操作仍由宿主窗口驱动：
- 列表数据：``win._refresh_history_toggle_panel()`` / ``win._refresh_archived_sessions()``
- 会话操作：卡片信号 → ``win._archive_history_session`` 等

本页通过 ``TabManagerWindow.get_current_window()`` 解析当前活跃窗口，因此
同一份页面天然跟随活跃窗口投影（宿主 ``refresh_workbench`` 会驱动重刷）。

接口契约（宿主 ``MainWidget._history_card`` 代理读取）：
- ``tabChanged`` / ``closed`` / ``set_current_tab`` / ``set_search_handler`` /
  ``set_extra_button_handler`` / ``_search_input`` / ``_current_tab`` /
  ``set_opacity`` / ``content_layout`` / ``refresh_style``
"""

from typing import Any, List, Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import QHBoxLayout, QLineEdit, QVBoxLayout, QWidget
from qfluentwidgets import ScrollArea, TransparentToolButton

from app.utils.design_tokens import Colors, font_size_css, get_unified_scrollbar_style
from app.utils.utils import get_font_family_css, get_icon
from app.widgets._workbench_helpers import _EmptyHint
from app.widgets.custom_title_bar import CustomTabButton


def _active_window() -> Optional[Any]:
    """解析当前活跃聊天窗口（宿主 ``OpenAIChatToolWindow``）；不可用时 None"""
    try:
        from app.widgets.tab_manager_window import TabManagerWindow

        tm = TabManagerWindow.get_instance()
        if tm is None:
            return None
        return tm.get_current_window()
    except Exception:
        return None


class HistoryPage(QWidget):
    """历史会话页（一级页签）：历史会话 / 归档 子页签 + 列表上方搜索框"""

    closed = pyqtSignal()  # 兼容契约：页内无关闭钮，保留信号位（宿主连接不失效）
    tabChanged = pyqtSignal(str)  # 子页签切换（history / archived）

    SUB_TABS = (("history", "历史会话"), ("archived", "归档"))

    def __init__(self, parent=None, context: Optional[dict] = None):
        super().__init__(parent)
        from .history_card import HistoryCard

        self._context = context or {}
        self._content: Optional[QWidget] = None
        self._current_tab = "history"
        self._search_input: Optional[QLineEdit] = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # ── 行1：子页签（居中）+ 右端导入按钮 ──
        tabs_row = QHBoxLayout()
        tabs_row.setSpacing(2)
        tabs_row.addStretch(1)
        self._sub_buttons: List[CustomTabButton] = []
        for tab_id, label in self.SUB_TABS:
            btn = CustomTabButton(tab_id, label, self)
            btn.clicked.connect(self._on_sub_tab_clicked)
            tabs_row.addWidget(btn)
            self._sub_buttons.append(btn)
        tabs_row.addStretch(1)
        self._import_btn = TransparentToolButton(get_icon("导入"), self)
        self._import_btn.setFixedSize(26, 26)
        self._import_btn.setToolTip("导入会话")
        self._import_btn.hide()  # handler 注入前隐藏
        tabs_row.addWidget(self._import_btn)
        layout.addLayout(tabs_row)

        # ── 行2：搜索框（列表之上，整行） ──
        self._search_input = QLineEdit(self)
        self._search_input.setPlaceholderText("🔍 搜索会话...")
        self._search_input.setFixedHeight(24)
        self._apply_search_style()
        layout.addWidget(self._search_input)

        # ── 内容占位（attach 后隐藏） ──
        self._hint = _EmptyHint("历史会话未加载", self)
        layout.addWidget(self._hint, 1)

        # ── 内容滚动区：scroll_area > content_widget > content_layout。
        #    ★ HistoryCard 自己无布局，条目经 get_content_layout() 沿父链上溯
        #    找 content_layout 属性后直接插入，去掉滚动容器会被压缩成一条条。
        self._scroll_area = ScrollArea(self)
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        Colors.refresh()
        self._scroll_area.setStyleSheet(
            "QScrollArea { border: none; background: transparent; }"
            "QScrollArea > QWidget > QWidget { background: transparent; }" + get_unified_scrollbar_style(8)
        )
        self._content_widget = QWidget()
        self._content_widget.setStyleSheet("background: transparent;")
        self._content_layout = QVBoxLayout(self._content_widget)
        self._content_layout.setContentsMargins(4, 2, 4, 2)
        self._content_layout.setSpacing(4)
        self._scroll_area.setWidget(self._content_widget)
        self._scroll_area.hide()  # attach 前隐藏（空滚动区会闪白底）
        layout.addWidget(self._scroll_area, 1)
        self._set_sub_tab_active(0)

        # ── 自带历史卡片 + 接线（替代宿主 _build_deferred_card_history） ──
        self._card = HistoryCard()
        self._card.sessionSelected.connect(self._on_session_selected)
        self._card.sessionArchived.connect(self._on_session_archived)
        self._card.sessionRenamed.connect(self._on_session_renamed)
        self._card.refreshRequested.connect(self._on_refresh_requested)
        self._card.sessionImported.connect(self._on_session_imported)
        self._card.sessionRestored.connect(self._on_session_restored)
        self._card.sessionPermanentlyDeleted.connect(self._on_session_deleted)
        self._card.archivedSessionRenamed.connect(self._on_archived_renamed)
        self._card.teamRestoreRequested.connect(self._on_team_restore)
        self._card.teamArchiveRequested.connect(self._on_team_archive)
        self._card.memberSelected.connect(self._on_member_selected)
        self.attach(self._card)
        self.set_search_handler("🔍 搜索会话...", self._card.set_search_filter)
        self.set_extra_button_handler(self._card.get_import_button_handler(), tooltip="导入会话")
        self.tabChanged.connect(self._on_tab_changed)

    # ── 对外：卡片引用 ──

    @property
    def card(self):
        """会话列表卡片（宿主 ``MainWidget._history_popup_card`` 代理读取）"""
        return self._card

    # ── 子页签 ──

    @property
    def content_layout(self):
        """内容布局（HistoryCard.get_content_layout() 沿父链上溯命中本属性）"""
        return self._content_layout

    def _on_sub_tab_clicked(self, tab_id: str) -> None:
        self.set_current_tab(tab_id)

    def set_current_tab(self, tab_id: str) -> None:
        """程序化切换子页签（变化时发射 tabChanged，由本页驱动列表刷新）"""
        if tab_id not in {t for t, _ in self.SUB_TABS} or tab_id == self._current_tab:
            return
        self._current_tab = tab_id
        for btn in self._sub_buttons:
            btn.set_active(btn.tab_id == tab_id)
        self.tabChanged.emit(tab_id)

    def _set_sub_tab_active(self, index: int) -> None:
        for i, btn in enumerate(self._sub_buttons):
            btn.set_active(i == index)

    # ── 挂载 / 宿主注入（兼容原 SystemCardFrame 接口） ──

    def attach(self, content: Any) -> None:
        """挂载 HistoryCard 内容（放进滚动区内容布局）"""
        if self._content is not None:
            return
        self._content = content
        self._hint.hide()
        self._content_layout.addWidget(content)
        content.show()
        self._scroll_area.show()

    def set_search_handler(self, placeholder: str, callback) -> None:
        """设置搜索框占位文本 + 文本变化回调"""
        if self._search_input is None:
            return
        self._search_input.setPlaceholderText(placeholder)
        self._search_input.textChanged.connect(callback)

    def set_extra_button_handler(self, handler, icon=None, tooltip="") -> None:
        """注入导入按钮回调（子页签行右端）"""
        if icon is not None:
            self._import_btn.setIcon(icon)
        self._import_btn.setToolTip(tooltip or "导入会话")
        self._import_btn.clicked.connect(handler)
        self._import_btn.show()

    def set_opacity(self, opacity: float) -> None:
        """透明度联动契约（原 SystemCardFrame 为空实现，此处同语义）"""

    # ── 数据刷新（跟随活跃窗口投影） ──

    def showEvent(self, event):  # noqa: N802 (Qt 命名)
        super().showEvent(event)
        self.refresh()

    def refresh(self) -> None:
        """从当前活跃窗口拉取列表数据（历史 / 归档 分流）"""
        win = _active_window()
        if win is None:
            return
        if self._current_tab == "archived":
            self._card.switch_tab("archived")
            win._refresh_archived_sessions()
        else:
            self._card.switch_tab("history")
            win._refresh_history_toggle_panel()

    def refresh_data(self) -> None:
        """工作台通用页协议入口（宿主 ``refresh_current_page_data`` 调用）"""
        self.refresh()

    # ── 卡片信号 → 宿主窗口（会话管理逻辑仍由窗口实现） ──

    def _win(self):
        return _active_window()

    def _on_tab_changed(self, tab_id: str) -> None:
        if self._search_input is not None:
            self._search_input.clear()
            self._search_input.setPlaceholderText(
                "🔍 搜索历史会话..." if tab_id == "history" else "🔍 搜索归档会话..."
            )
        win = self._win()
        if win is None:
            return
        self._card.switch_tab(tab_id)
        if tab_id == "archived":
            win._refresh_archived_sessions()
        else:
            win._refresh_history_toggle_panel()

    def _on_session_selected(self, index: int) -> None:
        win = self._win()
        if win is not None:
            win._on_history_session_selected(index)

    def _on_session_archived(self, index: int) -> None:
        win = self._win()
        if win is not None:
            win._archive_history_session(index)

    def _on_session_renamed(self, index: int, new_title: str) -> None:
        win = self._win()
        if win is not None:
            win._rename_history_session(index, new_title)

    def _on_refresh_requested(self) -> None:
        self.refresh()

    def _on_session_imported(self, record: dict) -> None:
        win = self._win()
        if win is not None:
            win._on_session_imported(record)

    def _on_session_restored(self, file_path: str) -> None:
        win = self._win()
        if win is not None:
            win._on_archived_session_restored(file_path)

    def _on_session_deleted(self, file_path: str) -> None:
        win = self._win()
        if win is not None:
            win._on_archived_session_deleted(file_path)

    def _on_archived_renamed(self, file_path: str, new_title: str) -> None:
        win = self._win()
        if win is not None:
            win._on_archived_session_renamed(file_path, new_title)

    def _on_team_restore(self, run_id: str) -> None:
        win = self._win()
        if win is not None:
            win._on_team_restore_requested(run_id)

    def _on_team_archive(self, run_id: str) -> None:
        win = self._win()
        if win is not None:
            win._on_team_archive_requested(run_id)

    def _on_member_selected(self, record: dict) -> None:
        win = self._win()
        if win is not None:
            win._on_team_member_selected(record)

    # ── 样式 ──

    def _apply_search_style(self) -> None:
        Colors.refresh()
        self._search_input.setStyleSheet(
            f"""
            QLineEdit {{
                background: {Colors.HOVER_BG};
                border: 1px solid {Colors.BORDER};
                border-radius: 4px;
                color: {Colors.TEXT_PRIMARY};
                padding: 2px 8px;
                {font_size_css(11)}
                {get_font_family_css()}
            }}
            QLineEdit:focus {{
                border: 1px solid {Colors.TEXT_ACCENT};
            }}
            QLineEdit::placeholder {{
                color: {Colors.INPUT_PLACEHOLDER};
            }}
        """
        )

    def refresh_style(self) -> None:
        self._hint.refresh_style()
        for btn in self._sub_buttons:
            btn.refresh_style()
        if self._search_input is not None:
            self._apply_search_style()
        if hasattr(self._card, "refresh_style"):
            self._card.refresh_style()
