# -*- coding: utf-8 -*-
"""agent_trace 试金石：只依赖 stream.subscribe + window.list，不再直连 backend 信号。"""
import ast
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
COLLECTOR = REPO / "plugins" / "agent_trace" / "ui" / "trace_collector.py"
CARD = REPO / "plugins" / "agent_trace" / "ui" / "trace_card.py"
MANIFEST = REPO / "plugins" / "agent_trace" / ".drifox-plugin" / "plugin.json"


def _src(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_no_direct_backend_signal_connect():
    """collector 不再自己 connect/disconnect backend 信号（全部走 stream.subscribe）。"""
    tree = ast.parse(_src(COLLECTOR))
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in ("connect", "disconnect") and isinstance(
                node.func.value, ast.Attribute
            ):
                bad.append(f"{ast.dump(node.func.value)[:60]}")
    assert not bad, f"仍直连信号: {bad}"


def test_no_tab_manager_import():
    assert "tab_manager_window" not in _src(COLLECTOR)
    assert "tab_manager_window" not in _src(CARD)


def test_manifest_declares_capabilities():
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert data.get("api_version") == 2
    caps = set(data.get("capabilities") or [])
    assert {"stream.subscribe", "window.list", "model.schema", "state.kv"} <= caps


def test_attach_accepts_capabilities():
    """attach 签名加了 capabilities（可选，缺省保持旧直连行为）。"""
    import inspect
    import sys

    sys.path.insert(0, str(REPO / "plugins" / "agent_trace"))
    try:
        from ui.trace_collector import TraceCollector

        params = inspect.signature(TraceCollector.attach).parameters
        assert "capabilities" in params
    finally:
        sys.path.pop(0)
