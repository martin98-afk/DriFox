# -*- coding: utf-8 -*-
"""回归：splitter 拖拽全程对话区控件必须保持可见。

两层历史 bug：
1. 旧 OPT-2 拖拽首帧 _content_area.setUpdatesEnabled(False) 冻结重绘，
   拖动中 layout 每帧重排使全区域失效，冻结期间失效区域只显示背景色
   =「拖动过程中对话区控件消失」。已移除冻结（与 #31 动画期实时显示
   对齐），拖拽全程 updatesEnabled 必须保持 True。
2. 拖拽跨折叠阈值触发折叠动画后，恢复链缺口曾致内容区永久冻结
   （f5a89eee5 修复）。动画结束后必须解冻。
"""
import pytest


@pytest.mark.ui
def test_splitter_drag_keeps_content_visible(ui_app, qtbot):
    tm = ui_app["tm"]
    tm.resize(1000, 700)
    qtbot.wait(300)
    ca = tm._content_area
    mw = ca.currentWidget()
    assert ca.updatesEnabled()

    # 模拟逐帧拖拽：落点取折叠线（frame 164）与收起宽（frame 60）之间
    # → 松手走折叠动画分支
    sizes = tm._splitter.sizes()
    tm._splitter.setSizes([120, max(0, sum(sizes) - 120)])
    tm._on_splitter_manually_moved(120, 1)
    tm._on_splitter_manually_moved(110, 1)  # 第二帧（真实拖拽为连续事件流）
    assert ca.updatesEnabled(), "拖拽过程中内容区必须保持可重绘（控件可见）"
    if mw is not None and hasattr(mw, "_resize_preview_active"):
        # WebView 卡片由 resize 预览模式兜底（占位块属预期性能策略）
        print(f"[drag-visible] 拖拽中 preview_active={mw._resize_preview_active}")

    tm._on_splitter_idle()
    qtbot.wait(900)

    assert tm._tab_panel._collapsed, "落点低于折叠线应落定为折叠态"
    assert ca.updatesEnabled(), "折叠动画结束后内容区必须可重绘"
    assert not getattr(tm, "_suppress_resize_preview", False), "动画预览抑制必须解除"
    if mw is not None and hasattr(mw, "_resize_preview_active"):
        assert not mw._resize_preview_active, "松手后 WebView 预览模式必须复位"


@pytest.mark.ui
def test_splitter_drag_expand_direction_keeps_visible(ui_app, qtbot):
    """展开方向拖拽（从折叠态拖出跨展开线）全程同样保持可见"""
    tm = ui_app["tm"]
    tm.resize(1000, 700)
    qtbot.wait(300)
    ca = tm._content_area
    panel = tm._tab_panel
    # 先折叠到收起态
    sizes = tm._splitter.sizes()
    tm._splitter.setSizes([56, max(0, sum(sizes) - 56)])
    tm._on_splitter_manually_moved(56, 1)
    tm._on_splitter_idle()
    qtbot.wait(1000)
    assert panel._collapsed, "前置：应已折叠"
    # 向外拖到展开线以上 → 展开动画
    expand_pos = panel._auto_expand_width + getattr(tm, "_FRAME_PADDING_X", 14) + 60
    if not hasattr(tm, "_FRAME_PADDING_X"):
        from app.widgets.tab_manager_window import _FRAME_PADDING_X
        expand_pos = panel._auto_expand_width + _FRAME_PADDING_X + 60
    sizes = tm._splitter.sizes()
    tm._splitter.setSizes([expand_pos, max(0, sum(sizes) - expand_pos)])
    tm._on_splitter_manually_moved(expand_pos, 1)
    assert ca.updatesEnabled(), "展开方向拖拽中内容区必须保持可重绘"
    tm._on_splitter_idle()
    qtbot.wait(1200)
    assert not panel._collapsed, "展开拖拽应落定展开态"
    assert ca.updatesEnabled(), "展开动画结束后内容区必须可重绘"
