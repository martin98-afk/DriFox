# -*- coding: utf-8 -*-
"""commands 按插件 source 精准化（方案 A）测试

覆盖：
1. reload_plugin_commands("a") 只清 A 插件命令，B 插件命令对象 id 不变
2. 同名命令双 source：删一留一
3. _plugin_name_of_md 三分支（commands / agents / 其他）
4. 缓存 roundtrip 保留 source
"""

import json

import pytest


@pytest.fixture()
def fresh_cmd_mgr():
    """每用例独立 CommandManager（绕过进程级单例污染）"""
    from app.core.commands.command_manager import CommandManager, CommandType

    mgr = CommandManager()
    return mgr, CommandType


# ── ① 精准重载不动其他插件 ────────────────────────────────────────


def test_reload_plugin_commands_preserves_other_plugin(fresh_cmd_mgr, tmp_path, monkeypatch):
    from app.core.commands import builtin_commands as bc
    from app.core.commands.command_manager import CommandManager

    mgr, CT = fresh_cmd_mgr
    monkeypatch.setattr(CommandManager, "get_instance", classmethod(lambda cls: mgr))

    # 两个插件各注册一条命令
    mgr.register(name="cmd_a", command_type=CT.FUNCTION, description="A", source="plugin:a")
    mgr.register(name="cmd_b", command_type=CT.FUNCTION, description="B", source="plugin:b")
    b_before = mgr.get_command("cmd_b")

    # 只有插件 a 有命令目录
    (tmp_path / "plugin-a" / "commands").mkdir(parents=True)
    (tmp_path / "plugin-a" / "commands" / "cmd_a.md").write_text(
        "---\ndescription: A new\n---\nbody\n", encoding="utf-8"
    )

    monkeypatch.setattr(
        "app.plugins.managers.plugin_manager.PluginManager.is_initialized",
        lambda self: True,
    )
    monkeypatch.setattr(
        "app.plugins.managers.plugin_manager.PluginManager.get_command_files_for_plugin",
        lambda self, name: (
            [tmp_path / "plugin-a" / "commands" / "cmd_a.md"] if name == "a" else []
        ),
    )
    monkeypatch.setattr(bc, "_rebind_command_shortcuts", lambda: None)

    count = bc.reload_plugin_commands("a")

    assert count == 1
    # B 插件的命令对象未被触碰（同一实例）
    assert mgr.get_command("cmd_b") is b_before
    # A 的 description 已更新
    assert mgr.get_command("cmd_a").description == "A new"


def test_reload_plugin_commands_removed_plugin_only_clears(fresh_cmd_mgr, tmp_path, monkeypatch):
    """插件目录已删除：只清不注册，无需调用方特判"""
    from app.core.commands import builtin_commands as bc
    from app.core.commands.command_manager import CommandManager

    mgr, CT = fresh_cmd_mgr
    monkeypatch.setattr(CommandManager, "get_instance", classmethod(lambda cls: mgr))

    mgr.register(name="gone", command_type=CT.FUNCTION, description="G", source="plugin:gone")
    mgr.register(name="stay", command_type=CT.FUNCTION, description="S", source="plugin:other")

    monkeypatch.setattr(
        "app.plugins.managers.plugin_manager.PluginManager.is_initialized",
        lambda self: True,
    )
    monkeypatch.setattr(
        "app.plugins.managers.plugin_manager.PluginManager.get_command_files_for_plugin",
        lambda self, name: [],
    )
    monkeypatch.setattr(bc, "_rebind_command_shortcuts", lambda: None)

    count = bc.reload_plugin_commands("gone")

    assert count == 0
    assert not mgr.has_command("gone")
    assert mgr.has_command("stay")


# ── ② 同名命令双 source ───────────────────────────────────────────


def test_unregister_source_same_name_different_type_keeps_other(fresh_cmd_mgr):
    """同名不同类型、不同 source：删一个来源不影响另一个。

    注：``_commands[name][type]`` 是单键结构，同类型下后注册者整体覆盖先注册者
    （含 source），故「同名双 source 删一留一」只在不同 CommandType 间成立。
    """
    from app.core.commands.command_manager import CommandType

    mgr, CT = fresh_cmd_mgr
    mgr.register(name="review", command_type=CT.PROMPT, description="builtin-prompt", source="")
    mgr.register(name="review", command_type=CT.FUNCTION, description="plugin-func", source="plugin:x")

    assert mgr.unregister_source("plugin:x") == ["review"]
    # 插件的 FUNCTION 条目被摘，内置的 PROMPT 条目保留
    assert mgr.has_command("review")
    assert mgr.get_command("review").source == ""


def test_unregister_source_same_type_overwrite_replaces_source(fresh_cmd_mgr):
    """同类型同名：插件注册整体覆盖内置，卸载后名字一并消失（单键模型）。"""
    from app.core.commands.command_manager import CommandType

    mgr, CT = fresh_cmd_mgr
    mgr.register(name="dup", command_type=CT.FUNCTION, description="builtin", source="")
    mgr.register(name="dup", command_type=CT.FUNCTION, description="plugin", source="plugin:x")

    assert mgr.unregister_source("plugin:x") == ["dup"]
    assert not mgr.has_command("dup")


def test_unregister_source_removes_name_when_last_source(fresh_cmd_mgr):
    from app.core.commands.command_manager import CommandType

    mgr, CT = fresh_cmd_mgr
    mgr.register(name="only", command_type=CT.FUNCTION, description="x", source="plugin:y")

    assert mgr.unregister_source("plugin:y") == ["only"]
    assert not mgr.has_command("only")


def test_names_by_source_dedups_multi_type(fresh_cmd_mgr):
    """同名多类型同 source：names_by_source 只返回一个名字"""
    from app.core.commands.command_manager import CommandType

    mgr, CT = fresh_cmd_mgr
    mgr.register(name="multi", command_type=CT.FUNCTION, description="f", source="plugin:z")
    mgr.register(name="multi", command_type=CT.PROMPT, description="p", source="plugin:z")

    assert mgr.names_by_source("plugin:z") == ["multi"]
    # unregister 后两个类型一起走
    assert mgr.unregister_source("plugin:z") == ["multi"]
    assert not mgr.has_command("multi")


def test_register_source_does_not_inherit_old_value(fresh_cmd_mgr):
    """同名覆盖时 source 必须取新值（不走旧值保留逻辑）"""
    from app.core.commands.command_manager import CommandType

    mgr, CT = fresh_cmd_mgr
    mgr.register(name="s", command_type=CT.FUNCTION, description="a", source="plugin:old")
    # 热重载后新条目 source 变化
    mgr.register(name="s", command_type=CT.FUNCTION, description="b", source="plugin:new")

    assert mgr.get_command("s").source == "plugin:new"
    assert mgr.unregister_source("plugin:old") == []
    assert mgr.unregister_source("plugin:new") == ["s"]


# ── ③ _plugin_name_of_md 三分支 ───────────────────────────────────


def test_plugin_name_of_md_commands_branch():
    from app.core.commands.builtin_commands import _plugin_name_of_md

    p = _plugin_name_of_md("D:/plugins/my-plugin/commands/foo.md")
    assert p == "plugin:my-plugin"


def test_plugin_name_of_md_agents_branch():
    from app.core.commands.builtin_commands import _plugin_name_of_md

    p = _plugin_name_of_md("D:/plugins/my-plugin/agents/bar.md")
    assert p == "plugin:my-plugin"


def test_plugin_name_of_md_other_returns_empty():
    from app.core.commands.builtin_commands import _plugin_name_of_md

    # 非 commands/agents 父目录 → 主程序内置
    assert _plugin_name_of_md("D:/plugins/my-plugin/README.md") == ""
    assert _plugin_name_of_md("D:/plugins/my-plugin/skills/foo.md") == ""
    # 路径过短
    assert _plugin_name_of_md("foo.md") == ""


# ── ④ 缓存 roundtrip 保留 source ──────────────────────────────────


def test_cache_roundtrip_preserves_source(tmp_path, monkeypatch):
    from app.core.commands import builtin_commands as bc
    from app.core.commands.command_manager import CommandManager, CommandType

    monkeypatch.setattr(bc, "_get_cache_file", lambda: tmp_path / "commands_cache.json")
    monkeypatch.setattr(bc, "_CACHE_VERSION", 6)

    serialized = [
        {
            "name": "cached_cmd",
            "type": "command",
            "description": "d",
            "argument_hint": "",
            "prompt_text": "",
            "parameters": [],
            "shortcut": "",
            "prompt_sections": {},
            "source": "plugin:cached",
        }
    ]
    serialized_agents = [
        {"name": "cached_agent", "description": "a", "prompt_text": "p", "source": "plugin:cached"},
    ]

    bc._save_cache("testkey", serialized, serialized_agents)

    # 读回
    data = json.loads((tmp_path / "commands_cache.json").read_text(encoding="utf-8"))
    assert data["version"] == 6
    assert data["commands"][0]["source"] == "plugin:cached"
    assert data["agents"][0]["source"] == "plugin:cached"

    loaded = bc._try_load_cache("testkey")
    assert loaded is not None

    mgr = CommandManager()
    n1 = bc._register_from_cached_commands(mgr, loaded["commands"])
    n2 = bc._register_from_cached_agents(mgr, loaded["agents"])
    assert (n1, n2) == (1, 1)
    assert mgr.get_command("cached_cmd").source == "plugin:cached"
    assert mgr.get_command("cached_agent").source == "plugin:cached"


def test_cache_version_bumped_to_6():
    """_CACHE_VERSION 必须已升（旧缓存无 source 字段，不升会读到脏数据）"""
    from app.core.commands import builtin_commands as bc

    assert bc._CACHE_VERSION == 6
