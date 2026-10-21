# -*- coding: utf-8 -*-
"""回归测试：欢迎卡片 tab 条（自绘胶囊 + emoji 剥离 + 无徽章会话卡）

改造背景（2026-09-17 用户反馈「tab 移到下方、美化、去拖泥带水」）：
1. tab 条原先用 qfluentwidgets ``SegmentedWidget``，自带蓝色下划指示条 +
   硬编码 14px 字号，与顶栏 / 工作台页签的自绘胶囊（``CustomTabButton`` +
   ``TabIndicatorController``）视觉不一致。
2. tab 与头像挤在同一行右端，切换动线横跨整卡。
3. 插件 label 自带彩色 emoji（🤖 / 📜 / 📅），与卡片内线性图标体系混排显脏。
4. 会话卡左侧 30px 彩色 emoji 徽章、分区标题 emoji + 计数胶囊、消息数警示橙
   均属视觉噪音，信息量为零。

测试策略：
- 纯 HTML 渲染函数（``_render_sessions_body``）直接断言输出结构。
- ``_strip_label_emoji`` 是静态纯函数，直接断言。
- tab 条构造走 offscreen 真实 widget（布局/几何断言）。
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication, QVBoxLayout, QWidget  # noqa: E402

from app.plugins.registries.ui_plugin_registry import UIPluginRegistry  # noqa: E402
from app.widgets import message_card as mc  # noqa: E402


@pytest.fixture(scope="module")
def _qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _cleanup_registry():
    UIPluginRegistry.get_instance().reset()
    yield
    UIPluginRegistry.get_instance().reset()


_SAMPLE = [
    {
        "title": "PyQt5性能优化",
        "session_id": "a1",
        "last_time": "2026-09-10 12:00:00",
        "message_count": 38,
        "created_at": "2026-09-03 10:00:00",
    },
    {
        "title": "插件系统评估与规划",
        "session_id": "b2",
        "last_time": "2026-09-11 09:30:00",
        "message_count": 30,
        "created_at": "2026-08-14 10:00:00",
    },
]


# ─── 1. emoji 剥离（插件 label 字面量不动，渲染层清洗）───────────


class TestStripLabelEmoji:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("🤖 助手", "助手"),
            ("📜 更新", "更新"),
            ("📅 日历", "日历"),
            ("💬 会话", "会话"),
            ("  📊  用量 ", "用量"),
            ("会话", "会话"),
            ("助手", "助手"),
        ],
    )
    def test_strips_leading_emoji_and_space(self, raw, expected):
        assert mc.MessageCard._strip_label_emoji(raw) == expected

    def test_empty_label_falls_back_to_input(self):
        """全 emoji 标签剥离后为空时回退原串，避免 tab 变成空胶囊"""
        assert mc.MessageCard._strip_label_emoji("🎯") == "🎯"


# ─── 2. 会话卡视觉（去 emoji 徽章 / 去计数胶囊 / 中性色 tag）─────────


class TestSessionsBodyVisual:
    def test_no_emoji_badges_or_section_icons(self):
        html = mc._render_sessions_body(_SAMPLE, _SAMPLE)
        for marker in ("📅", "🔥", "💬", "⚡"):
            assert marker not in html, f"emoji 残留: {marker}"

    def test_no_badge_or_section_count_classes(self):
        html = mc._render_sessions_body(_SAMPLE, _SAMPLE)
        assert "session-item-badge" not in html
        assert "session-header-icon" not in html
        assert "session-header-count" not in html

    def test_count_tag_is_neutral(self):
        """最活跃会话的消息数 tag 不再带警示橙类名"""
        html = mc._render_sessions_body(_SAMPLE, _SAMPLE)
        assert "session-item-tag-warn" not in html
        assert '<span class="session-item-tag">38 条</span>' in html

    def test_section_titles_kept(self):
        html = mc._render_sessions_body(_SAMPLE, _SAMPLE)
        assert "最近会话" in html
        assert "最活跃会话" in html

    def test_click_chain_preserved(self):
        """视觉改造不能碰点击链（context-tag + data-session-id）"""
        html = mc._render_sessions_body(_SAMPLE, _SAMPLE)
        assert 'class="context-tag session-item"' in html
        assert 'data-type="session"' in html
        assert 'data-session-id="a1"' in html

    def test_css_dead_rules_removed(self):
        """废弃 CSS 规则同步删除，避免留死代码"""
        src = mc.__file__
        with open(src, encoding="utf-8") as fh:
            text = fh.read()
        for dead in (".session-item-badge {", ".session-header-icon {", ".session-header-count {"):
            assert dead not in text, f"死 CSS 残留: {dead}"


# ─── 3. tab 条构造（自绘胶囊 + 布局位置）────────────────────────


def _make_card(_qt_app):
    from app.widgets.message_card import create_welcome_card

    card = create_welcome_card(
        parent=None,
        agent_name="Drifox",
        agent_description="",
        recent_sessions=_SAMPLE,
        top_by_count=_SAMPLE,
        mode="sessions",
    )
    return card


class TestWelcomeTabBar:
    def test_uses_native_capsule_not_segmented_widget(self, _qt_app):
        card = _make_card(_qt_app)
        try:
            assert getattr(card, "_welcome_tab_host", None) is not None
            assert card._welcome_tab_ids[0] == "sessions"
            # 旧组件不应再出现在卡片子树里
            from qfluentwidgets import SegmentedWidget

            assert not card.findChildren(SegmentedWidget)
        finally:
            card.deleteLater()

    def test_buttons_are_custom_capsules(self, _qt_app):
        card = _make_card(_qt_app)
        try:
            from app.widgets.custom_title_bar import CustomTabButton

            assert card._welcome_tab_buttons
            for btn in card._welcome_tab_buttons:
                assert isinstance(btn, CustomTabButton)
        finally:
            card.deleteLater()

    def test_tab_bar_sits_below_content(self, _qt_app):
        """tab 条必须在卡片底部（内容下方），且头部已无头像行"""
        card = _make_card(_qt_app)
        try:
            card.resize(900, 700)
            card.show()
            _qt_app.processEvents()
            host = card._welcome_tab_host
            assert host is not None
            # 头部头像行已移除（2026-09-17 用户要求极简）
            assert not hasattr(card, "_av_label"), "welcome 卡片不该再建头像控件"
            # 内容区（viewer 容器）底边应在 tab 条之上
            container = card._viewer_container
            assert host.y() >= container.y(), "tab 条未落到内容区下方"
        finally:
            card.close()
            card.deleteLater()

    def test_header_separator_removed(self, _qt_app):
        """welcome 卡片不再有头部/底部分隔线"""
        from qfluentwidgets.components.widgets.card_widget import CardSeparator

        card = _make_card(_qt_app)
        try:
            seps = [c for c in card.findChildren(CardSeparator) if c.parent() is card]
            assert not seps, f"welcome 卡片仍有 {len(seps)} 条分隔线"
        finally:
            card.deleteLater()

    def test_tabs_are_centered(self, _qt_app):
        """tab 条水平居中：整组左右留白近似相等"""
        card = _make_card(_qt_app)
        try:
            card.resize(900, 700)
            card.show()
            for _ in range(8):
                _qt_app.processEvents()
            host = card._welcome_tab_host
            btns = card._welcome_tab_buttons
            assert btns
            left = min(b.geometry().x() for b in btns)
            right = max(b.geometry().right() for b in btns)
            left_gap = left
            right_gap = host.width() - 1 - right
            assert abs(left_gap - right_gap) <= 6, f"未居中：左 {left_gap} / 右 {right_gap}"
        finally:
            card.close()
            card.deleteLater()

    def test_plugin_tab_label_emoji_stripped(self, _qt_app):
        """插件注册的带 emoji label 在按钮文本上被清洗"""
        UIPluginRegistry.get_instance().register_welcome_tab(
            plugin_name="t",
            mode_key="changelog",
            label="📜 更新",
            render_func=lambda ctx: "<div>x</div>",
        )
        card = _make_card(_qt_app)
        try:
            assert "changelog" in card._welcome_tab_ids
            idx = card._welcome_tab_ids.index("changelog")
            assert card._welcome_tab_buttons[idx]._label.text() == "更新"
        finally:
            card.deleteLater()

    def test_switch_mode_syncs_highlight_without_animation(self, _qt_app):
        """静态路径（不经点击）切 mode 时高亮要跟着走"""
        UIPluginRegistry.get_instance().register_welcome_tab(
            plugin_name="t",
            mode_key="changelog",
            label="更新",
            render_func=lambda ctx: "<div>x</div>",
        )
        card = _make_card(_qt_app)
        try:
            card.set_welcome_mode("changelog")
            idx = card._welcome_tab_ids.index("changelog")
            assert card._welcome_tab_buttons[idx]._active is True
            i0 = card._welcome_tab_ids.index("sessions")
            assert card._welcome_tab_buttons[i0]._active is False
        finally:
            card.deleteLater()

    def test_click_does_not_kill_slide_animation(self, _qt_app):
        """点击路径不能让 set_welcome_mode 用 animate=False 把滑动动画盖掉"""
        UIPluginRegistry.get_instance().register_welcome_tab(
            plugin_name="t",
            mode_key="changelog",
            label="更新",
            render_func=lambda ctx: "<div>x</div>",
        )
        card = _make_card(_qt_app)
        try:
            card.resize(900, 700)
            card.show()
            _qt_app.processEvents()
            ctl = card._welcome_indicator_ctl
            assert ctl is not None
            card._on_welcome_mode_tab_clicked("changelog")
            assert ctl.running is True, "点击后滑动动画被瞬移覆盖"
        finally:
            card.close()
            card.deleteLater()

    def test_font_follows_system_delta(self, _qt_app):
        """tab 字号随系统字号 delta 缩放（CustomTabButton 不读 Qt 全局字体）"""
        card = _make_card(_qt_app)
        try:
            assert card._welcome_tab_buttons
            from app.utils.design_tokens import scale_font_size

            expected = scale_font_size(mc.MessageCard._WELCOME_TAB_FONT)
            assert card._welcome_tab_buttons[0]._font_size == expected
        finally:
            card.deleteLater()

    def test_host_height_matches_actual_rows(self, _qt_app):
        """宿主高度必须等于实测折行高度

        坑：Qt5 对「子布局带 heightForWidth」的 widget 用 minimumWidth 估高，
        6 个 tab 会被当成 3 行 → 卡片底部多出 60px 空白。现按实测宽度主动设高。
        """
        UIPluginRegistry.get_instance().register_welcome_tab(
            plugin_name="t",
            mode_key="changelog",
            label="更新",
            render_func=lambda ctx: "<div>x</div>",
        )
        card = _make_card(_qt_app)
        try:
            card.resize(1200, 600)
            card.show()
            for _ in range(8):
                _qt_app.processEvents()
            host = card._welcome_tab_host
            host_h = host.height()
            assert host_h > 0
            rows = {b.geometry().y() for b in card._welcome_tab_buttons}
            # 每个按钮都必须落在宿主矩形内（没被裁）
            for btn in card._welcome_tab_buttons:
                assert btn.geometry().bottom() <= host_h, f"{btn.tab_id} 超出宿主高度 {host_h}"
            # 宽卡片下两个 tab 应同行，宿主高度不超单行（无底部空白）
            assert len(rows) == 1, f"宽卡片下不该折行，实测 {len(rows)} 行"
            assert host_h < 2 * min(row_h for row_h in (b.height() for b in card._welcome_tab_buttons)) + 8, (
                f"两个同行 tab 却占了 {host_h}px 高度"
            )
        finally:
            card.close()
            card.deleteLater()

    def test_host_height_grows_when_wrapped(self, _qt_app):
        """窄宽度下 tabs 折行，宿主高度随之增加（不能裁掉第二行）"""
        reg = UIPluginRegistry.get_instance()
        for key, label in (("changelog", "更新"), ("calendar", "日历"), ("usage", "用量"), ("dash", "项目看板")):
            reg.register_welcome_tab(
                plugin_name=f"p{key}", mode_key=key, label=label, render_func=lambda ctx: "<div>x</div>"
            )
        card = _make_card(_qt_app)
        try:
            card.resize(1200, 600)
            card.show()
            for _ in range(8):
                _qt_app.processEvents()
            rows_wide = {b.geometry().y() for b in card._welcome_tab_buttons}
            wide_h = card._welcome_tab_host.height()

            card.resize(260, 600)
            for _ in range(8):
                _qt_app.processEvents()
            rows_narrow = {b.geometry().y() for b in card._welcome_tab_buttons}
            narrow_h = card._welcome_tab_host.height()

            assert len(rows_narrow) > len(rows_wide), f"窄宽度未折行（{rows_wide} -> {rows_narrow}）"
            assert narrow_h > wide_h, f"窄宽度未增高（wide={wide_h} narrow={narrow_h}）"
            # 每个按钮都必须落在宿主矩形内（第二行没被裁）
            for btn in card._welcome_tab_buttons:
                assert btn.geometry().bottom() <= narrow_h, f"{btn.tab_id} 超出宿主高度 {narrow_h}"
        finally:
            card.close()
            card.deleteLater()


# ─── 4. hover 残留回归（TabHoverSyncHost 兜底 + _start 停在途动画）─────────


class TestWelcomeTabHoverResidue:
    """2026-09-23 用户反馈「底部 tab 残留 hover」双根因：

    1. ``CustomTabButton._start`` 快速路径（current≈target）只 return 不
       stop，enter 后不足一帧就 leave 时在途动画独自跑完把 ``_hover_t``
       推到 1（快速扫过一排 tab 必现）；
    2. 子按钮 leaveEvent 在布局重排 / 抢焦点等场景不保证到达（顶栏
       CustomTitleBar._clear_tab_hover 同源教训），此前无宿主层兜底。
    """

    def test_host_is_hover_sync_host(self, _qt_app):
        """tab 宿主必须是 TabHoverSyncHost（带 Leave 清理 + 布局重算兜底）"""
        from app.widgets.custom_title_bar import TabHoverSyncHost

        card = _make_card(_qt_app)
        try:
            assert isinstance(card._welcome_tab_host, TabHoverSyncHost)
        finally:
            card.deleteLater()

    def test_inflight_hover_animation_stopped_on_revert(self, _qt_app):
        """根因 1 回归：置 hover 后一帧内撤销，在途动画必须被停掉

        旧行为：_start 快速路径只 return，0→1 动画继续跑完，_hover_t 残留 1.0。
        """
        import time

        from app.widgets.custom_title_bar import CustomTabButton, TabHoverSyncHost

        holder = QWidget()  # 模块级持引用防 GC（项目坑：QWidget 无主析构崩溃）
        host = TabHoverSyncHost(holder)
        lay = QVBoxLayout(host)
        lay.setContentsMargins(0, 0, 0, 0)
        btn = CustomTabButton("t0", "会话", host, font_size=12)
        lay.addWidget(btn)
        holder._ref_btn = btn
        holder._ref_host = host
        host.show()
        _qt_app.processEvents()

        btn.set_hover(True)  # 动画启动，尚未推进首帧
        btn.set_hover(False)  # 一帧内撤销
        for _ in range(40):
            _qt_app.processEvents()
            time.sleep(0.01)
        _qt_app.processEvents()
        try:
            assert btn._hover_t < 0.001, f"在途动画未被停掉，_hover_t={btn._hover_t}"
        finally:
            holder.deleteLater()

    def test_leave_event_clears_all_hover(self, _qt_app):
        """根因 2 回归：Leave 事件兜底清空全部按钮 hover"""
        from PyQt5.QtCore import QEvent

        from app.widgets.custom_title_bar import CustomTabButton, TabHoverSyncHost

        holder = QWidget()
        host = TabHoverSyncHost(holder)
        lay = QVBoxLayout(host)
        lay.setContentsMargins(0, 0, 0, 0)
        btns = [CustomTabButton(f"t{i}", f"标签{i}", host, font_size=12) for i in range(3)]
        for b in btns:
            lay.addWidget(b)
        holder._ref_btns = btns
        holder._ref_host = host
        host.show()
        _qt_app.processEvents()
        for b in btns:
            b.set_hover(True)
        _qt_app.sendEvent(host, QEvent(QEvent.Leave))
        try:
            assert all(not b._hovered for b in btns)
        finally:
            holder.deleteLater()

    def test_layout_change_reschedules_hover_sync(self, _qt_app):
        """布局重排（Resize/LayoutRequest）后延迟一拍重算 hover"""
        from PyQt5.QtCore import QEvent

        from app.widgets.custom_title_bar import TabHoverSyncHost

        holder = QWidget()
        host = TabHoverSyncHost(holder)
        holder._ref_host = host
        host.show()
        _qt_app.processEvents()
        calls = []
        host.sync_tab_hover = lambda: calls.append(1)  # 记录重算调用
        _qt_app.sendEvent(host, QEvent(QEvent.Resize))
        assert host._hover_resync_pending, "Resize 后未登记重算"
        _qt_app.processEvents()
        try:
            assert calls and not host._hover_resync_pending, "重算未被事件循环消费"
        finally:
            holder.deleteLater()
