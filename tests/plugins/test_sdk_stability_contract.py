# -*- coding: utf-8 -*-
"""sdk 稳定性契约 + 符号可解析性。

两条：
1. 每个收录符号都必须真能 import 到（防止 _SYMBOLS 里的模块路径写错，
   而那要到插件运行时才炸）
2. 稳定面（_STABLE）必须是 _SYMBOLS 的子集，且常用契约在稳定面内
"""

from __future__ import annotations

import importlib

import pytest

MUST_BE_STABLE = (
    "ToolResult",
    "PARAM_SCHEMA",
    "estimate_tokens",
    "Colors",
    "get_app_data_dir",
    "tool_registry_api",
    "caps_for",
    "pool_for",
    "html_escape",
    "cfg_int",
    "classify_command",
    "create_agent_manager",
    "GitWorktreeDetector",
    "WorktreeInfo",
    "replay_theme_qss",
)


def test_all_symbols_resolvable():
    """_SYMBOLS 里每个符号都能 import 到（路径/符号名没写错）。"""
    from app.plugins import sdk

    missing = []
    for name in sdk._SYMBOLS:
        try:
            importlib.import_module(sdk._SYMBOLS[name][0])
            getattr(importlib.import_module(sdk._SYMBOLS[name][0]), sdk._SYMBOLS[name][1])
        except Exception as e:  # noqa: BLE001
            missing.append(f"{name}: {e}")
    assert not missing, f"sdk 符号无法解析: {missing}"


def test_stable_is_subset_of_symbols():
    from app.plugins import sdk

    extra = set(sdk._STABLE) - set(sdk._SYMBOLS)
    assert not extra, f"_STABLE 含未收录符号: {sorted(extra)}"


@pytest.mark.parametrize("name", MUST_BE_STABLE)
def test_core_contracts_are_stable(name):
    from app.plugins import sdk

    assert sdk.sdk_stability(name) == "stable", f"{name} 应在稳定面（插件长期依赖它）"


def test_unknown_symbol_reports_unknown():
    from app.plugins import sdk

    assert sdk.sdk_stability("definitely_not_a_symbol") == "unknown"


def test_getattr_raises_for_unknown():
    from app.plugins import sdk

    with pytest.raises(AttributeError):
        sdk.definitely_not_a_symbol
