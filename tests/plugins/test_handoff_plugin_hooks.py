# -*- coding: utf-8 -*-
"""handoff 插件 hooks：注入消费 pending、触发防重入"""

import importlib.util
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def _load_hook(filename: str):
    p = _ROOT / "plugins" / "system-handoff" / "hooks" / filename
    spec = importlib.util.spec_from_file_location(f"handoff_hook_{filename[:-3]}", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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


def test_inject_consumes_pending(monkeypatch):
    mod = _load_hook("inject_handoff.py")
    kv = _KV(
        {
            "handoff_pending": {
                "content": "## 任务目标\nX",
                "ts": time.time(),
                "source_session_id": "s1",
            },
            "handoff_active": {"ts": time.time()},
        }
    )
    monkeypatch.setattr(mod, "caps_for", lambda cap: kv, raising=False)
    import app.plugins.sdk as sdk

    monkeypatch.setattr(sdk, "caps_for", lambda cap: kv)

    out = mod.hook("SessionStart", {})
    assert "## 任务目标" in out
    assert "handoff_pending" not in kv.data  # 读后清除
    assert "handoff_active" not in kv.data


def test_inject_empty_when_no_pending(monkeypatch):
    mod = _load_hook("inject_handoff.py")
    kv = _KV()
    import app.plugins.sdk as sdk

    monkeypatch.setattr(sdk, "caps_for", lambda cap: kv)
    assert mod.hook("SessionStart", {}) == ""


def test_inject_drops_stale_pending(monkeypatch):
    mod = _load_hook("inject_handoff.py")
    kv = _KV({"handoff_pending": {"content": "X", "ts": time.time() - 3600, "source_session_id": "s1"}})
    import app.plugins.sdk as sdk

    monkeypatch.setattr(sdk, "caps_for", lambda cap: kv)
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

    monkeypatch.setattr(sdk, "caps_for", lambda cap: kv)
    assert mod.hook("PostToolUse", {"token_count": 10, "token_limit": 100}) == ""
    assert kv.data == {}


def test_guide_only_for_primary():
    mod = _load_hook("inject_handoff_guide.py")
    assert "update_handoff" in mod.hook("BuildSystemPrompt", {"current_role": "primary"})
    assert mod.hook("BuildSystemPrompt", {"current_role": "subagent"}) == ""
