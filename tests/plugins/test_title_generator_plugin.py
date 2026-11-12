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
        "session.read": lambda: {"session_id": session_id},
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
