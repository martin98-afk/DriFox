# -*- coding: utf-8 -*-
"""探针：拖拽滚动条时的吸顶抖动

拖拽是绝对定位：value 由鼠标位置映射，任何补偿都会被下一帧鼠标移动推翻。
断言：
1. 拖拽期间高度冻结（不刷新），滚动值等于拖拽请求值（不被补偿篡改）
2. 松手后一次性重算
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

from qfluentwidgets import SmoothScrollDelegate  # noqa: E402


def _node(name, path, is_dir):
    it = QStandardItem(name)
    it.setData(path, FileTreeModel.COL_PATH)
    it.setData(bool(is_dir), FileTreeModel.COL_IS_DIR)
    return it


class Tree:
    def __init__(self, depth=8, files=4, viewport_h=300):
        self.model = QStandardItemModel()
        root = _node("root", "/root", False)
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
        self.delegate = SmoothScrollDelegate(self.view)
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
        self.l0 = self.model.index(0, 0).child(0, 0)

    def teardown(self):
        try:
            self.overlay.bind_model(None)
            self.overlay.deleteLater()
            self.view.setModel(None)
            self.host.close()
            self.host.deleteLater()
            pump(60)
        except RuntimeError:
            pass


def run_drag(t, start, end, label):
    bar = t.view.verticalScrollBar()
    sb = t.delegate.vScrollBar  # qfluentwidgets 自绘条（真实交互对象）
    bar.setValue(start)
    pump(40)
    heights = []
    mismatches = []
    sb.setSliderDown(True)  # emit sliderPressed
    pump(10)
    step = 1 if end > start else -1
    for v in range(start, end, step):
        sb.setValue(v)
        pump(8)
        heights.append(t.overlay.maximumHeight())
        if bar.value() != sb.value():
            mismatches.append((v, sb.value(), bar.value()))
    sb.setSliderDown(False)  # emit sliderReleased
    pump(40)
    after = (t.overlay.maximumHeight(), len(t.overlay.rows()), bar.value())
    frozen = len(set(heights)) == 1
    print(f"\n===== {label} =====")
    print(f"  拖拽帧 {len(heights)}, 高度取值 {sorted(set(heights))}, 冻结={frozen}")
    print(f"  滚动值被篡改 {len(mismatches)} 次 {mismatches[:3]}")
    print(f"  松手后 h={after[0]} rows={after[1]} v={after[2]}")
    return frozen, len(mismatches)


def main():
    t = Tree()
    try:
        bar = t.view.verticalScrollBar()
        print(f"row_h={t.overlay._measure_row_height()} vp={t.view.viewport().height()} max={bar.maximum()}")
        ok = []
        ok.append(run_drag(t, 400, 40, "向上拖拽 400→40"))
        ok.append(run_drag(t, 40, 430, "向下拖拽 40→430"))
        ok.append(run_drag(t, 250, 120, "小幅向上拖拽 250→120"))
        print("\n>>> 冻结/无篡改：", all(f and m == 0 for f, m in ok))
    finally:
        t.teardown()


if __name__ == "__main__":
    main()
