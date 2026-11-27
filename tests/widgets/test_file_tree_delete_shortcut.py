# -*- coding: utf-8 -*-
"""file-tree 插件：删除快捷键跨平台适配回归测试

问题：``FileTreeView.keyPressEvent`` 原来只认 ``Qt.Key_Delete``（对应 Windows
Delete / Mac fn+⌫）。MacBook 的 ⌫ 是 ``Qt.Key_Backspace``，⌘⌫ 在 Qt/macOS 下
映射为 ``Qt.ControlModifier | Qt.Key_Backspace``，两者都不匹配 → 按了没反应。

Qt 在 macOS 上把 Command 映射为 ControlModifier、Control 映射为 MetaModifier，
故两个修饰位都要认（与 preview.py 的 Ctrl+滚轮缩放口径一致）。
"""

import importlib.util
import sys
from pathlib import Path

from PyQt5.QtCore import Qt, QEvent
from PyQt5.QtGui import QKeyEvent
from PyQt5.QtWidgets import QApplication

_QAPP = QApplication.instance() or QApplication(sys.argv)

_PLUGIN_UI = Path(__file__).resolve().parent.parent.parent / "plugins" / "file-tree" / "ui"


def _load_package():
    if "file_tree_delete_ui" in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(
        "file_tree_delete_ui", _PLUGIN_UI / "__init__.py", submodule_search_locations=[str(_PLUGIN_UI)]
    )
    pkg = importlib.util.module_from_spec(spec)
    sys.modules["file_tree_delete_ui"] = pkg
    spec.loader.exec_module(pkg)


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_load_package()
tree_widget_mod = _load_module("file_tree_delete_ui.tree_widget", _PLUGIN_UI / "tree_widget.py")
FileTreeView = tree_widget_mod.FileTreeView


def _press(view, key, mods=Qt.NoModifier):
    view.keyPressEvent(QKeyEvent(QEvent.KeyPress, key, mods))


def _make_view():
    view = FileTreeView()
    calls = []
    view._emit_delete = lambda: calls.append(1)
    return view, calls


def test_delete_key_triggers_delete():
    """Delete 保留：Windows/Linux 标准键、Mac fn+⌫"""
    view, calls = _make_view()
    _press(view, Qt.Key_Delete)
    assert calls == [1]


def test_cmd_backspace_triggers_delete():
    """⌘⌫（Qt/macOS: ControlModifier + Backspace）触发删除 —— 用户痛点"""
    view, calls = _make_view()
    _press(view, Qt.Key_Backspace, Qt.ControlModifier)
    assert calls == [1]


def test_meta_backspace_triggers_delete():
    """部分平台把 Command 映射为 MetaModifier，同样要认"""
    view, calls = _make_view()
    _press(view, Qt.Key_Backspace, Qt.MetaModifier)
    assert calls == [1]


def test_shift_backspace_does_not_trigger_delete():
    """Shift+⌫ 等非删除组合不应误触发"""
    view, calls = _make_view()
    _press(view, Qt.Key_Backspace, Qt.ShiftModifier)
    assert calls == []
