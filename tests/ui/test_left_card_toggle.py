# -*- coding: utf-8 -*-
"""回归：左侧 UI 插件开合、窗口最大化/还原时，聊天正文不得被换成占位框。

采集的是**真实桌面合成像素**（screen.grabWindow(0)）而非 Qt 可见标志：
本轮历史 bug（右栏收起后正文 4131→0）只在合成层可见，isVisible() 全程为真。

覆盖：
- open / close：左侧插件卡片展开/收起全程，聊天正文保持可读；
- slide：开合走逐帧过渡（不是一帧瞬切），且 TabPanel 槽宽不被临时拉宽；
- maximize / restore：窗口状态切换全程 `_resize_preview_active` 保持 False。
"""
import pytest
from PyQt5.QtCore import QPoint, QRect
from PyQt5.QtGui import QImage
from PyQt5.QtWidgets import QApplication, QWidget


class _DockCard(QWidget):
    """最小左侧停靠卡：有背景、有最小宽，便于肉眼/像素定位。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(42, True)  # Qt.WA_StyledBackground，QSS 背景才绘制
        self.setStyleSheet("background:#2a2a2a;")
        self.setMinimumWidth(120)


def _make_chat_card(tm, mw, qtbot):
    from app.widgets.message_card import MessageCard

    screen = QApplication.primaryScreen()
    tm.resize(min(1400, screen.availableGeometry().width()), 760)
    tm.move(screen.availableGeometry().topLeft())
    tm.raise_()
    tm.activateWindow()
    qtbot.wait(400)
    card = MessageCard(role="assistant")
    mw.chat_layout.addWidget(card)
    card.set_content("\n\n".join(["<span style='color:#f00883'>█████████████████████████████████</span>"] * 6))
    card.ensure_rendered()
    qtbot.waitUntil(lambda: card.viewer._is_js_ready and card.viewer.isVisible(), timeout=30000)
    mw.chat_scroll_area.ensureWidgetVisible(card)
    mw._do_debounced_resize()
    qtbot.wait(400)
    return card


def _painted(screen, card, mw):
    """统计卡片正文区域里的亮粉像素（卡片底色/侧栏都不会被误计）。"""
    vp = mw.chat_scroll_area.viewport()
    rect = (
        QRect(card.viewer.mapToGlobal(QPoint()), card.viewer.size())
        .intersected(QRect(vp.mapToGlobal(QPoint()), vp.size()))
        .intersected(screen.geometry())
    )
    if rect.isEmpty():
        return 0
    shot = screen.grabWindow(0, rect.x(), rect.y(), min(rect.width(), 290), rect.height()).toImage()
    shot = shot.convertToFormat(QImage.Format_RGB32)
    ptr = shot.constBits()
    ptr.setsize(shot.byteCount())
    buf = bytes(ptr)
    return sum(buf[i + 2] > 180 and buf[i + 1] < 70 and 75 < buf[i] < 180 for i in range(0, len(buf) - 3, 32))


def _toggle_left_card(tm, card_id, open_it):
    from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

    reg = UIPluginRegistry.get_instance()
    if open_it:
        reg.register_floating_card("t-left-toggle", card_id, _DockCard, "left", title="左侧插件")
    reg.toggle_floating_card(card_id)


@pytest.mark.ui
@pytest.mark.parametrize("opening", [True, False])
def test_left_card_toggle_keeps_chat_text(ui_app, qtbot, opening):
    """插件开/关全程聊天正文保持可见：不得切占位框、不得整片闪空。"""
    tm, mw = ui_app["tm"], ui_app["chat_window"]
    screen = QApplication.primaryScreen()
    card = _make_chat_card(tm, mw, qtbot)
    card_id = "t-left-toggle-open" if opening else "t-left-toggle-close"
    if not opening:
        _toggle_left_card(tm, card_id, True)
        qtbot.wait(600)
    before = _painted(screen, card, mw)
    if before <= 20:
        # 采集器不可用（窗口被别的桌面窗口遮挡 / 合成层取不到）≠ 产品回归：
        # 跳过而不是误报，避免与他人并行跑 UI 测试时互相污染。
        pytest.skip(f"桌面不可采样（baseline={before}），跳过像素断言")
    frames = []
    try:
        _toggle_left_card(tm, card_id, opening)
        for _ in range(28):
            qtbot.wait(8)
            frames.append(_painted(screen, card, mw))
    finally:
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

        UIPluginRegistry.get_instance().hide_floating_card_globally(card_id)
        qtbot.wait(400)
        mw.chat_layout.removeWidget(card)
        card.hide()
        card.deleteLater()
    assert min(frames) > before * 0.12, f"opening={opening}: 正文像素基线={before}，逐帧={frames}"


@pytest.mark.ui
def test_left_card_width_animates(ui_app, qtbot, monkeypatch):
    """开合是逐帧过渡：中间帧宽必须严格落在起止之间，且 TabPanel 不被拉宽。"""
    from app.utils.design_tokens import Animations

    tm, mw = ui_app["tm"], ui_app["chat_window"]
    _make_chat_card(tm, mw, qtbot)
    monkeypatch.setattr(Animations, "motion_enabled", classmethod(lambda cls: True))
    card_id = "t-left-toggle-slide"
    start_w = tm._splitter.sizes()[0]
    from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

    reg = UIPluginRegistry.get_instance()
    widths = []
    panels = []
    try:
        _toggle_left_card(tm, card_id, True)
        for _ in range(24):
            qtbot.wait(10)
            sp = tm._sidebar_splitter
            frame_w = tm._splitter.sizes()[0]
            widths.append(frame_w)
            sizes = sp.sizes()
            slot = sizes[sp.indexOf(tm._global_left_wrapper)]
            panel_w = sizes[sp.indexOf(tm._tab_panel)]
            panels.append(panel_w)
            # 内层铺满外层：panel + 卡片槽 ≈ frame − padding。只改外层时多出的
            # 宽度会被 QSplitter 全塞给 TabPanel（列表被拉宽再弹回）。
            assert abs((panel_w + slot) - (frame_w - 4)) <= 12, f"内层几何自相矛盾：{panel_w}+{slot} vs {frame_w}"
    except Exception:
        reg.hide_floating_card_globally(card_id)
        raise
    end_w = widths[-1]
    assert end_w > start_w + 100, f"卡片区未展开：{start_w} → {end_w}"
    middle = [w for w in widths if start_w + 5 < w < end_w - 5]
    assert len(set(middle)) >= 3, f"未观察到逐帧过渡（疑似被最小尺寸钳住）：{widths}"
    # TabPanel 从展开宽平滑收到收起宽（不瞬切、也不反向弹）
    assert panels[-1] <= tm._tab_panel._collapsed_min_width + 6, f"TabPanel 未收到收起宽：{panels}"
    assert all(b <= a + 4 for a, b in zip(panels, panels[1:])), f"TabPanel 宽度出现反向弹跳：{panels}"

    # ── 关闭方向：同样逐帧回退，且内层几何始终自洽（TabPanel 不被撑到超过恢复宽）──
    # 卡片隐藏经 CardManager/容器防抖，实测有 ~100ms 前摇，采样窗口需留足。
    # 先把恢复宽压到 250：默认恢复宽可能顶到 400 上限，「被多出空间拉宽」就测不出来。
    tm._saved_panel_frame_width = 250
    closes = []
    back_panels = []
    backs = []
    reg.toggle_floating_card(card_id)
    for _ in range(40):
        qtbot.wait(12)
        sp = tm._sidebar_splitter
        frame_w = tm._splitter.sizes()[0]
        closes.append(frame_w)
        sizes = sp.sizes()
        if len(sizes) < 2:
            continue
        slot = sizes[sp.indexOf(tm._global_left_wrapper)]
        panel_w = sizes[sp.indexOf(tm._tab_panel)]
        backs.append(slot)
        back_panels.append(panel_w)
        # TabPanel 不得被「卡片区让出的宽度」撑到超过恢复宽：否则表现为关闭
        # 瞬间 tab 列表先被拉宽再弹回。
        assert panel_w <= 254, f"关闭中 TabPanel 被拉宽到 {panel_w}（frame={frame_w} slot={slot}）"
    back_w = closes[-1]
    # 终值 = 强制恢复宽 250（不是展开前的 start_w：本用例把它压到了 250 以可测）
    assert abs(back_w - 250) <= 20, f"卡片区未收回：{end_w} → {back_w}"
    closing_middle = [w for w in closes if back_w + 5 < w < end_w - 5]
    assert len(set(closing_middle)) >= 3, f"关闭未逐帧过渡：{closes}"
    # 恢复宽是渐变上去的（250−4=246），不是瞬切到满宽
    assert back_panels[-1] >= 240, f"TabPanel 未恢复到恢复宽：{back_panels}"
    # 卡片区槽位一路收到 0（中途不得反弹回大值）
    moving = backs[len(backs) // 3 :]
    assert all(b <= a + 8 for a, b in zip(moving, moving[1:])), f"卡片区槽位出现反弹：{backs}"
    assert backs[-1] <= 8, f"卡片区槽位未收到 0：{backs[-1]}"


@pytest.mark.ui
@pytest.mark.parametrize("state", ["maximize", "restore"])
def test_window_state_change_keeps_chat_text(ui_app, qtbot, state):
    """最大化/还原：状态切换期不得进卡片 resize 预览，也不得冻结内容区绘制。

    这里不采截屏像素：最大化后窗口走 DWM 合成，Qt5 的 screen.grabWindow 在本机
    拿不到 WebEngine 的硬件合成层（实测同一区域时有时无，属采集工具限制）。
    改为断言两条真实机制——进预览（正文换占位框）与冻结 updates（新露出区域
    停在窗口底色），两者都是用户可见闪烁的直接来源。
    """
    tm, mw = ui_app["tm"], ui_app["chat_window"]
    card = _make_chat_card(tm, mw, qtbot)
    if state == "restore":
        tm.showMaximized()
        qtbot.wait(700)
    bad = []
    try:
        tm.showNormal() if state == "restore" else tm.showMaximized()
        for _ in range(24):
            qtbot.wait(12)
            bad.append(
                (
                    bool(mw._resize_preview_active),
                    not tm._content_area.updatesEnabled(),
                    not card.viewer.isVisible(),
                )
            )
    finally:
        # 复位窗口几何：最大化态会污染后续用例的截屏采样（本机 maximized
        # 窗口的 WebEngine 硬件合成层采不到），也会让 tm.resize 失效。
        tm.showNormal()
        tm.resize(min(1200, QApplication.primaryScreen().availableGeometry().width()), 760)
        tm.move(QApplication.primaryScreen().availableGeometry().topLeft())
        qtbot.wait(500)
        mw.chat_layout.removeWidget(card)
        card.hide()
        card.deleteLater()
    assert not tm.isMaximized(), f"{state}: 用例收尾后窗口仍处于最大化态"
    assert not any(p for p, _f, _v in bad), f"{state}: 切换期进入了 resize 预览（正文换占位）：{bad}"
    assert not any(f for _p, f, _v in bad), f"{state}: 切换期冻结了对话区绘制：{bad}"
    assert not any(v for _p, _f, v in bad), f"{state}: 切换期 viewer 被隐藏：{bad}"
