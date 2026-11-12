# -*- coding: utf-8 -*-
"""宿主标题编排：三处触发收敛 + 旧符号清零。"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_title_request_event_contract():
    """三处触发点统一调 _publish_title_request，publish 只有一处。"""
    src = (ROOT / "app/main_widget.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "_publish_title_request"
    ]
    assert len(calls) == 3
    publishes = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "publish"
        and n.args
        and isinstance(n.args[0], ast.Constant)
        and n.args[0].value == "title.request"
    ]
    assert len(publishes) == 1
    for call in publishes:
        kws = {kw.arg for kw in call.keywords}
        assert {"window_id", "session_id", "messages", "fallback"} <= kws


def test_topic_summary_module_removed():
    assert not (ROOT / "app/core/workers/topic_summary.py").exists()


def test_main_widget_no_legacy_title_symbols():
    src = (ROOT / "app/main_widget.py").read_text(encoding="utf-8")
    for symbol in (
        "_maybe_generate_topic_summary",
        "_on_topic_summary_generated",
        "_topic_summary_ready",
        "_topic_summary_cancelled",
        "_handle_title_gen_command",
        "_update_title_gen_param_description",
        "_on_title_gen_model_config_changed",
        "TopicSummaryTask",
        "llm_title_gen_default_model",
    ):
        assert symbol not in src, symbol


def test_config_key_removed():
    # 全局 ConfigItem 已删；迁移函数里保留的旧键字面量是兼容锚点，不算残留。
    src = (ROOT / "app/utils/config.py").read_text(encoding="utf-8")
    assert 'ConfigItem("LLM", "TitleGenDefaultModel"' not in src
    assert "llm_title_gen_default_model" not in src


def test_title_gen_command_unregistered():
    assert '"title-gen"' not in (ROOT / "app/main_widget.py").read_text(encoding="utf-8")
