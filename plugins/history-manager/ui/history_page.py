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

项目选择面板（可折叠，默认收起）：折叠头一行展示当前过滤目标（项目 icon +
项目名），点开复用宿主 ``ProjectSelectorCardContent``（项目选择卡片本体：
首行「全部项目」→ 不过滤项目，视图下行内显示项目小标签；点具体项目 →
宿主窗口切项目并自动切工作目录，本页同时过滤到该项目）。新建 / 选择文件夹 /
导入项目的工具条随面板一起迁入本页，均转调宿主窗口方法执行。

接口契约（宿主 ``MainWidget._history_card`` 代理读取）：
- ``tabChanged`` / ``closed`` / ``set_current_tab`` / ``set_search_handler`` /
  ``set_extra_button_handler`` / ``_search_input`` / ``_current_tab`` /
  ``set_opacity`` / ``content_layout`` / ``refresh_style``
- ``open_project_selector`` / ``collapse_project_selector`` /
  ``refresh_project_selector_data``（宿主标题栏项目 icon 与项目增删后的驱动入口）
"""

from typing import Any, List, Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import FluentIcon, ScrollArea, TransparentToolButton

from loguru import logger

from app.utils.design_tokens import Colors, font_size_css, get_unified_scrollbar_style
from app.utils.utils import get_font_family_css, get_icon
from app.widgets._workbench_helpers import _EmptyHint
from app.widgets.cards.settings.project_selector_card import (
    ProjectSelectorCardContent,
    _SquareAvatar,
    get_project_color,
)
from app.widgets.custom_title_bar import CustomTabButton


# 项目过滤器哨兵值（「当前项目」跟随活跃窗口；「全部项目」混合视图）
_PROJECT_CURRENT = "__current__"
_PROJECT_ALL = "__all__"

# 卡片最小宽度：左侧停靠区 / 分隔条压缩时的保底宽度（低于此值搜索框与条目被挤扁）
_CARD_MIN_WIDTH = 240


class _ProjectSelectorHeader(QFrame):
    """项目选择折叠头：项目 icon + 项目全名 + 展开箭头（点击展开/收起面板）

    左侧停靠区宽度有限，项目选择卡片默认收起只留这一行；宿主标题栏项目 icon
    与本行点击都会展开面板。头像复用项目方块的缩写绘制逻辑（项目名 → 缩写 + 颜色），
    名称展示**完整**项目名（不省略，宽度超出时挤压同行搜索框）。
    """

    clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("projectSelectorHeader")
        self.setFixedHeight(30)
        self.setCursor(Qt.PointingHandCursor)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 0, 8, 0)
        layout.setSpacing(6)
        self._avatar = _SquareAvatar("全", get_project_color("全部项目"), self, size=22)
        layout.addWidget(self._avatar, 0)
        self._name_label = QLabel("", self)
        layout.addWidget(self._name_label, 1)
        self._arrow_label = QLabel("▾", self)
        layout.addWidget(self._arrow_label, 0)
        self._apply_style()

    def _apply_style(self) -> None:
        Colors.refresh()
        self.setStyleSheet(
            f"""
            QFrame#projectSelectorHeader {{
                background: {Colors.HOVER_BG};
                border: 1px solid {Colors.BORDER};
                border-radius: 4px;
            }}
            QFrame#projectSelectorHeader:hover {{
                background: {Colors.HOVER_BG_STRONG};
            }}
            QFrame#projectSelectorHeader QLabel {{
                background: transparent;
                border: none;
                color: {Colors.TEXT_PRIMARY};
                {font_size_css(12)}
                {get_font_family_css()}
            }}
        """
        )

    def refresh_style(self) -> None:
        """主题/字体变更后重刷样式"""
        self._apply_style()

    def set_project(self, name: str, is_all: bool = False) -> None:
        """更新折叠头显示（项目 icon 缩写 + 颜色随项目名）"""
        name = name or "默认项目"
        self._name_label.setText(name)
        self._avatar.set_project(name, get_project_color(name))
        self._avatar.setToolTip(name)
        self.setToolTip("全部项目（点击展开项目选择）" if is_all else f"当前项目：{name}（点击展开项目选择）")

    def set_expanded(self, expanded: bool) -> None:
        """更新展开箭头方向"""
        self._arrow_label.setText("▴" if expanded else "▾")

    def mousePressEvent(self, event):  # noqa: N802 (Qt 命名)
        self.clicked.emit()
        super().mousePressEvent(event)


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

        # ── 行1：子页签（居中，单独一行） ──
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
        layout.addLayout(tabs_row)

        # ── 行2：项目选择（icon + 全名）+ 搜索框 + 导入 / 新建会话按钮 ──
        self._project_header = _ProjectSelectorHeader(self)
        self._project_header.clicked.connect(self._toggle_project_panel)
        self._project_filter_raw = _PROJECT_CURRENT  # 默认「跟随活跃窗口项目」

        self._search_input = QLineEdit(self)
        self._search_input.setPlaceholderText("🔍 搜索会话...")
        self._search_input.setFixedHeight(30)
        self._search_input.setMinimumWidth(80)  # 项目全名较长时的兜底
        self._apply_search_style()
        filter_row = QHBoxLayout()
        filter_row.setSpacing(6)
        filter_row.addWidget(self._project_header, 0)
        filter_row.addWidget(self._search_input, 1)
        # 顺序：导入（左）→ 新建（右）
        self._import_btn = TransparentToolButton(get_icon("导入"), self)
        self._import_btn.setFixedSize(30, 30)
        self._import_btn.setToolTip("导入会话")
        self._import_btn.hide()  # handler 注入前隐藏
        filter_row.addWidget(self._import_btn)
        self._new_session_btn = TransparentToolButton(get_icon("新会话"), self)
        self._new_session_btn.setFixedSize(30, 30)
        self._new_session_btn.setToolTip("新建会话")
        self._new_session_btn.clicked.connect(self._on_new_session_clicked)
        filter_row.addWidget(self._new_session_btn)
        layout.addLayout(filter_row)

        # ── 行3：项目选择面板（默认收起；展开时占满卡片高度） ──
        self._project_panel = self._build_project_panel()
        self._project_panel_open = False  # 不依赖 Qt 可见性（祖先未显示时 isVisible 恒 False）
        self._content_ready = False  # attach 后置 True（面板展开时要让出列表区域）
        self._project_panel.hide()
        layout.addWidget(self._project_panel, 1)

        # 卡片最小宽度保底（左侧停靠区拖窄时搜索框/条目不被挤扁）
        self.setMinimumWidth(_CARD_MIN_WIDTH)

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
        # 构造期只同步折叠头文本；面板数据（项目列表 + 会话数/根目录）留到
        # 展开或宿主驱动刷新时再拉，避免卡片创建路径上多打一轮 DB 统计查询。
        self._sync_project_header()

    # ── 项目选择面板（折叠；复用宿主项目选择卡片） ──

    def _build_project_panel(self) -> QWidget:
        """构建项目选择面板：项目选择卡片内容 + 新建/文件夹/导入工具条

        复用宿主 ``ProjectSelectorCardContent``（项目行 icon / 元数据 / hover
        导出归档与宿主项目卡片完全一致）；项目增删等数据操作仍由宿主窗口实现，
        插件只做 UI 承载与信号转发。
        """
        panel = QFrame(self)
        panel.setObjectName("projectSelectorPanel")
        vbox = QVBoxLayout(panel)
        vbox.setContentsMargins(0, 2, 0, 2)
        vbox.setSpacing(4)

        self._project_selector = ProjectSelectorCardContent(panel)
        self._project_selector.allProjectsSelected.connect(self._on_all_projects_selected)
        self._project_selector.projectSelected.connect(self._on_project_row_selected)
        self._project_selector.newProjectCreated.connect(
            lambda name: self._call_window("_on_new_project_created", name)
        )
        self._project_selector.archiveProject.connect(lambda name: self._call_window("_on_archive_project", name))
        self._project_selector.exportProject.connect(lambda name: self._call_window("_on_export_project", name))
        self._project_selector.importProjectRequested.connect(
            lambda: self._call_window("_on_import_project")
        )
        self._project_selector.projectFileDropped.connect(
            lambda path: self._call_window("_on_project_file_dropped", path)
        )
        self._project_selector.openFolderRequested.connect(
            lambda name, root: self._call_window("_on_open_project_folder", name, root)
        )
        self._project_selector.folderDropped.connect(
            lambda path: self._call_window("_on_project_folder_dropped", path)
        )
        vbox.addWidget(self._project_selector, 1)

        # ── 工具条：新建/搜索输入框 + 新建 + 选择文件夹 + 导入项目 ──
        tools = QHBoxLayout()
        tools.setSpacing(4)
        self._project_new_edit = QLineEdit(panel)
        self._project_new_edit.setPlaceholderText("新建/搜索项目...")
        self._project_new_edit.setFixedHeight(24)
        self._project_new_edit.setMinimumWidth(80)
        self._project_new_edit.setStyleSheet(
            f"""
            QLineEdit {{
                background: {Colors.HOVER_BG};
                border: 1px solid {Colors.BORDER};
                border-radius: 4px;
                color: {Colors.TEXT_PRIMARY};
                padding: 2px 6px;
                {font_size_css(11)}
                {get_font_family_css()}
            }}
            QLineEdit:focus {{
                border: 1px solid {Colors.TEXT_ACCENT};
            }}
        """
        )
        self._project_new_edit.returnPressed.connect(self._on_new_project_submitted)
        tools.addWidget(self._project_new_edit, 1)

        self._project_new_btn = TransparentToolButton(FluentIcon.ADD, panel)
        self._project_new_btn.setFixedSize(24, 24)
        self._project_new_btn.setToolTip("创建项目")
        self._project_new_btn.clicked.connect(self._on_new_project_submitted)
        tools.addWidget(self._project_new_btn, 0)

        self._project_folder_btn = TransparentToolButton(FluentIcon.FOLDER, panel)
        self._project_folder_btn.setFixedSize(24, 24)
        self._project_folder_btn.setToolTip("选择文件夹作为项目根目录")
        self._project_folder_btn.clicked.connect(lambda: self._call_window("_on_project_open_folder_btn"))
        tools.addWidget(self._project_folder_btn, 0)

        self._project_import_btn = TransparentToolButton(get_icon("导入"), panel)
        self._project_import_btn.setFixedSize(24, 24)
        self._project_import_btn.setToolTip("导入项目（从 .drifox_project 压缩包）")
        self._project_import_btn.clicked.connect(lambda: self._call_window("_on_import_project"))
        tools.addWidget(self._project_import_btn, 0)

        vbox.addLayout(tools)
        return panel

    def _call_window(self, method_name: str, *args) -> None:
        """调用活跃窗口方法（插件只承载 UI，项目数据操作仍在宿主窗口）"""
        win = _active_window()
        fn = getattr(win, method_name, None) if win is not None else None
        if callable(fn):
            try:
                fn(*args)
            except Exception:
                logger.warning(f"[history-manager] 调用窗口方法 {method_name} 失败")

    def _sync_project_header(self) -> None:
        """折叠头显示当前过滤目标（「全部项目」/ 具体项目）"""
        if self._project_filter_raw == _PROJECT_ALL:
            self._project_header.set_project("全部项目", is_all=True)
            return
        self._project_header.set_project(self._resolved_project_filter() or "默认项目")

    def refresh_project_selector_data(self) -> None:
        """刷新项目选择面板数据（项目列表 / 会话数·工作目录数 / 根目录）

        项目列表来自 ``HistoryManager``（全局单例），当前项目与元数据来自活跃窗口；
        宿主新增 ``all_entry_label="全部项目"`` 聚合行（不过滤项目视图）。
        """
        win = _active_window()
        hm = _active_history_manager()
        projects: List[str] = []
        try:
            projects = list(hm.get_project_list()) if hm is not None else []
        except Exception:
            projects = []
        if not projects and self._card is not None:
            projects = self._card.get_project_list()  # 兜底：从已加载列表聚合
        current = getattr(win, "_current_project", "默认项目") if win is not None else "默认项目"
        if current and current not in projects:
            projects.insert(0, current)

        meta_map = {}
        root_dir_map = {}
        if win is not None:
            meta_builder = getattr(win, "_build_project_meta_map", None)
            root_builder = getattr(win, "_build_project_root_dir_map", None)
            try:
                meta_map = meta_builder(projects) if callable(meta_builder) else {}
            except Exception:
                meta_map = {}
            try:
                root_dir_map = root_builder(projects) if callable(root_builder) else {}
            except Exception:
                root_dir_map = {}

        self._project_selector.set_projects_data(
            projects,
            current,
            meta_map,
            root_dir_map,
            all_entry_label="全部项目",
        )
        self._sync_project_header()

    def open_project_selector(self) -> None:
        """外部入口（宿主标题栏项目 icon / ``/project_selector``）：展开面板并刷新数据"""
        self.refresh_project_selector_data()
        self._set_project_panel_visible(True)

    def collapse_project_selector(self) -> None:
        """收起面板（宿主切换项目、归档项目后调用）"""
        self._set_project_panel_visible(False)

    def _toggle_project_panel(self) -> None:
        """折叠头点击：展开/收起面板（展开时先拉最新数据）"""
        will_show = not self._project_panel_open
        if will_show:
            self.refresh_project_selector_data()
        self._set_project_panel_visible(will_show)

    def _set_project_panel_visible(self, visible: bool) -> None:
        """展开/收起面板；展开时占满卡片高度（临时让出会话列表区域）"""
        self._project_panel_open = bool(visible)
        self._project_panel.setVisible(visible)
        self._scroll_area.setVisible((not visible) and self._content_ready)
        self._hint.setVisible((not visible) and not self._content_ready)
        self._project_header.set_expanded(visible)

    def _on_project_row_selected(self, project: str) -> None:
        """项目行点击：与旧项目下拉一致，仅作为会话筛选（不切窗口项目、不新建会话）

        跨项目会话仍可直接点开：窗口侧 ``_on_history_session_selected`` 会按
        会话记录自动切项目与工作目录。
        """
        self._project_filter_raw = project or _PROJECT_CURRENT
        if self._card is not None:
            self._card.set_show_project_labels(False)
        self._set_project_panel_visible(False)
        self._sync_project_header()
        self.refresh()

    def _on_all_projects_selected(self) -> None:
        """「全部项目」行点击：不过滤项目（行内显示项目标签）+ 收起面板"""
        self._project_filter_raw = _PROJECT_ALL
        if self._card is not None:
            self._card.set_show_project_labels(True)
        self._set_project_panel_visible(False)
        self._sync_project_header()
        self.refresh()

    def _on_new_session_clicked(self) -> None:
        """新建会话：当前筛选到具体项目时先切到该项目；「全部项目」下保持原项目

        与「点项目行仅做筛选」配套：筛选本身不动窗口项目；点「新建会话」时
        才把窗口项目切到被筛选的项目（宿主 ``_on_project_selected`` 内含
        「切项目 + 工作目录 + 新建会话 + 团队广播」）。筛选为「全部项目」
        或跟随当前项目时，直接在原项目下新建。
        """
        target = self._project_filter_raw
        win = _active_window()
        current = getattr(win, "_current_project", None) if win is not None else None
        if target and target not in (_PROJECT_ALL, _PROJECT_CURRENT) and target != current:
            self._call_window("_on_project_selected", target)
            return
        self._call_window("_create_new_session")

    def _on_new_project_submitted(self) -> None:
        """工具条输入框回车 / + 按钮：交窗口统一处理（命中已有项目则切换）"""
        name = self._project_new_edit.text().strip()
        if not name:
            return
        self._project_new_edit.clear()
        self._set_project_panel_visible(False)
        self._call_window("_on_header_new_project", name)

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
        self._content_ready = True
        self._hint.hide()
        self._content_layout.addWidget(content)
        content.show()
        self._scroll_area.setVisible(not self._project_panel_open)

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
        self._sync_project_header()
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

    # ── 列表数据联动 ──

    def _on_card_data_changed(self) -> None:
        """列表数据渲染完成：注入右键菜单项目列表 + （面板展开时）刷新项目数据"""
        hm = _active_history_manager()
        if hm is not None and self._card is not None:
            try:
                self._card.set_project_list(hm.get_project_list())
            except Exception:
                pass
        if self._project_panel_open:
            self.refresh_project_selector_data()

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
                {font_size_css(12)}
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
        self._project_header.refresh_style()
        if hasattr(self._card, "refresh_style"):
            self._card.refresh_style()
