# -*- coding: utf-8 -*-
"""system-title-generator 插件：清单契约与纯生成链。"""
import json
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "plugins" / "system-title-generator"


def test_manifest_declares_system_type_and_model_field():
    manifest = json.loads(
        (PLUGIN_ROOT / ".drifox-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    assert manifest["type"] == "system"
    assert manifest["api_version"] == 2
    assert manifest["components"]["ui"] is True
    fields = {f["key"]: f for f in manifest["config_schema"]["fields"]}
    assert fields["title_model"]["type"] == "model"
    assert fields["title_model"]["default"] == ""
    for cid in (
        "plugin.config.read",
        "model.complete",
        "provider.list",
        "model.enumerate",
        "session.read",
        "ui.events",
        "session.title.commit",
    ):
        assert cid in manifest["capabilities"]
    assert "session.title.snapshot" not in manifest["capabilities"]
    assert "session.title.cancel" not in manifest["capabilities"]


import importlib.util


def _load_generator():
    path = PLUGIN_ROOT / "ui" / "generator.py"
    spec = importlib.util.spec_from_file_location("_title_gen_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pool(*, config="", session_id="s-1", complete="", commits=None, events=None):
    sink = commits if commits is not None else []
    return {
        "plugin.config.read": lambda key, default="": config if key == "title_model" else default,
        "session.read": lambda window_id="": {"session_id": session_id},
        "model.complete": lambda prompt, **kw: complete,
        "session.title.commit": lambda wid, title: (sink.append((wid, title)) or True),
        "ui.events": lambda: {
            "subscribe": lambda event, cb: (events or {}).setdefault(event, []).append(cb),
            "publish": lambda event, **kw: None,
        },
    }


def test_extract_json_three_forms():
    ctrl = _load_generator()._TitleController
    assert ctrl.extract_json('```json\n{"topic_summary": "季度报表"}\n```') == {
        "topic_summary": "季度报表"
    }
    assert ctrl.extract_json('说明: {"topic_summary": "调试bug"}') == {
        "topic_summary": "调试bug"
    }
    assert ctrl.extract_json('{"topic_summary": "咨询问题"}') == {
        "topic_summary": "咨询问题"
    }
    assert ctrl.extract_json("没有 JSON") is None


def test_build_prompt_has_previous_summary_branch():
    ctrl = _load_generator()._TitleController
    msgs = [{"role": "user", "content": "请帮我分析这份季度报表并生成摘要"}]
    assert "之前的标题" in ctrl.build_prompt(msgs, "旧标题")
    assert "之前的标题" not in ctrl.build_prompt(msgs)


def test_request_generates_and_emits_with_model_query(monkeypatch):
    module = _load_generator()
    seen = {}
    emitted = []

    def fake_complete(prompt, **kw):
        seen.update(kw)
        return '```json\n{"topic_summary": "季度报表分析"}\n```'

    pool = _pool(config="OpenCode:gpt-4o")
    pool["model.complete"] = fake_complete
    ctrl = module._TitleController(pool)
    monkeypatch.setattr(ctrl, "_emit", lambda w, s, t: emitted.append((w, s, t)))
    ctrl._generate(
        "w-1", "s-1",
        [{"role": "user", "content": "请帮我分析这份季度报表并生成摘要"}],
        "请帮我分析这份季度报", "OpenCode:gpt-4o",
    )
    assert seen.get("model_query") == "OpenCode:gpt-4o"
    assert seen.get("temperature") == 0.1
    assert emitted == [("w-1", "s-1", "季度报表分析")]


def test_failure_uses_fallback(monkeypatch):
    module = _load_generator()
    emitted = []
    pool = _pool(complete="not json")
    ctrl = module._TitleController(pool)
    monkeypatch.setattr(ctrl, "_emit", lambda w, s, t: emitted.append((w, s, t)))
    ctrl._generate(
        "w-1", "s-1",
        [{"role": "user", "content": "请帮我分析这份季度报表并生成摘要"}],
        "请帮我分析这份季度报", "",
    )
    assert emitted == [("w-1", "s-1", "请帮我分析这份季度报")]


def test_session_changed_discards_result():
    module = _load_generator()
    calls = []
    pool = _pool(session_id="s-2", commits=calls)
    ctrl = module._TitleController(pool)
    ctrl._on_result("w-1", "s-1", "季度报表分析")
    assert calls == []


def test_session_read_targets_origin_window():
    module = _load_generator()
    seen = []
    calls = []

    def read(window_id=""):
        seen.append(window_id)
        return {"session_id": "s-1"}

    pool = _pool(commits=calls)
    pool["session.read"] = read
    ctrl = module._TitleController(pool)
    ctrl._on_result("w-1", "s-1", "季度报表分析")
    assert seen == ["w-1"]
    assert calls == [("w-1", "季度报表分析")]


def test_title_request_refreshes_pool(monkeypatch):
    """每次 title.request 都重取能力池：建池时的 host 可能已被销毁。

    回归：控制器在插件加载时建池，host 固化；窗口重建后旧池里的
    model.complete 抛 "OpenAIChatToolWindow has been deleted" 并静默返回空串，
    标题回退成首条消息前 15 字（实测 "/plugin-creator"）。
    """
    module = _load_generator()
    initial_pool = _pool()
    fresh_pool = _pool()
    ctrl = module._TitleController(initial_pool)

    import app.plugins.capabilities.resolve as resolve

    monkeypatch.setattr(resolve, "pool_for", lambda *a, **kw: fresh_pool)

    started = []

    class _NoThread:
        def __init__(self, target=None, args=(), daemon=None):
            started.append(args)

        def start(self):
            pass

    monkeypatch.setattr(module.threading, "Thread", _NoThread)
    ctrl._on_title_request(
        {
            "window_id": "w-1",
            "session_id": "s-1",
            "messages": [{"role": "user", "content": "帮我分析这份季度报表并生成摘要"}],
            "fallback": "帮我分析这份季度报表并",
        }
    )
    assert ctrl._pool is fresh_pool
    assert started and started[0][0] == "w-1"

def test_controller_restart_reconnects_bridge(monkeypatch):
    """同进程 destroy→create 不重新 import 时仍能收到结果。"""
    module = _load_generator()
    subscribed = {}
    pool = _pool(events=subscribed)
    committed = []
    pool["session.title.commit"] = lambda wid, title: committed.append((wid, title))

    first = module._TitleController(pool)
    first.start()
    first.stop()
    second = module._TitleController(pool)
    second.start()

    bridge = module._BRIDGE
    assert bridge is not None
    bridge.resultReady.emit("w-1", "s-1", "标题")
    assert committed == [("w-1", "标题")]
    second.stop()
