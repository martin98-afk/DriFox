# -*- coding: utf-8 -*-
"""DeferredPaintStack 切页首绘合并（#25）行为测试。

契约：切换期间 updates 冻结、下一拍恢复、每次切换恰好一次恢复调度；
env 关闭走原生路径；开/关两态像素等价。
"""

from __future__ import annotations

import sys

import pytest
from PyQt5.QtWidgets import QApplication, QLabel, QWidget

import app.widgets.deferred_paint_stack as dps
from app.widgets.deferred_paint_stack import DeferredPaintStack


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication(sys.argv)


def _make_stack(n: int = 3) -> DeferredPaintStack:
    stack = DeferredPaintStack()
    for i in range(n):
        page = QWidget()
        lay = QLabel(f"page-{i}", page)
        lay.setStyleSheet(f"background: rgb({40 * i},120,180); color: white;")
        stack.addWidget(page)
    stack.resize(200, 100)
    return stack


def _drain_events(app):
    """排干事件循环：singleShot(0) 的恢复任务在此触发。"""
    for _ in range(5):
        app.processEvents()


def test_switch_freezes_updates_then_restores(qapp):
    stack = _make_stack()
    stack.setCurrentIndex(1)
    assert stack.currentIndex() == 1
    assert not stack.updatesEnabled()  # 切换后冻结，首绘推迟
    _drain_events(qapp)
    assert stack.updatesEnabled()  # 下一拍恢复
    assert stack.currentIndex() == 1  # 切换语义不受影响


def test_one_flush_per_switch(qapp):
    stack = _make_stack()
    calls = []
    orig = stack._flush_deferred_paint
    stack._flush_deferred_paint = lambda: (calls.append(1), orig())
    stack.setCurrentWidget(stack.widget(1))
    stack.setCurrentWidget(stack.widget(2))
    _drain_events(qapp)
    assert len(calls) == 2  # 每次切换恰好一次恢复调度
    assert stack.updatesEnabled()


def test_env_off_native_path(qapp, monkeypatch):
    monkeypatch.setattr(dps, "DEFERRED_PAINT_ENABLED", False)
    stack = _make_stack()
    stack.setCurrentIndex(1)
    assert stack.currentIndex() == 1  # 原生路径立即生效
    assert stack.updatesEnabled()  # 从未冻结


def test_idempotent_rapid_switch(qapp):
    """半途再切：冻结幂等，两次 singleShot 恢复无副作用。"""
    stack = _make_stack()
    stack.setCurrentIndex(1)
    stack.setCurrentIndex(2)  # 仍处冻结期再切
    _drain_events(qapp)
    assert stack.updatesEnabled()
    assert stack.currentIndex() == 2


def test_pixel_equivalence_on_off(qapp, monkeypatch):
    """开/关两态切页完成后 grab() 逐字节一致（仅时机不同，产物等价）。"""
    pixmaps = {}
    for tag, enabled in (("off", False), ("on", True)):
        monkeypatch.setattr(dps, "DEFERRED_PAINT_ENABLED", enabled)
        stack = _make_stack()
        stack.setCurrentIndex(1)
        _drain_events(qapp)
        assert stack.updatesEnabled()
        pixmaps[tag] = stack.grab().toImage()
    assert pixmaps["on"] == pixmaps["off"]  # QImage == 逐像素比较
