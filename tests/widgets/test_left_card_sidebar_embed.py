# -*- coding: utf-8 -*-
"""左侧 UI 卡片区嵌入侧边栏（#tabFrame 内 sidebarSplitter）回归测试

产品语义（2026-10-12 确认）：
1. left 卡片宿主从对话区 dock_splitter 迁到 #tabFrame 内的 sidebarSplitter，
   卡片嵌在侧边栏圆角框内部右侧（「从侧边栏里长出来」）；
2. 卡片打开：TabPanel 自动收起让位（46px），卡片区展开，frame 加宽，
   对话区被挤窄；
3. 卡片关闭：卡片区收 0，TabPanel 恢复打开前宽度，frame 缩回；
4. 拖主 splitter（tabFrame ↔ chatFrame）：增量只加卡片宽度，TabPanel
   锁定收起宽不动；
5. 拖内部分隔条（TabPanel ↔ 卡片区）：两者互借宽度，frame 总宽不变。

注意：offscreen 下 moveSplitter 有 Qt 内部路径问题，用 setSizes +
手动调用宿主回调模拟拖拽（与 test_dock_splitter_memory_regression 同策略）。
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from PySide6.QtCore import QCoreApplication, QEventLoop, Qt, QTimer
from PySide6.QtWidgets import QApplication, QWidget

QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
app = QApplication(["test"])

from app.plugins.registries.ui_plugin_registry import UIPluginRegistry
from app.widgets.tab_manager_window import (
    _COLLAPSED_PANEL_WIDTH,
    _FRAME_PADDING_X,
    _TAB_PANEL_MAX_CONTENT_WIDTH,
    TabManagerWindow,
)

_TM = None  # 模块级持有，防 GC 后 Qt C++ 对象悬垂


def _pump(ms: int = 30):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec_()


def _flush_resize():
    """强制投递待处理的 resize/布局事件（qtbot 环境下 Qt 会延迟到事件循环）"""
    for _ in range(6):
        QApplication.processEvents()
        QApplication.sendPostedEvents(None, 0)


def _make_tm():
    global _TM
    tm = TabManagerWindow.create_instance()
    tm.resize(1400, 900)
    tm.show()
    _pump(300)
    # 等足启动补射（_restore_sidebar_collapsed 排了 80/200/400/700ms 四档，
    # 补射会在卡片区未展开时按存档态重设 frame 宽度，把用例内的拖拽结果冲掉）
    _pump(600)
    _close_all_left_cards(tm)
    # 侧边栏复位到展开态（TabManagerWindow 是单例，上一条用例可能把它折叠了）
    if tm._tab_panel._collapsed:
        tm._tab_panel.set_collapsed(False)
        tm._on_sidebar_toggled(False, animate=False)
        for _ in range(4):
            QApplication.processEvents()
        _pump(300)
    # ★ 强制把内部分配与外层 frame 拉回干净基线：单例跨用例流转会留下
    #   「tab 被拖宽 / 卡片槽残留 / frame 上限放宽」等痕迹，不重置会让后续
    #   用例的初始断言与拖拽语义全部失真（实测组合跑失败、单跑通过）。
    sp = tm._sidebar_splitter
    if sp.count() == 2 and not tm._left_card_area_active():
        total_in = sum(sp.sizes())
        sp.setSizes([_COLLAPSED_PANEL_WIDTH, max(0, total_in - _COLLAPSED_PANEL_WIDTH)])
        tm._tab_frame.setMaximumWidth(_TAB_PANEL_MAX_CONTENT_WIDTH + _FRAME_PADDING_X)
        tm._on_sidebar_toggled(False, animate=False)
        for _ in range(4):
            QApplication.processEvents()
        _pump(300)
    return tm


def _close_all_left_cards(tm):
    """清理所有 left 容器可见卡片（TabManagerWindow 是单例，跨用例/跨文件共享状态）

    必须把卡片编排留下的全部状态复位干净，否则污染后续测试文件（实测
    test_dock_repro2 在本文件之后跑会失败）：
    - 卡片隐藏（走 hide_floating_card_globally）
    - TabPanel 占位抑制开关（_left_card_present）复位
    - 侧边栏内部分配复位为 [面板宽, 0]
    - frame 上限复位
    """
    reg = UIPluginRegistry.get_instance()
    for cid, info in list(reg.get_floating_cards().items()):
        if info.container != "left":
            continue
        try:
            reg.hide_floating_card_globally(cid)
        except Exception:
            pass
    for _ in range(4):
        QApplication.processEvents()
    _pump(400)
    # 状态复位兜底（卡片隐藏路径可能未走完宿主编排的关闭分支）
    panel = getattr(tm, "_tab_panel", None)
    if panel is not None:
        panel._left_card_present = False
        panel._dragging_splitter = False
    tm._left_card_drag_dirty = False
    tm._left_card_prev_collapsed = False
    wrapper = getattr(tm, "_global_left_wrapper", None)
    sp = getattr(tm, "_sidebar_splitter", None)
    # ★ 复位顺序：先把 tab 宽度按当前总宽重新分配，再清卡片槽位。
    #   反过来（先清卡片槽）会让 splitter 在「总宽 = 面板当前宽」的情形下
    #   把剩余空间摊给两窗格（实测残留 [296, 0] → 清卡片后变 [38, 38] 畸形
    #   分配，后续 setSizes 全部失效）。
    if panel is not None and sp is not None and sp.count() == 2:
        cur = sp.sizes()
        total = sum(cur)
        tab_idx = sp.indexOf(panel)
        idx = sp.indexOf(wrapper) if wrapper is not None else -1
        if tab_idx >= 0 and idx >= 0 and total > 0:
            sp.setSizes([panel._collapsed_min_width, max(0, total - panel._collapsed_min_width)])
    if wrapper is not None:
        # 卡片已全部 hide → 显式走 wrapper 收起（幂等：已收起时内部早退）
        wrapper._sync()
    for _ in range(3):
        QApplication.processEvents()
    _pump(300)
    if sp is not None and sp.count() == 2:
        idx = sp.indexOf(wrapper) if wrapper is not None else -1
        if idx >= 0:
            sizes = sp.sizes()
            if sizes[idx] != 0:
                sizes[idx] = 0
                sizes[0] = max(sizes[0], panel._collapsed_min_width if panel is not None else 46)
                sp.setSizes(sizes)
    for _ in range(3):
        QApplication.processEvents()
    _pump(200)


def _show_left_card(tm, card_id="tp-left-embed"):
    reg = UIPluginRegistry.get_instance()
    reg.register_floating_card("tp", card_id, QWidget, "left", title="T")
    reg.toggle_floating_card(card_id)
    for _ in range(6):
        QApplication.processEvents()
    _pump(300)


def _inner(tm):
    """返回 (sidebarSplitter, wrapper索引, tabPanel槽位, 卡片槽位)"""
    sp = tm._sidebar_splitter
    idx = sp.indexOf(tm._global_left_wrapper)
    sizes = sp.sizes()
    return sp, idx, sizes[0], sizes[idx]


class TestStructure:
    def test_splitter_hosts_tabpanel_and_card_wrapper(self):
        """结构：sidebarSplitter 直接子项 = [TabPanel, left wrapper]"""
        tm = _make_tm()
        sp = tm._sidebar_splitter
        assert sp.count() == 2, f"sidebarSplitter 应有两个窗格，实际 {sp.count()}"
        assert sp.widget(0) is tm._tab_panel
        assert sp.widget(1) is tm._global_left_wrapper

    def test_card_wrapper_inside_tab_frame(self):
        """卡片 wrapper 的父链在 #tabFrame 内（不再是对话区 dock_splitter）"""
        tm = _make_tm()
        assert tm._global_left_wrapper.parent() is tm._sidebar_splitter
        # 沿父链上溯必须经过 tab_frame（卡片嵌在侧边栏圆角框内部）
        ancestors = []
        w = tm._global_left_wrapper
        while w is not None:
            w = w.parentWidget()
            ancestors.append(w)
        assert tm._tab_frame in ancestors, "wrapper 父链应包含 #tabFrame"
        assert tm._global_left_wrapper not in (
            tm._dock_splitter.widget(0),
            tm._dock_splitter.widget(1),
        ), "wrapper 不应再挂在 dock_splitter 上"

    def test_dock_splitter_kept_for_mode_probe(self):
        """dockSplitter 保留（外部按 objectName 探测 Tab 模式），仅含对话列"""
        tm = _make_tm()
        assert tm._dock_splitter.objectName() == "dockSplitter"
        assert tm._dock_splitter.count() == 1
        assert tm._dock_splitter.widget(0) is tm._chat_vsplitter


class TestOpenClose:
    def test_open_expands_card_and_collapses_tab(self):
        """卡片打开：卡片区展开、TabPanel 收到收起宽、frame 加宽"""
        tm = _make_tm()
        opened_frame_w = tm._splitter.sizes()[0]
        _show_left_card(tm)
        sp, idx, tab_w, card_w = _inner(tm)
        assert card_w > 0, f"卡片槽位应展开，实际 {card_w}"
        assert tab_w == _COLLAPSED_PANEL_WIDTH, f"TabPanel 应收起到 {_COLLAPSED_PANEL_WIDTH}，实际 {tab_w}"
        assert tm._tab_panel._collapsed is True
        assert tm._splitter.sizes()[0] > opened_frame_w, "frame 应整体加宽（TabPanel + 卡片）"

    def test_close_restores_tab_width(self):
        """卡片关闭：卡片区收 0、TabPanel 恢复打开前宽度"""
        tm = _make_tm()
        base_frame_w = tm._splitter.sizes()[0]
        _show_left_card(tm, "tp-left-embed-close")
        _pump(400)
        # 关闭卡片
        reg = UIPluginRegistry.get_instance()
        reg.toggle_floating_card("tp-left-embed-close")
        for _ in range(6):
            QApplication.processEvents()
        _pump(400)
        sp, idx, tab_w, card_w = _inner(tm)
        assert card_w == 0, f"卡片槽位应收 0，实际 {card_w}"
        assert tm._tab_panel._collapsed is False, "TabPanel 应恢复展开态"
        assert abs(tm._splitter.sizes()[0] - base_frame_w) <= 20, (
            f"frame 应收回到打开前宽度 {base_frame_w}，实际 {tm._splitter.sizes()[0]}"
        )

    def test_titlebar_toggle_while_card_open_collapses_all(self):
        """卡片占位期点标题栏折叠按钮：卡片隐藏 + 面板落折叠态（不留细条）

        修复前：占位期按钮点击翻转编排态后 emit，宿主宽度路径只压 frame，
        卡片未隐藏被挤成细条残留；且按钮翻转污染 panel._collapsed 后，
        卡片关闭回调的 user_widened 误判把面板展开落位。
        """
        from app.utils.config import Settings

        tm = _make_tm()
        _show_left_card(tm, "tp-left-toggle-all")
        _pump(400)
        assert tm._left_card_area_active(), "前置：卡片应已打开"
        tm.titleBar._sidebar_btn.click()
        for _ in range(6):
            QApplication.processEvents()
        _pump(400)
        _, _, tab_w, card_w = _inner(tm)
        assert card_w == 0, f"卡片槽位应收 0，实际 {card_w}"
        assert not tm._left_card_area_active(), "卡片可见意图应清除"
        assert tm._tab_panel._collapsed is True, "面板应落折叠态"
        assert abs(tm._splitter.sizes()[0] - (_COLLAPSED_PANEL_WIDTH + _FRAME_PADDING_X)) <= 4, (
            f"frame 应收到收起宽 {_COLLAPSED_PANEL_WIDTH + _FRAME_PADDING_X}，实际 {tm._splitter.sizes()[0]}"
        )
        assert Settings.get_instance().ui_sidebar_collapsed.value is True, "用户终态应落盘为折叠"
        assert tm.titleBar._sidebar_btn.toolTip() == "展开侧边栏", "按钮提示应回到折叠态文案"


class TestMainSplitterDrag:
    def test_drag_main_splitter_only_adds_card_width(self):
        """拖主把手：增量只加到卡片区，TabPanel 保持收起宽"""
        tm = _make_tm()
        _show_left_card(tm, "tp-left-drag")
        _pump(400)
        _, _, tab_w_before, card_w_before = _inner(tm)

        # 模拟用户把 frame 拖宽 120（pos = frame 右边界）
        pos = tm._splitter.sizes()[0] + 120
        tm._on_splitter_manually_moved(pos, 0)
        tm._on_splitter_idle()
        _pump(400)

        _, _, tab_w_after, card_w_after = _inner(tm)
        assert tab_w_after == _COLLAPSED_PANEL_WIDTH, f"TabPanel 应保持收起宽，实际 {tab_w_after}"
        assert card_w_after > card_w_before, f"卡片区应变宽：{card_w_before} → {card_w_after}"
        assert tab_w_before == tab_w_after

    def test_drag_main_splitter_shrink_respects_card_min(self):
        """拖主把手收窄：卡片区跟着变窄（不低于下限），TabPanel 仍锁定"""
        tm = _make_tm()
        _show_left_card(tm, "tp-left-drag-shrink")
        _pump(400)
        _, _, _, card_w_before = _inner(tm)

        pos = tm._splitter.sizes()[0] - 80
        tm._on_splitter_manually_moved(pos, 0)
        tm._on_splitter_idle()
        _pump(400)

        _, _, tab_w_after, card_w_after = _inner(tm)
        assert tab_w_after == _COLLAPSED_PANEL_WIDTH
        assert card_w_after < card_w_before, f"卡片区应收窄：{card_w_before} → {card_w_after}"


class TestInnerSplitterDrag:
    def test_inner_drag_widens_tab_keeping_frame(self):
        """拖内部分隔条：TabPanel 变宽，frame 总宽不变"""
        tm = _make_tm()
        _show_left_card(tm, "tp-left-inner")
        _pump(400)
        sp, idx, tab_w_before, card_w_before = _inner(tm)
        frame_before = tm._splitter.sizes()[0]

        # 内部互借：tab → 200，卡片等额减少
        sizes = list(sp.sizes())
        delta = 200 - tab_w_before
        sizes[0] = 200
        sizes[idx] = max(0, card_w_before - delta)
        sp.setSizes(sizes)
        _pump(300)

        _, _, tab_w_after, card_w_after = _inner(tm)
        assert tab_w_after == 200, f"TabPanel 应变宽到 200，实际 {tab_w_after}"
        assert card_w_after == card_w_before - delta, f"卡片区应等额让位，实际 {card_w_after}"
        assert abs(tm._splitter.sizes()[0] - frame_before) <= 2, "frame 总宽不应变化（内部分隔条互借）"


class TestFrameCap:
    def test_frame_cap_expanded_after_open(self):
        """卡片打开后 frame 上限放宽（可容纳卡片宽）"""
        tm = _make_tm()
        _show_left_card(tm, "tp-left-cap")
        _pump(400)
        from app.widgets.tab_manager_window import _TAB_PANEL_MAX_CONTENT_WIDTH, _FRAME_PADDING_X

        static_cap = _TAB_PANEL_MAX_CONTENT_WIDTH + _FRAME_PADDING_X
        assert tm._tab_frame.maximumWidth() > static_cap, "卡片打开后 frame 上限应放宽"

    def test_frame_cap_restored_after_close(self):
        """卡片关闭后 frame 上限回到静态值"""
        tm = _make_tm()
        _show_left_card(tm, "tp-left-cap-close")
        _pump(400)
        UIPluginRegistry.get_instance().toggle_floating_card("tp-left-cap-close")
        for _ in range(6):
            QApplication.processEvents()
        _pump(400)
        from app.widgets.tab_manager_window import _TAB_PANEL_MAX_CONTENT_WIDTH, _FRAME_PADDING_X

        assert tm._tab_frame.maximumWidth() == _TAB_PANEL_MAX_CONTENT_WIDTH + _FRAME_PADDING_X


class TestCollapsedRoundTrip:
    def test_close_keeps_collapsed_when_opened_while_collapsed(self):
        """侧边栏本来折叠 → 开卡片 → 关卡片：必须回到折叠态（不得意外展开）"""
        tm = _make_tm()
        # 用户手动收起侧边栏
        tm._tab_panel.set_collapsed(True)
        tm._on_sidebar_toggled(True, animate=False)
        for _ in range(4):
            QApplication.processEvents()
        _pump(300)
        assert tm._tab_panel._collapsed is True
        collapsed_frame_w = tm._splitter.sizes()[0]

        # 开卡片再关
        _show_left_card(tm, "tp-left-fracollapse")
        _pump(400)
        assert tm._tab_panel._collapsed is True, "卡片打开期 TabPanel 应保持折叠（卡片区仍展开）"

        UIPluginRegistry.get_instance().toggle_floating_card("tp-left-fracollapse")
        for _ in range(6):
            QApplication.processEvents()
        _pump(400)

        assert tm._tab_panel._collapsed is True, "关闭卡片后应回到折叠态（打开前就是折叠）"
        assert abs(tm._splitter.sizes()[0] - collapsed_frame_w) <= 20, (
            f"frame 应回到折叠宽 {collapsed_frame_w}，实际 {tm._splitter.sizes()[0]}"
        )

    def test_close_expands_when_opened_while_expanded(self):
        """侧边栏本来展开 → 开卡片 → 关卡片：回到展开态（原行为不回归）"""
        tm = _make_tm()
        assert tm._tab_panel._collapsed is False
        expanded_frame_w = tm._splitter.sizes()[0]

        _show_left_card(tm, "tp-left-fraexpand")
        _pump(400)
        UIPluginRegistry.get_instance().toggle_floating_card("tp-left-fraexpand")
        for _ in range(6):
            QApplication.processEvents()
        _pump(400)

        assert tm._tab_panel._collapsed is False, "关闭卡片后应回到展开态"
        assert abs(tm._splitter.sizes()[0] - expanded_frame_w) <= 20, (
            f"frame 应回到展开宽 {expanded_frame_w}，实际 {tm._splitter.sizes()[0]}"
        )


class TestInnerHandleStyle:
    def test_sidebar_splitter_handle_styled(self):
        """内部分隔条须有可见样式（WA_StyledBackground + 非空 QSS）"""
        from PySide6.QtCore import Qt as _Qt

        tm = _make_tm()
        sp = tm._sidebar_splitter
        assert sp.count() == 2
        handle = sp.handle(1)
        assert handle is not None, "sidebarSplitter 应有分隔条"
        assert handle.testAttribute(_Qt.WA_StyledBackground), "handle 须设 WA_StyledBackground 才能画 QSS"
        assert "border-left" in handle.styleSheet(), "handle 样式应含可见分隔线"
        # 贯穿上下：不得留纵向 margin（用户反馈「头尾有空白」）
        assert "margin: 0" in handle.styleSheet(), "handle 不应留纵向 margin"


class TestNoCollapseWhileCardOpen:
    def test_inner_drag_does_not_collapse_frame(self):
        """卡片打开期拖内部分隔条：frame 不得坍缩（截图坍缩回归）

        用真实 splitter setSizes 驱动（等价真实拖拽），逐档检查外层 frame 宽度
        稳定。内部分隔条语义 = 两窗格互借宽度，frame 总宽不应变化。
        """
        tm = _make_tm()
        _show_left_card(tm, "tp-left-nocollapse")
        _pump(400)
        sp = tm._sidebar_splitter
        frame_w = tm._splitter.sizes()[0]
        assert frame_w >= _COLLAPSED_PANEL_WIDTH + _FRAME_PADDING_X + 40, f"卡片打开后 frame 应含卡片区，实际 {frame_w}"

        for w in (120, 80, 50, 46, 200, 300):
            sizes = list(sp.sizes())
            total = sum(sizes)
            sp.setSizes([w, max(0, total - w)])
            _flush_resize()
            _pump(250)
            cur = tm._splitter.sizes()[0]
            # 内部分隔条语义：frame 总宽不变（互借），更不得坍缩到收起宽
            assert abs(cur - frame_w) <= 5, f"tab={w} 时 frame 由 {frame_w} 变为 {cur}（内拖不应改 frame）"

    def test_inner_drag_form_follows_width(self):
        """卡片占位期面板形态跟宽度自治（用户反馈 bug 回归）

        旧实现：占位期只按 _collapsed 同步 UI、宽度完全不参与判定 → 无论怎么
        拖，面板永远停在图标条（「内部拉动 tab 无法退出折叠模式」）。
        现语义：占位期做形态自治（跟宽度切换紧凑/展开），但不 emit（防坍缩）。

        验证方式：直接驱动 TabPanel.resizeEvent（不依赖 Qt 事件投递时序，
        该时序在 pytest 环境下不可靠），检查阈值 150/156 的双向切换。
        """
        from PySide6.QtCore import QSize
        from PySide6.QtGui import QResizeEvent

        tm = _make_tm()
        panel = tm._tab_panel
        # 前置复位：单例跨用例会留下面板宽度/形态痕迹，先把面板拉回收起宽，
        # 再进入本用例的阈值验证（否则初始宽度可能已在展开区，首条断言即错位）。
        _sp = tm._sidebar_splitter
        if _sp.count() == 2:
            _sizes = list(_sp.sizes())
            _total = sum(_sizes)
            _tab_idx = _sp.indexOf(panel)
            if 0 <= _tab_idx < len(_sizes):
                _sizes[_tab_idx] = _COLLAPSED_PANEL_WIDTH
                _sp.setSizes(_sizes)
                _flush_resize()
                _pump(250)
        panel._left_card_present = True
        panel._collapsed = True

        def _push_width(w: int):
            panel.resize(w, panel.height())
            panel.resizeEvent(QResizeEvent(QSize(w, panel.height()), QSize(panel.width(), panel.height())))

        # 折叠线 150 / 展开线 156（滞回区 6px）
        _push_width(46)
        assert panel._collapsed is True, "w=46 应收起"
        _push_width(150)
        assert panel._collapsed is True, "w=150（折叠线，未越展开线）应保持收起"
        _push_width(156)
        assert panel._collapsed is False, "w=156（展开线）应退出折叠形态"
        _push_width(200)
        assert panel._collapsed is False, "w=200 应保持展开"
        _push_width(150)
        assert panel._collapsed is False, "从展开态拖到 150（滞回区内）不应折叠"
        _push_width(100)
        assert panel._collapsed is True, "w=100（跌破折叠线）应重新折叠"
        # 清理：恢复自治
        panel._left_card_present = False
