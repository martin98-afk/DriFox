# -*- coding: utf-8 -*-
"""回归测试：拖动窗口期间 ensure_settings_popup 不得同步构建设置卡

背景（2026-09-01 [DRAG-POS] 日志定位）：启动后 5s 的 gitee 绑定提醒定时器
若恰逢用户正在拖动标题栏 → ensure_settings_popup 同步构造 LLMSettingsCard
→ 构造链（MCPListSettingCard._refresh 的 processEvents）干扰焦点/捕获
→ Windows 取消 SC_MOVE 模态循环 → 窗口被系统弹回拖动起点，
即用户报告的"拖完又跳回原位置"。

约束：any_window_dragging=True 时：
1. 本次调用不同步构建（LLMSettingsCard 不实例化）；
2. 安排延迟重试（拖动结束后构建成功，懒构建语义不丢）。
"""

import sys

import pytest
from PyQt5.QtCore import Qt

sys.modules  # noqa: B018  (仅确保 sys 导入)

QApplication_ShareOpenGL = Qt.AA_ShareOpenGLContexts  # noqa: F841


def _ensure_qapp():
    from PyQt5.QtWidgets import QApplication

    return QApplication.instance() or QApplication(sys.argv)


@pytest.fixture(autouse=True)
def _reset_singletons():
    """重置 CardManager 与 controller 单例，避免测试间污染"""
    from app.widgets.cards.card_manager import CardManager
    from app.widgets.cards import global_card_controller as gcc

    CardManager.reset_instance()
    gcc._controller = None
    yield
    CardManager.reset_instance()
    gcc._controller = None


class _SignalStub:
    """支持任意信号 connect 的替身（动态属性）"""

    def __init__(self):
        self._hook_manager = None

    def __getattr__(self, name):
        return self

    def __call__(self, *a, **kw):
        return None

    def connect(self, *a, **kw):
        pass

    def _refresh(self, **kw):
        pass


def _make_controller(monkeypatch, instances):
    """controller + 记录实例化的 FakeLLMSettingsCard"""
    from app.widgets.cards import global_card_controller as gcc

    class FakeLLMSettingsCard:
        def __init__(self, parent=None):
            instances.append(self)
            self.configChanged = _SignalStub()
            self.closed = _SignalStub()
            self.hookListCard = _SignalStub()
            self.llmProviderCard = _SignalStub()
            self.mcpListCard = _SignalStub()

        def setVisible(self, v):
            pass

    monkeypatch.setattr("app.widgets.cards.settings.llm_settings_card.LLMSettingsCard", FakeLLMSettingsCard)
    monkeypatch.setattr("app.core.hook_manager.HookManager", object)

    container = type("C", (), {"added": [], "add_card": lambda self, cid, w: self.added.append((cid, w))})()
    ctrl = gcc.GlobalCardController(object(), container)
    return ctrl, container


def test_dragging_blocks_sync_build_and_retries_later(qtbot, monkeypatch):
    """拖动中调用：不同步构建 + 拖动结束后延迟重试构建成功"""
    _ensure_qapp()
    from app.utils import window_drag_state as wds
    from app.widgets.cards.card_manager import CardManager

    CardManager.get_instance()  # 提前初始化，保证单例状态干净

    instances = []
    ctrl, container = _make_controller(monkeypatch, instances)

    # 模拟拖动中
    wds.any_window_dragging = True
    try:
        ctrl.ensure_settings_popup()
        assert instances == [], "拖动中不得同步构建设置卡（会把窗口弹回拖动起点）"
        assert container.added == []

        # 模拟拖动结束：800ms 重试到期时标志已复位 → 构建成功
        wds.any_window_dragging = False
        qtbot.wait(1200)  # > 守卫的 800ms 重试间隔

        assert len(instances) == 1, "拖动结束后延迟重试应完成懒构建"
        assert len(container.added) == 1
        assert container.added[0][0] == "settings"
    finally:
        wds.any_window_dragging = False


def test_not_dragging_builds_immediately(qtbot, monkeypatch):
    """非拖动态调用：行为与修复前一致（立即构建）"""
    _ensure_qapp()
    from app.utils import window_drag_state as wds

    assert wds.any_window_dragging is False

    instances = []
    ctrl, container = _make_controller(monkeypatch, instances)

    ctrl.ensure_settings_popup()

    assert len(instances) == 1
    assert len(container.added) == 1
