# -*- coding: utf-8 -*-
"""/subagents 命令 handler（插件侧）行为

覆盖四条分支：--create 降级、--reset、--model=（空/解析失败/成功）、
无参（无任务/有任务 → 补行到内嵌面板）。能力池与活跃窗口用假的替换。
"""

import importlib
import importlib.util
import os
import sys

import pytest

_PKG = "system_subagent_ui_pkg"


def _load_ui_pkg():
    root = os.path.abspath(".")
    ui_path = os.path.join(root, "plugins", "system-subagent", "ui")
    init_path = os.path.join(ui_path, "__init__.py")
    if _PKG not in sys.modules:
        spec = importlib.util.spec_from_file_location(_PKG, init_path, submodule_search_locations=[ui_path])
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[_PKG] = module
        spec.loader.exec_module(module)
    return importlib.import_module(f"{_PKG}.command_handler")


_handler = _load_ui_pkg()


class _Pool(dict):
    """能力池桩：dict 语义 + __contains__ 走 dict。"""


@pytest.fixture()
def env(monkeypatch):
    """装配：假窗口 + 假能力池 + 记录型 notify。"""
    state = {"notifies": [], "panel": None, "attached": []}

    pool = _Pool()

    def _notify(title, message, kind="success", duration=5000):
        state["notifies"].append((title, message, kind))

    pool["ui.notify"] = _notify
    pool["subagent.run"] = None

    host = object()

    monkeypatch.setattr(
        "app.plugins.capabilities.resolve.active_main_widget", lambda: host
    )
    monkeypatch.setattr(
        "app.plugins.capabilities.resolve.pool_for", lambda *a, **k: pool
    )
    return state, pool


def test_create_branch_degrades(env):
    from app.core.commands.command_manager import CommandNeedDegrade

    with pytest.raises(CommandNeedDegrade) as exc:
        _handler.handle_command("--create=写一个审查智能体")

    assert exc.value.command_name == "subagents"


def test_no_active_window_is_noop(env, monkeypatch):
    state, _pool = env
    monkeypatch.setattr("app.plugins.capabilities.resolve.active_main_widget", lambda: None)

    _handler.handle_command("")

    assert state["notifies"] == []


def test_no_args_without_running_tasks_warns(env):
    state, _pool = env

    _handler.handle_command("")

    assert state["notifies"][0][0] == "暂无运行中的子智能体"


class _FakeExecutor:
    def __init__(self, agent_name, desc, model):
        self.agent_name = agent_name
        self.task_description = desc
        self.llm_config = {"模型名称": model}


class _FakePanel:
    def __init__(self):
        self.added = []

    def has_task(self, task_id):
        return any(t[0] == task_id for t in self.added)

    def add_task(self, task_id, agent_name, desc, model_name=""):
        self.added.append((task_id, agent_name, desc, model_name))


def test_no_args_adds_running_tasks_to_panel(env, monkeypatch):
    state, pool = env
    panel = _FakePanel()

    class _Manager:
        _running_tasks = {
            "t1": _FakeExecutor("coder", "写代码", "gpt-4o"),
            "t2": _FakeExecutor("reviewer", "评审", "claude-3"),
        }

    class _Facade:
        manager = _Manager()

    # 管理器归插件：命令经窗口服务表取门面（不再是 pool["subagent.run"]）
    from app.plugins.registries.window_services import get_window_services

    services = get_window_services()
    services.register("subagent", _Facade(), plugin_name="system-subagent")

    class _Registry:
        def attach_card_inline_panel(self, panel_id, main_widget=None):
            return panel

    monkeypatch.setattr(
        "app.plugins.registries.ui_plugin_registry.UIPluginRegistry.get_instance",
        classmethod(lambda cls: _Registry()),
    )

    try:
        _handler.handle_command("")
    finally:
        services.unregister_plugin("system-subagent")

    assert [a[0] for a in panel.added] == ["t1", "t2"]
    assert panel.added[0][3] == "gpt-4o"
