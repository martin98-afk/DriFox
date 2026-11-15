# -*- coding: utf-8 -*-
"""回归：插件 hooks 必须早于任何会话创建就绪

背景（2026-11-15 实测）：技能/子智能体清单从 BuildSystemPrompt 拆到 SessionStart
后，启动后**首次**对话的清单仍出现在 system prompt 里，手动新建一次会话才恢复。
根因是注册时序：
- agents+hooks 全量 reload 在 PluginHostService 的 idle 延迟队列（delay 2s）
- 窗口 showEvent 的 critical 任务立刻 create_session 并触发 SessionStart
顺序颠倒时 HookManager.trigger_event 首行 `if event_name not in self._hooks:
return []` 静默空返，会话消息里没有清单 → 首条消息退回 system prompt 兜底注入
并随 session.system_prompt 持久化。

修复：PluginHostService 初始化关键路径补一次 hooks-only 注册
（AgentManager.load_plugin_hooks_only，只遍历 hooks 目录，不解析 agents）。

本文件覆盖两个新增面：
1. AgentManager.load_plugin_hooks_only 只注册 hooks、不加载 agents
2. PluginHostService._init_plugin_system 同步段确实调用了它
"""

import json
import pathlib

import pytest


def _write_plugin(root: pathlib.Path, name: str, hooks: dict, *, with_agent: bool = False) -> pathlib.Path:
    """造一个最小插件目录：hooks/hooks.json（+ 可选 agents/）。"""
    plugin_dir = root / name
    hooks_dir = plugin_dir / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "hooks.json").write_text(json.dumps({"hooks": hooks}, ensure_ascii=False), encoding="utf-8")
    if with_agent:
        agents_dir = plugin_dir / "agents"
        agents_dir.mkdir()
        (agents_dir / "demo.md").write_text(
            "---\nname: demo\ndescription: d\n---\n\n正文\n", encoding="utf-8"
        )
    return plugin_dir


class _FakePlugin:
    def __init__(self, path, name, is_system=True):
        self.path = path
        self.name = name
        self.is_system = is_system


class _FakePluginManager:
    """最小 PluginManager 替身：get_enabled_plugins / is_component_enabled。"""

    def __init__(self, plugins, initialized=True, component_enabled=True):
        self._plugins = plugins
        self._initialized = initialized
        self._component_enabled = component_enabled

    def is_initialized(self):
        return self._initialized

    def get_enabled_plugins(self):
        return self._plugins

    def is_component_enabled(self, _name, _component):
        return self._component_enabled


@pytest.fixture()
def manager(monkeypatch):
    """裸 AgentManager + 独立 HookManager（不触真实插件目录）。"""
    from app.core.conversation.agent import AgentManager
    from app.core.hooks.hook_manager import HookManager

    hm = HookManager()
    # 隔离类级共享注册表，避免污染其他用例
    monkeypatch.setattr(HookManager, "_shared_hooks", {})
    monkeypatch.setattr(HookManager, "_shared_skill_to_hooks", {})
    monkeypatch.setattr(HookManager, "_shared_config_watchers", {})
    monkeypatch.setattr(HookManager, "_shared_restore_snapshots", {})
    hm._hooks = HookManager._shared_hooks
    hm._skill_to_hooks = HookManager._shared_skill_to_hooks
    hm._config_watchers = HookManager._shared_config_watchers

    am = AgentManager.__new__(AgentManager)
    am._hook_manager = hm
    am._agents = {}
    am._hidden_agents = {}
    am._plugin_agents = {}
    am._global_permission = {}
    am.agents_dir = None
    am._system_prompt_cache = {}
    return am, hm


def test_load_plugin_hooks_only_registers_session_start(manager, monkeypatch, tmp_path):
    """核心断言：hooks-only 注册后 SessionStart 有 rule，agents 一个没加载。"""
    from app.plugins.managers import plugin_manager as pm_mod

    plugin_dir = _write_plugin(
        tmp_path,
        "demo-plugin",
        {"SessionStart": [{"hooks": [{"type": "prompt", "prompt": "hello", "id": "h1"}]}]},
        with_agent=True,
    )
    pm = _FakePluginManager([_FakePlugin(plugin_dir, "demo-plugin")])
    monkeypatch.setattr(pm_mod.PluginManager, "get_instance", classmethod(lambda _cls: pm))

    am, hm = manager
    assert hm._hooks.get("SessionStart") in (None, [])  # 注册前为空
    assert am.load_plugin_hooks_only() == 1
    assert len(hm._hooks.get("SessionStart") or []) == 1
    # agents 面必须保持不加载（仍由 idle 队列承担）
    assert am._agents == {}


def test_load_plugin_hooks_only_noop_without_hook_manager(manager, monkeypatch):
    am, _hm = manager
    am._hook_manager = None
    assert am.load_plugin_hooks_only() == 0


def test_load_plugin_hooks_only_noop_before_plugin_scan(manager, monkeypatch):
    """PluginManager 未初始化时返回 0，不抛异常（GateWay 早于 PluginHost 场景）。"""
    from app.plugins.managers import plugin_manager as pm_mod

    pm = _FakePluginManager([], initialized=False)
    monkeypatch.setattr(pm_mod.PluginManager, "get_instance", classmethod(lambda _cls: pm))
    am, _hm = manager
    assert am.load_plugin_hooks_only() == 0


def test_load_plugin_hooks_only_respects_disabled_hooks_component(manager, monkeypatch, tmp_path):
    """hooks 组件整类停用的插件不注册（与 _load_agents 同口径）。"""
    from app.plugins.managers import plugin_manager as pm_mod

    plugin_dir = _write_plugin(
        tmp_path,
        "off-plugin",
        {"SessionStart": [{"hooks": [{"type": "prompt", "prompt": "x", "id": "h1"}]}]},
    )
    pm = _FakePluginManager([_FakePlugin(plugin_dir, "off-plugin")], component_enabled=False)
    monkeypatch.setattr(pm_mod.PluginManager, "get_instance", classmethod(lambda _cls: pm))

    am, hm = manager
    assert am.load_plugin_hooks_only() == 1  # 遍历了，但跳过注册
    assert not hm._hooks.get("SessionStart")


def test_plugin_host_calls_hooks_only_in_sync_phase(monkeypatch):
    """_init_plugin_system 同步段必须调用 load_plugin_hooks_only。"""
    from app.core.services.plugin_host_service import PluginHostService
    from app.plugins.managers import plugin_manager as pm_mod

    calls = []

    class _AM:
        def load_plugin_hooks_only(self):
            calls.append("hooks_only")
            return 1

        def list_agents(self):
            return []

    class _PM:
        def is_initialized(self):
            return True

        def initialize(self, _dir):
            return None

        def list_plugins(self):
            return []

    pm = _PM()
    monkeypatch.setattr(pm_mod.PluginManager, "get_instance", classmethod(lambda _cls: pm))

    host = PluginHostService.__new__(PluginHostService)
    host._agent_manager = _AM()
    host._start_plugin_watcher = lambda: None
    host._init_plugin_system()
    assert calls == ["hooks_only"]
