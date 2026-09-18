# -*- coding: utf-8 -*-
"""输入框工具计数刷新回归门（2026-09-13）

历史 bug：计数刷新寄生在懒创建的工具控制卡上——
① 未开过卡片的窗口，插件装/卸/热重载（registry 变更）后计数停在启动值；
② agent 激活的 togglesChanged 先于卡片转发连接而丢失。

修复后 main_widget 直连两个数据源（registry.on_change + controller 信号）。
本文件验证：不创建工具控制卡时，两条链路都能刷新计数标签。
（同步自 pyside6 分支 42d56151，PyQt5 适配）

⚠️ 本文件严禁在模块顶层 import app.*：pytest 收集期 QApplication 尚未创建，
   此时拉起 app.main_widget 全量导入链会让 qfw 单例（qconfig/Settings 桥）
   的 C++ 对象在后续测试中被连带析构（PyQt5 所有权语义；PySide6 无此问题），
   污染同进程其他 setTheme 测试。app 系 import 全部推迟到 fixture 内，
   经 conftest.qapp 保证 QApplication 先行。
"""

import types

import pytest
from PyQt5.QtCore import QTimer, pyqtSignal
from PyQt5.QtWidgets import QLabel, QPushButton, QWidget

pytest.importorskip("PyQt5.QtWidgets")


class _Host(QWidget):
    """最小宿主 stub：与 OpenAIChatToolWindow 相同的信号面 + 计数控件"""

    _tool_registry_changed = pyqtSignal(int)

    def __init__(self):
        super().__init__()
        self._tool_count_label = QLabel("0/0")
        self._tool_toggle_btn = QWidget()
        self._tool_restore_btn = QPushButton()
        self._tool_permission_controller = None

    def wire(self, tool_registry):
        """复刻 main_widget.__init__ 的接线（数据源直连 + 0ms 单发去抖）。

        必须在方法绑定（MethodType）之后调用。
        """
        self._tool_count_dirty_timer = QTimer(self)
        self._tool_count_dirty_timer.setSingleShot(True)
        self._tool_count_dirty_timer.timeout.connect(self._refresh_tool_toggle_btn)
        self._tool_registry_changed.connect(self._on_tool_count_dirty)
        self._tool_permission_controller.togglesChanged.connect(lambda _t: self._on_tool_count_dirty())
        self._tool_permission_controller.activeAgentChanged.connect(lambda _n: self._on_tool_count_dirty())
        tool_registry.on_change(self._on_tool_registry_changed)


@pytest.fixture()
def host(qapp):
    """依赖 conftest.qapp：QApplication 先于 app 包 import 存在。"""
    from app.core.tools.tool_permission_controller import ToolPermissionController
    from app.main_widget import OpenAIChatToolWindow
    from app.tools.registry import ToolRegistry

    app = qapp
    ToolRegistry.reset_instance()
    h = _Host()
    # 先绑定 main_widget 真实方法（unbound → 实例），再接线
    h._on_tool_registry_changed = types.MethodType(OpenAIChatToolWindow._on_tool_registry_changed, h)
    h._on_tool_count_dirty = types.MethodType(OpenAIChatToolWindow._on_tool_count_dirty, h)
    h._refresh_tool_toggle_btn = types.MethodType(OpenAIChatToolWindow._refresh_tool_toggle_btn, h)
    h._tool_permission_controller = ToolPermissionController(h)
    h.wire(ToolRegistry.get_instance())
    yield h
    h.deleteLater()
    ToolRegistry.reset_instance()
    app.processEvents()


def _register_probe(reg, name, danger="safe"):
    reg.register(
        name,
        {"type": "function", "function": {"name": name}},
        impl=lambda **kw: "ok",
        danger=danger,
        icon="read",
        cn_name="计数探针",
        group="测试组",
        description="回归测试探针工具",
        aliases=[],
        source="plugin:test",
    )


def _settle(app):
    """两轮事件分发：queued/direct 信号 → 0ms 单发 timer → 刷新执行"""
    app.processEvents()
    app.processEvents()


def test_registry_change_refreshes_count_without_tool_card(host):
    """未开工具卡：registry 注册新工具后计数刷新（历史 bug ① 回归门）"""
    from app.tools.registry import ToolRegistry

    from app.tools.registry import ToolRegistry

    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance()
    reg = ToolRegistry.get_instance()
    name = "test_count_refresh_probe_safe"
    before = host._tool_count_label.text()

    _register_probe(reg, name, danger="safe")
    _settle(app)
    after_add = host._tool_count_label.text()
    assert after_add != before, f"注册工具后计数未刷新: {before} → {after_add}"

    reg.unregister(name)
    _settle(app)
    assert host._tool_count_label.text() == before, "注销工具后计数未回退"


def test_agent_apply_refreshes_count_without_tool_card(host):
    """未开工具卡：agent 权限注入后计数刷新（历史 bug ② 回归门）"""
    from app.core.tools.tool_permission_controller import ToolPermissionController
    from app.tools.registry import ToolRegistry

    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance()
    reg = ToolRegistry.get_instance()
    name = "test_count_refresh_probe_danger"
    _register_probe(reg, name, danger="dangerous")
    _settle(app)
    with_tool = host._tool_count_label.text()
    assert with_tool.startswith("1/"), f"危险探针未计入: {with_tool}"

    # agent permission 规则关掉全部工具 → 危险计数归零（此前 emit 先于卡片连接而丢失）
    ctrl: ToolPermissionController = host._tool_permission_controller
    ctrl.apply_agent("probe_agent", {}, {name: "deny"})
    _settle(app)
    after_agent = host._tool_count_label.text()
    assert after_agent.startswith("0/"), f"agent 注入后危险计数未刷新: {with_tool} → {after_agent}"
    assert ctrl.is_agent_active()
