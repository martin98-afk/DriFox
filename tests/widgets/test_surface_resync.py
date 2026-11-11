# -*- coding: utf-8 -*-
"""CodeWebViewer 绘制几何自愈回归（macOS 内容被横向压成竖条）。

现象（2026-11-09 用户截图）：流式输出中 / 切 tab 回来，消息卡整块内容被压
进左侧约 1/6 宽的竖条，文字成 1-3px 竖线而行高正常，拖动窗口 resize 一次
即恢复。缩放比恒定 + 行高不变 → DOM 布局正确，错的是 QtWebEngine 的
render surface 与 widget 尺寸失配（隐藏期尺寸变化后合成面没跟上）。

Qt 5.15 没有公开 API 读该 surface 尺寸，只能让 view 尺寸真的变化一次来
触发重发：`_schedule_surface_resync` 在 showEvent 里把高度 -1 再复原，两拍
必须跨事件循环（否则被 Qt 合并成「尺寸没变」）。

本测试不创建 QWebEngineView（重且 offscreen 易 abort），用裸 QWidget 承
载方法验证调用序列与两个早退条件。
"""
import os
import sys
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from app.widgets.card_viewers import CodeWebViewer  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _spy(widget: QWidget) -> list:
    """记录 resize 调用并真正执行。"""
    calls: list = []
    orig = widget.resize
    widget.resize = lambda a, b: (calls.append((a, b)), orig(a, b))
    widget._restore_surface_geometry_after_resync = types.MethodType(
        CodeWebViewer._restore_surface_geometry_after_resync, widget
    )
    return calls


def test_resync_shrinks_then_restores_across_event_loop(qapp):
    w = QWidget()
    w.resize(880, 600)
    calls = _spy(w)

    CodeWebViewer._schedule_surface_resync(w)
    qapp.processEvents()

    assert calls == [(880, 599), (880, 600)], "必须两拍：先 -1 再复原"
    assert (w.width(), w.height()) == (880, 600), "复原后几何不得漂移"


def test_resync_skips_tiny_widget(qapp):
    w = QWidget()
    w.resize(4, 4)
    calls = _spy(w)

    CodeWebViewer._schedule_surface_resync(w)
    qapp.processEvents()

    assert calls == [], "尺寸过小（未完成布局）不得抖动"


def test_resync_can_be_disabled_by_env(qapp, monkeypatch):
    monkeypatch.setenv("DRIFOX_SURFACE_RESYNC", "0")
    w = QWidget()
    w.resize(880, 600)
    calls = _spy(w)

    CodeWebViewer._schedule_surface_resync(w)
    qapp.processEvents()

    assert calls == [], "DRIFOX_SURFACE_RESYNC=0 必须彻底关闭"
