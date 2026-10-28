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


def test_trigger_skips_when_active(monkeypatch):
    mod = _load_hook("auto_compact_trigger.py")
    kv = _KV({"handoff_active": {"ts": time.time()}})
    sent = []
    import app.plugins.sdk as sdk

    monkeypatch.setattr(
        sdk,
        "caps_for",
        lambda cap: kv if cap == "state.kv" else (lambda *a, **kw: sent.append(a)),
    )
    assert mod.hook("PostToolUse", {"token_count": 90, "token_limit": 100}) == ""
    assert sent == []


def test_trigger_enqueues_when_over_threshold(monkeypatch):
    mod = _load_hook("auto_compact_trigger.py")
    kv = _KV()
    sent = []
    import app.plugins.sdk as sdk

    monkeypatch.setattr(
        sdk,
        "caps_for",
        lambda cap: kv if cap == "state.kv" else (lambda *a, **kw: sent.append((a, kw))),
    )
    out = mod.hook("PostToolUse", {"token_count": 90, "token_limit": 100})
    assert "上下文交接" in out          # 提示直接进上下文，模型下一轮就能看到
    assert len(sent) == 1               # 队列里仍留一条兜底
    assert "handoff_active" in kv.data


def test_trigger_under_threshold_noop(monkeypatch):
    mod = _load_hook("auto_compact_trigger.py")
    kv = _KV()
    import app.plugins.sdk as sdk

    monkeypatch.setattr(sdk, "caps_for", lambda cap: kv if cap == "state.kv" else None)
    assert mod.hook("PostToolUse", {"token_count": 10, "token_limit": 100}) == ""
    assert kv.data == {}


def test_guide_only_for_primary():
    mod = _load_hook("inject_handoff_guide.py")
    assert "update_handoff" in mod.hook("BuildSystemPrompt", {"current_role": "primary"})
    assert mod.hook("BuildSystemPrompt", {"current_role": "subagent"}) == ""
