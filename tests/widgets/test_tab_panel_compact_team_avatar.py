# -*- coding: utf-8 -*-
"""折叠态团队成员块视觉回归测试（hsl 角色色 / 状态环 / 居中边距）

背景：折叠态成员块原先一律灰色。根因是角色胶囊色为 ``hsl(h, s%, l%)`` 形式，
``QColor("hsl(...)")`` 恒 invalid、``_TabProjectIcon._parse_rgba`` 也只认
``rgba()``/``#``，全部落到灰色兜底 (128,128,128)。同色解析问题修完后，26px
方块仍无法用整行流光表达运行态，改用方块外侧 2px 状态环。
"""

from unittest.mock import patch

import pytest
from PyQt5.QtGui import QColor

from app.widgets.tab_panel import TabPanel, _parse_rgba


@pytest.fixture
def panel(qtbot):
    with patch("app.widgets.cards.settings.gitee_card.GiteeAccountRow._auto_enable_sync"):
        p = TabPanel()
    p.set_mode("list", persist=False)
    qtbot.addWidget(p)
    return p


def _team_member(panel, role: str, title: str = "成员", team_id: str = "teamX"):
    """建一个团队成员 tab 并返回其 TabItem"""
    idx = panel.add_tab(title)
    panel.set_tab_team(idx, team_id)
    panel.set_tab_team_mode(idx, True)
    panel.update_tab_capsule(idx, role)
    return panel._items[idx]


# ══════════════════════════════════════════════════════════
# hsl 解析（全灰根因）
# ══════════════════════════════════════════════════════════


class TestHslParsing:
    def test_module_parse_rgba_supports_hsl(self):
        """hsl 串必须换算成有效颜色（QColor(str) 对 hsl 恒 invalid）"""
        c = _parse_rgba("hsl(333, 65%, 50%)")
        assert c.isValid()
        # 与 QSS 渲染同一色相（QColor.fromHsl(333, 166, 128) 实测 (209,45,119)）
        assert abs(c.red() - 209) <= 3 and abs(c.green() - 45) <= 3 and abs(c.blue() - 119) <= 3

    def test_different_hues_produce_different_colors(self):
        """不同角色色相 → 不同颜色（成员块可区分的前提）"""
        hues = [_parse_rgba(f"hsl({h}, 65%, 50%)") for h in (204, 265, 333)]
        assert len({(c.red(), c.green(), c.blue()) for c in hues}) == 3

    def test_rgba_and_hex_unchanged(self):
        """既有 rgba / #hex 分支零回归"""
        assert _parse_rgba("rgba(33,139,255,255)").getRgb() == (33, 139, 255, 255)
        assert _parse_rgba("#2E7D32").getRgb()[:3] == (46, 125, 50)

    def test_icon_parse_rgba_supports_hsl(self):
        """_TabProjectIcon._parse_rgba 复用模块级实现，同样认 hsl"""
        from app.widgets.tab_panel import _TabProjectIcon

        c = _TabProjectIcon._parse_rgba("hsl(204, 65%, 50%)")
        assert c.isValid()
        assert (c.red(), c.green(), c.blue()) != (128, 128, 128)


# ══════════════════════════════════════════════════════════
# 折叠态成员块颜色 / 首字母
# ══════════════════════════════════════════════════════════


class TestCollapsedMemberColor:
    def test_role_color_not_gray(self, panel):
        """折叠态成员块使用角色色，不再是灰色兜底"""
        panel.set_collapsed(True)
        item = _team_member(panel, "leader")
        assert item._icon_widget._color.isValid()
        assert item._icon_widget._color.getRgb()[:3] != (128, 128, 128)

    def test_distinct_members_distinct_colors(self, panel):
        """不同角色 → 方块颜色互不相同（折叠态可分辨）"""
        panel.set_collapsed(True)
        roles = ["leader", "coder", "build"]
        cols = []
        for role in roles:
            item = _team_member(panel, role)
            c = item._icon_widget._color
            cols.append((c.red(), c.green(), c.blue()))
        assert len(set(cols)) == len(roles), f"角色色应互不相同: {cols}"

    def test_latin_role_initial_uppercased(self, panel):
        """拉丁角色首字母大写（26px 方块里小写只剩 x-height，偏小偏轻）"""
        panel.set_collapsed(True)
        assert _team_member(panel, "leader")._icon_widget._initials == "L"
        assert _team_member(panel, "coder")._icon_widget._initials == "C"

    def test_cjk_role_initial_unchanged(self, panel):
        """中文字符不受大写影响"""
        panel.set_collapsed(True)
        assert _team_member(panel, "主持人")._icon_widget._initials == "主"


# ══════════════════════════════════════════════════════════
# 状态环（折叠态运行状态可读性）
# ══════════════════════════════════════════════════════════


class TestCompactStatusRing:
    def test_idle_has_no_ring(self, panel):
        panel.set_collapsed(True)
        item = _team_member(panel, "coder")
        assert item._icon_widget._ring_state == ""

    def test_streaming_ring(self, panel):
        panel.set_collapsed(True)
        item = _team_member(panel, "coder")
        panel.update_tab_streaming(panel._items.index(item), True)
        assert item._icon_widget._ring_state == "streaming"

    def test_question_ring(self, panel):
        panel.set_collapsed(True)
        item = _team_member(panel, "coder")
        panel.update_tab_question(panel._items.index(item), True)
        assert item._icon_widget._ring_state == "question"

    def test_error_ring_overrides_streaming(self, panel):
        """error 优先级高于 streaming（同一 tab 两者并存时以红环为准）"""
        panel.set_collapsed(True)
        item = _team_member(panel, "coder")
        panel.update_tab_streaming(panel._items.index(item), True, True)
        assert item._icon_widget._ring_state == "error"

    def test_ring_cleared_when_status_ends(self, panel):
        panel.set_collapsed(True)
        item = _team_member(panel, "coder")
        idx = panel._items.index(item)
        panel.update_tab_streaming(idx, True)
        panel.update_tab_streaming(idx, False)
        assert item._icon_widget._ring_state == ""

    def test_ring_kept_on_expand(self, panel):
        """展开态保留状态环（环是唯一状态表达，不随折叠切换）"""
        panel.set_collapsed(True)
        item = _team_member(panel, "coder")
        panel.update_tab_streaming(panel._items.index(item), True)
        panel.set_collapsed(False)
        assert item._icon_widget._ring_state == "streaming"

    def test_non_team_tab_also_has_ring(self, panel):
        """非团队 Tab（项目 icon 语义）折叠态同样画状态环

        折叠窄条下项目 icon 是唯一可见元素，整行流光已在这个宽度下失效，
        运行态只能靠环表达——项目 tab 与团队角色块共用同一套。
        """
        panel.set_collapsed(True)
        idx = panel.add_tab("普通会话", project_initials="DF", project_color="#2E7D32")
        panel.update_tab_streaming(idx, True)
        assert panel._items[idx]._icon_widget._ring_state == "streaming"

    def test_ring_kept_when_expanded(self, panel):
        """展开态保留状态环（整行流光已移除，环是唯一状态表达）"""
        panel.set_collapsed(True)
        idx = panel.add_tab("普通会话", project_initials="DF", project_color="#2E7D32")
        panel.update_tab_streaming(idx, True)
        panel.set_collapsed(False)
        assert panel._items[idx]._icon_widget._ring_state == "streaming"

    def test_streaming_ring_visible_in_both_themes(self):
        """流式环在两种主题下都必须可见：环色与侧栏底有足够亮度差

        反例（实测）：直接复用 _RAINBOW_COLORS 再提亮 55，浅色主题下环色 lum
        逼近 0.9，2px 细线在 #fafafc 底上淡到看不见。故环色按主题定 L。
        """
        from PyQt5.QtGui import qGray

        from app.widgets.tab_panel import _TabProjectIcon

        icon = _TabProjectIcon(size=26)
        icon.set_project("DF", "rgba(46, 125, 50, 255)")
        icon.set_status_ring("streaming", 0.0)
        c = icon._ring_color()
        gray = qGray(c.rgb())
        # 浅色主题（测试默认）: 侧栏底 ~250，环必须显著更暗
        assert gray < 190, f"浅色主题下环过亮，会被底色淹没: gray={gray}"

    def test_streaming_ring_hue_rotates_with_phase(self):
        """流式环色随相位轮转（沿用团队块动效：色相轮回即「在转」信号）"""
        from app.widgets.tab_panel import _TabProjectIcon

        icon = _TabProjectIcon(size=26)
        icon.set_project("DF", "rgba(46, 125, 50, 255)")
        icon.set_status_ring("streaming", 0.0)
        c0 = icon._ring_color().hue()
        icon.set_status_ring("streaming", 180.0)
        c180 = icon._ring_color().hue()
        assert c0 != c180, "相位推进必须改变色相"

    def test_streaming_ring_contrasts_with_block_color(self):
        """环色与方块色不同色系（红方块配绿环等），不糊成一片

        方块就是项目色，环若取同色系高亮版，2px 细线会被方块吃掉。
        """
        from app.widgets.tab_panel import _TabProjectIcon

        # 绿方块 + 相位 0（红环）：色相差异必须显著
        icon = _TabProjectIcon(size=26)
        icon.set_project("DF", "rgba(46, 125, 50, 255)")
        icon.set_status_ring("streaming", 0.0)
        ring_h = icon._ring_color().hslHue()
        block_h = icon._color.hslHue()
        diff = min(abs(ring_h - block_h), 360 - abs(ring_h - block_h))
        assert diff > 30, f"环与方块色相太近，会糊成一片: ring={ring_h} block={block_h}"

    def test_ring_color_breathing_streaming(self, panel):
        """question 环 alpha 随相位呼吸（不同相位亮度不同）"""
        panel.set_collapsed(True)
        item = _team_member(panel, "coder")
        icon = item._icon_widget
        icon.set_status_ring("question", 90.0)
        a90 = icon._ring_color().alpha()
        icon.set_status_ring("question", 270.0)
        a270 = icon._ring_color().alpha()
        assert a90 != a270
        assert all(0 <= a <= 255 for a in (a90, a270))

    def test_error_ring_constant(self):
        """error 环恒亮，不参与呼吸（报错是需一眼确认的事实）"""
        from app.widgets.tab_panel import _TabProjectIcon

        icon = _TabProjectIcon(size=26)
        icon.set_status_ring("error", 90.0)
        a90 = icon._ring_color().alpha()
        icon.set_status_ring("error", 270.0)
        assert icon._ring_color().alpha() == a90

    def test_block_size_stable_across_status(self, panel, qtbot):
        """方块几何与缩写字号与状态无关（不抽搐）

        实现方式：容器紧凑态放大到 26px 给环让位，方块恒 20px（内缩 3px），
        状态来去只改绘制，不改几何。
        """
        panel.setFixedWidth(46)
        panel.show()
        panel.set_collapsed(True)
        item = _team_member(panel, "coder")
        qtbot.wait(30)
        icon = item._icon_widget
        box_idle, inset_idle = icon.size(), icon._block_inset
        panel.update_tab_streaming(panel._items.index(item), True)
        qtbot.wait(30)
        assert icon.size() == box_idle
        assert icon._block_inset == inset_idle
        # 方块恒 = 容器 - 2 * 内缩，故方块尺寸也不变
        assert icon.width() - 2 * icon._block_inset == box_idle.width() - 2 * inset_idle

    def test_icon_box_always_reserves_ring(self, panel):
        """外框恒留环位（折叠/展开同档），方块尺寸由内缩守恒"""
        panel.set_collapsed(True)
        item = _team_member(panel, "coder")
        icon = item._icon_widget
        compact_box, compact_inset = icon.width(), icon._block_inset
        assert compact_box > item._icon_size
        panel.set_collapsed(False)
        # 展开态外框不退回到 icon 尺寸（环仍要画）
        assert icon.width() == compact_box
        assert icon._block_inset == compact_inset
        # 方块 = 外框 - 2*内缩，两种形态同一值
        assert icon.width() - 2 * icon._block_inset == item._icon_size

    def test_compact_box_team_fits_row(self, panel):
        """团队模式列表态行高 32px：外框不得超出行高（否则被裁）"""
        panel.set_collapsed(True)
        item = _team_member(panel, "coder")
        assert item.height() == 32
        assert item._icon_widget.height() <= 32

    def test_tooltip_carries_state_in_compact(self, panel):
        """折叠态 icon tooltip 带上状态文案（环只表达类别，细节靠 hover）"""
        panel.set_collapsed(True)
        item = _team_member(panel, "coder")
        idx = panel._items.index(item)
        panel.update_tab_streaming(idx, True)
        assert "正在生成" in item._icon_widget.toolTip()
        panel.update_tab_streaming(idx, False)
        panel.update_tab_question(idx, True)
        assert "等待你的回答" in item._icon_widget.toolTip()

    def test_tooltip_plain_when_expanded(self, panel):
        """展开态 tooltip 只留标题（不重复状态，标题本身可见）"""
        panel.set_collapsed(True)
        item = _team_member(panel, "coder")
        panel.update_tab_streaming(panel._items.index(item), True)
        panel.set_collapsed(False)
        assert item._icon_widget.toolTip() == item._title


# ══════════════════════════════════════════════════════════
# 折叠态团队框边距（窄条居中，消除 3px 偏右）
# ══════════════════════════════════════════════════════════


class TestCompactTeamMargins:
    def test_margins_tightened_and_restored(self, panel):
        """折叠收紧边距、展开还原（往返对称）"""
        _team_member(panel, "coder")
        grp = panel._team_groups["teamX"]
        gp = grp.layout()
        orig = gp.getContentsMargins()
        assert orig == (6, 4, 6, 4)

        panel.set_collapsed(True)
        assert gp.getContentsMargins() == (1, 3, 1, 3)

        panel.set_collapsed(False)
        assert gp.getContentsMargins() == orig

    def test_repeated_collapse_idempotent(self, panel):
        """重复折叠/展开 3 轮后边距仍是展开态原值（不累积漂移）"""
        _team_member(panel, "coder")
        gp = panel._team_groups["teamX"].layout()
        for _ in range(3):
            panel.set_collapsed(True)
            panel.set_collapsed(False)
        assert gp.getContentsMargins() == (6, 4, 6, 4)
