# -*- coding: utf-8 -*-
"""delegate wheel 抢断回归测试（悬浮卡片滚动失效根修，2026-10-13）。

症状：
    「大模型的消息卡片经常出现滚动失效——鼠标悬浮在卡片上时无法滚动」。

根因（探针实测，tests/debug/probe_webview_wheel_sendinput.py）：
    滚轮事件的真实第一接收者是 WebEngine 内部 delegate
    （RenderWidgetHostViewQtDelegateWidget，即 focusProxy），不是
    CodeWebViewer 本身。body 有滚动空间（[MEM] 面积上限截断、高度收敛窗口期的
    瞬时溢出）时 Chromium 直接消费 wheel，CodeWebViewer.wheelEvent() override
    永远收不到 → 「外层优先」两级规则失效（S2 超限卡 8/8 全吞）；卡内滚到边界
    后 Blink 也不向 Qt 宿主 scroll-chaining → 事件黑洞（S3 仍全吞）。
    而 wheelEvent 老路径只在 body 无溢出（事件上抛）时可达 → 表现为"经常失效"。

修复不变式（本文件即守卫）：
    ① focusProxy 就绪后幂等挂 self 为其 eventFilter（init / heightReported /
      showEvent 三补装点；renderer 崩溃重建后由回报自然重装）；
    ② delegate 收到 wheel：外层有余量 → 转发外层并吃掉（返回 True）；
    ③ 外层已到边界 → 放行（返回 False），卡内 body 内滚安全网保持可达；
    ④ 非 filter 目标对象的事件不拦截。

这些测试不加载页面（不触发 Chromium 导航），可在无 GPU 环境运行。
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication, QWidget

QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)


@pytest.fixture()
def viewer(qtbot):
    from app.core.infra.webengine_profile import init_shared_web_profile

    init_shared_web_profile()
    from app.widgets.card_viewers import CodeWebViewer

    try:
        v = CodeWebViewer()
    except Exception as e:  # noqa: BLE001 - 无 WebEngine 环境直接跳过
        pytest.skip(f"CodeWebViewer 不可用: {e}")
    qtbot.addWidget(v)
    return v


def _mount_with_scroll_area(viewer, value, minimum, maximum):
    """挂到带桩 chat_scroll_area 的宿主上（同 test_body_height_cap_sync 模式）。

    宿主经 viewer._test_host 保活（viewer 由 qtbot 持有 → parent 链整条存活）。
    """
    outer = MagicMock()
    outer.wheelEvent.side_effect = lambda _e: outer._got.append(True)
    outer._got = []
    outer.verticalScrollBar.return_value = SimpleNamespace(
        value=lambda: value, minimum=lambda: minimum, maximum=lambda: maximum
    )
    card = QWidget()
    card._parent = SimpleNamespace(chat_scroll_area=outer)
    viewer._test_host = card
    viewer.setParent(card)
    return outer


def _wheel(dy: int) -> QWheelEvent:
    return QWheelEvent(
        QPointF(20, 20),
        QPointF(20, 20),
        QPoint(0, 0),
        QPoint(0, dy),
        Qt.NoButton,
        Qt.NoModifier,
        Qt.NoScrollPhase,
        False,
    )


def _ready_filter_target(viewer):
    """取 focusProxy 并标记为已安装目标；未就绪则 skip。"""
    fp = viewer.focusProxy()
    if fp is None:
        pytest.skip("focusProxy 未就绪（无 WebEngine 环境）")
    viewer._wheel_filter_target = fp
    return fp


# ── ① 幂等挂装 ──


def test_filter_installed_on_focus_proxy_and_idempotent(viewer, qtbot):
    fp = viewer.focusProxy()
    if fp is None:
        pytest.skip("focusProxy 未就绪（无 WebEngine 环境）")
    # __init__ 尾部首装已生效（setPage 链内 delegate 即就绪）
    assert viewer._wheel_filter_target is fp
    # 幂等（行为级）：重复调用后，一次 wheel 事件只产生一次外层转发
    # （Qt installEventFilter 自带去重 + 链首短路，双重挂装会双倍滚动）
    outer = _mount_with_scroll_area(viewer, 100, 0, 500)
    viewer._ensure_wheel_filter_installed()
    viewer._ensure_wheel_filter_installed()
    from PySide6.QtCore import QEvent

    ev = _wheel(-120)
    QApplication.sendEvent(fp, ev)
    assert outer._got == [True]


# ── ②③ delegate 侧两级裁决 ──


def test_delegate_filter_intercepts_and_forwards_outer(viewer):
    fp = _ready_filter_target(viewer)
    outer = _mount_with_scroll_area(viewer, 100, 0, 500)
    ev = _wheel(-120)
    # 外层有余量 → 拦截（True）且外层收到
    assert viewer.eventFilter(fp, ev) is True
    assert outer._got == [True]
    assert ev.isAccepted()


def test_delegate_filter_passes_at_outer_bottom(viewer):
    fp = _ready_filter_target(viewer)
    outer = _mount_with_scroll_area(viewer, 500, 0, 500)
    ev = _wheel(-120)
    # 外层已到底还往下滚 → 放行（False），卡内 body 内滚安全网
    assert viewer.eventFilter(fp, ev) is False
    assert outer._got == []


def test_delegate_filter_ignores_non_target(viewer):
    fp = _ready_filter_target(viewer)
    outer = _mount_with_scroll_area(viewer, 100, 0, 500)
    ev = _wheel(-120)
    # 非安装目标的事件不拦截（放行给 Qt 正常派发链）；
    # 桩用同型 QWidget（PyQt eventFilter 签名要求 QObject）
    stranger = QWidget()
    assert viewer.eventFilter(stranger, ev) is False
    assert outer._got == []


# ── ⑤ [WHEEL-ARM] 武装态无内滚条时的外层兜底（2026-10-14 修） ──


def test_armed_state_forwards_outer_when_body_has_no_overflow(viewer):
    """点击过正文（armed）+ 内外都无内滚条 → 滚轮必须交外层。

    修复前：delegate filter 走 _route_wheel_armed，unknown 分支 return False
    后直接落到 super().eventFilter，事件被 delegate 终结、不上抛 wheelEvent
    → 外层滚动区永远收不到。表现为"点击过卡片正文的无内滚条消息，滚轮彻底失效"。

    ⚠️ 本用例必须显式声明内层容器不可滚。只看 body 几何的初版兜底把
    #tool-content 的滚轮也判给了外层（折叠框内部滚不动），判据已收紧为
    「内外都滚不动」，见 test_wheel_belongs_to_outer_unit。
    """
    fp = _ready_filter_target(viewer)
    outer = _mount_with_scroll_area(viewer, 100, 0, 500)
    # 武装态 + body 几何有效但不溢出 + 内层容器也不溢出
    viewer._wheel_inner_armed = True
    viewer._on_body_geometry_reported(scroll_height=400, scroll_top=0, client_height=400)
    viewer._on_inner_scrollable_reported(False)
    ev = _wheel(-120)
    assert viewer.eventFilter(fp, ev) is True, "内外都无滚轮时必须接手转发外层"
    assert outer._got == [True]
    assert ev.isAccepted()


def test_armed_state_does_not_double_scroll_when_body_overflows(viewer):
    """armed + body 溢出 → 卡内滚，外层不得动（防兜底打穿"内部优先"）。"""
    fp = _ready_filter_target(viewer)
    outer = _mount_with_scroll_area(viewer, 100, 0, 500)
    viewer._wheel_inner_armed = True
    viewer._on_body_geometry_reported(scroll_height=1200, scroll_top=0, client_height=400)
    ev = _wheel(-120)
    assert viewer.eventFilter(fp, ev) is False, "body 溢出态必须放行给卡内滚动"
    assert outer._got == [], "外层不得被兜底误触发（否则内外双重滚动）"


# ── ⑥ [WHEEL-ARM] 折叠框滚轮归属（2026-10-14 第二修） ──


def test_armed_state_keeps_inner_scroll_when_body_has_no_overflow(viewer):
    """armed + body 不溢出 + 内层容器（#tool-content）可滚 → 必须放行给卡内。

    折叠框是独立 max-height 滚动区，滚动不改变 body 几何。只看 body 几何的
    兜底会把它的滚轮误判给外层 → "工具与思考折叠框内部滚不动"。
    """
    fp = _ready_filter_target(viewer)
    outer = _mount_with_scroll_area(viewer, 100, 0, 500)
    viewer._wheel_inner_armed = True
    viewer._on_body_geometry_reported(scroll_height=400, scroll_top=0, client_height=400)
    viewer._on_inner_scrollable_reported(True)
    ev = _wheel(-120)
    assert viewer.eventFilter(fp, ev) is False, "内层容器可滚时必须放行给 Chromium"
    assert outer._got == [], "外层不得抢走折叠框的滚轮"


def test_armed_state_forwards_outer_only_when_nothing_inner_scrolls(viewer):
    """armed + body 不溢出 + 内层容器也不可滚 → 才交外层（兜底收紧后的正确形态）。"""
    fp = _ready_filter_target(viewer)
    outer = _mount_with_scroll_area(viewer, 100, 0, 500)
    viewer._wheel_inner_armed = True
    viewer._on_body_geometry_reported(scroll_height=400, scroll_top=0, client_height=400)
    viewer._on_inner_scrollable_reported(False)
    ev = _wheel(-120)
    assert viewer.eventFilter(fp, ev) is True, "内外都滚不动才接手转发外层"
    assert outer._got == [True]


def test_armed_state_passes_when_inner_scrollability_unknown(viewer):
    """旧骨架未回传内层可滚标志（None）→ 保守放行，不抢内层滚轮。"""
    fp = _ready_filter_target(viewer)
    outer = _mount_with_scroll_area(viewer, 100, 0, 500)
    viewer._wheel_inner_armed = True
    viewer._on_body_geometry_reported(scroll_height=400, scroll_top=0, client_height=400)
    viewer._on_inner_scrollable_reported(None)
    ev = _wheel(-120)
    assert viewer.eventFilter(fp, ev) is False
    assert outer._got == []


def test_wheel_belongs_to_outer_unit(viewer):
    """兜底判据纯逻辑：只有"内外都滚不动"才归外层。"""
    # body 溢出 → 卡内的事，外层别碰
    viewer._on_body_geometry_reported(1200, 0, 400)
    viewer._on_inner_scrollable_reported(True)
    assert viewer._wheel_belongs_to_outer(-120) is False
    # body 不溢出 + 内层可滚 → 折叠框的事，外层别碰
    viewer._on_body_geometry_reported(400, 0, 400)
    viewer._on_inner_scrollable_reported(True)
    assert viewer._wheel_belongs_to_outer(-120) is False
    # body 不溢出 + 内层不可滚 → 外层
    viewer._on_inner_scrollable_reported(False)
    assert viewer._wheel_belongs_to_outer(-120) is True
    # 内层未知 → 保守放行
    viewer._on_inner_scrollable_reported(None)
    assert viewer._wheel_belongs_to_outer(-120) is False
    # 几何未回传 → 保守放行
    viewer._body_geom_valid = False
    viewer._on_inner_scrollable_reported(False)
    assert viewer._wheel_belongs_to_outer(-120) is False


def test_inner_scrollable_reset_on_reuse(viewer):
    """viewer 复用必须清空内层可滚缓存，否则上张卡的状态污染下一张。"""
    viewer._on_inner_scrollable_reported(True)
    viewer._on_body_geometry_reported(400, 0, 400)
    viewer.reset_for_reuse()
    assert viewer._inner_scrollable is None
    assert viewer._body_geom_valid is False

def test_height_payload_parses_inner_scrollable_field():
    """[WHEEL-ARM] 第 6 字段解析：'1'/'0'/缺失 三态。"""
    from app.widgets.card_viewers import _parse_height_payload

    # 新骨架：6 字段齐全
    assert _parse_height_payload("400|0|400|1|123|1")[5] is True
    assert _parse_height_payload("400|0|400|1|123|0")[5] is False
    # 旧骨架：只到第 5 字段 → None（Python 侧按"可能可滚"放行）
    assert _parse_height_payload("400|0|400|1|123")[5] is None
    # 更旧骨架：仅高度 → None
    assert _parse_height_payload("400")[5] is None
    # 第 6 字段非 0/1 → None
    assert _parse_height_payload("400|0|400|1|123|2")[5] is None


def test_inner_scrollable_signal_wired():
    """静态接线守卫：信号必须连到缓存方法（防断线后折叠框滚轮被抢）。"""
    import io
    import os

    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    src = io.open(os.path.join(root, "app", "widgets", "card_viewers.py"), encoding="utf-8").read()
    assert "bodyInnerScrollableChanged" in src, "必须存在内层可滚信号"
    assert "_on_inner_scrollable_reported" in src, "必须存在缓存方法"
    assert "_wheel_belongs_to_outer" in src, "兜底判据必须含内层容器可滚性"
    # JS 侧必须真的回传第 6 字段（探针验证过 tool-content 能滚但 body 几何不动）
    assert "+ '|' + _ins" in src, "reportHeight 必须携带内层可滚字段"


# ── ④ 静态接线守卫 ──


def test_wheel_intercept_wiring_static():
    import io
    import os

    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    src = io.open(os.path.join(root, "app", "widgets", "card_viewers.py"), encoding="utf-8").read()
    assert "_ensure_wheel_filter_installed" in src, "必须存在 delegate 抢断补装"
    # 三个补装点：init 尾部 + heightReported + showEvent（防回归时只装一处）
    assert src.count("self._ensure_wheel_filter_installed()") >= 3, (
        "补装点必须 ≥3（init / heightReported / showEvent），"
        "renderer 崩溃重建 delegate 后依赖回报信号重装"
    )
    assert "eventFilter" in src and "QEvent.Wheel" in src
