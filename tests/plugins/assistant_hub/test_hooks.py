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

    def list_assistants_sorted_by_stable(self):
        return [_A()]

    def identity_and_persona(self, aid):
        return "# 小狐\n\n你是 小狐——马丁的专属 AI 助手。"

    def read_pinned(self, aid):
        return [("pin-1", "用户喜欢简洁回复")]

    def identity_block(self, aid):
        a = self.get(aid)
        persona = self.identity_and_persona(aid)
        if not persona.strip():
            return ""
        header = f"# 助手：{a.name or a.id}\n\n你是 {a.name or a.id}"
        return header + "\n\n" + persona.strip()

    def memory_system_block(self, aid, workdir=""):
        a = self.get(aid)
        if a is None or not a.memory_enabled:
            return ""
        return (
            "<memory_system>\n\n# 第一层 — 全局记忆\n文件：hub/xiaohu-x1/MEMORY.md\n\n"
            "# 第二层 — 项目记忆\n目录：.drifox/memory/xiaohu-x1/\n\n</memory_system>"
            "\n\n<global_memory>\n用户喜欢简洁回复\n</global_memory>"
            "<project_memory>\n项目：DriFox 重构中\n</project_memory>"
            "<daily_logs>\n- 2026-10-13（120 字）\n</daily_logs>"
        )

    def pinned_block(self, aid):
        return "# 人工提示\n\n以下是用户人工添加的明确要求，直接遵守即可。\n\n- 用户喜欢简洁回复"


def _patch_mgr(monkeypatch):
    mgr = _Mgr()
    monkeypatch.setattr(m, "_get_manager", lambda: mgr)
    return mgr


def test_hook_injects_identity_and_memory(monkeypatch):
    """BuildSystemPrompt hook：身份块 + 记忆体系块（说明书/全局/项目/日志）全进系统提示词。"""
    _patch_mgr(monkeypatch)
    context = {"current_role": "primary", "agent_identity_content": "原build提示词", "project_root": "/proj"}
    out = m.hook("BuildSystemPrompt", context)
    assert out and "小狐" in out
    assert "# 助手：小狐" in out
    assert "memory_system" in out and "第一层" in out
    assert "<global_memory>" in out and "简洁回复" in out
    assert "<project_memory>" in out and "DriFox 重构中" in out
    assert "<daily_logs>" in out
    assert context["agent_identity_content"] == ""  # 置空防重复注入


def test_hook_non_primary_noop(monkeypatch):
    _patch_mgr(monkeypatch)
    context = {"current_role": "subagent"}
    assert m.hook("BuildSystemPrompt", context) == ""


def test_hook_memory_disabled_no_memory_block(monkeypatch):
    """助手记忆关闭：memory_system_block 返回空，身份块保留。"""
    mgr = _patch_mgr(monkeypatch)

    class _A2(_A):
        memory_enabled = False

    mgr.get = lambda aid: _A2()
    mgr.memory_system_block = lambda aid, workdir="": ""
    out = m.hook("BuildSystemPrompt", {"current_role": "primary", "project_root": "/proj"})
    assert "小狐" in out and "memory_system" not in out


def test_on_session_start_injects_pinned_only(monkeypatch):
    """SessionStart hook：只注入人工提示（记忆走系统提示词单一通道）。"""
    _patch_mgr(monkeypatch)
    out = m.on_session_start("SessionStart", {"state": "startup"})
    assert out and "人工提示" in out and "简洁回复" in out
    # 记忆内容不进会话消息
    assert "memory_system" not in out and "global_memory" not in out
    assert "# 助手：" not in out


def test_on_session_start_no_active_assistant(monkeypatch):
    mgr = _patch_mgr(monkeypatch)
    mgr.active_id = lambda: ""
    assert m.on_session_start("SessionStart", {}) == ""


def test_on_session_start_uses_session_override(monkeypatch):
    """会话 override 优先：临时助手无 pinned 时返回空串，不回落到主助手。"""
    mgr = _patch_mgr(monkeypatch)
    mgr.session_overrides["s2"] = "temp-1"
    mgr.pinned_block = lambda aid: "" if aid == "temp-1" else "# 人工提示\n\n- x"
    out = m.on_session_start("SessionStart", {"state": "compact", "session_id": "s2"})
    assert out == ""


def test_on_session_start_mgr_unavailable(monkeypatch):
    monkeypatch.setattr(m, "_get_manager", lambda: None)
    assert m.on_session_start("SessionStart", {}) == ""


def test_on_pre_user_mention_switches(monkeypatch):
    """@提及：命中助手名 → 设置会话 override 并失效 prompt 缓存。"""
    mgr = _patch_mgr(monkeypatch)
    invalidated = []
    mgr.set_session_override = lambda sid, aid: (mgr.session_overrides.update({sid: aid}), True)[1]
    mgr._invalidate_session_prompt = lambda sid: invalidated.append(sid)
    out = m.on_pre_user("PreUserMessage", {"current_role": "primary", "message": "让 @小狐 看看", "session_id": "s1"})
    assert out == ""
    assert mgr.session_overrides["s1"] == "xiaohu-x1"
    assert invalidated == ["s1"]


def test_on_pre_user_no_mention(monkeypatch):
    mgr = _patch_mgr(monkeypatch)
    out = m.on_pre_user("PreUserMessage", {"current_role": "primary", "message": "普通消息", "session_id": "s1"})
    assert out == "" and mgr.session_overrides == {}


def test_on_stop_records_map(monkeypatch):
    mgr = _patch_mgr(monkeypatch)
    out = m.on_stop("Stop", {"current_role": "primary", "session_id": "s1"})
    assert out == "" and mgr.session_map == {"s1": "xiaohu-x1"}
    # 无活跃助手：不异常、不记录
    mgr.active_id = lambda: ""
    assert m.on_stop("Stop", {"current_role": "primary", "session_id": "s2"}) == ""
    assert "s2" not in mgr.session_map


def test_on_stop_uses_session_override(monkeypatch):
    mgr = _patch_mgr(monkeypatch)
    mgr.session_overrides["s2"] = "b-1"
    out = m.on_stop("Stop", {"current_role": "primary", "session_id": "s2"})
    assert out == "" and mgr.session_map == {"s2": "b-1"}


def test_on_stop_non_primary_noop(monkeypatch):
    mgr = _patch_mgr(monkeypatch)
    out = m.on_stop("Stop", {"current_role": "subagent", "session_id": "s1"})
    assert out == "" and mgr.session_map == {}

# ── @ 提及边界：与输入框胶囊还原同口径 ──

class _Named:
    memory_enabled = True

    def __init__(self, name, aid):
        self.name = name
        self.id = aid

def _multi_mgr(monkeypatch, assistants):
    mgr = _Mgr()
    mgr.list_assistants_sorted_by_stable = lambda: list(assistants)
    monkeypatch.setattr(m, "_get_manager", lambda: mgr)
    switched = {}
    mgr.set_session_override = lambda sid, aid: (switched.__setitem__(sid, aid), True)[1]
    mgr._invalidate_session_prompt = lambda sid: None
    return mgr, switched

def test_mention_after_chinese_char_switches(monkeypatch):
    """中文紧邻 @（「问下@小狐」）也要切换——旧实现要求 @ 前为空白/串首。"""
    _, switched = _multi_mgr(monkeypatch, [_Named("小狐", "xiaohu-x1")])
    m.on_pre_user("PreUserMessage", {"current_role": "primary", "message": "问下@小狐，这个怎么改", "session_id": "s1"})
    assert switched == {"s1": "xiaohu-x1"}

def test_mention_email_like_not_matched(monkeypatch):
    """邮箱/路径里的 @ 不算提及（前边界为 ASCII 字母数字）。"""
    _, switched = _multi_mgr(monkeypatch, [_Named("小狐", "xiaohu-x1")])
    m.on_pre_user("PreUserMessage", {"current_role": "primary", "message": "mail a@小狐 无效", "session_id": "s1"})
    assert switched == {}

def test_mention_long_name_priority(monkeypatch):
    """长名优先：@小狐助手 不应被前缀名「小狐」抢走。"""
    _, switched = _multi_mgr(
        monkeypatch,
        [_Named("小狐", "xiaohu-x1"), _Named("小狐助手", "xiaohu-pro")],
    )
    m.on_pre_user("PreUserMessage", {"current_role": "primary", "message": "@小狐助手 看下", "session_id": "s1"})
    assert switched == {"s1": "xiaohu-pro"}

def test_mention_name_followed_by_chinese_not_matched(monkeypatch):
    """名字后紧跟中文（无分隔）不匹配——与输入框胶囊还原的后边界口径一致。"""
    _, switched = _multi_mgr(monkeypatch, [_Named("小狐", "xiaohu-x1")])
    m.on_pre_user("PreUserMessage", {"current_role": "primary", "message": "@小狐帮我看看", "session_id": "s1"})
    assert switched == {}
