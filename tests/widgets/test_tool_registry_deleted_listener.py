# -*- coding: utf-8 -*-
"""ToolRegistry 监听者 deleted wrapper 硬判回归测试（工单 F14b）

背景：PyQt 已析构 wrapper 读属性不抛异常——QWidget 子类经 sip.delete 析构后
weakref 仍返回空壳 wrapper，`func.__get__(obj)` 照常复活 bound method，
notify 回调 RuntimeError 或空转，「监听者泄漏」防护失效。

修复：_from_listener_ref tuple 分支 sip.isdeleted 硬判，deleted 返回 None
→ 复用 _dispatch_notify 现有 None 分支清理。
"""

import gc
import sys

import shiboken6 as sip
from PySide6.QtWidgets import QApplication, QWidget


def _ensure_qapp():
    return QApplication.instance() or QApplication(sys.argv)


from loguru import logger

from app.tools.registry import ToolRegistry, _from_listener_ref, _to_listener_ref


class _WarnCapture:
    def __init__(self):
        self.msgs = []

    def __call__(self, message):
        self.msgs.append(str(message))

    def any_with(self, keyword):
        return any(keyword in m for m in self.msgs)


class _ListeningWidget(QWidget):
    """QWidget 子类：持有 bound method 供注册（bound method 才走 tuple 弱引用路径）"""

    def __init__(self):
        super().__init__()
        self.hits = []

    def on_version(self, _version):
        self.hits.append(_version)


def test_deleted_widget_no_callback_no_warning_and_cleanup():
    """①sip.delete 析构后 notify：无异常逃逸、无 warning、我的监听回落（单例含既有监听，用快照相对断言）"""
    _ensure_qapp()
    registry = ToolRegistry.get_instance()
    baseline = len(registry._listeners)
    w = _ListeningWidget()
    version_at_reg = registry._version
    registry.on_change(w.on_version)  # 立即回调一次 → hits == [version_at_reg]
    assert w.hits == [version_at_reg]
    assert len(registry._listeners) == baseline + 1

    sip.delete(w)
    registry._notify_change()  # 直接驱动分发，绕过版本挂起逻辑

    assert len(registry._listeners) == baseline
    assert w.hits == [version_at_reg]  # 空壳不得复活回调（hits 不追加）


def test_pure_python_listener_gc_cleanup_unregressed():
    """②纯 Python 监听对象 del+gc → None 分支跳过 + 清理（既有行为不回归）"""

    class _Pure:
        def __init__(self):
            self.hits = []

        def on_version(self, _v):
            self.hits.append(_v)

    _ensure_qapp()
    registry = ToolRegistry.get_instance()
    baseline = len(registry._listeners)
    p = _Pure()
    registry.on_change(p.on_version)
    assert len(registry._listeners) == baseline + 1

    del p
    gc.collect()
    registry._notify_change()

    assert len(registry._listeners) == baseline


def test_on_change_immediate_callback_warning_preserved():
    """③注册时回调抛异常普通函数 → on_change 立即回调 warning 保留（:605 行为）"""
    _ensure_qapp()
    registry = ToolRegistry.get_instance()
    capture = _WarnCapture()
    sink_id = logger.add(capture, level="WARNING")
    try:

        def _boom(_version):
            raise ValueError("boom")

        registry.on_change(_boom)
        assert capture.any_with("变更监听回调失败")
    finally:
        logger.remove(sink_id)
        registry.off_change(_boom)


def test_real_timeline_close_deletelater_notify_no_runtimeerror():
    """④真实时序 show→on_change→close→deleteLater→processEvents×2→notify 无 RuntimeError"""
    _ensure_qapp()
    app = _ensure_qapp()
    registry = ToolRegistry.get_instance()
    baseline = len(registry._listeners)
    w = _ListeningWidget()
    w.show()
    registry.on_change(w.on_version)
    w.close()
    w.deleteLater()
    app.processEvents()
    app.processEvents()
    # 实测（py3.14/PyQt5）：无事件循环环境下 DeferredDelete 不真正析构 C++ 对象，
    # isdeleted 仍 False → 回调合法执行。场景断言=无 RuntimeError 逃逸，监听合理保留；
    # 真析构路径的清理行为由场景①（sip.delete）锁定
    registry._notify_change()
    assert len(registry._listeners) == baseline + 1
    assert w.hits[-1] == registry._version


def test_off_change_idempotent():
    """⑤off_change 幂等：注销不存在监听静默（不误伤既有监听）"""
    _ensure_qapp()
    registry = ToolRegistry.get_instance()
    baseline = len(registry._listeners)
    w = _ListeningWidget()
    registry.off_change(w.on_version)  # 从未注册 → 静默
    assert len(registry._listeners) == baseline
    registry.on_change(w.on_version)
    registry.off_change(w.on_version)
    registry.off_change(w.on_version)  # 二次注销 → 静默
    assert len(registry._listeners) == baseline


def test_from_listener_ref_deleted_tuple_returns_none():
    """_from_listener_ref 单元级：deleted wrapper tuple → None；活对象照常解析"""
    _ensure_qapp()
    w = _ListeningWidget()
    ref = _to_listener_ref(w.on_version)
    assert _from_listener_ref(ref) is not None
    sip.delete(w)
    assert _from_listener_ref(ref) is None
