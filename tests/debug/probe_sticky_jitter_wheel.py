# -*- coding: utf-8 -*-
"""探针 v2：真实滚轮路径下的吸顶抖动量化

复刻真实接线：SmoothScrollDelegate（useAni 默认 False，走平滑滚动引擎，
每帧向原生滚动条投递合成 wheel 事件）。

指标：跟踪固定目录行 l0 在**屏幕坐标系**里的 y（= 吸顶条高 + visualRect.top()），
持续向上滚时该 y 必须单调不减；出现回退 = 视觉抖动（内容与滚轮方向相反跳动）。
"""
import importlib.util
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QPoint, Qt
from PyQt5.QtGui import QStandardItem, QStandardItemModel, QWheelEvent
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


_PLUGIN_UI_S = str(_PLUGIN_UI)
if "file_tree_sticky_ui" not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        "file_tree_sticky_ui", _PLUGIN_UI / "__init__.py",
        submodule_search_locations=[_PLUGIN_UI_S],
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
        # 与 cards.py 完全一致：平滑滚动委托挂在树身
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
        pump(50)
        self.overlay.bind_model(self.model)
        pump(50)
        self.l0 = self.model.index(0, 0).child(0, 0)  # root/l0

    def screen_y(self, idx):
        """该行在 host 坐标系里的 y（与滚轮视觉位移同口径）"""
        return self.view.geometry().top() + self.view.visualRect(idx).top()

    def teardown(self):
        try:
            self.overlay.bind_model(None)
            self.overlay.deleteLater()
            self.view.setModel(None)
            self.host.close()
            self.host.deleteLater()
            pump(50)
        except RuntimeError:
            pass


def wheel(view, pixels, positive=True):
    """模拟一格滚轮（120*1.5 目标位移由引擎分摊到多帧）"""
    delta = -pixels if positive else pixels
    ev = QWheelEvent(
        QPoint(10, 10), QPoint(10, 10), QPoint(0, 0), QPoint(0, delta),
        Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False,
    )
    QApplication.sendEvent(view.viewport(), ev)


def run(t, start, clicks, positive, label):
    bar = t.view.verticalScrollBar()
    bar.setValue(start)
    pump(30)
    t0 = t.screen_y(t.l0)
    samples = []
    for _ in range(clicks):
        wheel(t.view, 120, positive)
        for _ in range(30):  # 引擎 400ms / 60fps ≈ 24 帧
            pump(16)
            samples.append((
                bar.value(), t.overlay.maximumHeight(), len(t.overlay.rows()),
                t.view.viewport().height(), t.screen_y(t.l0),
            ))
    ys = [s[4] for s in samples]
    # 视觉回退：向上滚（positive=False）时 y 必须不减；向下滚时 y 必须不增
    sign = +1 if not positive else -1
    backsteps = 0
    biggest = 0
    prev = ys[0]
    for y in ys[1:]:
        d = (y - prev) * sign
        if d < 0:
            backsteps += 1
            biggest = min(biggest, d)
        prev = y
    heights = sorted({s[1] for s in samples})
    print(f"\n===== {label} =====")
    print(f"  采样 {len(samples)} 帧, 视觉回退 {backsteps} 次, 最大回退 {biggest}px")
    print(f"  吸顶高度取值 {heights}")
    print(f"  滚动值轨迹 {samples[0][0]} → {samples[-1][0]}, 行 y {ys[0]} → {ys[-1]}")
    return backsteps


def check_rest_stability(t, label):
    """滚动停止后必须静止：自激振荡的典型表现是停手后高度还在变"""
    seq = []
    for _ in range(60):  # 约 1 秒
        pump(16)
        seq.append((t.view.verticalScrollBar().value(), t.overlay.maximumHeight()))
    uniq = set(seq)
    print(f"  静止 1s: 状态取值 {uniq}")
    assert len(uniq) <= 2, f"{label}: 停止后仍在变化 {uniq}"


def main():
    t = Tree()
    try:
        bar = t.view.verticalScrollBar()
        print(f"row_h={t.overlay._measure_row_height()} vp_h={t.view.viewport().height()} max={bar.maximum()}")
        total = 0
        # 临界区：l0 即将被吸顶条遮住。向上滚（最易抖）
        total += run(t, 200, 6, False, "向上滚 6 格（跨 l0 退出临界）")
        check_rest_stability(t, "向上 6 格后")
        total += run(t, 120, 6, False, "向上滚 6 格（跨 root/l0 临界）")
        total += run(t, 60, 6, False, "向上滚 6 格（跨首次吸顶）")
        check_rest_stability(t, "向上滚到顶后")
        # 向下滚对照
        total += run(t, 30, 6, True, "向下滚 6 格（跨首次吸顶）")
        total += run(t, 150, 6, True, "向下滚 6 格（跨加层临界）")
        check_rest_stability(t, "向下滚后")
        # 长距离连续滚动（真实用户从中间一路滚到底再滚回来）
        total += run(t, 200, 30, True, "向下滚 30 格（长距离）")
        total += run(t, 505, 30, False, "向上滚 30 格（长距离）")
        check_rest_stability(t, "长距离往返后")
        print(f"\n>>> 视觉回退总帧数 = {total}")
    finally:
        t.teardown()


if __name__ == "__main__":
    main()
