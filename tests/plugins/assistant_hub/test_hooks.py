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

    def __init__(self):
        self.session_overrides = {}
        self.session_map = {}

    def active_id(self):
        return "xiaohu-x1"

    def has(self, aid):
        return bool(aid)

    def get_session_override(self, sid):
        return self.session_overrides.get(sid, "")

    def record_session_aid(self, sid, aid):
        self.session_map[sid] = aid

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

    def identity_block(self, aid):
        """模拟 manager.identity_block（BuildSystemPrompt hook 注入系统提示词）。"""
        a = self.get(aid)
        persona = self.identity_and_persona(aid)
        if not persona.strip():
            return ""
        header = f"# 助手：{a.name or a.id}\n\n你是 {a.name or a.id}"
        return header + "\n\n" + persona.strip()

    def memory_block(self, aid):
        """模拟 manager.memory_block（SessionStart hook 注入会话消息）。"""
        a = self.get(aid)
        parts = []
        pin_lines = [f"- {(c or '').strip()}" for _pid, c in self.read_pinned(aid) if (c or "").strip()]
        if pin_lines:
            parts.append("# 人工提示\n\n以下是用户人工添加的明确要求，直接遵守即可。\n\n" + "\n".join(pin_lines))
        if a.memory_enabled:
            parts += ["## 记忆使用规则", "# 长期记忆\n\n" + self.compiled_memory(aid)]
        return "\n\n".join(parts)


def _patch_mgr(monkeypatch):
    mgr = _Mgr()
    monkeypatch.setattr(m, "_get_manager", lambda: mgr)
    return mgr


def test_identity_block_persona_only(monkeypatch):
    """BuildSystemPrompt hook 用的 identity_block：人格段 + header，不含记忆。"""
    _patch_mgr(monkeypatch)
    block = m._identity_block("xiaohu-x1")
    assert block.startswith("# 助手：小狐")
    assert "小狐" in block  # persona 段
    # 关键：记忆已迁出系统提示词
    assert "记忆使用规则" not in block
    assert "人工提示" not in block
    assert "今日" not in block
    assert "经验索引" not in block  # 经验不注入 prompt（渐进式披露走工具）


def test_memory_block_pinned_rules_memory(monkeypatch):
    """SessionStart hook 用的 memory_block：人工提示 + 记忆规则 + 长期记忆。"""
    _patch_mgr(monkeypatch)
    block = m._memory_block("xiaohu-x1")
    assert block  # 非空
    assert "人工提示" in block and "用户喜欢简洁回复" in block
    assert "记忆使用规则" in block
    assert "今日" in block
    # 不含人格 header（人格在 BuildSystemPrompt 注入）
    assert "# 助手：" not in block


def test_memory_block_disabled_no_rules(monkeypatch):
    """memory_enabled=False：memory_block 不输出记忆规则 + 长期记忆，但人工提示仍保留。"""
    mgr = _patch_mgr(monkeypatch)

    class _A2(_A):
        memory_enabled = False

    mgr.get = lambda aid: _A2()
    block = m._memory_block("xiaohu-x1")
    assert "记忆使用规则" not in block
    assert "今日" not in block
    assert "人工提示" in block and "用户喜欢简洁回复" in block  # 不受开关控制


def test_hook_replaces_identity_context(monkeypatch):
    """BuildSystemPrompt hook：注入人格块到 system prompt，置空预取防重复。"""
    _patch_mgr(monkeypatch)
    context = {"current_role": "primary", "agent_identity_content": "原build提示词"}
    out = m.hook("BuildSystemPrompt", context)
    assert out and "小狐" in out
    # 关键：BuildSystemPrompt hook 输出不再含记忆
    assert "记忆使用规则" not in out
    assert "人工提示" not in out
    assert context["agent_identity_content"] == ""  # 置空防重复注入


def test_hook_non_primary_noop(monkeypatch):
    _patch_mgr(monkeypatch)
    context = {"current_role": "subagent"}
    assert m.hook("BuildSystemPrompt", context) == ""


def test_on_session_start_returns_memory(monkeypatch):
    """SessionStart hook：注入人工提示 + 记忆块到 session.messages（不进系统提示词）。"""
    _patch_mgr(monkeypatch)
    out = m.on_session_start("SessionStart", {"state": "startup"})
    assert out
    assert "人工提示" in out and "用户喜欢简洁回复" in out
    assert "记忆使用规则" in out
    assert "今日" in out
    # 不含人格 header（人格由 BuildSystemPrompt 提供）
    assert "# 助手：" not in out


def test_on_session_start_no_active_assistant(monkeypatch):
    """无激活助手：SessionStart hook 返回空串，不报错。"""
    mgr = _patch_mgr(monkeypatch)
    mgr.active_id = lambda: ""
    assert m.on_session_start("SessionStart", {}) == ""


def test_on_session_start_mgr_unavailable(monkeypatch):
    """manager 不可用：返回空串，不抛异常。"""
    monkeypatch.setattr(m, "_get_manager", lambda: None)
    assert m.on_session_start("SessionStart", {}) == ""


def test_on_stop_counts_turn(monkeypatch):
    mgr = _patch_mgr(monkeypatch)
    calls = []

    class _Ticker:
        @staticmethod
        def on_turn_finished(aid):
            calls.append(aid)

    monkeypatch.setattr(m, "_get_ticker", lambda mgr_: _Ticker())
    out = m.on_stop("Stop", {"current_role": "primary", "session_id": "s1"})
    assert out == "" and calls == ["xiaohu-x1"]  # 无 override → 主助手
    assert mgr.session_map == {"s1": "xiaohu-x1"}  # 归属已记录
    # 无活跃助手：不异常
    mgr.active_id = lambda: ""
    assert m.on_stop("Stop", {"current_role": "primary"}) == ""


def test_on_stop_uses_session_override(monkeypatch):
    """临时助手会话：轮次计入临时助手并记录归属，不落主助手。"""
    mgr = _patch_mgr(monkeypatch)
    mgr.session_overrides["s2"] = "b-1"
    calls = []

    class _Ticker:
        @staticmethod
        def on_turn_finished(aid):
            calls.append(aid)

    monkeypatch.setattr(m, "_get_ticker", lambda mgr_: _Ticker())
    out = m.on_stop("Stop", {"current_role": "primary", "session_id": "s2"})
    assert out == "" and calls == ["b-1"]
    assert mgr.session_map == {"s2": "b-1"}


def test_on_stop_non_primary_noop(monkeypatch):
    mgr = _patch_mgr(monkeypatch)
    calls = []

    class _Ticker:
        @staticmethod
        def on_turn_finished(aid):
            calls.append(aid)

    monkeypatch.setattr(m, "_get_ticker", lambda mgr_: _Ticker())
    out = m.on_stop("Stop", {"current_role": "subagent", "session_id": "s1"})
    assert out == "" and calls == []
    assert mgr.session_map == {}
