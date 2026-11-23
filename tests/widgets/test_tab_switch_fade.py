# -*- coding: utf-8 -*-
"""#A4 页签切换淡出回归：过渡触发/同页豁免/程序切换豁免/收敛清理（环境无关断言）。"""
import time

from PyQt5.QtWidgets import QLabel, QWidget

import app.utils.motion as motion
from app.widgets.deferred_paint_stack import DeferredPaintStack


def _spy_fade(monkeypatch):
    calls = []
    orig = motion.fade_widget

    def spy(widget, target, **kw):
        ret = orig(widget, target, **kw)
        calls.append((widget, target, ret))
        return ret

    monkeypatch.setattr(motion, "fade_widget", spy)
    return calls


def _make_stack(qapp, pages=3):
    stack = DeferredPaintStack()
    host = QWidget()
    host.resize(500, 400)
    stack.setParent(host)
    stack.setGeometry(0, 0, 500, 400)
    for i in range(pages):
        w = QWidget()
        stack.addWidget(w)
    host.show()
    qapp.processEvents()
    return host, stack


def test_faded_switch_triggers_fade(qapp, monkeypatch):
    calls = _spy_fade(monkeypatch)
    host, stack = _make_stack(qapp)
    stack.setCurrentWidget(stack.widget(0))
    qapp.processEvents()
    stack.set_current_widget_faded(stack.widget(1))
    assert stack.currentWidget() is stack.widget(1), "切换必须落位目标页"
    assert len(calls) == 1, "必须触发一次淡出"
    widget, target, ret = calls[0]
    assert isinstance(widget, QLabel) and target == 0.0, "fade 对象=旧页快照层，方向=淡出"
    if ret:
        assert not stack._fade_overlay.isHidden(), "动画态遮罩层驻留"
    else:
        # 直通态（系统减少动态效果）：遮罩层同步清理
        assert getattr(stack, "_fade_overlay", None) is None, "直通态遮罩层同步清理"
    host.close()


def test_same_page_no_fade(qapp, monkeypatch):
    calls = _spy_fade(monkeypatch)
    host, stack = _make_stack(qapp)
    stack.setCurrentWidget(stack.widget(0))
    qapp.processEvents()
    stack.set_current_widget_faded(stack.widget(0))
    assert calls == [], "同页切换不触发淡出"
    assert stack.currentWidget() is stack.widget(0)
    host.close()


def test_plain_set_current_widget_exempt(qapp, monkeypatch):
    """程序化/启动恢复路径走原 setCurrentWidget，天然豁免动画"""
    calls = _spy_fade(monkeypatch)
    host, stack = _make_stack(qapp)
    stack.setCurrentWidget(stack.widget(0))
    stack.setCurrentWidget(stack.widget(2))
    stack.setCurrentIndex(1)
    assert calls == [], "原生切换路径不触发淡出（豁免）"
    assert stack.currentWidget() is stack.widget(1)
    host.close()


def test_overlay_converges_clean(qapp, monkeypatch):
    """收敛断言：淡出结束遮罩层销毁记账清空（动画态等待/直通态立即）"""
    calls = _spy_fade(monkeypatch)
    host, stack = _make_stack(qapp)
    stack.setCurrentWidget(stack.widget(0))
    qapp.processEvents()
    stack.set_current_widget_faded(stack.widget(1))
    overlay = calls[0][0] if calls else None
    end = time.perf_counter() + 2.0
    while time.perf_counter() < end and getattr(stack, "_fade_overlay", None) is not None:
        qapp.processEvents()
        time.sleep(0.02)
    assert getattr(stack, "_fade_overlay", None) is None, "淡出后记账必须清空"
    if overlay is not None:
        try:
            assert overlay.graphicsEffect() is None, "effect 必须移除"
        except RuntimeError:
            pass  # 直通态 deleteLater 后 C++ 已删，等价清理完成
    host.close()


def test_fade_failure_falls_back_to_target(qapp, monkeypatch):
    """fade 异常时兜底直达目标页（切换功能不受影响）"""
    def boom(widget, target, **kw):
        raise RuntimeError("inject")

    monkeypatch.setattr(motion, "fade_widget", boom)
    host, stack = _make_stack(qapp)
    stack.setCurrentWidget(stack.widget(0))
    qapp.processEvents()
    stack.set_current_widget_faded(stack.widget(1))
    assert stack.currentWidget() is stack.widget(1), "过渡失败必须兜底落位目标页"
    host.close()
