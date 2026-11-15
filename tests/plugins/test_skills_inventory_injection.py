# -*- coding: utf-8 -*-
"""偏好技能 / 子智能体清单注入：auto_enable 免启用、enabled 过滤、去重与事件分工

清单渲染已从主程序 AgentManager 迁到 system-hooks 插件（数据经能力池自取），
这些断言随之针对 hook。
"""

import importlib.util
from pathlib import Path

import pytest

_HOOKS_DIR = Path(__file__).resolve().parents[2] / "plugins" / "system-hooks" / "hooks"

def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"_test_{name}", str(_HOOKS_DIR / f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

@pytest.fixture()
def skills_hook():
    return _load("inject_skills_content")

@pytest.fixture()
def subagents_hook():
    return _load("inject_subagent_info")

def _skills(*entries):
    return [
        {"name": name, "qualified_name": name, "description": desc, "auto_enable": auto}
        for name, desc, auto in entries
    ]

@pytest.fixture()
def patch_sdk(monkeypatch):
    """替换 hook 依赖的 SDK 符号：技能来源、启用名单、会话快照。"""

    def _install(skills, enabled=(), messages=None):
        import app.plugins.sdk as sdk

        monkeypatch.setattr(sdk, "get_local_skills", lambda project_dir=None: skills)

        def caps_for(cap_id, surface=""):
            if cap_id == "config.read":
                return lambda key, default=None: list(enabled) if key == "llm_enabled_skills" else default
            if cap_id == "session.read":
                return lambda *a, **k: {"messages": messages or []}
            return None

        monkeypatch.setattr(sdk, "caps_for", caps_for)

    return _install

def test_auto_enable_skill_included_without_enabled_list(skills_hook, patch_sdk):
    patch_sdk(_skills(("normal-skill", "N", False), ("proj-skill", "P", True)), enabled=())
    out = skills_hook.hook("SessionStart", {"project_root": "/proj"})
    assert "proj-skill" in out
    assert "normal-skill" not in out

def test_enabled_list_still_respected(skills_hook, patch_sdk):
    patch_sdk(_skills(("a", "A", False)), enabled=("a",))
    assert "### a" in skills_hook.hook("SessionStart", {})

    patch_sdk(_skills(("a", "A", False)), enabled=())
    assert skills_hook.hook("SessionStart", {}) == ""

def test_skills_wrapped_with_tag(skills_hook, patch_sdk):
    patch_sdk(_skills(("a", "A", False)), enabled=("a",))
    out = skills_hook.hook("SessionStart", {})
    assert out.startswith("<enabled_skills>")
    assert out.rstrip().endswith("</enabled_skills>")

def test_build_system_prompt_skips_when_session_has_inventory(skills_hook, patch_sdk):
    """主对话（context 带 session_id）且会话消息已有清单 → 系统提示不重复注入。"""
    messages = [
        {"role": "user", "_hook_event": "SessionStart", "content": "<enabled_skills>x</enabled_skills>"}
    ]
    patch_sdk(_skills(("a", "A", False)), enabled=("a",), messages=messages)
    assert skills_hook.hook("BuildSystemPrompt", {"session_id": "s1"}) == ""

def test_build_system_prompt_injects_without_session(skills_hook, patch_sdk):
    """子智能体 / Gateway（context 无 session_id）→ 系统提示兜底注入。"""
    patch_sdk(_skills(("a", "A", False)), enabled=("a",), messages=[])
    assert "### a" in skills_hook.hook("BuildSystemPrompt", {})

def test_subagents_only_on_session_start(subagents_hook, monkeypatch):
    """子智能体清单只在 SessionStart 注入；其余事件返回空。"""

    class _Agent:
        name = "explore"
        description = "探索"

    class _Manager:
        def list_subagents(self, include_hidden=True):
            return [_Agent()]

    import app.plugins.sdk as sdk

    monkeypatch.setattr(sdk, "caps_for", lambda cap_id, surface="": _Manager())
    out = subagents_hook.hook("SessionStart", {})
    assert "**explore**" in out
    assert subagents_hook.hook("BuildSystemPrompt", {}) == ""
