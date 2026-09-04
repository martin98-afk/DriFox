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
    skills_enabled = True


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

    def enabled_skills(self, aid):
        return [
            {"name": "drifox-dev", "description": "DriFox 开发规范", "path": "/tmp/skills/drifox-dev.md"}
        ]

    def experience_read_index(self, aid):
        return "# 经验索引"

    def prompt_block(self, aid):
        """模拟 manager.prompt_block 组装语义（真组装在 test_manager_ext 用真 manager 测）。"""
        a = self.get(aid)
        parts = []
        persona = self.identity_and_persona(aid)
        if persona.strip():
            parts.append(persona.strip())
        pin_lines = [f"- {(c or '').strip()}" for _pid, c in self.read_pinned(aid) if (c or "").strip()]
        if pin_lines:
            parts.append("# 人工提示\n\n以下是用户人工添加的明确要求，直接遵守即可。\n\n" + "\n".join(pin_lines))
        if a.memory_enabled:
            parts += ["## 记忆使用规则", "# 长期记忆\n\n" + self.compiled_memory(aid)]
        skills = self.enabled_skills(aid)
        if skills:
            lines = [f"- {s['name']}：{s.get('description') or ''}（{s['path']}）" for s in skills]
            parts.append("# 助手技能\n\n先用 read 工具读取技能全文再执行：\n" + "\n".join(lines))
        header = f"# 助手：{a.name or a.id}\n\n你是 {a.name or a.id}"
        return header + "\n\n" + "\n\n".join(parts)


def _patch_mgr(monkeypatch):
    mgr = _Mgr()
    monkeypatch.setattr(m, "_get_manager", lambda: mgr)
    return mgr


def test_block_contains_persona_memory_rules_pinned(monkeypatch):
    _patch_mgr(monkeypatch)
    block = m._assistant_prompt_block("xiaohu-x1")
    assert "# 小狐" in block
    assert "记忆使用规则" in block  # 无声记忆规则
    assert "人工提示" in block and "用户喜欢简洁回复" in block
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
    assert "人工提示" in block and "用户喜欢简洁回复" in block  # 人工提示不受记忆开关控制
    assert "小狐" in block  # persona 段仍在
    assert "今日" not in block  # memory.md 不注入


def test_block_contains_skill_section(monkeypatch):
    """技能段（渐进披露）：name+简介+路径入 prompt，正文不入（模型用 read 读盘）。"""
    _patch_mgr(monkeypatch)
    block = m._assistant_prompt_block("xiaohu-x1")
    assert "# 助手技能" in block
    assert "drifox-dev" in block and "DriFox 开发规范" in block
    assert "read" in block  # 引导模型用 read 工具读全文


def test_block_skills_disabled(monkeypatch):
    mgr = _patch_mgr(monkeypatch)
    mgr.enabled_skills = lambda aid: []  # 总开关关/全部过滤
    block = m._assistant_prompt_block("xiaohu-x1")
    assert "# 助手技能" not in block


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
