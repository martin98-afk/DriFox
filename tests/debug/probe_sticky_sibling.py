# -*- coding: utf-8 -*-
"""探针：第二层两个吸顶目录切换（兄弟替换）时的抖动

结构：/root/d0/{a,b}/ 各 5 个文件，全部展开。
从 a 的中段滚到 b 的中段，逐像素记录 (滚动值, 条高, 吸顶路径)。
切换瞬间若条高掉一档再涨回 = 抖。
"""
import importlib.util
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtGui import QStandardItem, QStandardItemModel
from PyQt5.QtWidgets import QAbstractItemView, QApplication, QTreeView, QVBoxLayout, QWidget

_QAPP = QApplication.instance() or QApplication(sys.argv)
_PLUGIN_UI = Path(__file__).resolve().parents[2] / "plugins" / "file-tree" / "ui"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def pump(ms=0):
    end = time.time() + ms / 1000.0
    while True:
        QApplication.processEvents()
        if time.time() >= end:
            break
        time.sleep(0.002)


if "file_tree_sticky_ui" not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        "file_tree_sticky_ui", _PLUGIN_UI / "__init__.py",
        submodule_search_locations=[str(_PLUGIN_UI)],
    )
    pkg = importlib.util.module_from_spec(spec)
    sys.modules["file_tree_sticky_ui"] = pkg
    spec.loader.exec_module(pkg)
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
    def __init__(self, siblings=2, files=20, viewport_h=300):
        self.model = QStandardItemModel()
        root = _node("root", "/root", False)
        self.model.appendRow([root])
        d0 = _node("d0", "/root/d0", True)
        root.appendRow([d0])
        self.leaves = []
        for s in range(siblings):
            name = chr(ord("a") + s)
            d = _node(name, f"/root/d0/{name}", True)
            d0.appendRow([d])
            self.leaves.append(d)
            self.leaf_indexes = getattr(self, 'leaf_indexes', [])
            for j in range(files):
                d.appendRow([_node(f"{name}{j}.py", f"/root/d0/{name}/{name}{j}.py", False)])

        self.view = QTreeView()
        self.view.setModel(self.model)
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
        pump(60)
        self.overlay.bind_model(self.model)
        pump(60)

    def teardown(self):
        try:
            self.overlay.bind_model(None)
            self.overlay.deleteLater()
            self.view.setModel(None)
            self.host.close()
            self.host.deleteLater()
            pump(40)
        except RuntimeError:
            pass


def sweep(t, start, end):
    bar = t.view.verticalScrollBar()
    bar.setValue(start)
    pump(30)
    recs = []
    for v in range(start, end, 1 if end > start else -1):
        bar.setValue(v)
        pump(2)
        recs.append((bar.value(), t.overlay.maximumHeight(),
                     tuple(os.path.basename(p) for p in t.overlay.row_paths())))
    return recs


def report(recs, label):
    heights = [r[1] for r in recs]
    seq = []
    for r in recs:
        if not seq or seq[-1] != (r[1], r[2]):
            seq.append((r[1], r[2]))
    drops = [(a, b) for a, b in zip(heights, heights[1:]) if b < a]
    print(f"\n===== {label} =====")
    print(f"  条高取值 {sorted(set(heights))}, 下降 {len(drops)} 次")
    print("  状态序列（条高, 吸顶路径）:")
    for h, paths in seq:
        print(f"    h={h:3d} {list(paths)}")
    return len(drops)


def main():
    t = Tree()
    try:
        bar = t.view.verticalScrollBar()
        rh = t.overlay._measure_row_height()
        print(f"row_h={rh} max={bar.maximum()} vp={t.view.viewport().height()}")
        # a 的最后一个文件 → b 的第一个文件 大约跨 2 行
        start = 0
        # 找到 a 区间的中段
        a_top = t.view.visualRect(t.leaves[0].index()).top()
        b_top = t.view.visualRect(t.leaves[1].index()).top()
        print(f"a rect_top={a_top} b rect_top={b_top}")
        sw = sweep(t, b_top - 3 * rh, b_top + 3 * rh)
        report(sw, f"跨 a→b 切换（{b_top - 3 * rh} → {b_top + 3 * rh}）")
        sw2 = sweep(t, b_top + 3 * rh, b_top - 3 * rh)
        report(sw2, f"回滚 b→a（{b_top + 3 * rh} → {b_top - 3 * rh}）")
    finally:
        t.teardown()


if __name__ == "__main__":
    main()
