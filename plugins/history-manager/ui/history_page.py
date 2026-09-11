# -*- coding: utf-8 -*-
"""历史会话页（workbench_tab：``page_id="history-manager"``）

原为 ``app/widgets/workbench_panel.HistoryPage``（宿主内置页），现随
``history-manager`` 插件迁出：历史会话 / 归档 子页签 + 项目切换器 + 搜索框 +
右端导入按钮，**自带** ``HistoryCard``（会话列表本体）。

数据流（架构反转：数据服务化）：
- 列表数据：本页自拉（``HistoryManager.get_instance()`` 全局单例，会话数据
  全局只有一份），不再经宿主窗口方法注入
- 纯数据写操作（置顶 / 移动项目）：插件直走数据服务，写后调窗口
  ``_notify_history_data_changed()`` 做欢迎卡片失效 + 跨窗口联动
- 窗口态操作保留信号转发：点击加载会话（``win._on_history_session_selected``）、
  归档（含当前会话清场，``win._archive_history_session``）等
- 当前会话高亮：只读活跃窗口 ``_current_session_id``（窗口态）

项目切换器：``当前项目（跟随活跃窗口）/ 全部项目 / 具体项目``；「全部项目」
视图下行内显示项目小标签，点击跨项目会话直接加载（窗口侧自动切项目与工作目录）。

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


# 项目过滤器哨兵值（「当前项目」跟随活跃窗口；「全部项目」混合视图）
_PROJECT_CURRENT = "__current__"
_PROJECT_ALL = "__all__"


def _active_history_manager():
    """会话数据全局唯一入口（HistoryManager 单例）；异常时返回 None"""
    try:
        from app.utils.history_manager import HistoryManager

        return HistoryManager.get_instance()
    except Exception:
        return None


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

        # ── 行2：项目切换器 + 搜索框 ──
        from qfluentwidgets import ComboBox

        self._project_combo = ComboBox(self)
        self._project_combo.setFixedHeight(24)
        self._project_combo.setMinimumWidth(110)
        self._project_combo.currentIndexChanged.connect(self._on_project_filter_changed)
        self._project_filter_raw = _PROJECT_CURRENT  # 打开默认「当前项目」

        self._search_input = QLineEdit(self)
        self._search_input.setPlaceholderText("🔍 搜索会话...")
        self._search_input.setFixedHeight(24)
        self._apply_search_style()
        filter_row = QHBoxLayout()
        filter_row.setSpacing(6)
        filter_row.addWidget(self._project_combo, 0)
        filter_row.addWidget(self._search_input, 1)
        layout.addLayout(filter_row)

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
        self._card.refreshRequested.connect(self._on_refresh_requested)
        self._card.sessionImported.connect(self._on_session_imported)
        self._card.sessionRestored.connect(self._on_session_restored)
        self._card.sessionPermanentlyDeleted.connect(self._on_session_deleted)
        self._card.teamRestoreRequested.connect(self._on_team_restore)
        self._card.teamArchiveRequested.connect(self._on_team_archive)
        self._card.memberSelected.connect(self._on_member_selected)
        self._card.dataChanged.connect(self._on_card_data_changed)
        self._card.pinToggled.connect(self._on_pin_toggled)
        self._card.moveToProjectRequested.connect(self._on_move_to_project)
        self.attach(self._card)
        self.set_search_handler("🔍 搜索会话...", self._card.set_search_filter)
        self.set_extra_button_handler(self._card.get_import_button_handler(), tooltip="导入会话")
        self.tabChanged.connect(self._on_tab_changed)
        self._rebuild_project_options()

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

    # ── 数据自拉（架构反转：会话数据全局一份，经 HistoryManager 单例直接获取） ──

    def refresh(self) -> None:
        """自拉数据渲染（不再绕宿主窗口方法）"""
        if self._current_tab == "archived":
            self._card.switch_tab("archived")
            self._card.set_archived_sessions(self._enrich_archived_list())
        else:
            self._card.switch_tab("history")
            hm = _active_history_manager()
            history_list = hm.get_history_list(self._resolved_project_filter(), merge_team=True) if hm else []
            self._card.set_history(history_list, self._locate_current_index(history_list))

    def _resolved_project_filter(self) -> Optional[str]:
        """解析项目过滤器（「当前项目」跟随活跃窗口；「全部项目」→ None 不过滤）"""
        if self._project_filter_raw == _PROJECT_ALL:
            return None
        if self._project_filter_raw == _PROJECT_CURRENT:
            win = _active_window()
            return getattr(win, "_current_project", "默认项目") if win else "默认项目"
        return self._project_filter_raw

    def _locate_current_index(self, history_list: List[dict]) -> Optional[int]:
        """在列表中定位活跃窗口当前会话（团队合并条目按成员命中）"""
        win = _active_window()
        current_sid = getattr(win, "_current_session_id", None) if win else None
        if not current_sid:
            return None
        for i, session in enumerate(history_list):
            if session.get("team_merged"):
                members = session.get("members") or []
                if any(m.get("session_id") == current_sid for m in members):
                    return i
            elif session.get("session_id") == current_sid:
                return i
        return None

    def _enrich_archived_list(self) -> List[dict]:
        """归档会话列表 enrich（mtime 预览缓存；逻辑自 main_widget._refresh_archived_sessions 迁入）"""
        import json as _json
        import os as _os

        from app.utils.session_preview import get_message_preview

        hm = _active_history_manager()
        if hm is None:
            return []
        archived_list = hm.get_archived_sessions()
        if not hasattr(self, "_archived_cache"):
            self._archived_cache = {}  # path → (mtime, enrich_dict)

        enriched_list = []
        for session in archived_list:
            fp = session["path"]
            cached = self._archived_cache.get(fp)
            try:
                current_mtime = _os.path.getmtime(fp)
            except OSError:
                current_mtime = 0

            if cached and cached[0] == current_mtime:
                session["message_count"] = cached[1].get("message_count", 0)
                session["last_time"] = cached[1].get("last_time", "")
                session["preview"] = cached[1].get("preview", "")
            else:
                try:
                    with open(fp, "r", encoding="utf-8") as f:
                        data = _json.loads(f.read())
                    messages = data.get("messages", [])
                    # 🛡️ R7：team 邮件（_hook_event="TeamMail"）计入 user 消息数
                    msg_count = data.get(
                        "message_count",
                        len(
                            [
                                m
                                for m in messages
                                if m.get("role") == "user"
                                and (not m.get("_hook_event") or m.get("_hook_event") == "TeamMail")
                            ]
                        ),
                    )
                    last_time = data.get("last_time", data.get("saved_at", ""))
                    preview = get_message_preview(messages) if messages else ""
                    session["message_count"] = msg_count
                    session["last_time"] = last_time
                    session["preview"] = preview
                    self._archived_cache[fp] = (
                        current_mtime,
                        {"message_count": msg_count, "last_time": last_time, "preview": preview},
                    )
                except Exception:
                    pass
            enriched_list.append(session)

        # 清理已不存在文件的缓存键（删除/恢复归档后立即生效，防无限增长）
        current_paths = {s["path"] for s in archived_list}
        for stale_key in [k for k in self._archived_cache if k not in current_paths]:
            self._archived_cache.pop(stale_key, None)
        return enriched_list

    def refresh_data(self) -> None:
        """工作台通用页协议入口（宿主 ``refresh_current_page_data`` 调用）"""
        self.refresh()

    def show_card(self) -> None:
        """浮动卡显示入口（CardManager show_card 钩子）：显示即刷新列表

        ★ ``setVisible(True)`` 必须留：CardContainer.add_card 挂载时显式
        ``setVisible(False)``，容器靠 ``not isHidden()`` 判定是否有可见卡片
        才展开（card_container._schedule_expand）。CardManager.show_card 只在
        卡片**没有** ``show_card`` 方法时才兜底 ``setVisible(True)``；本页有
        该方法，故显示动作只能由本方法自己完成，缺失即「点侧栏按钮无反应、
        左侧卡打不开」。
        """
        self.setVisible(True)
        self.refresh()

    # ── 卡片信号 → 宿主窗口（会话管理逻辑仍由窗口实现） ──

    def _win(self):
        return _active_window()

    def _on_tab_changed(self, tab_id: str) -> None:
        if self._search_input is not None:
            self._search_input.clear()
            self._search_input.setPlaceholderText("🔍 搜索历史会话..." if tab_id == "history" else "🔍 搜索归档会话...")
        self.refresh()

    def _on_session_selected(self, index: int) -> None:
        win = self._win()
        if win is not None:
            win._on_history_session_selected(index)

    def _on_session_archived(self, index: int) -> None:
        win = self._win()
        if win is not None:
            win._archive_history_session(index)

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

    # ── 纯数据写操作（插件直走数据服务，写后通知窗口联动） ──

    def _notify_windows_data_changed(self) -> None:
        """数据变更后通知窗口联动（欢迎卡片失效 + 跨窗口广播；窗口方法本体不变）"""
        win = self._win()
        if win is not None and hasattr(win, "_notify_history_data_changed"):
            win._notify_history_data_changed()

    def _on_pin_toggled(self, index: int, pinned: bool) -> None:
        """右键置顶/取消置顶（纯数据操作，不经窗口）"""
        hm = _active_history_manager()
        record = self._card.get_history_at_index(index) if self._card is not None else None
        if hm is None or not record:
            return
        session_id = record.get("session_id")
        if not session_id:
            return
        hm.set_session_pinned(session_id, bool(pinned))
        self._notify_windows_data_changed()

    def _on_move_to_project(self, index: int, project: str) -> None:
        """右键移动到项目（纯数据操作，不经窗口）"""
        hm = _active_history_manager()
        record = self._card.get_history_at_index(index) if self._card is not None else None
        if hm is None or not record or not project:
            return
        session_id = record.get("session_id")
        if not session_id:
            return
        full_index = hm.find_index_by_session_id(session_id)
        if full_index is None:
            return
        hm.move_to_project(full_index, project)
        self._notify_windows_data_changed()
        self.refresh()

    # ── 项目切换器 ──

    def _rebuild_project_options(self) -> None:
        """重建项目下拉（保留当前选择语义；「当前项目」标签动态显示窗口项目名）"""
        combo = self._project_combo
        combo.blockSignals(True)
        combo.clear()
        win = _active_window()
        current = getattr(win, "_current_project", "默认项目") if win else "默认项目"
        combo.addItem(f"当前项目（{current}）", userData=_PROJECT_CURRENT)
        combo.addItem("全部项目", userData=_PROJECT_ALL)
        try:
            hm = _active_history_manager()
            projects = hm.get_project_list() if hm is not None else None
            if not projects and self._card is not None:
                projects = self._card.get_project_list()  # 兜底：从已加载列表聚合
            for proj in projects or []:
                combo.addItem(proj, userData=proj)
        except Exception:
            pass
        idx = combo.findData(self._project_filter_raw)
        combo.setCurrentIndex(idx if idx >= 0 else 0)
        combo.blockSignals(False)

    def _on_project_filter_changed(self, index: int) -> None:
        """下拉切换：更新过滤器 → 项目标签显隐 → 自拉数据刷新"""
        data = self._project_combo.itemData(index)
        if data is None:
            return
        self._project_filter_raw = data
        if self._card is not None:
            self._card.set_show_project_labels(data == _PROJECT_ALL)
        self.refresh()

    def _on_card_data_changed(self) -> None:
        """列表数据渲染完成：注入右键菜单项目列表 + 重建下拉选项"""
        hm = _active_history_manager()
        if hm is not None and self._card is not None:
            try:
                self._card.set_project_list(hm.get_project_list())
            except Exception:
                pass
        self._rebuild_project_options()

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
