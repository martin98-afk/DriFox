# -*- coding: utf-8 -*-
"""回归：system-timer 清桥必须先精确断开 backend→bridge 订阅连接

背景（2026-11-08 关任意 tab 必现崩溃）：_ensure_subscriptions 的清桥分支此前
只 bridge.event.disconnect() + deleteLater()，未断开 _connections 中 backend
5 个信号到桥的订阅。悬挂连接留到桥析构批次清理，触发 C 层异常
「TypeError: native Qt signal is not callable」（excepthook 拿不到 traceback，
复现链：tests/debug/repro_close_tab_full_boot.py）。

本测试锁两个行为：
1. 建桥后 backend emit 能穿透到 scheduler._on_event（订阅生效）
2. 窗口消失清桥后 emit 不再穿透 + _connections 同步清空（悬挂连接已断）

修复前本测试第 2 步红：emit 仍穿透（连接悬挂）。
"""
import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PyQt5.QtCore import QEventLoop, QObject, QTimer, pyqtSignal

pytest.importorskip("PyQt5.QtWidgets")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parents[2]
_UI_MODULE = "ui_plugin_system_timer"

def _drain(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec_()

def _load_modules():
    """按 ui loader 同款方式加载 ui/__init__.py 与 scheduler.py（同模块名互认）"""
    p = _ROOT / "plugins" / "system-timer" / "ui" / "__init__.py"
    for name in list(sys.modules):
        if name == _UI_MODULE or name.startswith(_UI_MODULE + "."):
            del sys.modules[name]
    spec = importlib.util.spec_from_file_location(_UI_MODULE, p)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_UI_MODULE] = mod
    spec.loader.exec_module(mod)

    sub_p = _ROOT / "plugins" / "system-timer" / "ui" / "scheduler.py"
    sub_spec = importlib.util.spec_from_file_location(_UI_MODULE + ".scheduler", sub_p)
    sub = importlib.util.module_from_spec(sub_spec)
    sys.modules[_UI_MODULE + ".scheduler"] = sub
    sub_spec.loader.exec_module(sub)
    return mod, sub

class _FakeBackend(QObject):
    """形状对齐 ChatBackend 被 timer 订阅的 5 个信号"""

    stream_finished = pyqtSignal(str)
    stream_started = pyqtSignal()
    messages_updated = pyqtSignal()
    error_occurred = pyqtSignal(str)
    session_changed = pyqtSignal(str)

class _FakeRegistry:
    def __init__(self):
        self._window_main_widgets: dict = {}

def test_bridge_cleanup_disconnects_backend_signals(qapp, monkeypatch):
    ui_mod, sub_mod = _load_modules()
    TimerScheduler = sub_mod.TimerScheduler

    calls = []
    monkeypatch.setattr(
        TimerScheduler, "_on_event", lambda self, *a: calls.append(a[0] if a else "")
    )

    s = TimerScheduler()
    backend = _FakeBackend()
    fake_widget = SimpleNamespace(backend=backend)
    reg = _FakeRegistry()
    reg._window_main_widgets = {"win_t": fake_widget}

    import app.plugins.registries.ui_plugin_registry as ui_registry_mod

    monkeypatch.setattr(
        ui_registry_mod.UIPluginRegistry,
        "get_instance",
        classmethod(lambda cls: reg),
    )

    # 1. 建桥：订阅生效，backend emit 穿透到 _on_event
    s._ensure_subscriptions()
    assert "win_t" in s._bridges, "窗口在表时应建桥"
    assert len(s._connections) == 5, "应订阅 backend 的 5 个对话流事件"
    backend.stream_finished.emit("r1")
    _drain(100)
    assert calls and calls[0] == "stream_finished", "订阅后 emit 应穿透到调度器"

    # 2. 窗口消失 → 清桥：订阅同步断开
    reg._window_main_widgets = {}
    s._ensure_subscriptions()
    _drain(100)

    assert s._bridges == {}, "窗口消失后桥应被释放"
    assert s._connections == [], "清桥必须同步清 _connections（悬挂连接根因）"
    backend.stream_finished.emit("r2")
    _drain(100)
    assert len(calls) == 1, "清桥后 emit 不得再穿透（悬挂连接未断开即回归）"
