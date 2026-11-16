# -*- coding: utf-8 -*-
"""探针：复现 file-tree 吸顶条在临界区逐像素滚动时的抖动

用 QStandardItemModel 构造可控的深目录链（每层目录 + 若干文件行），
逐像素滚动，记录 (bar.value, overlay.maximumHeight, rows, vp_h, bar.max)。
单向滚动中高度变化方向反转 = 抖动。
"""
import importlib.util
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtGui import QStandardItem, QStandardItemModel
from PyQt5.QtWidgets import QAbstractItemView, QApplication, QTreeView, QVBoxLayout, QWidget

_QAPP = QApplication.instance() or QApplication(sys.argv)
_PLUGIN_UI = Path(__file__).resolve().parents[2] / "plugins" / "file-tree" / "ui"


def _load_pkg():
    if "file_tree_sticky_ui" in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(
        "file_tree_sticky_ui", _PLUGIN_UI / "__init__.py",
        submodule_search_locations=[str(_PLUGIN_UI)],
    )
    pkg = importlib.util.module_from_spec(spec)
    sys.modules["file_tree_sticky_ui"] = pkg
    spec.loader.exec_module(pkg)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def pump(n=3):
    for _ in range(n):
        QApplication.processEvents()


_load_pkg()
tree_widget_mod = _load("file_tree_sticky_ui.tree_widget", _PLUGIN_UI / "tree_widget.py")
sticky_mod = _load("file_tree_sticky_ui.sticky_scroll", _PLUGIN_UI / "sticky_scroll.py")
StickyScrollOverlay = sticky_mod.StickyScrollOverlay
FileTreeModel = tree_widget_mod.FileTreeModel


def _node(name, path, is_dir):
    it = QStandardItem(name)
    it.setData(path, FileTreeModel.COL_PATH)
    it.setData(bool(is_dir), FileTreeModel.COL_IS_DIR)
    return it


class Tree:
    """root/l0/l1/.../l{depth-1}，每层目录下挂 files 个文件（模拟真实目录内容）"""

    def __init__(self, depth=8, files=4, viewport_h=300):
        self.model = QStandardItemModel()
        root = _node("root", "/root", True)
        self.model.appendRow([root])
        base = root
        for i in range(depth):
            d = _node(f"l{i}", f"/root/{i}", True)
            base.appendRow([d])
            for j in range(files):
                d.appendRow([_node(f"f{i}_{j}.py", f"/root/{i}/f{j}.py", False)])
            base = d

        self.view = QTreeView()
        self.view.setModel(self.model)
        # ★ 复刻真实接线：qfluentwidgets.SmoothScrollDelegate 会把树设成逐像素滚动，
        #   抖动只在 per-pixel 下出现（per-item 一步一整行，天然不振荡）
        self.view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.host = QWidget()
        lay = QVBoxLayout(self.host)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.overlay = StickyScrollOverlay(self.view, self.host)
        lay.addWidget(self.overlay)
        lay.addWidget(self.view, 1)
        self.host.resize(320, viewport_h)
        self.host.show()
        self.view.expandAll()
        pump()
        self.overlay.bind_model(self.model)
        pump()

    def teardown(self):
        try:
            self.overlay.bind_model(None)
            self.overlay.deleteLater()
            self.view.setModel(None)
            self.host.close()
            self.host.deleteLater()
            pump(5)
        except RuntimeError:
            pass


def trace(t, start, steps, direction, label=""):
    bar = t.view.verticalScrollBar()
    bar.setValue(start)
    pump()
    recs = []
    for _ in range(steps):
        nxt = max(0, min(bar.maximum(), bar.value() + direction))
        if nxt == bar.value():
            break
        bar.setValue(nxt)
        pump()
        recs.append((bar.value(), t.overlay.maximumHeight(), len(t.overlay.rows()),
                     t.overlay.isVisible(), t.view.viewport().height(), bar.maximum()))
    return recs


def analyse(recs, label):
    flips = 0
    prev_dir = 0
    changes = []
    last_h = None
    for row in recs:
        h = row[1]
        if last_h is None:
            last_h = h
            continue
        d = (h > last_h) - (h < last_h)
        if d != 0:
            changes.append(row)
            if prev_dir != 0 and d != prev_dir:
                flips += 1
            prev_dir = d
        last_h = h
    print(f"\n===== {label}: {len(recs)} 步, 高度变化 {len(changes)} 次, 方向反转 {flips} 次 =====")
    for row in changes[:50]:
        print(f"  value={row[0]:5d} h={row[1]:4d} rows={row[2]} vis={row[3]} vp_h={row[4]:4d} max={row[5]}")
    if len(changes) > 50:
        print(f"  ... 省略 {len(changes) - 50} 条")
    return flips


def main():
    t = Tree()
    try:
        bar = t.view.verticalScrollBar()
        rh = t.overlay._measure_row_height()
        print(f"row_h={rh} vp_h={t.view.viewport().height()} max={bar.maximum()} tree_h={t.view.height()}")
        total_flips = 0
        total_flips += analyse(trace(t, 0, 120, +1), "全区间向下 1px×120")
        total_flips += analyse(trace(t, 120, 120, -1), "全区间向上 1px×120")
        # 精细：从顶部开始持续向下 600 步（覆盖 3 层目录进出）
        total_flips += analyse(trace(t, 0, 600, +1), "顶部起向下 1px×600")
        total_flips += analyse(trace(t, 600, 600, -1), "回滚 1px×600")
        print(f"\n>>> 抖动总次数(高度方向反转) = {total_flips}")
    finally:
        t.teardown()


if __name__ == "__main__":
    main()
