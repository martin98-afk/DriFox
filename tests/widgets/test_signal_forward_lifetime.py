# -*- coding: utf-8 -*-
"""信号转发连接生命周期回归（2026-09-18 代码框按钮闪退）。

背景：``src.sig.connect(dst.sig.emit)``（bound ``.emit`` 转发）产生的连接
不绑定 dst 的 C++ 生命周期——dst 析构后连接残留；再次 emit 时 Qt 调用
已析构对象的转发槽 → ``Qt5Core!QObject::signalsBlocked`` AV READ 0x0
（实机：代码框右上按钮点击闪退，message_card.py 代码块动作链
page → viewer → card 三跳全用此写法）。且 ``disconnect(bound.emit)``
永远抛 TypeError（每次访问 ``.emit`` 是新对象，无法匹配），导致
``_disconnect_viewer_signals`` 静默失效。

修复约定：跨对象转发一律 signal-to-signal 直连 ``src.sig.connect(dst.sig)``
—— Qt 记录 receiver QObject，dst 析构自动断连；``disconnect(dst.sig)`` 亦可。
"""

import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QEventLoop, QObject, QTimer, pyqtSignal  # noqa: E402


class _Node(QObject):
    """三层链节点替身：sig 签名与 message_card 的 codeActionRequested 一致。"""

    sig = pyqtSignal(str, str)


def _pump_once():
    loop = QEventLoop()
    QTimer.singleShot(0, loop.quit)
    loop.exec_()


def _wire(style):
    """按实机拓扑搭三层链 page → viewer → card，返回 (page, viewer, card, got)。"""
    page, viewer, card = _Node(), _Node(), _Node()
    got = []
    card.sig.connect(lambda a, b: got.append((a, b)))
    if style == "direct":
        page.sig.connect(viewer.sig)
        viewer.sig.connect(card.sig)
    else:  # forward：旧行为（bound .emit 转发）
        page.sig.connect(viewer.sig.emit)
        viewer.sig.connect(card.sig.emit)
    return page, viewer, card, got


def test_direct_forwarding_delivers(qapp):
    """直连全活：转发功能等价（emit 正常命中下游）。"""
    page, _viewer, _card, got = _wire("direct")
    page.sig.emit("code", "copy")
    assert got == [("code", "copy")]


def test_direct_auto_disconnects_on_receiver_destroy(qapp):
    """直连：receiver 销毁（deleteLater 真实路径）→ 连接自动清理。

    回归断言：若有人把连接改回 ``.emit`` 转发，此处 receivers() 不会归零，
    且后续 emit 会调用已析构对象 → 进程级 AV（测试进程直接死）。
    """
    page, viewer, _card, got = _wire("direct")
    viewer.deleteLater()
    _pump_once()  # DeferredDelete 生效，viewer C++ 析构

    assert page.receivers(page.sig) == 0, "receiver 销毁后连接未自动断开"
    page.sig.emit("code", "copy")
    assert got == []


def test_direct_disconnect_works(qapp):
    """直连可用信号对象 disconnect（对应 _disconnect_viewer_signals）。"""
    page, viewer, card, got = _wire("direct")
    viewer.sig.disconnect(card.sig)
    page.sig.emit("code", "copy")
    assert got == []


def test_emit_forward_connection_lingers(qapp):
    """文档性对照（锁定 PyQt 行为认知）：bound ``.emit`` 转发连接在 receiver
    销毁后**残留**——这正是必须改直连的原因。

    注意：本测试绝不 emit（残留连接会 AV 杀死测试进程），仅断言连接残留。
    若未来 PyQt 修正了该行为（连接自动断开），可删除本测试。
    """
    page, viewer, _card, _got = _wire("forward")
    viewer.deleteLater()
    _pump_once()

    # PyQt 行为变化（bound.emit 转发随 receiver 销毁自动断开）时本对照测试可删
    assert page.receivers(page.sig) >= 1, "bound.emit 转发连接仍残留（预期行为）"
