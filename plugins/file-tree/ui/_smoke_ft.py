# -*- coding: utf-8 -*-
"""冒烟：file-tree 树视图 delegate 接管验证（验证后删除）"""
import importlib.util
import os
import sys
import types

os.environ["QT_QPA_PLATFORM"] = "offscreen"
ROOT = "D:/work/DriFox"
sys.path.insert(0, ROOT)

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

app = QApplication(sys.argv)


def ns(name: str, path: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__path__ = [path]
    mod.__package__ = name
    sys.modules[name] = mod
    return mod


ns("plugins", f"{ROOT}/plugins")
ns("plugins.file-tree", f"{ROOT}/plugins/file-tree")
ns("plugins.file-tree.ui", f"{ROOT}/plugins/file-tree/ui")

spec = importlib.util.spec_from_file_location("plugins.file-tree.ui.cards", f"{ROOT}/plugins/file-tree/ui/cards.py")
m = importlib.util.module_from_spec(spec)
sys.modules["plugins.file-tree.ui.cards"] = m
spec.loader.exec_module(m)

card = m.FileTreeCard()
# 内层树视图：delegate 接管（原生垂直条强制 AlwaysOff）
assert card._tree_view.verticalScrollBarPolicy() == Qt.ScrollBarAlwaysOff, "tree_view 原生垂直条未接管"
# 滚动模式被 delegate 设为像素级（QAbstractItemView 分支）
from PyQt5.QtWidgets import QAbstractItemView

assert card._tree_view.verticalScrollMode() == QAbstractItemView.ScrollPerPixel, "未切换 ScrollPerPixel"
print("[OK] file-tree _tree_view: delegate 已接管 + ScrollPerPixel")
print("SMOKE_PASS")
