# -*- coding: utf-8 -*-
"""回归：拖 splitter 跨折叠阈值触发折叠动画后，右侧内容区必须解冻重绘。

历史 bug：_on_splitter_idle 在折叠动画运行时跳过 _finish_splitter_idle，
而 _on_sidebar_anim_finished 的 finally 只恢复卡片预览抑制、未恢复
_content_area.setUpdatesEnabled(True)。动画结束后内容区永久冻结绘制，
表现为拖一下侧栏 splitter 后对话区全部控件消失（再拖一次或切页才恢复）。
"""
import pytest


@pytest.mark.ui
def test_splitter_drag_collapse_restores_content_updates(ui_app, qtbot):
    tm = ui_app["tm"]
    tm.resize(1000, 700)
    qtbot.wait(300)
    ca = tm._content_area
    assert ca.updatesEnabled()

    # 落点取折叠线（frame 164）与收起宽（frame 60）之间 → 走折叠动画分支
    sizes = tm._splitter.sizes()
    tm._splitter.setSizes([120, max(0, sum(sizes) - 120)])
    tm._on_splitter_manually_moved(120, 1)
    assert not ca.updatesEnabled(), "拖拽首帧应冻结内容区重绘"

    tm._on_splitter_idle()
    qtbot.wait(900)

    assert tm._tab_panel._collapsed, "落点低于折叠线应落定为折叠态"
    assert ca.updatesEnabled(), "折叠动画结束后右侧内容区必须解冻重绘"
