# -*- coding: utf-8 -*-
"""批1 懒创建回归门：system_cards 两卡（tool_control / question）懒化生效验证。

T8 清单验证门：
① build 后两属性均 None（懒化生效）
② _ensure_tool_control_card 后非 None 且 controller 已绑定
③ _toggle_tool_control_card 未 ensure 时调用不抛异常（入口补 ensure）
④ SystemCardsModule.build 稳态构建分段 ≤8ms（原两卡同步构造 ~60ms 移出）
"""

import time
import types

import pytest
from PyQt5.QtCore import QObject, pyqtSignal
from PyQt5.QtWidgets import QWidget

from app.widgets.ui_composition import compose

pytest.importorskip("PyQt5.QtWidgets")


class _StubCardManager:
    def register_card(self, *a, **k):
        pass

    def hide_card(self, *a, **k):
        pass

    def show_card(self, *a, **k):
        pass

    def toggle_card(self, *a, **k):
        pass

    def is_card_visible(self, *a, **k):
        return False

    def add_card(self, *a, **k):
        pass

    def on_card_shown(self, *a, **k):
        pass

    def on_card_hidden(self, *a, **k):
        pass


class _StubContainer:
    def add_card(self, *a, **k):
        pass


class _StubController(QObject):
    """ToolPermissionController 信号面 stub（ensure 的 set_controller 会连 4 个信号）"""

    togglesChanged = pyqtSignal(dict)
    behaviorChanged = pyqtSignal(str)
    policiesChanged = pyqtSignal(dict)
    activeAgentChanged = pyqtSignal(str)

    def get_toggles(self):
        return {}

    def get_behavior(self):
        return "deny"

    def get_active_tool_behavior_map(self):
        return {}

    def get_tool_policies(self):
        return {}

    def get_active_agent_name(self):
        return ""

    def get_user_behavior(self):
        return "deny"

    def get_user_tool_policies(self):
        return {}


def _qconfig_alive() -> bool:
    """qfluentwidgets 全局 qconfig 存活检测。

    同进程先跑重 WebEngine 测试（tests/perf）时 qconfig 的 C++ 对象会被
    Qt 清理（qfluentwidgets 全局单例生命周期限制，非业务回归）；此时
    qfw 组件构造类断言无法执行，显式跳过并注明。
    """
    try:
        from qfluentwidgets import qconfig

        from PyQt5 import sip

        return not sip.isdeleted(qconfig)
    except Exception:
        return False


class _Host(QWidget):
    """最小宿主 stub：build + ensure 方法所需的属性/回调面"""

    def __init__(self):
        super().__init__()
        self._window_id = "lazy_test_window"
        self._system_card_ids = []
        self._bottom_card_container = _StubContainer()
        self._card_manager = _StubCardManager()
        self._tool_permission_controller = None
        # 懒创建占位（与改造后 system_cards_module 的 None 占位一致）
        self._tool_control_card = None
        self._question_floating_widget = None

    def _register_cards_to_manager(self):
        pass

    def _restore_after_system_close(self):
        pass

    def _refresh_tool_toggle_btn(self):
        pass

    def _init_builtin_commands(self, *a, **k):
        pass

    def _on_question_answered(self, *a, **k):
        pass

    def _on_question_cancelled(self, *a, **k):
        pass

    def _on_question_preview_requested(self, *a, **k):
        pass

    def _on_system_card_opened(self, *a, **k):
        pass

    def _on_system_card_closed(self, *a, **k):
        pass


def _compose_host():
    from app.widgets.modules.system_cards_module import SystemCardsModule

    host = _Host()
    compose(host, ["system_cards"])
    return host


def test_lazy_not_built_after_compose(fresh_registry, qapp):
    """① build 后两卡属性均 None：构造期不再同步创建卡片"""
    from app.widgets.modules.system_cards_module import SystemCardsModule

    fresh_registry.register_ui_module("system_cards", SystemCardsModule, plugin_name="system")
    host = _compose_host()
    assert host._tool_control_card is None, "tool_control 卡在 build 期被同步创建（懒化未生效）"
    assert host._question_floating_widget is None, "question 卡在 build 期被同步创建（懒化未生效）"


def test_ensure_tool_control_card_binds_controller(fresh_registry, qapp):
    """② ensure 后卡非 None 且 controller 已绑定、注册一次"""
    from app.main_widget import OpenAIChatToolWindow
    from app.widgets.modules.system_cards_module import SystemCardsModule

    fresh_registry.register_ui_module("system_cards", SystemCardsModule, plugin_name="system")
    host = _Host()
    ctrl = _StubController()
    host._tool_permission_controller = ctrl

    if not _qconfig_alive():
        pytest.skip("qconfig 已被同进程重 WebEngine 测试删除（测试基建限制，非回归）")
    OpenAIChatToolWindow._ensure_tool_control_card(host)
    assert host._tool_control_card is not None
    assert host._tool_control_card._controller is ctrl

    # 幂等：二次 ensure 不重建、不重复注册
    card = host._tool_control_card
    OpenAIChatToolWindow._ensure_tool_control_card(host)
    assert host._tool_control_card is card


def test_toggle_without_ensure_no_throw(fresh_registry, qapp):
    """③ 卡未创建时调 _toggle_tool_control_card：入口 ensure 兜底，不抛异常"""
    from app.main_widget import OpenAIChatToolWindow
    from app.widgets.modules.system_cards_module import SystemCardsModule

    fresh_registry.register_ui_module("system_cards", SystemCardsModule, plugin_name="system")
    host = _Host()
    host._tool_permission_controller = _StubController()

    assert host._tool_control_card is None
    if not _qconfig_alive():
        pytest.skip("qconfig 已被同进程重 WebEngine 测试删除（测试基建限制，非回归）")
    # unbound 调用真实方法：ensure 与 toggle 同在 OpenAIChatToolWindow 类，
    # stub host 需把 ensure 绑定到实例（模拟真实窗口的方法解析）
    host._ensure_tool_control_card = types.MethodType(OpenAIChatToolWindow._ensure_tool_control_card, host)
    OpenAIChatToolWindow._toggle_tool_control_card(host)  # 不应抛 AttributeError
    assert host._tool_control_card is not None, "toggle 后卡片仍未创建（ensure 兜底缺失）"


def test_build_time_within_budget(fresh_registry, qapp):
    """④ build 稳态构建分段 ≤8ms（两卡构造移出后只剩占位与回调注册）"""
    from app.widgets.modules.system_cards_module import SystemCardsModule

    fresh_registry.register_ui_module("system_cards", SystemCardsModule, plugin_name="system")
    host = _Host()
    t0 = time.perf_counter()
    compose(host, ["system_cards"])
    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert elapsed_ms <= 8.0, f"SystemCardsModule.build 耗时 {elapsed_ms:.1f}ms > 8ms（同步构造回归？）"
