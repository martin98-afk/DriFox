# -*- coding: utf-8 -*-
"""test_hooks.py — inject_assistant hook 测试（独立加载场景）。"""
import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_MODULE = _ROOT / "plugins" / "assistant_hub" / "hooks" / "inject_assistant.py"

spec = importlib.util.spec_from_file_location("test_hooks_mod", str(_MODULE))
m = importlib.util.module_from_spec(spec)
sys.modules.setdefault("test_hooks_mod", m)
spec.loader.exec_module(m)


class _A:
    name = "小狐"
    id = "xiaohu-x1"
    memory_enabled = True
    experience_enabled = False


class _Mgr:
    """假 manager：固定 active 助手与注入内容。"""

    last_on_stop = 0

    def active_id(self):
        return "xiaohu-x1"

    def has(self, aid):
        return bool(aid)

    def get(self, aid):
        return _A()

    def identity_and_persona(self, aid):
        return "# 小狐\n\n你是 小狐——马丁的专属 AI 助手。"

    def read_pinned(self, aid):
        return [("pin-1", "用户喜欢简洁回复")]

    def compiled_memory(self, aid):
        return "## 今日\n\n- 在开发助手中心"

    def experience_read_index(self, aid):
        return "# 经验索引"


def _patch_mgr(monkeypatch):
    mgr = _Mgr()
    monkeypatch.setattr(m, "_get_manager", lambda: mgr)
    return mgr


def test_block_contains_persona_memory_rules_pinned(monkeypatch):
    _patch_mgr(monkeypatch)
    block = m._assistant_prompt_block("xiaohu-x1")
    assert "# 小狐" in block
    assert "记忆使用规则" in block  # 无声记忆规则
    assert "置顶记忆" in block and "用户喜欢简洁回复" in block
    assert "今日" in block
    # 经验不注入 prompt（渐进式披露走工具）
    assert "经验索引" not in block


def test_block_memory_disabled(monkeypatch):
    mgr = _patch_mgr(monkeypatch)

    class _A2(_A):
        memory_enabled = False

    mgr.get = lambda aid: _A2()
    block = m._assistant_prompt_block("xiaohu-x1")
    assert "记忆使用规则" not in block
    assert "小狐" in block  # persona 段仍在


def test_hook_replaces_identity_context(monkeypatch):
    _patch_mgr(monkeypatch)
    context = {"current_role": "primary", "agent_identity_content": "原build提示词"}
    out = m.hook("BuildSystemPrompt", context)
    assert out and "小狐" in out
    assert context["agent_identity_content"] == ""  # 置空防重复注入


def test_hook_non_primary_noop(monkeypatch):
    _patch_mgr(monkeypatch)
    context = {"current_role": "subagent"}
    assert m.hook("BuildSystemPrompt", context) == ""


def test_on_stop_counts_turn(monkeypatch):
    mgr = _patch_mgr(monkeypatch)
    calls = []

    class _Ticker:
        @staticmethod
        def on_turn_finished():
            calls.append(1)

    monkeypatch.setattr(m, "_get_ticker", lambda mgr_: _Ticker())
    out = m.on_stop("Stop", {"current_role": "primary", "session_id": "s1"})
    assert out == "" and len(calls) == 1
    # 无活跃助手：不异常
    mgr.active_id = lambda: ""
    assert m.on_stop("Stop", {"current_role": "primary"}) == ""
