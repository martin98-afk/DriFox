# -*- coding: utf-8 -*-
"""宿主标题编排：三处触发收敛 + 旧符号清零。"""
import ast
from pathlib import Path


def test_title_request_event_contract():
    """三处触发点统一调 _publish_title_request，publish 只有一处。"""
    src = Path("app/main_widget.py").read_text(encoding="utf-8")
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
