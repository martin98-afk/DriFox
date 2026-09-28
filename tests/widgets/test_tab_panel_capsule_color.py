# -*- coding: utf-8 -*-
"""角色胶囊颜色跨进程稳定回归测试

背景：set_capsule / _apply_compact_icon 原用内置 ``hash(text)`` 生成色相，
Python str hash 进程级随机化（PYTHONHASHSEED）→ 每次重启角色颜色都变。
现抽 ``_stable_text_hue``（md5）模块级纯函数，两处接入。

- 同文本色相恒定
- 关键角色色相锁死 md5 结果（防未来退回随机 hash）
- set_capsule 不传 color 时胶囊样式使用稳定 hsl 色相
"""

from unittest.mock import patch

import pytest

from app.widgets.tab_panel import TabPanel, _stable_text_hue


@pytest.fixture
def panel(qtbot):
    with patch("app.widgets.cards.settings.gitee_card.GiteeAccountRow._auto_enable_sync"):
        p = TabPanel()
    p.set_mode("list", persist=False)
    qtbot.addWidget(p)
    return p


class TestStableTextHue:
    """_stable_text_hue 纯函数：同文本同色恒定"""

    def test_same_text_same_hue(self):
        assert _stable_text_hue("coder") == _stable_text_hue("coder")
        assert _stable_text_hue("角色·ming") == _stable_text_hue("角色·ming")

    def test_different_text_different_hue(self):
        # 语义校验（非密码学要求）：不同文本色相可区分即可，允许极小概率碰撞
        hues = {_stable_text_hue(t) for t in ("coder", "plan", "build", "leader", "review", "ming")}
        assert len(hues) >= 5, f"6 个常见角色色相应基本可区分，实际 {hues}"

    def test_known_hue_values_locked(self):
        """锁死 md5 色相值：防未来退回进程随机 hash（重启变色回归）"""
        assert _stable_text_hue("coder") == 204
        assert _stable_text_hue("plan") == 7
        assert _stable_text_hue("build") == 265
        assert _stable_text_hue("leader") == 333
        assert _stable_text_hue("review") == 180
        assert _stable_text_hue("ming") == 161


class TestSetCapsuleStableColor:
    """set_capsule 不传 color 时走稳定色相"""

    def test_capsule_style_uses_stable_hue(self, panel):
        idx = panel.add_tab("会话A")
        item = panel._items[idx]
        item.set_capsule("coder")
        expected = "hsl(204, 65%, 50%)"
        assert expected in item._capsule_label.styleSheet(), "胶囊样式应含 md5 锁定的色相"
        assert item._capsule_color == expected, "紧凑态图标兜底色应同步稳定色相"

    def test_explicit_color_not_overridden(self, panel):
        """显式传 color 时不走 hash 兜底（原语义不变）"""
        idx = panel.add_tab("会话A")
        item = panel._items[idx]
        item.set_capsule("coder", color="hsl(1, 99%, 50%)")
        assert item._capsule_color == "hsl(1, 99%, 50%)"
