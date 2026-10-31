# -*- coding: utf-8 -*-
"""ContributionRegistry：键幂等覆盖、按插件注销、查询稳定排序"""

from app.plugins.registries.contribution_registry import ContributionRegistry


def _fresh():
    return ContributionRegistry()


def test_register_and_query_skill_source():
    reg = _fresh()
    reg.register_skill_source("p1", "src", provider=lambda wd: [], description="d")
    sources = reg.iter_skill_sources()
    assert len(sources) == 1
    plugin, name, entry = sources[0]
    assert (plugin, name) == ("p1", "src")
    assert entry["description"] == "d"


def test_register_idempotent_override():
    reg = _fresh()
    reg.register_skill_source("p1", "src", provider=lambda wd: ["a"])
    reg.register_skill_source("p1", "src", provider=lambda wd: ["b"])
    assert len(reg.iter_skill_sources()) == 1
    assert reg.iter_skill_sources()[0][2]["provider"]("x") == ["b"]


def test_unregister_source_removes_all_kinds():
    reg = _fresh()
    reg.register_skill_source("p1", "src", provider=lambda wd: [])
    reg.register_command("p1", {"name": "cmd-a", "description": "x"})
    reg.register_agent("p1", {"name": "agent-a", "description": "x"})
    reg.register_command("p2", {"name": "cmd-b", "description": "y"})
    reg.unregister_source("p1")
    assert reg.iter_skill_sources() == []
    assert [c["name"] for c in reg.get_commands()] == ["cmd-b"]
    assert reg.get_agent_def("agent-a") is None


def test_command_agent_lookup_and_sort():
    reg = _fresh()
    reg.register_command("p2", {"name": "cmd-b"})
    reg.register_command("p1", {"name": "cmd-a"})
    reg.register_agent("p1", {"name": "agent-a", "prompt": "P"})
    assert [c["name"] for c in reg.get_commands()] == ["cmd-a", "cmd-b"]
    d = reg.get_command("cmd-b")
    assert d["_plugin"] == "p2"
    d = reg.get_agent_def("agent-a")
    assert d["_plugin"] == "p1" and d["prompt"] == "P"
    assert reg.get_command("missing") is None


def test_register_command_requires_name():
    import pytest

    reg = _fresh()
    with pytest.raises(ValueError):
        reg.register_command("p1", {"description": "no name"})
