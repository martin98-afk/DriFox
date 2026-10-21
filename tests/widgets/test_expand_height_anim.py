# -*- coding: utf-8 -*-
"""回归测试：展开卡高度动画（DynamicHeightExpandCardMixin / animate_expand_height）

背景
----
设置面板里「工具 / 智能体 / 技能」三张动态内容卡用 mixin 接管高度，
但接管时把基类的展开动画一并停掉了（`expandAni.stop()` + 瞬时
`setFixedHeight`），表现为「折叠展开没有动画」，与 Hooks / MCP / 服务商等
原生卡观感不一致。

修复：高度仍由 mixin 接管，但改为对 `maximumHeight` 做属性动画，动画期间
放开最小高度约束、每帧同步滚动条，结束后 `setFixedHeight` 定格；内容在动画
期间继续增长（分批构建）时不做逐帧校正，动画结束统一收敛。

本文件只验证动画调度与高度收敛语义，不依赖真实设置面板。
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QLabel
from qfluentwidgets import ExpandSettingCard, FluentIcon

from app.widgets.cards.settings.expand_height_mixin import (
    DynamicHeightExpandCardMixin,
    _EXPAND_DURATION,
    animate_expand_height,
)


class _DemoCard(DynamicHeightExpandCardMixin, ExpandSettingCard):
    """最小动态内容卡：mixin 接管高度 + 动画"""

    def __init__(self, parent=None):
        super().__init__(FluentIcon.SETTING, "标题", "副标题", parent)
        self.viewLayout.addWidget(QLabel("内容行"))


class _PlainCard(ExpandSettingCard):
    """不用 mixin、只调 animate_expand_height 的卡（插件配置卡的路径）"""

    def __init__(self, parent=None):
        super().__init__(FluentIcon.SETTING, "标题", "副标题", parent)
        self.viewLayout.addWidget(QLabel("内容行"))


def _wait(ms: int = _EXPAND_DURATION * 2 + 200):
    """跑事件循环等动画与收敛校正结束"""
    QTest.qWait(ms)


def _expanded_height(card) -> int:
    return card.card.height() + card.viewLayout.sizeHint().height()


def test_expand_animates_then_lands(qapp):
    """展开：起步于折叠高度，动画结束后落到 header + 内容实测高度"""
    card = _DemoCard()
    header_h = card.card.height()
    target = _expanded_height(card)
    assert target > header_h, "测试前提：内容高度必须大于 0"

    card.setExpand(True)
    # 动画启动：内容已可见（量高度需要），高度尚未到目标
    assert card._expanding is True
    assert not card.view.isHidden()
    # 动画中途：高度已离开起点、尚未到终点
    QTest.qWait(max(1, _EXPAND_DURATION // 4))
    assert header_h < card.height() < target

    _wait()
    assert card._expanding is False
    assert card.height() == _expanded_height(card)
    assert not card.view.isHidden()


def test_collapse_animates_then_hides_view(qapp):
    """折叠：动画期间内容不瞬间消失，结束后高度回到 header 并隐藏内容"""
    card = _DemoCard()
    card.setExpand(True)
    _wait()
    expanded_h = card.height()
    assert expanded_h > card.card.height()

    card.setExpand(False)
    assert card._expanding is True
    assert not card.view.isHidden(), "折叠动画途中内容不应立即隐藏"
    QTest.qWait(max(1, _EXPAND_DURATION // 4))
    assert card.card.height() < card.height() < expanded_h

    _wait()
    assert card._expanding is False
    assert card.height() == card.card.height()
    assert card.view.isHidden(), "折叠完成后内容必须隐藏（布局缓存不再算它）"


def test_content_growth_during_animation_converges(qapp):
    """动画期间内容增长（分批构建）时不打断动画，结束后收敛到新高度"""
    card = _DemoCard()
    card.setExpand(True)
    # 模拟分批构建：动画途中又挂上一行
    card.viewLayout.addWidget(QLabel("动画期间新增的行"))

    _wait()
    assert card._expanding is False
    assert card.height() == _expanded_height(card)
    assert card.height() > card.card.height() + QLabel("内容行").sizeHint().height()


def test_reverse_toggle_mid_animation(qapp):
    """动画中途反向切换：从当前高度起新动画，最终落在折叠态"""
    card = _DemoCard()
    card.setExpand(True)
    QTest.qWait(_EXPAND_DURATION // 4)
    card.setExpand(False)

    _wait()
    assert card._expanding is False
    assert card.height() == card.card.height()
    assert card.view.isHidden()


def test_animate_expand_height_helper_returns_false_when_settled(qapp):
    """高度已等于目标时不开动画：调用方直接定格（幂等）"""
    card = _PlainCard()
    card.setFixedHeight(card.card.height() + card.viewLayout.sizeHint().height())
    assert animate_expand_height(card, card.height(), None) is False


def test_plain_card_helper_animates(qapp):
    """非 mixin 卡片（插件配置卡）用同一 helper 也能跑动画"""
    card = _PlainCard()
    start = card.height()
    target = start + card.viewLayout.sizeHint().height()

    assert animate_expand_height(card, target, None) is True
    assert card.height() == start
    QTest.qWait(max(1, _EXPAND_DURATION // 4))
    assert start < card.height() < target
    _wait()
    assert card.height() == target
