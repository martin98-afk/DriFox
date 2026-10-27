# -*- coding: utf-8 -*-
"""handoff 插件工具：switch=true 时写 pending 并登记三个动作"""

import importlib.util
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def _load_tool():
    p = _ROOT / "plugins" / "system-handoff" / "tools" / "update_handoff.py"
    spec = importlib.util.spec_from_file_location("handoff_tool_under_test", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Caps:
    """caps_for 替身：state.kv 进 dict，session.* 记 actions"""

    def __init__(self):
        self.kv = {}
        self.actions = []
        self.kv_api = {
            "get": self.kv.get,
            "set": lambda k, v: self.kv.__setitem__(k, v),
            "delete": self.kv.pop,
        }

    def __call__(self, cap_id):
        if cap_id == "state.kv":
            return self.kv_api
        if cap_id in ("session.interrupt", "session.new", "session.send"):
            return lambda **kw: self.actions.append(cap_id)
        return None


def test_switch_true_writes_pending_and_enqueues(tmp_path, monkeypatch):
    mod = _load_tool()
    caps = _Caps()
    monkeypatch.setattr(mod, "caps_for", caps)

    res = mod._update_handoff_impl(
        "## 任务目标\nX", switch=True, tool_ctx={"workdir": str(tmp_path), "session_id": "s1"}
    )

    assert res.success is True
    assert caps.kv["handoff_pending"]["source_session_id"] == "s1"
    assert caps.kv["handoff_pending"]["content"] == "## 任务目标\nX"
    assert "handoff_active" in caps.kv
    assert caps.actions == ["session.interrupt", "session.new", "session.send"]
    assert (tmp_path / ".drifox" / "handoff" / "s1.md").is_file()


def test_switch_false_only_writes(tmp_path, monkeypatch):
    mod = _load_tool()
    caps = _Caps()
    monkeypatch.setattr(mod, "caps_for", caps)

    res = mod._update_handoff_impl(
        "## 任务目标\nX", switch=False, tool_ctx={"workdir": str(tmp_path), "session_id": "s1"}
    )

    assert res.success is True
    assert caps.kv == {}
    assert caps.actions == []


def test_empty_content_rejected(tmp_path, monkeypatch):
    mod = _load_tool()
    monkeypatch.setattr(mod, "caps_for", _Caps())
    res = mod._update_handoff_impl("   ", switch=True, tool_ctx={"workdir": str(tmp_path), "session_id": "s1"})
    assert res.success is False


def test_missing_session_rejected(tmp_path, monkeypatch):
    mod = _load_tool()
    monkeypatch.setattr(mod, "caps_for", _Caps())
    res = mod._update_handoff_impl("## 任务目标\nX", switch=True, tool_ctx={"workdir": str(tmp_path)})
    assert res.success is False


def test_send_text_carries_source_title(tmp_path, monkeypatch):
    mod = _load_tool()

    class _Caps2(_Caps):
        def __call__(self, cap_id):
            if cap_id == "session.read":
                return lambda: {"name": "修复登录 bug"}
            return super().__call__(cap_id)

    caps = _Caps2()
    monkeypatch.setattr(mod, "caps_for", caps)
    sent = []
    orig = caps.__call__

    def _call(cap_id):
        if cap_id == "session.send":
            return lambda **kw: sent.append(kw.get("text", ""))
        return orig(cap_id)

    monkeypatch.setattr(mod, "caps_for", _call)
    mod._update_handoff_impl("## 任务目标\\nX", switch=True, tool_ctx={"workdir": str(tmp_path), "session_id": "s1"})
    assert sent and "继续完成「修复登录 bug」会话的交接任务" in sent[0]
