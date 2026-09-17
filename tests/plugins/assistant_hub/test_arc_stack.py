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


def test_expand_wrap_and_clamp(qtbot=None):
    """展开态放不下自动换行 + 容器动态增高；收起态多卡弧宽钳制不横向溢出。"""
    stack = m.ArcCardStack()
    stack.resize(360, m.CONTAINER_H)
    stack.set_assistants([{"id": f"a{i}", "name": f"助手{i}", "color": "#7C3AED", "avatar_path": ""} for i in range(7)])
    # 360 宽容 4 卡/行：7 助手 + 新建 = 8 项 → 两行，容器动态增高一行
    stack._expanded = True
    stack._relayout(animate=False)
    assert stack._expanded_rows() == 2
    assert stack.height() == m.CONTAINER_H + (m.CARD_SIZE + m.ROW_GAP)
    # 卡片控件含 PAD 绘制余量（选中光环/名字画出内容区不被控件边界裁剪）
    card = stack._cards[0]
    assert (card.width(), card.height()) == (m.CARD_W, m.CARD_H)
    # 行内步长固定 SPREAD_STEP，两行 y 只有两种取值
    xs = [c.pos().x() for c in stack._cards[:4]]
    assert xs[1] - xs[0] == m.SPREAD_STEP == xs[2] - xs[1]
    assert len({c.pos().y() for c in stack._cards}) == 2

    # 收起态：极窄容器塞 20 卡，钳角后整体不超出容器左右边界
    stack2 = m.ArcCardStack()
    stack2.resize(260, m.CONTAINER_H)
    stack2.set_assistants([{"id": f"b{i}", "name": f"n{i}", "color": "#7C3AED", "avatar_path": ""} for i in range(20)])
    stack2._expanded = False
    stack2._relayout(animate=False)
    assert min(c.pos().x() for c in stack2._cards) >= 0
    assert max(c.pos().x() + c.width() for c in stack2._cards) <= stack2.width()
