# -*- coding: utf-8 -*-
"""handoff 插件工具：switch=true 时写 pending 并登记三个动作"""

import importlib.util
import json
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
        "## 任务目标\nX",
        switch=True,
        tool_ctx={"workdir": str(tmp_path), "session_id": "s1", "env": {"app_data_dir": str(tmp_path)}},
    )

    assert res.success is True
    assert res.end_turn is True  # 插件通用契约：写完即本轮收尾
    # 待注入内容走文件通道（不依赖窗口级 state.kv，SessionStart 期间 host 常不可用）
    pending_file = tmp_path / "plugin_data" / "system-handoff" / "pending.json"
    assert pending_file.is_file()
    assert json.loads(pending_file.read_text(encoding="utf-8"))["source_session_id"] == "s1"
    assert "handoff_active" in caps.kv  # 只留防重入标记
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
    assert res.end_turn is False  # 只暂存不切会话，本轮继续
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


def test_continue_prompt_falls_back_to_goal_headline():
    """会话标题还是占位（"新对话"）时，从交接正文的任务目标首行动态取回退"""
    mod = _load_tool()
    body = "## 任务目标\n发布 DriFox v0.6.11（GitHub Release + CI）\n\n## 已完成\n- x"
    assert "发布 DriFox v0.6.11" in mod._continue_prompt("新对话", body)
    assert mod._continue_prompt("", body).startswith("继续完成「发布 DriFox v0.6.11")
    # 都没有 → 纯固定文案，不留空标题
    assert mod._continue_prompt("", "").startswith("根据交接文档继续未完成的工作")


def test_manifest_declares_used_capabilities():
    """能力声明守卫：漏声明 session.read 会让标题动态注入静默失效（capabilities 裁剪）"""
    import json

    manifest = _ROOT / "plugins" / "system-handoff" / ".drifox-plugin" / "plugin.json"
    caps = set(json.loads(manifest.read_text(encoding="utf-8"))["capabilities"])
    assert {"session.interrupt", "session.new", "session.send", "session.read", "storage.data_dir"} <= caps


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

def test_prefers_tool_pool_over_global_caps(tmp_path, monkeypatch):
    """能力池优先：caps_for 的 tool host 是「最后创建的主窗口」，多标签下会指错窗口。

    工具池由 ToolExecutor 建（host=本窗口执行器），window_id 天然正确。这里断言
    动作全部取自 tool_ctx["capabilities"]，且完全不回退到全局 caps_for。
    """
    mod = _load_tool()
    actions = []

    def _accept(name):
        def _fn(**kw):
            actions.append((name, kw.get("text", "")))
            return True

        return _fn

    pool = {
        "state.kv": {"get": lambda k, d=None: None, "set": lambda k, v: None, "delete": lambda k: None},
        "session.read": lambda: {"name": "池里的标题"},
        "session.interrupt": _accept("session.interrupt"),
        "session.new": _accept("session.new"),
        "session.send": _accept("session.send"),
    }

    def _boom(cap_id):
        raise AssertionError(f"不应回退到全局 caps_for: {cap_id}")

    monkeypatch.setattr(mod, "caps_for", _boom)
    res = mod._update_handoff_impl(
        "## 任务目标\nX",
        switch=True,
        tool_ctx={
            "workdir": str(tmp_path),
            "session_id": "s1",
            "env": {"app_data_dir": str(tmp_path)},
            "capabilities": pool,
        },
    )

    assert res.success is True
    assert res.end_turn is True
    assert [a[0] for a in actions] == ["session.interrupt", "session.new", "session.send"]
    assert "池里的标题" in actions[2][1]

def test_enqueue_rejected_keeps_turn_and_reports(tmp_path, monkeypatch):
    """入队被拒（window_id 缺失 / 去重 / 正在 drain）不能谎报「随后开新会话继续」。

    静默吞掉会让本轮 end_turn 收尾而会话没切走，用户只看到对话停住，
    这正是交接「只中断不新建」的现场。
    """
    mod = _load_tool()

    class _RejectingCaps(_Caps):
        def __call__(self, cap_id):
            if cap_id == "session.interrupt":
                return lambda **kw: False  # enqueue 被拒
            return super().__call__(cap_id)

    caps = _RejectingCaps()
    monkeypatch.setattr(mod, "caps_for", caps)
    res = mod._update_handoff_impl(
        "## 任务目标\nX",
        switch=True,
        tool_ctx={"workdir": str(tmp_path), "session_id": "s1", "env": {"app_data_dir": str(tmp_path)}},
    )

    assert res.success is True
    assert res.end_turn is False, "入队失败必须继续本轮，否则会话不切却静默收尾"
    assert "session.interrupt" in str(res.data.get("handoff_switch_error"))
    assert "切会话失败" in str(res.content)
    assert caps.actions == []
