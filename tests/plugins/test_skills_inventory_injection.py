# -*- coding: utf-8 -*-
"""偏好技能清单注入：auto_enable 条目免启用、enabled 名单过滤、主对话去重

清单渲染已从主程序 AgentManager 迁到 system-hooks 插件（数据经能力池/SDK 自取），
这些断言随之针对 hook 本身。
"""

import importlib.util
from pathlib import Path

import pytest

_HOOK_PATH = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "system-hooks"
    / "hooks"
    / "inject_skills_content.py"
)

@pytest.fixture()
def hook_mod():
    spec = importlib.util.spec_from_file_location("_test_inject_skills_content", str(_HOOK_PATH))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def _skills(*entries):
    return [
        {
            "name": name,
            "qualified_name": name,
            "description": description,
            "auto_enable": auto_enable,
        }
        for name, description, auto_enable in entries
    ]

@pytest.fixture()
def patch_sdk(monkeypatch, hook_mod):
    """替换 hook 依赖的 SDK 符号：技能清单 / 启用名单 / 会话探针。"""

    def _install(skills, enabled=(), messages=None):
        import app.plugins.sdk as sdk

        monkeypatch.setattr(sdk, "get_local_skills", lambda project_dir=None: skills)
        monkeypatch.setattr(
            sdk,
            "caps_for",
            lambda cap_id, surface="": (
                (lambda key, default=None: list(enabled) if key == "llm_enabled_skills" else default)
                if cap_id == "config.read"
                else (lambda: {"messages": messages or []})
            ),
        )

    return _install

def test_auto_enable_skill_included_without_enabled_list(hook_mod, patch_sdk):
    patch_sdk(
        _skills(("normal-skill", "N", False), ("proj-skill", "P", True)),
        enabled=(),
    )
    out = hook_mod.hook("SessionStart", {"project_root": "/proj"})
    assert "proj-skill" in out
    assert "normal-skill" not in out

def test_enabled_list_still_respected(hook_mod, patch_sdk):
    patch_sdk(_skills(("a", "A", False)), enabled=("a",))
    assert "### a" in hook_mod.hook("SessionStart", {})

    patch_sdk(_skills(("a", "A", False)), enabled=())
    assert hook_mod.hook("SessionStart", {}) == ""

def test_build_system_prompt_skips_when_session_has_inventory(hook_mod, patch_sdk):
    """主对话（context 带 session_id）且会话消息已有清单 → 系统提示不再重复注入。"""
    messages = [
        {"role": "user", "_hook_event": "SessionStart", "content": "<enabled_skills>x</enabled_skills>"}
    ]
    patch_sdk(_skills(("a", "A", False)), enabled=("a",), messages=messages)
    assert hook_mod.hook("BuildSystemPrompt", {"session_id": "s1"}) == ""

def test_build_system_prompt_injects_when_no_session(hook_mod, patch_sdk):
    """子智能体 / Gateway（context 无 session_id）→ 系统提示兜底注入。"""
    patch_sdk(_skills(("a", "A", False)), enabled=("a",), messages=[])
    out = hook_mod.hook("BuildSystemPrompt", {})
    assert "### a" in out

def test_subagent_info_only_on_session_start(hook_mod, patch_sdk, monkeypatch):
    """子智能体清单只在 SessionStart 注入；其余事件返回空。"""
    spec = importlib.util.spec_from_file_location(
        "_test_inject_subagent_info", str(_HOOK_PATH.with_name("inject_subagent_info.py"))
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class _Agent:
        def __init__(self, name, description):
            self.name = name
            self.description = description

    class _Manager:
        def list_subagents(self, include_hidden=True):
            return [_Agent("explore", "探索")]

    import app.plugins.sdk as sdk

    monkeypatch.setattr(sdk, "caps_for", lambda cap_id, surface="": _Manager())
    out = module.hook("SessionStart", {})
    assert "**explore**" in out
    assert module.hook("BuildSystemPrompt", {}) == ""
