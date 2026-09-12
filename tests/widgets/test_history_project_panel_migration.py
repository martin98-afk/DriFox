# -*- coding: utf-8 -*-
"""项目选择卡片迁入 history-manager 插件 + 浮动卡最小宽度（回归守卫）

背景（三项改动）：
1. 历史会话浮动卡（左侧停靠区）被拖窄时内容挤压 → 卡片需要最小宽度下限；
2. 项目选择卡片（原宿主 TOP 容器卡片）整体迁入 history-manager 插件，成为
   会话列表上方的可折叠面板，复用 ``ProjectSelectorCardContent``（首行「全部项目」）；
3. 宿主标题栏项目 icon 点击 → 打开插件并展开面板（经 ``HistoryService`` 入口），
   项目增删 / 切换 / 导入导出等窗口态操作仍由宿主 MainWidget 实现（插件只转发）。

本文件用静态源码守卫（真实 UI 交互太重，断言源码结构即可防回归）。
"""

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_PAGE = _ROOT / "plugins" / "history-manager" / "ui" / "history_page.py"
_PLUGIN_INIT = _ROOT / "plugins" / "history-manager" / "ui" / "__init__.py"
_CARD = _ROOT / "app" / "widgets" / "cards" / "settings" / "project_selector_card.py"
_MAIN = _ROOT / "app" / "main_widget.py"
_CARDS_MODULE = _ROOT / "app" / "widgets" / "modules" / "system_cards_module.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_floating_card_declares_min_width():
    """历史插件卡片声明最小宽度并落到 setMinimumWidth（左侧停靠区拖窄保底）"""
    src = _read(_PAGE)
    assert "_CARD_MIN_WIDTH = 240" in src
    assert "self.setMinimumWidth(_CARD_MIN_WIDTH)" in src


def test_project_panel_reuses_selector_card_with_all_entry():
    """插件面板复用宿主项目选择卡片，聚合行「全部项目」在面板首行工具条自建"""
    page = _read(_PAGE)
    assert "from app.widgets.cards.settings.project_selector_card import" in page
    assert "ProjectSelectorCardContent(panel)" in page
    assert 'ProjectItem("全部项目", False, panel, is_all_entry=True)' in page
    assert "collapse_project_selector" in page
    card = _read(_CARD)
    assert "allProjectsSelected = pyqtSignal()" in card
    assert "is_all_entry" in card


def test_host_no_longer_owns_project_card():
    """宿主不再注册 / 持有项目选择卡片（迁移完成，避免双份 UI）"""
    main = _read(_MAIN)
    assert "_project_selector_card_content" not in main
    assert "self._project_selector_card" not in main
    assert "self._project_new_edit" not in main
    assert "_project_selector_card_content" not in _read(_CARDS_MODULE)


def test_host_project_icon_opens_plugin_panel():
    """标题栏项目 icon 点击 → 打开插件并展开面板（命令入口同样指向新入口）"""
    main = _read(_MAIN)
    assert "def _open_project_selector_panel" in main
    assert "def _collapse_project_selector_panel" in main
    assert "tm.open_workbench_history()" in main
    assert "svc.open_project_selector()" in main
    assert '"project_selector": ("选择项目", self._open_project_selector_panel)' in main


def test_service_exposes_project_panel_entry():
    """插件服务门面暴露面板入口（宿主不直接调插件页私有方法）"""
    src = _read(_PLUGIN_INIT)
    for name in ("open_project_selector", "collapse_project_selector", "refresh_project_selector_data"):
        assert f"def {name}(self)" in src, f"HistoryService 缺少 {name}"


def test_sub_tabs_own_first_row():
    """第一行只有子页签（历史会话 / 归档），项目选择 + 搜索在第二行"""
    src = _read(_PAGE)
    assert src.index("行1：子页签（居中，单独一行）") < src.index("行2：项目选择")
    assert src.index("self._sub_buttons: List[CustomTabButton] = []") < src.index(
        "self._project_header = _ProjectSelectorHeader(self)"
    )
    # 子页签行内不再放导入 / 新建按钮
    tabs_row = src[src.index("tabs_row.addStretch(1)") : src.index("layout.addLayout(tabs_row)")]
    assert "self._import_btn" not in tabs_row
    assert "self._new_session_btn" not in tabs_row


def test_import_left_of_new_session_in_filter_row():
    """第三行右端顺序：导入（左）→ 新建会话（右）"""
    src = _read(_PAGE)
    assert 'get_icon("新会话")' in src
    assert 'self._new_session_btn.setToolTip("新建会话")' in src
    row = src[src.index("filter_row = QHBoxLayout()") : src.index("layout.addLayout(filter_row)")]
    assert row.index("filter_row.addWidget(self._import_btn)") < row.index(
        "filter_row.addWidget(self._new_session_btn)"
    )


def test_filter_row_controls_are_enlarged():
    """项目选择 / 搜索框 / 按钮整行放大（30px 高，名称 12px 字号）"""
    src = _read(_PAGE)
    assert "self.setFixedHeight(30)" in src  # 折叠头
    assert "self._search_input.setFixedHeight(30)" in src
    assert "self._import_btn.setFixedSize(30, 30)" in src
    assert "self._new_session_btn.setFixedSize(30, 30)" in src
    assert "size=22" in src  # 项目 icon


def test_project_header_shows_full_name():
    """折叠头显示完整项目名（不做省略 / 不设宽度上限）"""
    src = _read(_PAGE)
    header = src[src.index("class _ProjectSelectorHeader") : src.index("def _active_history_manager")]
    assert "_ElidedLabel" not in header
    assert "setMaximumWidth" not in header
    assert "self._name_label = QLabel(" in header


def test_new_session_switches_to_filtered_project():
    """新建会话：筛选到具体项目（非当前）时切到该项目；全部项目 / 当前项目保持原项目"""
    src = _read(_PAGE)
    body = src[src.index("def _on_new_session_clicked") : src.index("def _on_new_project_submitted")]
    assert "_PROJECT_ALL" in body and "_PROJECT_CURRENT" in body
    assert "self._call_window(\"_on_project_selected\", target)" in body
    assert "self._call_window(\"_create_new_session\")" in body


def test_project_row_click_is_filter_only():
    """项目行点击只做会话筛选：不切窗口项目（否则会连带新建会话）"""
    src = _read(_PAGE)
    body = src[src.index("def _on_project_row_selected") : src.index("def _on_all_projects_selected")]
    assert "_on_project_selected" not in body
    assert "self._project_filter_raw = project" in body


def test_panel_expand_takes_full_height():
    """面板展开时让出会话列表区域，占满卡片高度（不再限高 220）"""
    src = _read(_PAGE)
    assert "_PROJECT_PANEL_MAX_HEIGHT" not in src
    assert "self._scroll_area.setVisible((not visible) and self._content_ready)" in src
    assert "layout.addWidget(self._project_panel, 1)" in src
