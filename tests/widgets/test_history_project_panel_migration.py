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
    assert "self._set_tab_filter(project" in body  # 按标签页分桶写筛选记忆


def test_panel_expand_takes_full_height():
    """面板展开时让出会话列表区域，占满切换区高度（不再限高；走交叉动画）

    ★ 面板与列表区同挂 ``_swap_host``、stretch 皆为 1：展开态面板占满 host，
    收起态列表占满 host。动画期间由 ``setFixedHeight`` 硬控（min=max），
    stretch 抢不走。
    """
    src = _read(_PAGE)
    assert "_PROJECT_PANEL_MAX_HEIGHT" not in src
    assert "layout.addWidget(self._swap_host, 1)" in src
    assert "swap_lay.addWidget(self._project_panel, 1)" in src
    # 展开/收起走交叉动画
    body = src[src.index("def _set_project_panel_visible") : src.index("def _sync_list_visibility")]
    assert "QParallelAnimationGroup" in body
    assert "_PANEL_EXPAND_MS" in body
    assert "Animations.motion_enabled()" in body  # 减少动效时直置终值


def test_panel_crossfade_total_comes_from_host_height():
    """★ 交叉总量取「切换区实际高度」，既不是内容高度也不是卡片高度估算

    三条硬约束（各自对应一次实测故障）：
    1. 按内容 sizeHint 定高 → 项目少时面板只占约 100px，下方一大片空白；
    2. 按「卡片高度 − 常量行高」估算（旧 ``_available_panel_height``）→
       行高常量与真实布局脱节，面板比可用区差几像素 → 内容被挤出或留缝；
    3. 按「面板高 + 列表高」→ **隐藏端不被布局更新、残留上次高度**
       （实测初始 panel=30 而 shell=626，和 656 远大于可用 626）。
    """
    src = _read(_PAGE)
    assert "_measure_panel_height" not in src
    assert "_TAB_ROW_H" not in src and "_FILTER_ROW_H" not in src
    body = src[src.index("def _set_project_panel_visible") : src.index("def _sync_list_visibility")]
    assert "self._swap_host.height()" in body
    assert "sizeHint" not in body


def test_panel_and_list_swap_via_crossfade():
    """★ 面板与列表区必须「交叉过渡」，不得「先冻结列表再长面板」

    旧实现在动画第 0 帧就 ``_scroll_area.setVisible(False)``（列表凭空消失）、
    收起时又在末帧才显示（列表凭空出现）→ 两次硬切，这是展开难看的主因。
    现在是两端同帧反向缩放：面板 0↔A、列表区 A↔0。

    守恒前提：两端同挂 ``_swap_host`` 且内部 spacing=0 → 隐藏端不吃外层
    spacing，「面板高 + 列表高」恒等于 host 高度。
    """
    src = _read(_PAGE)
    assert "self._swap_host = QWidget(self)" in src
    assert "swap_lay.setSpacing(0)" in src
    body = src[src.index("def _set_project_panel_visible") : src.index("def _sync_list_visibility")]
    assert "self._cross_driver.set_total(total)" in body
    assert 'QPropertyAnimation(self._cross_driver, b"value"' in body
    # 不得再走「冻结列表区」老路
    assert "self._scroll_area.setVisible(False)" not in body


def test_panel_is_clipping_shell_over_fixed_body():
    """★ 两端都必须是「裁剪外壳 + 钉住内容」两层结构

    若内容直接挂在面板上，逐帧 ``setFixedHeight`` 时面板内 ``QVBoxLayout`` 会
    **压缩子控件**（内嵌滚动区自带 ``setMinimumHeight(40)``）→ 视觉上「内容被
    挤扁往中间缩」。故内容挂在 body，动画期间 body 高度钉死、只被外壳裁剪。
    列表区（``_list_shell`` > ``_list_body``）同构，否则交叉过渡的一侧会重排。
    """
    src = _read(_PAGE)
    assert "self._project_panel_body = body" in src
    assert "self._list_body = QWidget(self._list_shell)" in src
    # 动画期间两侧内容体都被钉住（setFixedHeight），而非随外壳收缩
    body = src[src.index("def _set_project_panel_visible") : src.index("def _sync_list_visibility")]
    assert "body.setFixedHeight(max(total, 1))" in body  # 展开：面板内容钉终高
    assert "body.setFixedHeight(max(panel_from, 1))" in body  # 收起：面板内容钉住
    assert "list_body.setFixedHeight(max(total, 1))" in body  # 列表内容同样钉住


def test_panel_height_driver_uses_fixed_height_not_maximum():
    """★ 动画驱动必须是逐帧 ``setFixedHeight``，不是 ``maximumHeight``

    ``maximumHeight`` 只给父布局一个上限，控件实际高度仍由布局施舍；面板内有
    最小高度约束的子控件时每帧重排 → 双向收缩。正解是驱动对象逐帧
    ``setFixedHeight``（与宿主 ``expand_height_mixin._CardHeightDriver`` 同机制）。
    """
    src = _read(_PAGE)
    assert "class _PanelHeightDriver(QObject)" in src
    driver = src[src.index("class _PanelHeightDriver") : src.index("class _CrossHeightDriver")]
    assert "setFixedHeight(self._value)" in driver
    assert "pyqtProperty(int" in driver
    anim_body = src[src.index("def _set_project_panel_visible") : src.index("def _sync_list_visibility")]
    assert 'QPropertyAnimation(self._cross_driver, b"value"' in anim_body
    # 不得用 maximumHeight 属性做动画（docstring 里的对比说明不算）
    assert 'QPropertyAnimation(self._project_panel, b"maximumHeight")' not in anim_body


def test_cross_driver_sets_both_ends_in_one_pass():
    """★ 两端高度必须由**同一个**驱动互补计算，不能各跑一条动画

    两条独立插值各自取整会累积误差：实测中间帧 411 + 214 = 625，而可用高度是
    626 → 过渡途中底部露 1px 缝。同一驱动 ``head = v, tail = total − v``
    = 严格守恒。
    """
    src = _read(_PAGE)
    driver = src[src.index("class _CrossHeightDriver") : src.index("class _TopPadDriver")]
    assert "self._head.value = head" in driver
    assert "self._tail.value = max(self._total - head, 0)" in driver
    assert "def set_total(self, total: int)" in driver


def test_panel_anim_interrupt_keeps_height_for_takeover():
    """动画被打断（快速连点）保留当前高度供反向续接，不跳回 0

    旧实现 stop 后立刻 ``_release_panel_area()`` 补齐终态 → 连点时先跳到终态
    再从反向起点动（「跳一下再动」）。
    """
    src = _read(_PAGE)
    stop = src[src.index("def _stop_project_panel_anim") : src.index("def _on_project_panel_anim_finished")]
    assert "group.stop()" in stop
    assert "self._release_panel_area()" not in stop  # 不再补齐终态（保留高度续接）
    # 起点从当前可见端的高度续接（隐藏端按 0 计，避免残留旧高度）
    body = src[src.index("def _set_project_panel_visible") : src.index("def _sync_list_visibility")]
    assert "self._cross_driver.value = panel_from" in body
    assert "if panel.isVisible() else 0" in body


def test_panel_release_clears_both_min_and_max():
    """动画终态必须同时放开 min/max（两端都要）

    动画期间走的是 ``setFixedHeight``（min 与 max 被同时收紧）；只放开
    maximumHeight 的话面板会永久卡死在动画末值高度。
    """
    src = _read(_PAGE)
    release = src[src.index("def _release_panel_area") : src.index("def _stop_project_panel_anim")]
    assert "w.setMinimumHeight(0)" in release
    assert "w.setMaximumHeight(_PANEL_H_UNLIMITED)" in release
    # 两端四个 widget 都要放开（漏掉列表区 → 收起后列表卡在 0 高）
    for name in ("self._project_panel_body", "self._list_body", "self._project_panel", "self._list_shell"):
        assert name in release, f"终态漏放开 {name}"


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


# ── 真实几何（离屏）：源码守卫挡不住「布局与逐帧 setFixedHeight 打架」 ──


def _load_page_module():
    """按文件路径导入 history_page（插件目录带连字符，不能走普通 import）"""
    import importlib

    return importlib.import_module("plugins.history-manager.ui.history_page")


def test_panel_crossfade_height_is_conserved_and_monotonic(qapp):
    """展开/收起全程：「面板高 + 列表高」恒等于切换区高度，且高度单调

    这是本文件唯一跑真实布局的用例，覆盖源码断言抓不到的一类回归：
    - 中间帧高度非单调抖动（布局 stretch 与逐帧 setFixedHeight 互抢）；
    - 两端各自取整导致的总和掉 1px（过渡途中底部露缝）。

    动画用 ``pause()`` + ``setCurrentTime`` 手动步进，排除真实时钟干扰。
    """
    mod = _load_page_module()
    page = mod.HistoryPage()
    try:
        page.resize(300, 700)
        page.show()
        qapp.processEvents()
        total = page._swap_host.height()
        if total <= 0:  # 无显示环境 / 尚未布局：跳过几何断言
            return
        panel, shell = page._project_panel, page._list_shell

        for visible, duration in ((True, mod._PANEL_EXPAND_MS), (False, mod._PANEL_COLLAPSE_MS)):
            page._set_project_panel_visible(visible)
            group = page._panel_anim
            group.pause()
            prev = None
            for i in range(13):
                group.setCurrentTime(int(duration * i / 12))
                qapp.processEvents()
                p, s = panel.height(), shell.height()
                assert p + s == total, f"交叉不守恒：{p} + {s} != {total}"
                if prev is not None:
                    assert (p >= prev) if visible else (p <= prev), f"高度非单调：{prev} -> {p}"
                prev = p
            group.setCurrentTime(duration)
            qapp.processEvents()
        # 收起终态：面板让位、列表区拿回全部高度
        assert panel.height() == 0
        assert shell.height() == total
    finally:
        page.hide()
        page.setParent(None)
        page.deleteLater()


def test_panel_takeover_from_mid_animation_does_not_jump(qapp):
    """连点反向：从当前高度续接，不先跳回 0 / 终值再动"""
    mod = _load_page_module()
    page = mod.HistoryPage()
    try:
        page.resize(300, 700)
        page.show()
        qapp.processEvents()
        total = page._swap_host.height()
        if total <= 0:
            return
        panel = page._project_panel

        page._set_project_panel_visible(True)
        group = page._panel_anim
        group.pause()
        group.setCurrentTime(mod._PANEL_EXPAND_MS // 3)
        qapp.processEvents()
        mid = panel.height()
        assert 0 < mid < total, f"中途高度应在 (0, {total}) 区间，实际 {mid}"

        page._set_project_panel_visible(False)  # 反向接管
        page._panel_anim.pause()
        page._panel_anim.setCurrentTime(0)
        qapp.processEvents()
        assert abs(panel.height() - mid) <= 1, f"反向起点跳变：{mid} -> {panel.height()}"
    finally:
        page.hide()
        page.setParent(None)
        page.deleteLater()
