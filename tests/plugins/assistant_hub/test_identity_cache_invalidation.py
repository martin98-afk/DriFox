# -*- coding: utf-8 -*-
"""test_identity_cache_invalidation.py — 身份缓存失效与工具档位 session_id 穿透回归。

2026-09-22 三症状根因回归：
1. 切主助手（set_primary）后已打开会话的身份行仍显示旧助手（需新建会话才恢复）
2. 会话级临时助手（@提及）切换后身份行不跟随
3. 临时助手的工具档位不生效（engine 走 get_agent_tools_schema 时 session_id 断链，
   schema 过滤器恒收到空 sid → 永远按主助手档位过滤）
"""

import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_MODULE = _ROOT / "plugins" / "assistant_hub" / "assistant_manager.py"
_AGENT = _ROOT / "app" / "core" / "conversation" / "agent.py"

from app.core.infra import message_identity

spec = importlib.util.spec_from_file_location("assistant_hub_manager_test_identity", str(_MODULE))
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)


def _fresh_manager(tmp_path):
    m.AssistantManager.reset_instance()
    return m.AssistantManager.get_instance(root_dir=str(tmp_path / "hub"))


def _prime_cache(session_id: str) -> tuple:
    """往会话级身份缓存塞一条旧助手身份，模拟「该会话已用旧身份渲染过」。"""
    message_identity._IDENTITY_CACHE[(session_id, "assistant", "")] = message_identity.MessageIdentity(
        name="旧助手", avatar=""
    )
    return (session_id, "assistant", "")


def test_set_session_override_clears_identity_cache(tmp_path, monkeypatch):
    inst = _fresh_manager(tmp_path)
    inst.create("主助手")
    a2 = inst.create("临时助手")

    sid = "sess-cache-1"
    key = _prime_cache(sid)
    assert inst.set_session_override(sid, a2.id) is True
    # override 变化必须整表失效：旧助手身份不得残留
    assert key not in message_identity._IDENTITY_CACHE
    message_identity.clear_cache()

    # override 未变化（重复设置同 aid）→ 返回 False，缓存不动
    _prime_cache(sid)
    assert inst.set_session_override(sid, a2.id) is False
    assert key in message_identity._IDENTITY_CACHE
    message_identity.clear_cache()


def test_set_primary_clears_identity_cache(tmp_path, monkeypatch):
    inst = _fresh_manager(tmp_path)
    a1 = inst.create("旧主")
    a2 = inst.create("新主")
    monkeypatch.setattr(inst, "_write_yaml", lambda a: None)
    monkeypatch.setattr(type(inst), "_invalidate_session_prompt_caches", lambda *a, **k: None)

    key = _prime_cache("sess-any")
    assert inst.set_primary(a2.id) is True
    assert key not in message_identity._IDENTITY_CACHE
    assert a2.primary and not a1.primary
    message_identity.clear_cache()

    # 重复设置同一主助手 → changed=False（仅缓存清理被跳过；返回值恒为成功 True）
    _prime_cache("sess-any")
    assert inst.set_primary(a2.id) is True
    assert key in message_identity._IDENTITY_CACHE
    message_identity.clear_cache()


def test_get_agent_tools_schema_passes_session_id(monkeypatch):
    """engine 的 agent 分支调用必须把 session_id 带到 get_builtin_tools_schema。"""
    aspec = importlib.util.spec_from_file_location("agent_mod_test_identity", _AGENT)
    agent_mod = importlib.util.module_from_spec(aspec)
    sys.modules[aspec.name] = agent_mod
    aspec.loader.exec_module(agent_mod)

    captured = {}

    def _fake_gbs(agent_manager, builtin_tools=None, session_id=""):
        captured["session_id"] = session_id
        return []

    monkeypatch.setattr(agent_mod, "get_builtin_tools_schema", _fake_gbs)

    mgr = agent_mod.AgentManager.__new__(agent_mod.AgentManager)
    mgr._builtin_tools = None
    monkeypatch.setattr(mgr, "get_agent", lambda name: object())
    mgr.get_agent_tools_schema("build", session_id="sess-xyz")
    assert captured.get("session_id") == "sess-xyz"

    # 缺省为空（兼容子智能体等旧调用点，回落主助手档位）
    mgr.get_agent_tools_schema("build")
    assert captured.get("session_id") == ""
