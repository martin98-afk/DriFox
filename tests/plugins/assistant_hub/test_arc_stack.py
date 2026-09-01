# -*- coding: utf-8 -*-
"""test_arc_stack.py — 弧形卡片堆叠冒烟测试（QApplication 必需）。"""

import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt5.QtWidgets")

from PyQt5.QtWidgets import QApplication  # noqa: E402

_APP = QApplication.instance() or QApplication([])

_ROOT = Path(__file__).resolve().parents[3]
_UI_DIR = _ROOT / "plugins" / "assistant_hub" / "ui"

# 构造临时包 ui_plugin_assistant_hub（对齐主程序 UI 插件加载前缀，保留相对导入）
_pkg_name = "ui_plugin_assistant_hub"
if _pkg_name not in sys.modules:
    pkg = types.ModuleType(_pkg_name)
    pkg.__path__ = [str(_UI_DIR)]
    sys.modules[_pkg_name] = pkg


def _load(name: str, file: str):
    full = f"{_pkg_name}.{name}"
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(full, str(_UI_DIR / file))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


_avatar = _load("assistant_avatar", "assistant_avatar.py")  # noqa: F841 确保 RoundAvatar 先注册
m = _load("arc_stack", "arc_stack.py")


def test_stack_build_and_signals(qtbot=None):
    stack = m.ArcCardStack()
    stack.resize(520, m.CONTAINER_H)
    stack.set_assistants(
        [
            {"id": "a", "name": "小狐", "color": "#7C3AED", "avatar_path": ""},
            {"id": "b", "name": "hanako", "color": "#DB2777", "avatar_path": ""},
            {"id": "c", "name": "build", "color": "#0284C7", "avatar_path": ""},
        ]
    )
    assert len(stack._cards) == 3
    assert stack._add_card is not None
    # 收起态位置计算：3 张卡互不重叠完全
    pos = stack._positions(False)
    assert len(pos) == 3
    expanded = stack._positions(True)
    # 展开态横向均布
    xs = [p[0] for p in expanded]
    assert xs[1] - xs[0] == m.SPREAD_STEP == xs[2] - xs[1]
    # 信号
    got = []
    stack.selectionChanged.connect(got.append)
    stack._on_card_clicked("b")
    assert got == ["b"]
    created = []
    stack.createRequested.connect(lambda: created.append(1))
    stack._add_card.clicked.emit()
    assert created == [1]


def test_card_states():
    card = m._AgentCard("x", "测试", "#123456", "")
    card.set_selected(True)
    card.set_primary(True)
    assert card._selected and card._primary
