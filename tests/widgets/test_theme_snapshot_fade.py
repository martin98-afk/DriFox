# -*- coding: utf-8 -*-
"""#A3 主题切换快照淡出回归（环境无关版）。

用 fade_widget spy 的返回值判定运行在动画态（ret=True）还是直通终值态
（ret=False，系统「减少动态效果」开启时 fade_widget 同步置终值），两种
模式分别断言最终一致性，不依赖 Animations.motion_enabled 的进程缓存。
"""
import time

from PyQt5.QtWidgets import QLabel, QWidget

import app.utils.motion as motion
from app.main_widget import OpenAIChatToolWindow


class _Host(QWidget):
    """QWidget 宿主：动态绑定 OpenAIChatToolWindow 的快照方法（免构造全窗）"""

    _snapshot = OpenAIChatToolWindow._snapshot_theme_transition


def _spy_fade(monkeypatch):
    """包装 motion.fade_widget 记录调用与返回值"""
    calls = []
    orig = motion.fade_widget

    def spy(widget, target, **kw):
        ret = orig(widget, target, **kw)
        calls.append((widget, target, ret))
        return ret

    monkeypatch.setattr(motion, "fade_widget", spy)
    return calls


def test_snapshot_fades_overlay(qapp, monkeypatch):
    calls = _spy_fade(monkeypatch)
    h = _Host()
    h.resize(400, 300)
    h.show()
    qapp.processEvents()
    h._snapshot()
    assert len(calls) == 1, "切换必须触发一次 fade"
    widget, target, ret = calls[0]
    assert isinstance(widget, QLabel) and target == 0.0, "fade 对象=快照层，方向=淡出"
    overlay = getattr(h, "_theme_fade_overlay", None)
    if ret:
        # 动画态：遮罩层驻留等待淡出
        assert overlay is widget and not overlay.isHidden(), "动画态遮罩层须驻留可见"
        h.close()
    else:
        # 直通态（系统减少动态效果）：同步清理
        assert overlay is None, "直通态遮罩层应同步清理"
        h.close()
        return


def test_invisible_window_skips(qapp, monkeypatch):
    calls = _spy_fade(monkeypatch)
    h = _Host()  # 不 show
    h._snapshot()
    assert calls == [], "不可见窗不触发 fade"
    assert getattr(h, "_theme_fade_overlay", None) is None, "不可见窗不建遮罩层"


def test_reentry_keeps_single_overlay(qapp, monkeypatch):
    """二次切换：旧层销毁只留最新一张，最终收敛到无残留"""
    calls = _spy_fade(monkeypatch)
    h = _Host()
    h.resize(400, 300)
    h.show()
    qapp.processEvents()
    h._snapshot()
    h._snapshot()
    assert len(calls) == 2, "两次切换各触发一次 fade"
    first, second = calls[0][0], calls[1][0]
    if calls[1][2]:  # 动画态：旧层销毁、新层驻留
        assert not second.isHidden(), "新层须驻留"
        assert first.isHidden() or not first.isVisible(), "旧层须已销毁/隐藏"
    else:  # 直通态：两次都同步清理
        assert first.isHidden() and second.isHidden()
    h.close()


def test_overlay_fully_cleaned(qapp, monkeypatch):
    """收敛断言：淡出结束后记账清空 + effect 移除（两种模式最终一致）"""
    calls = _spy_fade(monkeypatch)
    h = _Host()
    h.resize(400, 300)
    h.show()
    qapp.processEvents()
    h._snapshot()
    overlay = calls[0][0] if calls else None
    end = time.perf_counter() + 2.0
    while time.perf_counter() < end and getattr(h, "_theme_fade_overlay", None) is not None:
        qapp.processEvents()
        time.sleep(0.02)
    assert getattr(h, "_theme_fade_overlay", None) is None, "淡出后记账必须清空"
    if overlay is not None:
        try:
            assert overlay.graphicsEffect() is None, "effect 必须移除"
        except RuntimeError:
            pass  # 直通态 deleteLater 后 C++ 已删，等价于清理完成
    h.close()
