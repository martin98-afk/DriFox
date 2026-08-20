# -*- coding: utf-8 -*-
"""GlobalCardController.ensure_settings_popup 重入防护回归测试

背景（P024）：LLMSettingsCard 构造链中 HookListSettingCard._refresh()
调用 QCoreApplication.processEvents()，事件重入可能再次进入
ensure_settings_popup；而 `self._settings_popup = LLMSettingsCard(...)`
是先构造后赋值，重入时 `_settings_popup` 仍为 None → 递归构建多张设置卡，
全部 add_card 到同一容器 → 界面重叠显示（日志特征：CardManager
「settings 已注册，将被覆盖」连续出现）。
"""

import sys

import pytest

# 必须在创建 QApplication 前设置 Qt 属性
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


class _FakeContainer:
    """最小容器替身：记录 add_card 调用"""

    def __init__(self):
        self.added = []

    def add_card(self, card_id, card_widget):
        self.added.append((card_id, card_widget))


def _make_controller(monkeypatch, fake_llm_cls):
    """构造一个 GlobalCardController，并把 LLMSettingsCard / HookManager 替换为 fake"""
    from app.widgets.cards import global_card_controller as gcc
    from app.widgets.cards.card_manager import CardManager, GLOBAL_WINDOW_ID

    fake_tab = object()  # LLMSettingsCard 的 parent，仅透传
    container = _FakeContainer()

    monkeypatch.setattr("app.widgets.cards.settings.llm_settings_card.LLMSettingsCard", fake_llm_cls)
    monkeypatch.setattr("app.core.hook_manager.HookManager", object)

    ctrl = gcc.GlobalCardController(fake_tab, container)
    mgr = CardManager.get_instance()
    return ctrl, mgr, container


def test_ensure_settings_popup_no_duplicate_on_reentry(qtbot, monkeypatch):
    """重入（构造期间再次调用 ensure_settings_popup）只构建一张卡"""
    _ensure_qapp()
    from app.widgets.cards import global_card_controller as gcc
    from app.widgets.cards.card_manager import GLOBAL_WINDOW_ID

    instances = []

    class FakeLLMSettingsCard:
        """模拟 LLMSettingsCard：构造中触发事件重入（processEvents 效应）"""

        def __init__(self, parent=None):
            instances.append(self)
            self.parent = parent
            # 模拟 HookListSettingCard._refresh() 的 processEvents 重入：
            # 此时外层 ensure_settings_popup 尚未给 _settings_popup 赋值
            ctrl.ensure_settings_popup()
            # 子卡替身：构造后 controller 会访问这些属性并 .connect(...)
            self.configChanged = _SignalStub()
            self.closed = _SignalStub()
            self.hookListCard = _SignalStub()
            self.llmProviderCard = _SignalStub()
            self.mcpListCard = _SignalStub()

        def setVisible(self, v):
            pass

    class _SignalStub:
        """支持任意信号 connect 的替身（动态属性）"""

        def __init__(self):
            self._hook_manager = None

        def __getattr__(self, name):
            # 任意信号/方法名都返回自身（connect / _refresh / 其它均兼容）
            return self

        def __call__(self, *a, **kw):
            return None

        def connect(self, *a, **kw):
            pass

        def _refresh(self, **kw):
            pass

    ctrl, mgr, container = _make_controller(monkeypatch=monkeypatch, fake_llm_cls=FakeLLMSettingsCard)

    ctrl.ensure_settings_popup()

    # 只构建一张卡
    assert len(instances) == 1, f"重入导致构建了 {len(instances)} 张设置卡"
    # 容器只 add 一次
    assert len(container.added) == 1
    assert container.added[0][0] == "settings"
    assert container.added[0][1] is instances[0]
    # CardManager 注册的是同一实例
    win_data = mgr._window_data[GLOBAL_WINDOW_ID]
    assert (
        win_data["cards"][__import__("app.widgets.cards.card_manager", fromlist=["ContainerType"]).ContainerType.TOP][
            "settings"
        ]
        is instances[0]
    )


def test_ensure_settings_popup_reentry_after_build_is_noop(qtbot, monkeypatch):
    """构建完成后再次调用 ensure_settings_popup 不重建（守卫仍生效）"""
    _ensure_qapp()
    from app.widgets.cards import global_card_controller as gcc

    instances = []

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

    ctrl, mgr, container = _make_controller(monkeypatch=monkeypatch, fake_llm_cls=FakeLLMSettingsCard)

    ctrl.ensure_settings_popup()
    ctrl.ensure_settings_popup()  # 第二次调用应直接返回

    assert len(instances) == 1
    assert len(container.added) == 1
