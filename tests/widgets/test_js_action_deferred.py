# -*- coding: utf-8 -*-
"""族⑤（T43）回归：js console 动作 emit 必须延迟到事件循环下一拍。

背景：用户实机「点击 context-tag 瞬间崩溃」——javaScriptConsoleMessage 运行
在 Chromium 回调栈内，同步 emit 会立即进入 Qt 信号链；「取消长请求 + 429 +
大批次回收」窗口内某跳 receiver 已析构但连接未断 → 悬空调用 AV。
修复：动作类 emit 统一 ``QTimer.singleShot(0, ...)`` 延迟派发。
"""

import base64
import os
import sys
import urllib.parse
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QEventLoop, QObject, QTimer, pyqtSignal  # noqa: E402


class _StubViewer(QObject):
    """只实现 javaScriptConsoleMessage 触达面的替身（本组用例仅 context 链）。"""

    contextActionRequested = pyqtSignal(str, str)

    def __init__(self):
        super().__init__()
        self.calls = []
        self.contextActionRequested.connect(lambda a, b: self.calls.append((a, b)))

    def _handle_context_lost(self):
        pass


def _pump_once():
    loop = QEventLoop()
    QTimer.singleShot(0, loop.quit)
    loop.exec_()


def _run_console(viewer, message):
    """未绑定调用真实实现（self=替身；ConsoleMonitorPage 为回调入口类）。"""
    from PyQt5.QtWebEngineWidgets import QWebEnginePage

    from app.widgets.message_card import ConsoleMonitorPage

    ConsoleMonitorPage.javaScriptConsoleMessage(
        viewer,
        QWebEnginePage.JavaScriptConsoleMessageLevel.InfoLevel.value,
        message,
        1,
        "js",
    )


def test_context_action_emit_is_deferred(qapp):
    """context||| 动作：同步栈内不得 emit，事件循环下一拍才派发。"""
    viewer = _StubViewer()
    tag = urllib.parse.quote("推荐问题A")
    typ = urllib.parse.quote("rec")

    _run_console(viewer, f"pywebview_action:context|||{tag}|||{typ}")

    # 关键断言：同步栈（Chromium 回调栈等价物）内尚未 emit
    assert viewer.calls == [], "emit 仍发生在回调栈内（未延迟）"

    _pump_once()
    assert viewer.calls == [("推荐问题A", "rec")], f"延迟派发参数错误: {viewer.calls}"


def test_fence_prompt_emit_is_deferred(qapp):
    """fence_prompt（<ask> 同链路）：延迟派发且 base64 还原。"""
    viewer = _StubViewer()
    text = base64.b64encode("追问内容".encode("utf-8")).decode("utf-8")

    _run_console(viewer, f"pywebview_action:fence_prompt:{text}")

    assert viewer.calls == [], "同步栈内已 emit（未延迟）"

    _pump_once()
    assert viewer.calls == [("追问内容", "ask")], f"延迟派发参数错误: {viewer.calls}"


def test_deferred_emit_survives_sync_stage(qapp):
    """延迟派发独立于同步栈：同步阶段状态断裂不影响排队的一次性派发。"""
    viewer = _StubViewer()
    tag = urllib.parse.quote("t")

    _run_console(viewer, f"pywebview_action:context|||{tag}|||rec")

    # 模拟「同步栈断裂」场景：此刻再向 viewer 塞任何脏状态都不影响已排队任务
    viewer.calls.append(("noise", "noise"))
    viewer.calls.clear()

    _pump_once()
    assert viewer.calls == [("t", "rec")], f"延迟派发丢失或重复: {viewer.calls}"
