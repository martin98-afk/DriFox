# -*- coding: utf-8 -*-
"""destroyed(QObject) 信号与 lambda 默认参数的坑（回归测试）。

背景（2026-11-01 tool_permission_controller.py 析构 AttributeError / 0xC0000409）：
- QObject.destroyed 的真实签名是 destroyed(QObject)，发射时**总是携带被销毁
  对象作实参**（无参 lambda 由 PyQt5 按 slot 参数个数截断，不传参）。
- `lambda _reg=reg, _slot=slot:` 这种带位置默认参数的 lambda 可接受 1~2 个
  位置实参 → _reg 被 destroyed 的实参（被销毁对象自身，wrapper 已退化成
  QObject）覆盖 → `_reg.off_change` 查找失败抛 AttributeError；析构路径里的
  未处理异常还会触发 0xC0000409 快速失败（有全局 exception hook 时降级为日志）。
- 正确写法：`lambda *_a, _reg=reg, _slot=slot:` —— *a 吞掉信号实参，
  keyword-only 默认参数不被覆盖。

⚠️ 测试**不得**直接运行「位置默认参数」的错误写法：它在 destroyed 发射时抛
异常且可能带崩测试进程（0xC0000409），这里用探针断言信号实参行为来锁机制。
"""

import pytest

pytest.importorskip("PySide6.QtCore")

from PySide6.QtCore import QCoreApplication, QObject  # noqa: E402
import shiboken6 as sip  # noqa: E402


@pytest.fixture()
def qcore_app():
    app = QCoreApplication.instance() or QCoreApplication([])
    yield app


class _FakeRegistry:
    def __init__(self):
        self.calls = []

    def off_change(self, slot):
        self.calls.append(slot)


def test_destroyed_signal_carries_destroyed_object(qcore_app):
    """destroyed 发射时携带被销毁对象作实参（坑的机制本体）。

    PyQt5 为实参新建裸 QObject wrapper（不与 Python 侧对象同一实例），
    且该 wrapper 已标记 deleted——这正是用户日志里
    "'QObject' object has no attribute 'off_change'" 的来源。
    """
    received = []
    obj = QObject()
    obj.destroyed.connect(lambda *a: received.append(a))
    sip.delete(obj)  # 强制立即 C++ 析构 → destroyed 同步发射
    assert len(received) == 1
    arg = received[0][0]
    # 发射时析构尚未完成：arg 是裸 QObject wrapper、未标记 deleted（所以
    # 属性查找报 AttributeError 而非 RuntimeError），但类型不再是子类
    assert isinstance(arg, QObject)
    assert type(arg) is QObject


def test_keyword_only_default_lambda_survives_destroyed(qcore_app):
    """修复写法 *_a + keyword-only 默认参数：off_change 收到正确 slot。"""
    reg = _FakeRegistry()
    slot = lambda: None  # noqa: E731
    obj = QObject()
    obj.destroyed.connect(
        lambda *_a, _reg=reg, _slot=slot: _reg.off_change(_slot)
    )
    sip.delete(obj)
    assert reg.calls == [slot]


def test_bare_lambda_still_works(qcore_app):
    """无参 lambda（代码库存量写法）：PyQt5 截断实参，行为不受影响。"""
    reg = _FakeRegistry()
    slot = lambda: None  # noqa: E731
    obj = QObject()
    obj.destroyed.connect(lambda: reg.off_change(slot))
    sip.delete(obj)
    assert reg.calls == [slot]
