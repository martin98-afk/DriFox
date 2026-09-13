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
    assert "ProjectSelectorCardContent(body)" in page
    assert 'ProjectItem("全部项目", False, body, is_all_entry=True)' in page
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
    """项目选择 / 搜索框 / 按钮整行放大（搜索框 30px 高，折叠头 32px，名称 12px 字号）

    折叠头 32px（原 30px）：容纳 16px 旋转 chevron + 更明显的块面（底色/
    描边/左侧项目色条），比同行搜索框高 2px 以形成"入口行"的视觉重量。
    """
    src = _read(_PAGE)
    assert "self.setFixedHeight(32)" in src  # 折叠头
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
    assert 'self._call_window("_on_project_selected", target)' in body
    assert 'self._call_window("_create_new_session")' in body


def test_project_row_click_is_filter_only():
    """项目行点击只做会话筛选：不切窗口项目（否则会连带新建会话）"""
    src = _read(_PAGE)
    body = src[src.index("def _on_project_row_selected") : src.index("def _on_all_projects_selected")]
    assert "_on_project_selected" not in body
    assert "self._project_filter_raw = project" in body


def test_panel_expand_takes_full_height():
    """面板展开时让出会话列表区域，占满卡片高度（不再限高；走高度动画）

    ★ 面板 stretch 必须为 1：展开态占满卡片剩余空间（按内容高度会在项目少时
    留下一大片空白）。动画期间由 setFixedHeight 硬控（min=max），stretch 抢不走。
    """
    src = _read(_PAGE)
    assert "_PROJECT_PANEL_MAX_HEIGHT" not in src
    # 展开态：列表区/占位隐藏；收起终态：列表区按 _content_ready 恢复
    assert "self._scroll_area.setVisible(False)" in src
    assert "self._scroll_area.setVisible(self._content_ready)" in src
    assert "layout.addWidget(self._project_panel, 1)" in src
    # 展开/收起走高度动画（Animations.EXPAND_MS）
    body = src[src.index("def _set_project_panel_visible") : src.index("def _release_panel_area")]
    assert "QPropertyAnimation" in body
    assert "Animations.EXPAND_MS" in body
    assert "Animations.motion_enabled()" in body  # 减少动效时直置终值


def test_panel_target_height_fills_available_not_content():
    """★ 展开目标高度必须是「卡片可用高度」，不是内容高度

    按内容高度量：项目少（如 1 个）时面板只占约 100px，而列表区在展开态是隐藏的
    → 视觉上一大片空白（用户实测反馈）。正解 = 占满可用高度，内容不足的空白由
    面板内部滚动区吸收。
    """
    src = _read(_PAGE)
    measure = src[src.index("def _measure_panel_height") : src.index("def _available_panel_height")]
    assert "self._available_panel_height()" in measure
    assert "sizeHint" not in measure, "不得按内容 sizeHint 定高（会留白）"
    # 可用高度 = 卡片高度 − 上方各行与边距/间距
    avail = src[src.index("def _available_panel_height") : src.index("def _release_panel_area")]
    assert "self.height() - used" in avail


def test_panel_is_clipping_shell_over_fixed_body():
    """★ 面板必须是「裁剪外壳 + 钉住内容」两层结构

    若内容直接挂在面板上，逐帧 ``setFixedHeight`` 时面板内 ``QVBoxLayout`` 会
    **压缩子控件**（内嵌滚动区自带 ``setMinimumHeight(40)``）→ 视觉上「内容被
    挤扁往中间缩」。故内容挂在 body，动画期间 body 高度钉死、只被外壳裁剪。
    """
    src = _read(_PAGE)
    assert "self._project_panel_body = body" in src
    # 动画期间 body 被钉住（setFixedHeight），而非随外壳收缩
    body = src[src.index("def _set_project_panel_visible") : src.index("def _measure_panel_height")]
    assert "body.setFixedHeight(target)" in body  # 展开方向钉住
    assert "body.setFixedHeight(max(body.height(), start))" in body  # 收起方向同样钉住


def test_panel_height_driver_uses_fixed_height_not_maximum():
    """★ 动画驱动必须是逐帧 ``setFixedHeight``，不是 ``maximumHeight``

    ``maximumHeight`` 只给父布局一个上限，控件实际高度仍由布局施舍；面板内有
    最小高度约束的子控件时每帧重排 → 双向收缩。正解是驱动对象逐帧
    ``setFixedHeight``（与宿主 ``expand_height_mixin._CardHeightDriver`` 同机制）。
    """
    src = _read(_PAGE)
    assert "class _PanelHeightDriver(QObject)" in src
    driver = src[src.index("class _PanelHeightDriver") : src.index("class _HeaderChevron")]
    assert "setFixedHeight(self._value)" in driver
    assert "pyqtProperty(int" in driver
    anim_body = src[src.index("def _set_project_panel_visible") : src.index("def _measure_panel_height")]
    assert 'QPropertyAnimation(driver, b"value"' in anim_body
    # 不得用 maximumHeight 属性做动画（docstring 里的对比说明不算）
    assert 'QPropertyAnimation(self._project_panel, b"maximumHeight")' not in anim_body


def test_panel_expand_anim_is_single_directional():
    """★ 动画必须单向：面板与列表区同属一个 QVBoxLayout，若动画期间列表区可见，
    布局会把腾出/收回的空间分给它 → 表现为「上面往下压、下面往上顶」两头夹。

    守卫：动画启动前先把下方区域冻结（隐藏），动画结束才交还。
    """
    src = _read(_PAGE)
    body = src[src.index("def _set_project_panel_visible") : src.index("def _release_panel_area")]
    # 冻结语句必须出现在启动动画之前
    freeze = body.index("self._scroll_area.setVisible(False)")
    start = body.index("anim.start()")
    assert freeze < start, "必须先把列表区冻结再启动动画（否则下方会往上顶）"
    assert "self._hint.setVisible(False)" in body[:start]
    # 展开起点必须真实为 0：先 setFixedHeight(0) 再启动，避免 setVisible 那次
    # 布局按 sizeHint 撑满一帧（先跳后动）
    assert body.index("self._project_panel.setFixedHeight(0)") < start
    # 终态交还走统一出口
    assert "def _release_panel_area" in src
    assert "self._project_panel.setVisible(False)" in src[src.index("def _release_panel_area") :]


def test_panel_anim_interrupt_releases_area():
    """动画被打断（快速连点）不得留下「半开高度 + 列表区消失」的坏状态"""
    src = _read(_PAGE)
    stop = src[src.index("def _stop_project_panel_anim") : src.index("def _on_project_panel_anim_finished")]
    assert "_release_panel_area()" in stop, "stop 后必须补齐终态（否则面板停在半高且列表不回来）"


def test_panel_release_clears_both_min_and_max():
    """动画终态必须同时放开 min/max

    动画期间走的是 ``setFixedHeight``（min 与 max 被同时收紧）；只放开
    maximumHeight 的话面板会永久卡死在动画末值高度。
    """
    src = _read(_PAGE)
    release = src[src.index("def _release_panel_area") : src.index("def _stop_project_panel_anim")]
    assert "body.setMinimumHeight(0)" in release
    assert "body.setMaximumHeight(_PANEL_H_UNLIMITED)" in release
    assert "self._project_panel.setMinimumHeight(0)" in release
    assert "self._project_panel.setMaximumHeight(_PANEL_H_UNLIMITED)" in release


def test_project_header_is_visually_prominent():
    """折叠头视觉强化：左侧项目色条 + 展开态/收起态可辨 + 旋转箭头动画"""
    src = _read(_PAGE)
    header = (
        src[src.index("class _HeaderChevron") : src.index("def _alpha_tint")]
        + src[src.index("def _alpha_tint") : src.index("def _active_history_manager")]
    )
    assert "border-left: 3px solid" in header  # 左侧项目色条
    assert "BORDER_ACCENT" in header  # 展开/hover 描边强调色
    assert "CARD_BG" in header or "HOVER_BG_STRONG" in header  # 更实的底色
    assert "QPropertyAnimation" in header  # 箭头旋转动画
    assert "bind_theme_qss" in src[: src.index("class _HeaderChevron")] or "bind_theme_qss" in header
