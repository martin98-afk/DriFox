# -*- coding: utf-8 -*-
"""回归：代理与插件 hooks 必须早于任何会话创建就绪

背景（2026-11-15 实测两次启动）：
agents+hooks 全量 reload 原先排在 PluginHostService 的 idle 延迟队列（delay 2s），
而窗口 showEvent 的 critical 任务立刻 create_session 并触发 SessionStart，产生两类空跑：

1. hooks 未注册 → HookManager.trigger_event 首行
   `if event_name not in self._hooks: return []` 静默返回空列表
   → 会话消息里两个清单都没有，首条消息退回 system prompt 兜底注入并持久化
   （症状：启动后首次对话的清单在系统提示词里，手动新建一次会话才正常）。
2. hooks 已注册但 agents 未解析 → inject_subagent_info 拿到空 list_subagents
   → 整块 <available_subagents> 不输出。技能清单走 config.read 不依赖
   AgentManager，于是表现为「有技能列表但没有智能体清单」。

修复：_init_plugin_system 在 PluginManager 扫描后同步 reload_agents（冷 243ms），
idle 队列那条保留（幂等热跑 10ms）。本文件锁定「同步段确实调用了 reload_agents」
以及「调用后两个清单的数据源都已就绪」。
"""

import json
import pathlib

import pytest


def _write_plugin(root: pathlib.Path, name: str, hooks: dict, *, agent_name: str = "") -> pathlib.Path:
    """造一个最小插件目录：hooks/hooks.json（+ 可选 agents/*.md）。"""
    plugin_dir = root / name
    hooks_dir = plugin_dir / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "hooks.json").write_text(json.dumps({"hooks": hooks}, ensure_ascii=False), encoding="utf-8")
    if agent_name:
        agents_dir = plugin_dir / "agents"
        agents_dir.mkdir()
        # is_subagent：非 primary 的角色，才能进 list_subagents
        (agents_dir / f"{agent_name}.md").write_text(
            "---\n"
            f"name: {agent_name}\n"
            "description: 测试用子智能体\n"
            "mode: subagent\n"
            "---\n\n"
            "正文\n",
            encoding="utf-8",
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

    # 隔离类级共享注册表，避免污染其他用例
    monkeypatch.setattr(HookManager, "_shared_hooks", {})
    monkeypatch.setattr(HookManager, "_shared_skill_to_hooks", {})
    monkeypatch.setattr(HookManager, "_shared_config_watchers", {})
    monkeypatch.setattr(HookManager, "_shared_restore_snapshots", {})

    hm = HookManager()
    hm._hooks = HookManager._shared_hooks
    hm._skill_to_hooks = HookManager._shared_skill_to_hooks
    hm._config_watchers = HookManager._shared_config_watchers

    am = AgentManager.__new__(AgentManager)
    am._hook_manager = hm
    am._agents = {}
    am._hidden_agents = {}
    am._plugin_agents = {}
    am._global_permission = {}
    am._builtin_tools = None
    am._system_prompt_cache = {}
    am.agents_dir = None
    return am, hm


def test_reload_agents_registers_hooks_and_agents_together(manager, monkeypatch, tmp_path):
    """核心断言：一次 reload_agents 后，SessionStart 有 rule 且子智能体可列举。"""
    from app.plugins.managers import plugin_manager as pm_mod

    plugin_dir = _write_plugin(
        tmp_path,
        "demo-plugin",
        {"SessionStart": [{"hooks": [{"type": "prompt", "prompt": "hello", "id": "h1"}]}]},
        agent_name="demo-sub",
    )
    pm = _FakePluginManager([_FakePlugin(plugin_dir, "demo-plugin")])
    monkeypatch.setattr(pm_mod.PluginManager, "get_instance", classmethod(lambda _cls: pm))

    am, hm = manager
    assert not hm._hooks.get("SessionStart")  # 注册前为空
    assert am.list_subagents(include_hidden=True) == []

    am.reload_agents()

    assert len(hm._hooks.get("SessionStart") or []) == 1
    names = [a.name for a in am.list_subagents(include_hidden=True)]
    assert "demo-sub" in names


def test_reload_agents_skips_plugin_with_hooks_component_disabled(manager, monkeypatch, tmp_path):
    """hooks 组件整类停用的插件不注册 hooks（与 _load_agents 同口径）。"""
    from app.plugins.managers import plugin_manager as pm_mod

    plugin_dir = _write_plugin(
        tmp_path,
        "off-plugin",
        {"SessionStart": [{"hooks": [{"type": "prompt", "prompt": "x", "id": "h1"}]}]},
    )
    pm = _FakePluginManager([_FakePlugin(plugin_dir, "off-plugin")], component_enabled=False)
    monkeypatch.setattr(pm_mod.PluginManager, "get_instance", classmethod(lambda _cls: pm))

    am, hm = manager
    am.reload_agents()
    assert not hm._hooks.get("SessionStart")


def test_plugin_host_reloads_agents_in_sync_phase(monkeypatch):
    """_init_plugin_system 同步段必须调用 reload_agents（不再只注册 hooks）。"""
    from app.core.services.plugin_host_service import PluginHostService
    from app.plugins.managers import plugin_manager as pm_mod

    calls = []

    class _AM:
        def reload_agents(self):
            calls.append("reload_agents")

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
    assert calls == ["reload_agents"]


def test_plugin_host_survives_reload_failure(monkeypatch):
    """同步段 reload 抛异常不能中断启动（后续延迟链仍要跑）。"""
    from app.core.services.plugin_host_service import PluginHostService
    from app.plugins.managers import plugin_manager as pm_mod

    deferred_calls = []

    class _AM:
        def reload_agents(self):
            raise RuntimeError("boom")

        def list_agents(self):
            return []

    class _PM:
        def is_initialized(self):
            return False  # 触发延迟链分支

        def initialize(self, _dir):
            return None

        def list_plugins(self):
            return []

    pm = _PM()
    monkeypatch.setattr(pm_mod.PluginManager, "get_instance", classmethod(lambda _cls: pm))

    host = PluginHostService.__new__(PluginHostService)
    host._agent_manager = _AM()
    host._start_plugin_watcher = lambda: None
    host._defer_non_critical_plugin_init = lambda _pm: deferred_calls.append("deferred")
    host._init_plugin_system()
    assert deferred_calls == ["deferred"]
