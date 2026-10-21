# -*- coding: utf-8 -*-
"""任务看板高度记账口径回归（2026-10-09 卡片虚高修复）。

症状：流式对话刚开始时卡片高度突然异常增高，随后收缩回正常（一次性，
后续正常）。真机日志（同一次会话内实测）：

    23:53:09  card.h=5087  panel.sizeHint=253  panel.h=253   ← 落定
    23:53:13  card.h=5306  panel.sizeHint=34   panel.h=253   ← 虚高 +219
    23:54:42  card.h=5087  panel.sizeHint=34   panel.h=34    ← 收敛

根因：``_on_todo_panel_height_changed`` 用 ``panel.sizeHint()``（意愿高度）
记账，而卡片实际占高由布局按 ``panel.height()`` 决定。面板重建 / 折叠切换的
**中间拍**两者不同源（sizeHint 已重算、布局尚未落定，或反之）→ delta 与卡片
真实变化不符（上例 sizeHint 253→34 造出 -219，而卡片实际 +219，**符号相反**）。

危害不只是显示：delta 会累加进 ``_last_height_delta``，外层
``_on_message_card_height_changed`` 据此抬 ``sb.setMaximum(+delta)``，
符号错了会把滚动上界往反方向推。

修复：取 ``max(sizeHint, height)``。中间拍取大 ⇒
- 正方向（展开）：不丢增量，上界该抬还是抬；
- 负方向（折叠）：delta 记 0，漏记一次收缩。外层负值分支本就只失效上界缓存
  （收缩由 Qt 自行压低），无害；后续落定拍会以真实差值补上。
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from PyQt5.QtWidgets import QWidget

from app.widgets.message_card import MessageCard


class _SizeStub:
    """sizeHint() 返回值桩（只用到 .height()）"""

    def __init__(self, h):
        self._h = h

    def height(self):
        return self._h


class _StubPanel:
    """最小面板桩：可控 sizeHint / height / isVisible。"""

    def __init__(self, hint, real, visible=True):
        self._hint = hint
        self._real = real
        self._visible = visible

    def sizeHint(self):
        return _SizeStub(self._hint)

    def height(self):
        return self._real

    def isVisible(self):
        return self._visible


def _make_card(qapp):
    container = QWidget()
    card = MessageCard(role="assistant", model_name="test-model")
    card.setParent(container)
    container.show()
    qapp.processEvents()
    return container, card


class TestEffectiveHeightCaliber:
    """计量口径：始终取「能正确反映卡片占高」的那个分量。"""

    def test_settled_uses_equal_value(self, qapp):
        container, card = _make_card(qapp)
        try:
            assert card._todo_panel_effective_height(_StubPanel(34, 34)) == 34
            assert card._todo_panel_effective_height(_StubPanel(253, 253)) == 253
        finally:
            container.deleteLater()
            qapp.processEvents()

    def test_midsettle_prefers_sizehint(self, qapp):
        """展开中间拍：sizeHint 已重算（大）、布局未落定（小）→ 取大，增量不丢。"""
        container, card = _make_card(qapp)
        try:
            assert card._todo_panel_effective_height(_StubPanel(253, 34)) == 253
        finally:
            container.deleteLater()
            qapp.processEvents()

    def test_midsettle_collapse_does_not_wrap_around(self, qapp):
        """折叠中间拍：sizeHint 已重算（小）、布局未落定（大，上例 253）。

        取大 ⇒ 得 253 而非 34。这正是修复点：若取小（旧行为读 sizeHint=34），
        记账值会在面板仍占 253px 时先掉到 34，造出与实际相反的 delta。
        """
        container, card = _make_card(qapp)
        try:
            assert card._todo_panel_effective_height(_StubPanel(34, 253)) == 253
        finally:
            container.deleteLater()
            qapp.processEvents()

    def test_hidden_or_none_is_zero(self, qapp):
        container, card = _make_card(qapp)
        try:
            assert card._todo_panel_effective_height(_StubPanel(253, 253, visible=False)) == 0
            assert card._todo_panel_effective_height(None) == 0
        finally:
            container.deleteLater()
            qapp.processEvents()


class TestNoFalseDelta:
    """端到端：中间拍不得发出 heightChanged、不得污染 _last_height_delta。"""

    def test_midsettle_emits_nothing(self, qapp):
        """复刻真机 23:53:13 那拍：sizeHint 253→34、height 仍 253。

        旧行为：cache 253→34，delta=-219，累加进 _last_height_delta 并 emit
        （外层按收缩处理，而卡片实际在涨）→ 虚高。
        修复后：口径取大 ⇒ 253，delta=0 ⇒ 直接 return。
        """
        container, card = _make_card(qapp)
        try:
            card._todo_panel = _StubPanel(253, 253)
            card._todo_panel_height_cache = 253

            emitted = []
            card.heightChanged.connect(lambda h: emitted.append(h))
            card._last_height_delta = 0

            # 中间拍
            card._todo_panel = _StubPanel(34, 253)
            card._on_todo_panel_height_changed()

            assert emitted == [], "中间拍不得发出 heightChanged"
            assert card._last_height_delta == 0, "中间拍不得污染锚定增量令牌"
        finally:
            container.deleteLater()
            qapp.processEvents()

    def test_settle_reports_real_shrink(self, qapp):
        """收敛拍：真实收缩（253→34）仍要如实上报负 delta（供外层失效上界缓存）。"""
        container, card = _make_card(qapp)
        try:
            card._todo_panel = _StubPanel(253, 253)
            card._todo_panel_height_cache = 253
            card._last_height_delta = 0

            emitted = []
            card.heightChanged.connect(lambda h: emitted.append(h))

            card._todo_panel = _StubPanel(34, 34)
            card._on_todo_panel_height_changed()

            assert card._todo_panel_height_cache == 34, "落定拍必须更新记账值"
            assert card._last_height_delta == -219, "真实收缩必须如实记账"
            assert emitted, "真实高度变化必须发出 heightChanged"
        finally:
            container.deleteLater()
            qapp.processEvents()

    def test_settle_reports_real_growth(self, qapp):
        """收敛拍：真实增长（34→253）如实上报正 delta（外层据此抬上界）。"""
        container, card = _make_card(qapp)
        try:
            card._todo_panel = _StubPanel(34, 34)
            card._todo_panel_height_cache = 34
            card._last_height_delta = 0

            emitted = []
            card.heightChanged.connect(lambda h: emitted.append(h))

            card._todo_panel = _StubPanel(253, 253)
            card._on_todo_panel_height_changed()

            assert card._todo_panel_height_cache == 253
            assert card._last_height_delta == 219
            assert emitted
        finally:
            container.deleteLater()
            qapp.processEvents()
