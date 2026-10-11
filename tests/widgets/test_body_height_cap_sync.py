# -*- coding: utf-8 -*-
"""[MEM-2] 卡片高度上限与「文档滚动容器上限」的一致性回归测试。

症状（2026-10-11 真机）：
    「消息卡片经常出现文本内容超出可视区域造成文本遮挡」——长回复的尾部
    （往往正是最后一段代码块的后几行）被卡片底边整行切掉，且**滚不到**。

根因：
    html{overflow:hidden} 关闭了文档级滚动，body 是唯一滚动容器；而 widget
    高度自 1aa78021 起按「物理面积」收缩（CodeWebViewer.MAX_SURFACE_MPX，
    dpr=2.25 + 卡片 1200 逻辑宽 → 上限仅 ~1975px），body 的 max-height 却仍是
    静态 MAX_HEIGHT（dpr=2.25 时 7111px）。于是内容高介于两者之间时：
        内容高 > widget 上限（被裁）   且   内容高 ≤ body max-height（body 无溢出）
    → body 不可滚、滚轮被外层聊天区吞掉 → 被裁内容既不可见也无任何滚动手径。

修复不变式（本文件即守卫）：
    ① clamp_surface_height(x) ≤ body_max_height_cap()，且 x 足够大时二者相等；
    ② body_max_height_cap 变化时通过 _dfxSetBodyMaxH 下发（幂等）；
    ③ JS 未就绪时挂起待推，就绪后补推（不能丢，否则整个渲染周期都用错上限）；
    ④ 外层滚动区已到边界时，滚轮必须交回页面（否则卡内滚动等于不存在）。

这些测试不加载页面（不触发 Chromium 导航），可在无 GPU 环境运行。

⚠️ 夹具一律走 pytest-qt 的 ``qtbot``，**不要**自建 module 级 QApplication 夹具：
   本文件跑完时它会把 QApplication 一并销毁，而共享 QWebEngineProfile 是模块级
   单例（init_shared_web_profile 只建一次）→ 后续文件的 page 夹具拿到已析构的
   profile（RuntimeError: QWebEngineProfile ... has been deleted）。
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


def _force_area_cap(viewer, cap: int) -> None:
    """把面积上限钉成固定值（测试环境 dpr 常为 1，面积上限不生效）。"""
    viewer._surface_area_height_cap = lambda _w, _c=cap: _c


def _fake_page():
    page = MagicMock()
    page._calls = []
    page.runJavaScript.side_effect = lambda code, *a, **k: page._calls.append(code)
    return page


# ── ① widget 上限 ≡ 文档滚动容器上限 ──


def test_clamp_equals_body_cap_when_over_limit(viewer):
    viewer.resize(1200, 400)
    _force_area_cap(viewer, 1500)
    assert viewer.body_max_height_cap() == 1500
    # 上报高度远超上限 → 钳到上限；钳后值必须与文档滚动容器上限同值，
    # 否则被裁的那段不可达（本回归的根因）。
    assert viewer.clamp_surface_height(999_999) == 1500


def test_clamp_keeps_small_height_and_floors_at_40(viewer):
    viewer.resize(700, 400)
    _force_area_cap(viewer, 5000)
    assert viewer.clamp_surface_height(1200) == 1200
    assert viewer.clamp_surface_height(3) == 40
    # 异常入参不炸：返回本 view 的上限
    assert viewer.clamp_surface_height(None) == 5000
    assert viewer.clamp_surface_height("x") == 5000


def test_body_cap_never_exceeds_class_limit(viewer):
    viewer.resize(700, 400)
    # 面积上限远大于类上限时，取类上限（单边纹理限制）
    viewer._surface_area_height_cap = lambda _w: 10**7
    assert viewer.body_max_height_cap() == viewer.MAX_HEIGHT


# ── ②/③ 下发与挂起 ──


def test_sync_pushes_cap_and_is_idempotent(viewer):
    viewer.resize(800, 300)
    _force_area_cap(viewer, 2000)
    page = _fake_page()
    viewer.page = lambda: page
    viewer._is_js_ready = True
    viewer._body_max_h_applied = 0

    assert viewer.sync_body_max_height() == 2000
    assert any("_dfxSetBodyMaxH(2000)" in c for c in page._calls)
    n = len(page._calls)
    # 幂等：上限未变 → 不再下发（高度应用点每次都调它）
    viewer.sync_body_max_height()
    assert len(page._calls) == n


def test_sync_follows_width_change(viewer):
    page = _fake_page()
    viewer.page = lambda: page
    viewer._is_js_ready = True
    viewer._body_max_h_applied = 0
    viewer.resize(800, 300)
    _force_area_cap(viewer, 4000)
    viewer.sync_body_max_height()
    _force_area_cap(viewer, 1800)  # 模拟窗口变宽 / dpr 变化后的新上限
    viewer.sync_body_max_height()
    assert any("_dfxSetBodyMaxH(4000)" in c for c in page._calls)
    assert any("_dfxSetBodyMaxH(1800)" in c for c in page._calls)


def test_sync_defers_until_js_ready(viewer):
    viewer.resize(800, 300)
    _force_area_cap(viewer, 2600)
    page = _fake_page()
    viewer.page = lambda: page
    viewer._is_js_ready = False
    viewer._body_max_h_applied = 0

    viewer.sync_body_max_height()
    assert page._calls == []  # 未就绪 → 不下发
    assert viewer._body_max_h_pending is True

    viewer._is_js_ready = True
    viewer.sync_body_max_height(force=True)  # _on_js_ready 的补推
    assert any("_dfxSetBodyMaxH(2600)" in c for c in page._calls)
    assert viewer._body_max_h_pending is False


# ── ④ 滚轮：外层到边界必须交回页面 ──


class _Bar:
    def __init__(self, value, minimum, maximum):
        self._v, self._min, self._max = value, minimum, maximum

    def value(self):
        return self._v

    def minimum(self):
        return self._min

    def maximum(self):
        return self._max


def _mount_with_scroll_area(qtbot, viewer, value, minimum, maximum):
    """把 viewer 挂到带 chat_scroll_area 桩的宿主上，返回「外层是否收到滚轮」记录。

    ⚠️ 宿主 QWidget 必须被引用住：setParent 后若宿主被 GC，Qt 会连带销毁子控件
    （viewer 变成 deleted object，后续调用一律 RuntimeError）。故同时登记进 qtbot。
    """
    outer = MagicMock()
    outer.wheelEvent.side_effect = lambda _e: outer._got.append(True)
    outer._got = []
    outer.verticalScrollBar.return_value = _Bar(value, minimum, maximum)
    card = QWidget()
    card._parent = SimpleNamespace(chat_scroll_area=outer)
    viewer._test_host = card  # 保活
    qtbot.addWidget(card)
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


def test_wheel_forwarded_when_outer_can_scroll(qtbot, viewer):
    outer = _mount_with_scroll_area(qtbot, viewer, value=100, minimum=0, maximum=500)
    viewer.wheelEvent(_wheel(-120))  # 外层还有余量
    assert outer._got == [True]  # → 归外层（既有体感）


def test_wheel_handed_to_page_at_outer_bottom(qtbot, viewer):
    outer = _mount_with_scroll_area(qtbot, viewer, value=500, minimum=0, maximum=500)
    viewer.wheelEvent(_wheel(-120))  # 外层已到底还往下滚
    assert outer._got == []  # → 交回页面（卡内 body 才有机会滚）


def test_wheel_handed_to_page_at_outer_top(qtbot, viewer):
    outer = _mount_with_scroll_area(qtbot, viewer, value=0, minimum=0, maximum=500)
    viewer.wheelEvent(_wheel(120))  # 外层已到顶还往上滚
    assert outer._got == []


# ── 静态接线守卫（资产变更必须同步 bump 骨架版本号）──


def test_skeleton_assets_wired():
    import io
    import os

    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    src = io.open(os.path.join(root, "app", "widgets", "card_viewers.py"), encoding="utf-8").read()
    assert "max-height: var(--dfx-body-max-h," in src, "body max-height 必须走 CSS 变量"
    assert "{_BODY_HEIGHT_CAP_JS}" in src, "骨架必须注入 _dfxSetBodyMaxH"
    # 滚轮两级链式：边界判定存在
    assert "_at_bound" in src, "wheelEvent 必须有外层边界判定（否则卡内滚动不可达）"

    core = io.open(os.path.join(root, "app", "widgets", "card_render_core.py"), encoding="utf-8").read()
    assert "window._dfxSetBodyMaxH = function" in core
    from app.widgets.card_render_core import _SKELETON_CACHE_VERSION

    assert _SKELETON_CACHE_VERSION >= 46, "骨架 CSS/JS 资产变更后必须 +1 版本号"
