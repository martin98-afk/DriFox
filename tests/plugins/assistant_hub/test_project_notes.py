# -*- coding: utf-8 -*-
"""test_project_notes.py — project_notes hook 开关解析测试（独立加载场景）。

回归覆盖：_enabled_for_primary 会话级临时助手 override 优先（原 bug：只读
主助手开关，@临时助手后项目笔记/项目上下文开关失效）。
"""

import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_MODULE = _ROOT / "plugins" / "assistant_hub" / "hooks" / "project_notes.py"

spec = importlib.util.spec_from_file_location("test_project_notes_mod", str(_MODULE))
m = importlib.util.module_from_spec(spec)
sys.modules.setdefault("test_project_notes_mod", m)
spec.loader.exec_module(m)


class _A:
    project_notes_enabled = True
    project_context_enabled = True


class _Mgr:
    """假 manager：overrides[sid]=aid → 该助手开关全关；否则主助手全开。"""

    def __init__(self):
        self.session_overrides = {}

    def active_id(self):
        return "main-1"

    def has(self, aid):
        return bool(aid)

    def get_session_override(self, sid):
        return self.session_overrides.get(sid, "")

    def get(self, aid):
        a = _A()
        if aid == "temp-1":
            a.project_notes_enabled = False
            a.project_context_enabled = False
        return a


def _patch_mgr(monkeypatch, mgr):
    monkeypatch.setattr(m, "_get_manager", lambda: mgr)


def test_subagent_always_allowed(monkeypatch):
    """子智能体始终注入，不受开关控制。"""
    _patch_mgr(monkeypatch, _Mgr())
    ctx = {"current_role": "subagent", "session_id": "s1"}
    assert m._enabled_for_primary(ctx, "project_notes_enabled") is True


def test_primary_no_session_reads_active_switch(monkeypatch):
    """无 session_id：读主助手开关（原行为保持）。"""
    mgr = _Mgr()
    _patch_mgr(monkeypatch, mgr)
    assert m._enabled_for_primary({"current_role": "primary"}, "project_notes_enabled") is True
    mgr.session_overrides["s1"] = "temp-1"  # 有 override 但无 session_id，不影响
    assert m._enabled_for_primary({"current_role": "primary"}, "project_notes_enabled") is True


def test_primary_uses_session_override_switch(monkeypatch):
    """会话 override 优先：临时助手关了开关 → 不注入（回归主 bug）。"""
    mgr = _Mgr()
    mgr.session_overrides["s1"] = "temp-1"
    _patch_mgr(monkeypatch, mgr)
    ctx = {"current_role": "primary", "session_id": "s1"}
    assert m._enabled_for_primary(ctx, "project_notes_enabled") is False
    assert m._enabled_for_primary(ctx, "project_context_enabled") is False
    # 其他会话无 override → 主助手开关，仍注入
    other = {"current_role": "primary", "session_id": "s2"}
    assert m._enabled_for_primary(other, "project_notes_enabled") is True


def test_primary_empty_override_falls_back_to_active(monkeypatch):
    """override 为空（未 @ 过 / 已清除）：回落主助手开关。"""
    mgr = _Mgr()
    _patch_mgr(monkeypatch, mgr)
    ctx = {"current_role": "primary", "session_id": "s1"}
    assert m._enabled_for_primary(ctx, "project_notes_enabled") is True


def test_mgr_unavailable_returns_false(monkeypatch):
    """manager 不可用：不允许注入，不抛异常。"""
    monkeypatch.setattr(m, "_get_manager", lambda: None)
    assert m._enabled_for_primary({"current_role": "primary"}, "project_notes_enabled") is False


def test_hook_notes_respects_override(monkeypatch, tmp_path):
    """hook_notes 端到端：@临时助手（开关关）→ 返回空串，不注入项目笔记。"""
    mgr = _Mgr()
    mgr.session_overrides["s1"] = "temp-1"
    _patch_mgr(monkeypatch, mgr)
    (tmp_path / "AGENTS.md").write_text("# 项目开发规范\n测试内容", encoding="utf-8")
    ctx = {
        "current_role": "primary",
        "session_id": "s1",
        "project_root": str(tmp_path),
        "project_name": tmp_path.name,
    }
    assert m.hook_notes("BuildSystemPrompt", ctx) == ""
