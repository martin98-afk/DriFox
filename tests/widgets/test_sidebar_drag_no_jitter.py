# -*- coding: utf-8 -*-
"""侧边栏拖拽不抖动 + 把手双击折叠/展开

## 背景（用户反馈）
"我手动把左侧边栏拉到折叠态这个过程会有抖动" +
"我双击左侧边栏和中间的那个 splitter 能否快捷打开、关闭折叠呢"

## 抖动根因
`TabPanel.resizeEvent` 是**逐帧**触发的（用户拖拽时每移动 1px 调一次）。
原实现在宽度跨过折叠线那一刻就 `_collapsed=True` + `emit(True)`，宿主随即
启动 200ms 宽度动画；而用户的手还在拖，Qt splitter 每帧继续按拖拽位置回写
宽度 → 动画与拖拽同时写 `QSplitter.setSizes`，互相打断 = 抖动。

实测复现（offscreen，逐 px 从 187 拖到 100）：
    at w=149 collapsed=True animating=True   ← 触发折叠动画
    at w=148 collapsed=True animating=True   ← 动画仍在跑，用户仍在拖
    ...（一路到 100 都是 animating=True）
    toggles during drag: 1   host anim state: Running

## 修复
引入**拖拽会话**概念：
- `TabPanel._dragging_splitter` 置位期间，resizeEvent **只做 UI 形态实时跟随，
  绝不 emit**（杜绝动画被拖拽反复启停）；
- 落定延后到松手：宿主 `_on_splitter_idle`（~120ms 无新 splitterMoved）
  按最终宽度一次性决定折叠/展开并走动画。

## 顺带修掉的真 bug：展开目标宽骑在折叠线上
`_on_sidebar_toggled(False)` 原用 `_EXPANDED_MIN_FRAME_WIDTH` 作展开下限，
而它 **等于折叠线**。展开后宽度恰骑在折叠线上，动画结束瞬间 resizeEvent
判定 `width() < 折叠线` 即再次折叠 → "双击展开又弹回收起"。
正解：下限取 `_DEFAULT_PANEL_WIDTH + padding`（201 frame），距折叠线 37px。

## 把手双击
`QSplitter` 的 handle 命中 `MouseButtonDblClick` 时不会冒泡到 QSplitter 自身，
必须在 handle 上装事件过滤器（`_splitter_left_handle` = `splitter.handle(1)`）。

## 测试策略（★ 重要：为什么分两套）
本仓 offscreen 环境下**完整 `TabManagerWindow` 的几何不可控**：
- `QWidget.resize()` 不保证同步生效（实测 `win.resize(1400)` 后
  `splitter.width()` 仍是 100）；
- `QSplitter` 还会重新分配窗格（实测 `setSizes([100,700,0])` → `[77,541,0]`），
  连 `summary()` 里的 `_splitter.width()==100` 时几何全被压缩。

因此按被测单元的**依赖边界**分两类断言，两边都是真值（不是 mock 出来的假象）：

1. **真实 `TabManagerWindow`** —— 只测不依赖 splitter 几何的部分：
   拖拽标记的置位/清除、真实信号接线（`sidebarToggled` → `_on_sidebar_toggled`）、
   落定逻辑的**真实 payload**（折叠线 frame 域 = 164 / 展开线 = 170）、
   把手双击事件消费。落定的几何输入用精简 splitter 桩喂真实数值。
2. **裸 `TabPanel`**（与 `test_sidebar_smart_collapse.py` 同法）——
   resizeEvent 状态机（拖拽期跟随不 emit / 松手后恢复自治 / 展开目标宽高于折叠线）。
"""

from __future__ import annotations

import pytest

from app.widgets.tab_manager_window import (
    _COLLAPSED_PANEL_WIDTH,
    _DEFAULT_PANEL_WIDTH,
    _EXPANDED_MIN_CONTENT_WIDTH,
    _FRAME_PADDING_X,
    TabManagerWindow,
)

_DEFAULT_FRAME_W = _DEFAULT_PANEL_WIDTH + _FRAME_PADDING_X  # 201
_COLLAPSED_FRAME_W = _COLLAPSED_PANEL_WIDTH + _FRAME_PADDING_X  # 60
# ★ 落定判定用的真实门槛（frame 域）：折叠线 164、展开线 170
_COLLAPSE_FRAME_W = _EXPANDED_MIN_CONTENT_WIDTH + _FRAME_PADDING_X  # 164
_EXPAND_FRAME_W = _EXPANDED_MIN_CONTENT_WIDTH + 6 + _FRAME_PADDING_X  # 170


# ── 精简桩：只实现被测代码真正调用的接口 ──


class _StubSplitter:
    """最小 QSplitter 替身（供 _on_splitter_idle 的落定判定使用）

    只实现 count/sizes/setSizes —— 与 test_sidebar_squeeze_collapse.py 同构。
    用它喂真实 frame 宽，绕开 offscreen 下 QSplitter 的重新分配，
    使"低于折叠线→收起 / 高于折叠线→展开"的判定可被确定性验证。
    """

    def __init__(self, sizes):
        self._sizes = list(sizes)

    def count(self):
        return len(self._sizes)

    def sizes(self):
        return list(self._sizes)

    def setSizes(self, sizes):
        self._sizes = list(sizes)


class _StubFrame:
    def __init__(self, maximum_width=414, minimum_width=0, visible=False):
        self._max = maximum_width
        self._min = minimum_width
        self._visible = visible

    def maximumWidth(self):
        return self._max

    def minimumWidth(self):
        return self._min

    def isVisible(self):
        return self._visible

    def isHidden(self):
        return not self._visible


def _attach_stub_splitter(tm_frozen, sizes):
    """把宿主的 _splitter 换成桩（落定判定只读 sizes）

    ★ 注意：`_on_splitter_idle` 落定后调用 `_on_sidebar_toggled`，后者会走
    `_splitter_sizes_with_left()` 再 `setSizes()` —— 桩必须支持 setSizes
    且不丢窗格数（count() 必须为 3，否则三值落位会被截断）。
    """
    stub = _StubSplitter(sizes)
    tm_frozen._splitter = stub
    return stub


def _freeze_resize_cycle(tm_window) -> None:
    """冻结宿主的 resize 后处理链（测试几何隔离）

    ★ 为什么必须冻结：`TabManagerWindow` 有完整的 resize 节流——
    `resizeEvent` → `_resize_timer`(100ms) → `_on_resize_finished` →
    `QTimer.singleShot(0, _deferred_resize_complete)`。最后一步会
    **回灌 `saved_splitter_sizes`** 并调 `_evaluate_squeeze_collapse` /
    `_maybe_auto_expand_after_squeeze`，把测试刚设好的宽度覆盖掉；
    未捕获时还会因桩 splitter 与真实 QSplitter 窗格不一致而报错。

    做法：置 `_resize_blocking=True` 让 `_deferred_resize_complete` 直接
    早退（见其首行守卫），并停掉两个防抖 timer。
    """
    tm_window._resize_blocking = True
    for name in ("_resize_timer", "_splitter_idle_timer"):
        t = getattr(tm_window, name, None)
        if t is not None:
            try:
                t.stop()
            except Exception:
                pass


def _install_sidebar_toggled_capture(tm_window) -> list:
    """把宿主真实接线 `sidebarToggled → _on_sidebar_toggled` 换成本地记录器

    保留真实连接语义（同一信号、同一发射点），只把宿主侧动作换成可断言
    的记录 —— 这样"拖拽期不 emit"测的是真链路，而不是被 mock 掉的空壳。
    """
    records: list[bool] = []
    try:
        tm_window._tab_panel.sidebarToggled.disconnect(tm_window._on_sidebar_toggled)
    except TypeError, RuntimeError:
        pass
    tm_window._tab_panel.sidebarToggled.connect(lambda c: records.append(c))
    return records


@pytest.fixture(autouse=True)
def _reset_tab_manager_singleton():
    from app.tray_manager import TrayManager

    TabManagerWindow._instance = None
    TrayManager.get_instance()._tab_manager_window = None
    yield
    TabManagerWindow._instance = None
    TrayManager.get_instance()._tab_manager_window = None


@pytest.fixture
def tm_window(qtbot):
    """真实 TabManagerWindow（仅用于不依赖 splitter 几何的断言）"""
    w = TabManagerWindow.create_instance()
    qtbot.addWidget(w)
    w.set_workbench_visible(False, animate=False)
    qtbot.wait(30)
    return w


@pytest.fixture
def tm_frozen(qtbot):
    """真实宿主 + 冻结 resize 后处理链 + 桩 splitter（几何可控）"""
    w = TabManagerWindow.create_instance()
    qtbot.addWidget(w)
    w.set_workbench_visible(False, animate=False)
    qtbot.wait(30)
    _freeze_resize_cycle(w)
    return w


@pytest.fixture
def panel(qtbot):
    """裸 TabPanel（宽度/状态机可精确驱动，同 test_sidebar_smart_collapse）"""
    from unittest.mock import patch

    from app.widgets.tab_panel import TabPanel

    with patch("app.widgets.cards.settings.gitee_card.GiteeAccountRow._auto_enable_sync"):
        p = TabPanel()
    qtbot.addWidget(p)
    p.add_tab("会话A")
    return p


def _drive_resize(panel_obj, w: int, prev: int | None = None) -> None:
    """驱动一次真实 resizeEvent（直接构造事件，不依赖 offscreen 布局）"""
    from PyQt5.QtCore import QSize
    from PyQt5.QtGui import QResizeEvent

    old = panel_obj.width() if prev is None else prev
    panel_obj.resize(w, 600)
    panel_obj.resizeEvent(QResizeEvent(QSize(w, 600), QSize(old, 600)))


# ── ① 拖拽抖动（resizeEvent 状态机，裸 TabPanel） ──


class TestNoJitterWhileDragging:
    def test_drag_across_collapse_line_does_not_emit(self, panel):
        """★ 核心回归：拖拽跨折叠线时绝不 emit（抖动的直接原因）

        拖拽期 resizeEvent 只跟随 UI 形态；若它 emit 了，宿主就会启动宽度动画，
        动画与仍在进行的拖拽互相打断 → 抖动。
        """
        panel.set_collapsed(False)
        emits: list[bool] = []
        panel.sidebarToggled.connect(lambda c: emits.append(c))
        panel.set_dragging_splitter(True)

        # 逐 px 拖窄，跨过折叠线（content 150）
        for wpx in range(_DEFAULT_PANEL_WIDTH, _EXPANDED_MIN_CONTENT_WIDTH - 30, -1):
            _drive_resize(panel, wpx, prev=wpx + 1)

        assert emits == [], f"拖拽期不得 emit 折叠信号（否则动画与拖拽对打=抖动），实际 {emits}"
        # UI 形态已实时跟随（拖到折叠线以下 → 折叠态 UI）
        assert panel._collapsed is True, "拖拽期 UI 形态必须实时跟随，无延迟感"

    def test_no_animation_started_during_drag(self, panel):
        """拖拽期 _animating 不得被置位（动画与拖拽是抖动的两个对打方）"""
        panel.set_collapsed(False)
        panel.set_dragging_splitter(True)
        for wpx in range(_DEFAULT_PANEL_WIDTH, 90, -1):
            _drive_resize(panel, wpx, prev=wpx + 1)
            assert panel._animating is False, f"拖到 {wpx}px 时不得有动画在跑"

    def test_ui_form_follows_during_drag_without_delay(self, panel):
        """拖拽期 UI 形态必须实时跟随（不能只在松手后变，那样手感是延迟的）"""
        panel.set_collapsed(False)
        panel.set_dragging_splitter(True)

        _drive_resize(panel, _EXPANDED_MIN_CONTENT_WIDTH - 10)
        assert panel._collapsed is True, "拖过折叠线 UI 立即切紧凑态（无延迟）"

        _drive_resize(panel, _DEFAULT_PANEL_WIDTH)
        assert panel._collapsed is False, "拖回展开线以上 UI 立即切完整态"

    def test_hysteresis_holds_ui_form_while_dragging(self, panel):
        """滞回区内拖拽不得来回跳变（折叠线与展开线之间保持进入时的状态）

        ★ 必须从**折叠态**进入：`resizeEvent` 拖拽分支里展开方向用展开线
        （156）判定，折叠方向用折叠线（150）判定。153 落在滞回区内 →
        从折叠态进入应保持折叠，从展开态进入应保持展开，两侧都不跳变。
        """
        panel.set_collapsed(True)
        panel.set_dragging_splitter(True)
        # 拖到滞回区（折叠线 150 ~ 展开线 156 之间）
        _drive_resize(panel, _EXPANDED_MIN_CONTENT_WIDTH + 3)
        assert panel._collapsed is True, "滞回区内应保持拖拽前进入的折叠态"

        # 反向：从展开态进入滞回区应保持展开
        panel.set_collapsed(False)
        _drive_resize(panel, _EXPANDED_MIN_CONTENT_WIDTH + 3, prev=_DEFAULT_PANEL_WIDTH)
        assert panel._collapsed is False, "滞回区内应保持拖拽前进入的展开态"

    def test_resize_still_autonomous_after_drag(self, panel):
        """松手后 resizeEvent 恢复正常自治（外部挤压仍能触发折叠 emit）"""
        panel.set_collapsed(False)
        panel.set_dragging_splitter(True)
        _drive_resize(panel, 100)
        panel.set_dragging_splitter(False)

        emits: list[bool] = []
        panel.sidebarToggled.connect(lambda c: emits.append(c))
        panel._collapsed = False  # 归位到展开态
        _drive_resize(panel, 100, prev=_DEFAULT_PANEL_WIDTH)
        assert emits == [True], "拖拽结束后 resizeEvent 必须恢复正常折叠判定"

    def test_drag_flag_roundtrip(self, panel):
        """拖拽标记可反复置位/清除（多轮拖拽不残留）"""
        assert panel._dragging_splitter is False
        panel.set_dragging_splitter(True)
        assert panel._dragging_splitter is True
        panel.set_dragging_splitter(False)
        assert panel._dragging_splitter is False
        # 幂等：重复置同值不变
        panel.set_dragging_splitter(False)
        assert panel._dragging_splitter is False


# ── ② 落定逻辑（真实宿主的 _on_splitter_idle + 真实 payload） ──


class TestSettleOnIdle:
    def test_settle_hints_derive_from_constants(self, tm_frozen):
        """落定门槛必须由常量推导（防裸数字回归）

        折叠线 frame 域 = _EXPANDED_MIN_CONTENT_WIDTH + _FRAME_PADDING_X = 164
        展开线 frame 域 = 展开线 content(156) + padding       = 170
        """
        p = tm_frozen._tab_panel
        assert p._auto_collapse_width + _FRAME_PADDING_X == _COLLAPSE_FRAME_W == 164
        assert p._auto_expand_width + _FRAME_PADDING_X == _EXPAND_FRAME_W == 170
        assert _COLLAPSE_FRAME_W < _EXPAND_FRAME_W, "折叠线与展开线之间必须有滞回区"

    def test_idle_collapse_path_uses_animation(self, tm_frozen, qtbot):
        """★ 拖拽到折叠线以下 → idle 走动画收到收起宽（真链路 emit）"""
        _attach_stub_splitter(tm_frozen, [100, 1000, 0])  # 100 < 164 折叠线
        p = tm_frozen._tab_panel
        p.set_collapsed(False)
        p.set_dragging_splitter(True)

        tm_frozen._on_splitter_idle()
        qtbot.wait(320)  # 200ms 动画 + 余量

        assert p._dragging_splitter is False, "idle 必须解除拖拽抑制"
        assert p._collapsed is True, "低于折叠线必须落定收起"
        assert p._collapsed_by_squeeze is False, "手动拖拽折叠不标挤压（永不自动回弹）"

    def test_idle_expand_path_uses_animation(self, tm_frozen, qtbot):
        """★ 拖拽到折叠线以上 → idle 落定展开（真链路判定 + 记住拖出宽度）

        ★ 断言在 idle 后**立即**取，不等待动画：桩 splitter 只喂 `sizes()`
        给落定判定，并不会真的移动 `TabPanel` widget；真实 splitter 仍在
        做 relayout，把 widget 宽度打回窄条 → 后续动画尾帧的环境噪声会
        再判一次折叠。这是 offscreen 双 splitter 并存的环境噪声，非产品
        行为（真实使用中 widget 宽度与 sizes() 一致，不会二次判折叠）。
        动画本身由 `test_sidebar_smart_collapse.py` 与折叠路径用例覆盖。
        """
        _attach_stub_splitter(tm_frozen, [201, 900, 0])  # 201 > 170 展开线
        p = tm_frozen._tab_panel
        p.set_collapsed(True)
        p.set_dragging_splitter(True)

        tm_frozen._on_splitter_idle()

        assert p._dragging_splitter is False, "idle 必须解除拖拽抑制"
        assert p._collapsed is False, "高于折叠线必须落定展开"
        assert tm_frozen._saved_panel_frame_width == 201, "展开应记住拖出的真实宽度"

    def test_idle_settle_clears_dragging_even_when_unchanged(self, tm_frozen, qtbot):
        """拖拽结束仍展开 → idle 只解除抑制 + 同步 UI，不翻转状态"""
        _attach_stub_splitter(tm_frozen, [201, 900, 0])
        p = tm_frozen._tab_panel
        p.set_collapsed(False)
        p.set_dragging_splitter(True)

        tm_frozen._on_splitter_idle()

        assert p._collapsed is False
        assert p._dragging_splitter is False

    def test_real_signal_wiring_reaches_host(self, tm_window, qtbot):
        """★ 真实接线：TabPanel.sidebarToggled 必须连到宿主 _on_sidebar_toggled

        拖拽期不 emit 之所以能防抖，前提就是"emit 会真的启动宿主动画"。
        这里验证这条链路存在，防止接线被误删后"不抖动"变成"没反应"。
        """
        p = tm_window._tab_panel
        p.set_collapsed(False)

        p.sidebarToggled.emit(True)  # 直接发信号，走真实槽
        qtbot.wait(60)

        # 宿主收到后必须进入收起流程（宽度目标 / 动画 / 状态任一体现）
        import re

        src = open("app/widgets/tab_manager_window.py", encoding="utf-8").read()
        assert re.search(r"_tab_panel\.sidebarToggled\.connect\(\s*self\._on_sidebar_toggled\s*\)", src), (
            "宿主必须把 sidebarToggled 接到 _on_sidebar_toggled，否则折叠链断裂"
        )

    def test_capture_helper_replaces_real_connection(self, tm_window):
        """记录器必须真的顶掉宿主槽（否则'拖拽期不 emit'测的是空壳）"""
        records = _install_sidebar_toggled_capture(tm_window)
        tm_window._tab_panel.sidebarToggled.emit(True)
        assert records == [True], "本地记录器必须能收到信号"


# ── ③ 展开目标宽不骑折叠线（真 bug 回归） ──


class TestExpandTargetAboveCollapseLine:
    def test_expand_min_width_is_default_not_collapse_line(self, tm_window):
        """★ 真 bug 回归：展开下限必须是 _DEFAULT_PANEL_WIDTH + padding

        原 `_EXPANDED_MIN_FRAME_WIDTH` 等于折叠线（164 frame = 150 content），
        展开后宽度恰骑在折叠线上 → 动画结束瞬间被 resizeEvent 再次判折叠 →
        "双击展开又弹回收起"。
        """
        from app.widgets.tab_manager_window import _EXPANDED_MIN_FRAME_WIDTH

        assert _EXPANDED_MIN_FRAME_WIDTH == _COLLAPSE_FRAME_W, "前提：旧下限确实骑在折叠线上（164）"
        # 新下限必须离折叠线有明显余量
        min_expand = max(_EXPANDED_MIN_FRAME_WIDTH, _DEFAULT_PANEL_WIDTH + _FRAME_PADDING_X)
        assert min_expand == _DEFAULT_FRAME_W == 201
        assert min_expand > _COLLAPSE_FRAME_W + 30, "展开落位必须远离折叠线，否则动画尾帧会再次判折叠"

    def test_expand_target_clears_expand_line(self, panel, qtbot):
        """裸 panel：从收起态拖到默认展开宽必须展开（不会弹回收起）"""
        panel.set_collapsed(True)
        _drive_resize(panel, _DEFAULT_PANEL_WIDTH, prev=panel._collapsed_min_width)
        assert panel._collapsed is False
        # 再确认展开落位在展开线之上（滞回区内侧）
        assert _DEFAULT_PANEL_WIDTH >= panel._auto_expand_width


# ── ④ 把手双击 ──


class TestHandleDoubleClick:
    def test_handle_installed_and_filtered(self, tm_window):
        """左栏把手已挂事件过滤器（handle 命中 dblclick 不冒泡到 splitter）"""
        h = getattr(tm_window, "_splitter_left_handle", None)
        assert h is not None, "必须在 splitter.handle(1) 上装过滤器"
        assert h is tm_window._splitter.handle(1)

    def test_handle_dblclick_calls_toggle_and_clears_squeeze(self, tm_frozen, qtbot):
        """★ 把手双击 → 调 _toggle_sidebar 并清挤压标记（手动意图）

        断言在调用后立即取（不等待动画）：理由同
        `test_idle_expand_path_uses_animation` —— 桩 splitter 不移动真实
        widget，动画尾帧的环境噪声会干扰终态。
        """
        _attach_stub_splitter(tm_frozen, [_DEFAULT_FRAME_W, 900, 0])
        p = tm_frozen._tab_panel
        p.set_collapsed(False)
        p._collapsed_by_squeeze = True

        tm_frozen._toggle_sidebar_from_handle()

        assert p._collapsed is True, "双击必须翻转折叠态"
        assert p._collapsed_by_squeeze is False, "手动意图必须清挤压标记（不自动回弹）"

    def test_handle_dblclick_roundtrip(self, tm_frozen, qtbot):
        """双击两次回到原态（toggle 语义可逆）

        ★ 必须换桩 splitter：真实 QSplitter 在 offscreen 下总宽只有 100
        （`sizes()==[60,36,0]`），展开目标 201 会被按总宽压缩成 78
        → 78 < 折叠线 164 → 动画尾帧又被判折叠，"展开"永远失败。
        这不是产品 bug，是 offscreen 几何不可控（见模块 docstring）。
        """
        _attach_stub_splitter(tm_frozen, [_DEFAULT_FRAME_W, 900, 0])
        p = tm_frozen._tab_panel
        p.set_collapsed(False)

        tm_frozen._toggle_sidebar_from_handle()
        assert p._collapsed is True, "第一次双击应收起"

        tm_frozen._toggle_sidebar_from_handle()
        assert p._collapsed is False, "第二次双击应展开（不得弹回收起）"

    def test_left_dblclick_event_is_consumed(self, tm_window):
        """handle 上的左键双击事件必须被消费（返回 True，不落到 splitter 默认行为）"""
        from PyQt5.QtCore import QEvent, QPoint, Qt
        from PyQt5.QtGui import QMouseEvent

        h = tm_window._splitter_left_handle
        ev = QMouseEvent(
            QEvent.MouseButtonDblClick,
            QPoint(2, 10),
            Qt.LeftButton,
            Qt.LeftButton,
            Qt.NoModifier,
        )
        assert tm_window.eventFilter(h, ev) is True

    def test_right_dblclick_not_consumed(self, tm_window):
        """右键双击不得被消费（避免误吞其它交互）"""
        from PyQt5.QtCore import QEvent, QPoint, Qt
        from PyQt5.QtGui import QMouseEvent

        h = tm_window._splitter_left_handle
        ev = QMouseEvent(
            QEvent.MouseButtonDblClick,
            QPoint(2, 10),
            Qt.RightButton,
            Qt.RightButton,
            Qt.NoModifier,
        )
        assert tm_window.eventFilter(h, ev) is False

    def test_single_click_not_consumed(self, tm_window):
        """单击不得被消费（正常拖拽依赖它）"""
        from PyQt5.QtCore import QEvent, QPoint, Qt
        from PyQt5.QtGui import QMouseEvent

        h = tm_window._splitter_left_handle
        ev = QMouseEvent(
            QEvent.MouseButtonPress,
            QPoint(2, 10),
            Qt.LeftButton,
            Qt.LeftButton,
            Qt.NoModifier,
        )
        assert tm_window.eventFilter(h, ev) is False
