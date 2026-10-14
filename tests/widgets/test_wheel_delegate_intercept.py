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
from PyQt5.QtCore import QPoint, QPointF, Qt
from PyQt5.QtGui import QWheelEvent
from PyQt5.QtWidgets import QApplication, QWidget

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
    from PyQt5.QtCore import QEvent

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
    """点击过正文（armed）+ body 无溢出 → 滚轮必须交外层。

    修复前：delegate filter 走 _route_wheel_armed，unknown 分支 return False
    后直接落到 super().eventFilter，事件被 delegate 终结、不上抛 wheelEvent
    → 外层滚动区永远收不到。表现为"点击过卡片正文的无内滚条消息，滚轮彻底失效"。
    """
    fp = _ready_filter_target(viewer)
    outer = _mount_with_scroll_area(viewer, 100, 0, 500)
    # 武装态 + body 几何有效但不溢出（max_scroll == 0 → state == "unknown"）
    viewer._wheel_inner_armed = True
    viewer._on_body_geometry_reported(scroll_height=400, scroll_top=0, client_height=400)
    ev = _wheel(-120)
    assert viewer.eventFilter(fp, ev) is True, "无内滚条的武装态必须接手转发外层"
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
