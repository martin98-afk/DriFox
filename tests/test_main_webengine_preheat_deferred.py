# -*- coding: utf-8 -*-
"""L4 WebEngine 预热推迟回归（#28）

覆盖：
1. 互斥：真实渲染先行（任意 QWebEngineView 实例在册）→ 预热跳过（构造零调用）
2. 保底：无真实渲染 → 预热创建 + app 引用挂载 + 5s 释放排程
3. 释放语义：释放回调销毁视图并清引用
4. 推迟接线：_deferred_startup 内 singleShot(1500) AST 守卫（防回退启动即拉）
"""

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip("PyQt5.QtWidgets")
pytest.importorskip("PyQt5.QtWebEngineWidgets")

from PyQt5.QtCore import QObject, QTimer  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

import main as main_mod  # noqa: E402 — main guard 保护，import 安全


class _FakeWebEngineView(QObject):
    """QWebEngineView 桩：不拉真实 Chromium，构造计数可断言"""

    constructed = []

    def __init__(self, parent=None):
        super().__init__(parent)
        _FakeWebEngineView.constructed.append(self)

    def setHtml(self, *a, **k):
        pass

    def hide(self):
        pass

    def deleteLater(self):
        pass


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def fake_view_cls(monkeypatch, qapp):
    """把 main 内函数 import 的 QWebEngineView 换成桩，并接管 allWidgets"""
    _FakeWebEngineView.constructed.clear()
    monkeypatch.setattr(
        "PyQt5.QtWebEngineWidgets.QWebEngineView", _FakeWebEngineView
    )
    yield _FakeWebEngineView
    # 清场：allWidgets 还原由 monkeypatch 自动处理


def _patch_allwidgets(monkeypatch, widgets):
    monkeypatch.setattr(QApplication, "allWidgets", staticmethod(lambda: widgets))


def test_real_render_first_skips_preheat(fake_view_cls, monkeypatch, qapp):
    """互斥：真实渲染先行（WebEngineView 实例在册）→ 预热构造零调用"""
    real_view = _FakeWebEngineView()  # 模拟欢迎卡/首条消息已创建的视图
    baseline = len(_FakeWebEngineView.constructed)  # real_view 自身计入基线
    _patch_allwidgets(monkeypatch, [real_view])

    app = SimpleNamespace(_preheat_webengine=None)
    main_mod._maybe_create_webengine_preheat(app)

    assert len(_FakeWebEngineView.constructed) == baseline, "真实渲染已发生时预热必须跳过（零新增构造）"
    assert app._preheat_webengine is None, "跳过时不得挂载预热引用"


def test_idle_creates_preheat_and_schedules_release(fake_view_cls, monkeypatch, qapp):
    """保底：无真实渲染 → 预热创建 + 引用挂载 + 5s 释放排程"""
    _patch_allwidgets(monkeypatch, [])
    scheduled = []
    monkeypatch.setattr(QTimer, "singleShot", staticmethod(lambda ms, cb: scheduled.append((ms, cb))))

    app = SimpleNamespace(_preheat_webengine=None)
    main_mod._maybe_create_webengine_preheat(app)

    assert len(_FakeWebEngineView.constructed) == 1, "空闲时必须创建预热视图"
    assert app._preheat_webengine is _FakeWebEngineView.constructed[0], "引用必须挂载（防 GC）"
    release = [(ms, cb) for ms, cb in scheduled if ms == 5000]
    assert len(release) == 1, "5s 释放必须排程"


def test_release_destroys_view_and_clears_ref(fake_view_cls, monkeypatch, qapp):
    """释放语义：回调销毁视图并清空 app 引用"""
    _patch_allwidgets(monkeypatch, [])
    captured = {}

    def _fake_singleShot(ms, cb):
        captured[ms] = cb

    monkeypatch.setattr(QTimer, "singleShot", staticmethod(_fake_singleShot))
    destroyed = []
    real_ctor = _FakeWebEngineView.__init__

    def _tracking_ctor(self, parent=None):
        real_ctor(self, parent)

    # 捕获 deleteLater（桩基类 QObject.deleteLater 不可直接观测，用标志模拟）
    class _TrackView(_FakeWebEngineView):
        def deleteLater(self):
            destroyed.append(1)

    monkeypatch.setattr(
        "PyQt5.QtWebEngineWidgets.QWebEngineView", _TrackView
    )
    app = SimpleNamespace(_preheat_webengine=None)
    main_mod._maybe_create_webengine_preheat(app)
    release_cb = captured[5000]
    assert callable(release_cb)

    release_cb()
    assert destroyed, "释放回调必须销毁预热视图"
    assert app._preheat_webengine is None, "释放后引用必须清空"


def test_deferred_startup_uses_1500ms_deferral():
    """AST 守卫：_deferred_startup 必须经 singleShot(1500) 推迟（防回退启动即拉）"""
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")
    assert "singleShot(1500, lambda: _maybe_create_webengine_preheat(app))" in src, (
        "预热必须经 1.5s 缓拉接线"
    )
    # 旧形态守卫：_deferred_startup 内不得再出现立即构造
    assert "QWebEngineView()\n            _preheat_view.setHtml" not in src, (
        "_deferred_startup 内不得残留启动即拉的立即构造"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
