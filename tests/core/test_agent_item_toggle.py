# -*- coding: utf-8 -*-
"""agents 细项级/组件级停用回归测试（D9/D10 在 agents 组件上的落地）

背景回归：tools 的细项停用在注册期由 _RegistryProxy 过滤，而 agents 走
AgentManager._load_agents_from_dir 直接解析注册，此前完全没消费禁用集——
被停用的智能体仍注册、仍注入 prompt，设置卡 token 统计只能按全量算。

覆盖：
1. 细项停用 → 不注册、不进 _plugin_agents、不注入 prompt
2. 细项停用对 yaml 智能体同样生效
3. 组件级停用 → reload_plugin_agents 卸载已有注册（开关「开→关」场景）
4. 组件级停用 → _load_agents_from_plugins 跳过（重启后场景）

设计：
- AgentManager 用 __new__ 绕过 __init__（避免触发全量加载），手动装配字段
- PluginManager 用实例级遮蔽打桩（对齐 test_plugin_component_toggles 的模式）
- 不写真实 Settings，避免污染用户配置
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.plugins.managers.plugin_manager import PluginManager


# ── 测试脚手架 ──────────────────────────────────


def _write_agent_md(path: Path, name: str, desc: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: {desc}\nmode: subagent\n---\nYou are {name}.\n",
        encoding="utf-8",
    )


def _write_agent_yaml(path: Path, name: str, desc: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"name: {name}\ndescription: {desc}\nmode: subagent\n", encoding="utf-8")


def _bare_manager(mod):
    """绕过 __init__ 的裸 AgentManager（不触发全量加载，字段手动装配）"""
    mgr = mod.AgentManager.__new__(mod.AgentManager)
    mgr.agents_dir = None
    mgr._agents = {}
    mgr._hidden_agents = {}
    mgr._hook_manager = None
    mgr._global_permission = {}
    mgr._builtin_tools = None
    mgr._plugin_agents = {}
    mgr._system_prompt_cache = {}
    return mgr


@pytest.fixture
def pm_stub():
    """PluginManager 实例级打桩：initialized/has/enabled + 内存态禁用集"""
    pm = PluginManager.get_instance()
    state = {"disabled": set(), "plugin": None}
    pm.__dict__["is_initialized"] = lambda: True
    pm.__dict__["has_plugin"] = lambda name: True
    pm.__dict__["is_enabled"] = lambda name: True
    pm.__dict__["get_plugin"] = lambda name: state["plugin"]
    pm.__dict__["is_component_enabled"] = lambda name, comp: f"{name}:{comp}" not in state["disabled"]
    pm.__dict__["is_item_enabled"] = lambda name, comp, item: (
        f"{name}:{comp}" not in state["disabled"] and f"{name}:{comp}:{item}" not in state["disabled"]
    )
    yield pm, state
    for key in ("is_initialized", "has_plugin", "is_enabled", "get_plugin", "is_component_enabled", "is_item_enabled"):
        pm.__dict__.pop(key, None)
    pm.invalidate_component_cache()


@pytest.fixture(autouse=True)
def _clear_agent_file_cache():
    """清理模块级 mtime 缓存，防跨用例污染"""
    import app.core.conversation.agent as agent_mod

    agent_mod._agent_file_cache.clear()
    yield
    agent_mod._agent_file_cache.clear()


# ── 细项级停用（D10）──────────────────────────────


def test_disabled_item_not_registered(pm_stub, tmp_path):
    """细项停用的智能体不注册：不进 _agents 也不进 _plugin_agents"""
    import app.core.conversation.agent as agent_mod

    _write_agent_md(tmp_path / "agents" / "alpha.md", "alpha", "kept")
    _write_agent_md(tmp_path / "agents" / "beta.md", "beta", "disabled")
    pm_stub[1]["disabled"].add("demo:agents:beta")

    mgr = _bare_manager(agent_mod)
    mgr._load_agents_from_dir(tmp_path / "agents", source_plugin="demo")

    assert set(mgr._plugin_agents["demo"]) == {"alpha"}
    assert "alpha" in mgr._agents
    assert "beta" not in mgr._agents
    assert "beta" not in mgr._hidden_agents


def test_disabled_item_not_in_prompt(pm_stub, tmp_path):
    """细项停用后 get_available_subagents_for_prompt 不再包含该智能体"""
    import app.core.conversation.agent as agent_mod

    _write_agent_md(tmp_path / "agents" / "alpha.md", "alpha", "kept")
    _write_agent_md(tmp_path / "agents" / "beta.md", "beta", "disabled")
    pm_stub[1]["disabled"].add("demo:agents:beta")

    mgr = _bare_manager(agent_mod)
    mgr._load_agents_from_dir(tmp_path / "agents", source_plugin="demo")
    prompt = mgr.get_available_subagents_for_prompt()

    assert "- **alpha**" in prompt
    assert "- **beta**" not in prompt


def test_disabled_item_yaml_also_filtered(pm_stub, tmp_path):
    """yaml 格式智能体的细项停用同样生效"""
    import app.core.conversation.agent as agent_mod

    _write_agent_yaml(tmp_path / "agents" / "gamma.yaml", "gamma", "disabled yaml agent")
    pm_stub[1]["disabled"].add("demo:agents:gamma")

    mgr = _bare_manager(agent_mod)
    mgr._load_agents_from_dir(tmp_path / "agents", source_plugin="demo")

    assert "gamma" not in mgr._plugin_agents.get("demo", set())
    assert "gamma" not in mgr._agents


def test_enabled_item_still_registered(pm_stub, tmp_path):
    """对照：无停用记录时全量注册（改动不破坏默认行为）"""
    import app.core.conversation.agent as agent_mod

    _write_agent_md(tmp_path / "agents" / "alpha.md", "alpha", "a")
    _write_agent_md(tmp_path / "agents" / "beta.md", "beta", "b")

    mgr = _bare_manager(agent_mod)
    mgr._load_agents_from_dir(tmp_path / "agents", source_plugin="demo")

    assert set(mgr._plugin_agents["demo"]) == {"alpha", "beta"}


# ── 组件级停用（D9）──────────────────────────────


def test_component_off_reload_unloads_existing(pm_stub, tmp_path):
    """「开→关」场景：组件级停用后 reload 卸载已有注册且不重新加载"""
    import app.core.conversation.agent as agent_mod

    agents_dir = tmp_path / "agents"
    _write_agent_md(agents_dir / "alpha.md", "alpha", "a")
    _write_agent_md(agents_dir / "beta.md", "beta", "b")

    mgr = _bare_manager(agent_mod)
    mgr._load_agents_from_dir(agents_dir, source_plugin="demo")
    assert set(mgr._plugin_agents["demo"]) == {"alpha", "beta"}

    pm_stub[1]["disabled"].add("demo:agents")
    pm_stub[1]["plugin"] = SimpleNamespace(name="demo", path=tmp_path)
    count = mgr.reload_plugin_agents("demo")

    assert count == 0
    assert "alpha" not in mgr._agents
    assert "beta" not in mgr._agents
    assert mgr._plugin_agents.get("demo", set()) == set()
    assert mgr.get_available_subagents_for_prompt() == ""


def test_component_off_skip_load_from_plugins(pm_stub, tmp_path, monkeypatch):
    """重启场景：_load_agents_from_plugins 跳过 agents 组件停用的插件"""
    import app.core.conversation.agent as agent_mod

    plugin_dir = tmp_path / "demo"
    _write_agent_md(plugin_dir / "agents" / "alpha.md", "alpha", "a")
    pm_stub[1]["disabled"].add("demo:agents")

    plugin = SimpleNamespace(name="demo", path=plugin_dir)
    pm = pm_stub[0]
    monkeypatch.setattr(pm, "get_enabled_plugins", lambda: [plugin], raising=False)

    mgr = _bare_manager(agent_mod)
    mgr._load_agents_from_plugins()

    assert mgr._plugin_agents.get("demo", set()) == set()
    assert "alpha" not in mgr._agents


def test_component_on_after_off_reloads(pm_stub, tmp_path):
    """「关→开」场景：重新启用后 reload 恢复注册（含此前细项停用的）"""
    import app.core.conversation.agent as agent_mod

    agents_dir = tmp_path / "agents"
    _write_agent_md(agents_dir / "alpha.md", "alpha", "a")
    _write_agent_md(agents_dir / "beta.md", "beta", "b")
    pm_stub[1]["disabled"].add("demo:agents:beta")

    mgr = _bare_manager(agent_mod)
    mgr._load_agents_from_dir(agents_dir, source_plugin="demo")
    assert set(mgr._plugin_agents["demo"]) == {"alpha"}

    pm_stub[1]["disabled"].discard("demo:agents")
    pm_stub[1]["plugin"] = SimpleNamespace(name="demo", path=tmp_path)
    count = mgr.reload_plugin_agents("demo")

    assert count == 1
    assert set(mgr._plugin_agents["demo"]) == {"alpha"}


# ── 细项开关精准增删（替代全量 reload）──────────────


def test_set_item_disable_removes_from_registry_and_prompt(pm_stub, tmp_path):
    """set_agent_item_enabled(False)：注册与 prompt 注入同步摘除"""
    import app.core.conversation.agent as agent_mod

    agents_dir = tmp_path / "agents"
    _write_agent_md(agents_dir / "alpha.md", "alpha", "a")
    _write_agent_md(agents_dir / "beta.md", "beta", "b")

    mgr = _bare_manager(agent_mod)
    mgr._load_agents_from_dir(agents_dir, source_plugin="demo")

    assert mgr.set_agent_item_enabled("demo", "alpha", False) is True
    assert "alpha" not in mgr._agents
    assert set(mgr._plugin_agents["demo"]) == {"beta"}
    prompt = mgr.get_available_subagents_for_prompt()
    assert "- **alpha**" not in prompt
    assert "- **beta**" in prompt


def test_set_item_enable_restores_from_file_cache(pm_stub, tmp_path):
    """set_agent_item_enabled(True)：file_cache 命中路径零 parse 恢复注册"""
    import app.core.conversation.agent as agent_mod

    agents_dir = tmp_path / "agents"
    _write_agent_md(agents_dir / "alpha.md", "alpha", "a")

    mgr = _bare_manager(agent_mod)
    mgr._load_agents_from_dir(agents_dir, source_plugin="demo")
    mgr.set_agent_item_enabled("demo", "alpha", False)
    assert "alpha" not in mgr._agents

    assert mgr.set_agent_item_enabled("demo", "alpha", True) is True
    assert "alpha" in mgr._agents
    assert set(mgr._plugin_agents["demo"]) == {"alpha"}


def test_set_item_enable_component_off_rejected(pm_stub, tmp_path):
    """无 file_cache（新进程场景）+ 组件级停用 → 回退 reload 被拦截，返回 False"""
    import app.core.conversation.agent as agent_mod

    pm_stub[1]["disabled"].add("demo:agents")
    pm_stub[1]["plugin"] = SimpleNamespace(name="demo", path=tmp_path)

    mgr = _bare_manager(agent_mod)
    assert mgr.set_agent_item_enabled("demo", "alpha", True) is False
    assert "alpha" not in mgr._agents


# ── 命令侧细项过滤与单条增删 ────────────────────────


@pytest.fixture
def cmd_env(monkeypatch, tmp_path):
    """CommandManager 独立实例 + _get_agent_files 打桩到临时目录"""
    from app.core.commands import builtin_commands as bc
    from app.core.commands.command_manager import CommandManager

    CommandManager.reset_instance()
    cmd_mgr = CommandManager.get_instance()

    agents_dir = tmp_path / "demo" / "agents"
    _write_agent_md(agents_dir / "alpha.md", "alpha", "kept agent")
    _write_agent_md(agents_dir / "beta.md", "beta", "disabled agent")
    monkeypatch.setattr(bc, "_get_agent_files", lambda: [agents_dir / "alpha.md", agents_dir / "beta.md"])

    yield bc, cmd_mgr

    CommandManager.reset_instance()


def _agent_entries(cmd_mgr, name: str) -> dict:
    return cmd_mgr._commands.get(name, {})


def test_register_agent_commands_skips_disabled_item(pm_stub, cmd_env):
    """细项停用的智能体不注册 /命令（与 AgentManager 注册期过滤对齐）"""
    from app.core.commands.command_manager import CommandType

    bc, cmd_mgr = cmd_env
    pm_stub[1]["disabled"].add("demo:agents:beta")

    registered = bc._register_builtin_agents_as_commands(cmd_mgr)

    assert [r["name"] for r in registered] == ["alpha"]
    assert CommandType.AGENT not in _agent_entries(cmd_mgr, "beta")
    assert CommandType.AGENT in _agent_entries(cmd_mgr, "alpha")


def test_set_agent_command_enabled_toggle(pm_stub, cmd_env):
    """set_agent_command_enabled 双向：停用摘单条、启用恢复单条，不牵连兄弟"""
    from app.core.commands.command_manager import CommandType

    bc, cmd_mgr = cmd_env
    bc._register_builtin_agents_as_commands(cmd_mgr)
    assert CommandType.AGENT in _agent_entries(cmd_mgr, "beta")

    assert bc.set_agent_command_enabled("beta", "demo", False) is True
    assert CommandType.AGENT not in _agent_entries(cmd_mgr, "beta")
    assert CommandType.AGENT in _agent_entries(cmd_mgr, "alpha")

    assert bc.set_agent_command_enabled("beta", "demo", True) is True
    assert CommandType.AGENT in _agent_entries(cmd_mgr, "beta")


def test_set_agent_command_enabled_blocked_by_component_off(pm_stub, cmd_env):
    """组件级停用期间 set_agent_command_enabled(True) 拒绝注册"""
    from app.core.commands.command_manager import CommandType

    bc, cmd_mgr = cmd_env
    pm_stub[1]["disabled"].add("demo:agents")

    assert bc.set_agent_command_enabled("alpha", "demo", True) is False
    assert CommandType.AGENT not in _agent_entries(cmd_mgr, "alpha")


def test_cached_agents_skip_disabled_item(pm_stub, cmd_env):
    """重启缓存命中路径：停用细项不因缓存复活（回归：缓存快照绕过禁用集）"""
    from app.core.commands.command_manager import CommandType

    bc, cmd_mgr = cmd_env
    pm_stub[1]["disabled"].add("demo:agents:beta")

    cached = [
        {"name": "alpha", "description": "kept", "prompt_text": "x", "source": "plugin:demo"},
        {"name": "beta", "description": "revived?", "prompt_text": "x", "source": "plugin:demo"},
    ]
    count = bc._register_from_cached_agents(cmd_mgr, cached)

    assert count == 1
    assert CommandType.AGENT in _agent_entries(cmd_mgr, "alpha")
    assert CommandType.AGENT not in _agent_entries(cmd_mgr, "beta")
