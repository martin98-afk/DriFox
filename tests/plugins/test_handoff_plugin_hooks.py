# -*- coding: utf-8 -*-
"""handoff 插件 hooks：注入消费 pending、触发防重入

注入通道是文件（`<app_data>/plugin_data/system-handoff/pending.json`）而不是
state.kv：SessionStart 常在窗口创建期触发，那时活跃窗口的 backend.tool_executor
还没建，窗口级 KV 整键缺失 → 注入静默失败（实测全部失败）。
"""

import importlib.util
import json
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def _load_hook(filename: str):
    p = _ROOT / "plugins" / "system-handoff" / "hooks" / filename
    spec = importlib.util.spec_from_file_location(f"handoff_hook_{filename[:-3]}", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _pending_file(tmp_path: Path) -> Path:
    return tmp_path / "plugin_data" / "system-handoff" / "pending.json"


def _write_pending(tmp_path: Path, payload: dict) -> Path:
    p = _pending_file(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return p


class _KV:
    def __init__(self, data=None):
        self.data = dict(data or {})

    def __getitem__(self, key):
        if key == "get":
            return self.data.get
        if key == "set":
            return lambda k, v: self.data.__setitem__(k, v)
        if key == "delete":
            return self.data.pop
        raise KeyError(key)


def test_inject_consumes_pending(tmp_path, monkeypatch):
    mod = _load_hook("inject_handoff.py")
    p = _write_pending(
        tmp_path,
        {"content": "## 任务目标\nX", "ts": time.time(), "source_session_id": "s1"},
    )
    kv = _KV({"handoff_active": {"ts": time.time()}})

    import app.plugins.sdk as sdk

    def _caps(cap):
        if cap == "storage.data_dir":
            return lambda: str(tmp_path)
        if cap == "state.kv":
            return kv
        return None

    monkeypatch.setattr(sdk, "caps_for", _caps)

    out = mod.hook("SessionStart", {})
    assert "## 任务目标" in out
    assert not p.exists(), "读后即清，同一份交接只注入一次"
    assert "handoff_active" not in kv.data, "防重入标记同步清掉"


def test_inject_empty_when_no_pending(tmp_path, monkeypatch):
    mod = _load_hook("inject_handoff.py")
    import app.plugins.sdk as sdk

    monkeypatch.setattr(sdk, "caps_for", lambda cap: (lambda: str(tmp_path)))
    assert mod.hook("SessionStart", {}) == ""


def test_inject_drops_stale_pending(tmp_path, monkeypatch):
    mod = _load_hook("inject_handoff.py")
    p = _write_pending(tmp_path, {"content": "X", "ts": time.time() - 3600, "source_session_id": "s1"})
    import app.plugins.sdk as sdk

    monkeypatch.setattr(sdk, "caps_for", lambda cap: (lambda: str(tmp_path)))
    assert mod.hook("SessionStart", {}) == ""
    assert not p.exists()


def test_inject_degrades_without_storage_cap(monkeypatch):
    """storage.data_dir 取不到（未声明/未注册）时不炸，返回空串"""
    mod = _load_hook("inject_handoff.py")
    import app.plugins.sdk as sdk

    monkeypatch.setattr(sdk, "caps_for", lambda cap: None)
    assert mod.hook("SessionStart", {}) == ""


def _caps_with_data_dir(tmp_path, sent, kv=None):
    """caps mock：storage.data_dir → tmp_path；session.send 记账；state.kv 可选"""
    import app.plugins.sdk as sdk

    def _caps(cap):
        if cap == "storage.data_dir":
            return lambda: str(tmp_path)
        if cap == "session.send":
            return lambda *a, **kw: (sent.append((a, kw)), True)[1]
        if cap == "state.kv":
            return kv
        return None

    return sdk, _caps


def _active_file(tmp_path: Path, key: str) -> Path:
    return tmp_path / "plugin_data" / "system-handoff" / "active" / f"{key}.json"


def test_trigger_skips_when_active(monkeypatch, tmp_path):
    """同一会话已有新鲜标记 → 不重复触发（标记按会话键落文件，不再用窗口级 kv）"""
    mod = _load_hook("auto_compact_trigger.py")
    sent = []
    sdk, caps = _caps_with_data_dir(tmp_path, sent)
    monkeypatch.setattr(sdk, "caps_for", caps)

    ctx = {"token_count": 90, "token_limit": 100, "session_id": "s1", "window_id": "w1"}
    assert mod.hook("PostToolUse", ctx) != ""      # 第一次触发并写标记
    assert mod.hook("PostToolUse", ctx) == ""      # 第二次被标记拦住
    assert len(sent) == 1
    assert _active_file(tmp_path, "s1").is_file()


def test_trigger_marker_is_per_session(monkeypatch, tmp_path):
    """标记按会话隔离：A 会话触发不抑制 B 会话"""
    mod = _load_hook("auto_compact_trigger.py")
    sent = []
    sdk, caps = _caps_with_data_dir(tmp_path, sent)
    monkeypatch.setattr(sdk, "caps_for", caps)

    assert mod.hook("PostToolUse", {"token_count": 90, "token_limit": 100, "session_id": "s1", "window_id": "w1"}) != ""
    assert mod.hook("PostToolUse", {"token_count": 90, "token_limit": 100, "session_id": "s2", "window_id": "w2"}) != ""
    assert len(sent) == 2


def test_trigger_enqueues_when_over_threshold(monkeypatch, tmp_path):
    mod = _load_hook("auto_compact_trigger.py")
    sent = []
    sdk, caps = _caps_with_data_dir(tmp_path, sent)
    monkeypatch.setattr(sdk, "caps_for", caps)

    out = mod.hook(
        "PostToolUse",
        {"token_count": 90, "token_limit": 100, "session_id": "s1", "window_id": "w1"},
    )
    assert "上下文交接" in out          # 提示直接进上下文，模型下一轮就能看到
    assert len(sent) == 1               # 队列里仍留一条兜底
    assert sent[0][1].get("ephemeral") is True
    # 定向到本会话所属窗口；不带就落到活跃窗口（串窗根因）
    assert sent[0][1].get("window_id") == "w1"


def test_trigger_skips_subagent(monkeypatch, tmp_path):
    """子智能体不参与交接：没有自己的会话，触发只会污染父会话文档"""
    mod = _load_hook("auto_compact_trigger.py")
    sent = []
    sdk, caps = _caps_with_data_dir(tmp_path, sent)
    monkeypatch.setattr(sdk, "caps_for", caps)

    ctx = {"token_count": 90, "token_limit": 100, "session_id": "s1", "window_id": "w1"}
    assert mod.hook("PostToolUse", {**ctx, "current_role": "subagent"}) == ""
    assert mod.hook("PostToolUse", {**ctx, "is_subagent_call": True}) == ""
    assert sent == []


def test_trigger_under_threshold_noop(monkeypatch, tmp_path):
    mod = _load_hook("auto_compact_trigger.py")
    sent = []
    sdk, caps = _caps_with_data_dir(tmp_path, sent)
    monkeypatch.setattr(sdk, "caps_for", caps)
    assert mod.hook("PostToolUse", {"token_count": 10, "token_limit": 100, "session_id": "s1"}) == ""
    assert sent == []
    assert not _active_file(tmp_path, "s1").exists()


def test_inject_clears_active_marker_by_source_session(tmp_path, monkeypatch):
    """注入端按 source_session_id 清掉对应会话的 active 文件（只清自己那一个）"""
    mod = _load_hook("inject_handoff.py")
    p = _write_pending(tmp_path, {"content": "## 任务目标\nX", "ts": time.time(), "source_session_id": "s1"})
    mine = _active_file(tmp_path, "s1")
    other = _active_file(tmp_path, "s2")
    for f in (mine, other):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"ts": time.time()}), encoding="utf-8")

    import app.plugins.sdk as sdk

    def _caps(cap):
        if cap == "storage.data_dir":
            return lambda: str(tmp_path)
        return None

    monkeypatch.setattr(sdk, "caps_for", _caps)
    assert "## 任务目标" in mod.hook("SessionStart", {})
    assert not p.exists()
    assert not mine.exists(), "源会话的防重入标记随注入消费清掉"
    assert other.exists(), "别的会话的标记不能被误清"


def test_guide_only_for_primary():
    mod = _load_hook("inject_handoff_guide.py")
    assert "update_handoff" in mod.hook("BuildSystemPrompt", {"current_role": "primary"})
    assert mod.hook("BuildSystemPrompt", {"current_role": "subagent"}) == ""
