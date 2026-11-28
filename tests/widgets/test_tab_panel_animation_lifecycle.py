# -*- coding: utf-8 -*-
"""标签删除后动画计数必须与剩余标签一致。"""

from unittest.mock import patch

import pytest

from app.widgets.tab_panel import TabPanel


@pytest.fixture
def panel(qtbot):
    with patch("app.widgets.cards.settings.gitee_card.GiteeAccountRow._auto_enable_sync"):
        widget = TabPanel()
    widget.set_mode("list", persist=False)
    qtbot.addWidget(widget)
    widget.show()
    with patch("app.widgets.tab_panel.Animations.motion_enabled", return_value=True):
        yield widget
    widget._stop_anim_timer()


def test_removing_question_tab_stops_idle_animation(panel, qtbot):
    panel.add_tab("等待回答")
    panel.update_tab_question(0, True)
    assert panel._anim_timer.isActive()
    ticks = []
    panel._anim_timer.timeout.connect(lambda: ticks.append(1))

    panel.remove_tab(0)

    assert panel._question_count == 0
    assert not panel._anim_timer.isActive()
    qtbot.wait(180)
    assert ticks == []


def test_removing_question_tab_keeps_remaining_stream_animation(panel):
    panel.add_tab("等待回答")
    panel.add_tab("正在输出")
    panel.update_tab_question(0, True)
    panel.update_tab_streaming(1, True)

    panel.remove_tab(0)

    assert panel._question_count == 0
    assert panel._streaming_count == 1
    assert panel._anim_timer.isActive()
    panel.update_tab_streaming(0, False)
    assert not panel._anim_timer.isActive()


def test_batch_removal_clears_all_animation_counts(panel):
    for title in ("等待回答", "正在输出", "输出报错"):
        panel.add_tab(title)
    panel.update_tab_question(0, True)
    panel.update_tab_streaming(1, True)
    panel.update_tab_streaming(2, False, error=True)

    panel.begin_batch_remove()
    for index in (2, 1, 0):
        panel.remove_tab(index)
    panel.end_batch_remove()

    assert (panel._streaming_count, panel._question_count, panel._error_count) == (0, 0, 0)
    assert not panel._anim_timer.isActive()


def test_hidden_and_resizing_panel_suspends_animation(panel, qtbot):
    panel.add_tab("正在输出")
    panel.update_tab_streaming(0, True)
    ticks = []
    panel._anim_timer.timeout.connect(lambda: ticks.append(1))
    panel.hide()
    panel.update_tab_question(0, True)
    qtbot.wait(400)
    assert ticks == []

    panel.set_resizing(True)
    panel.show()
    panel.update_tab_streaming(0, False, error=True)
    assert not panel._anim_timer.isActive()
    panel.set_resizing(False)
    qtbot.waitUntil(lambda: bool(ticks))
    panel.hide()
    panel.update_tab_question(0, False)
    panel.update_tab_streaming(0, False)
    panel.show()
    assert not panel._anim_timer.isActive()
