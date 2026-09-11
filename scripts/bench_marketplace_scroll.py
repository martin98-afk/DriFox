# -*- coding: utf-8 -*-
"""插件市场滚动性能基准（离屏诊断脚本，非运行时代码）

模拟 SmoothScrollDelegate 的逐帧滚动：每帧 setValue 步进 + 强制重绘，
统计每帧耗时并输出 cProfile 热点。
"""
import cProfile
import io
import os
import pstats
import statistics
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_LOGGING_RULES", "*.debug=false")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# 插件目录名带中划线无法直接 import，把插件根目录挂到 path 上按 ui.cards 加载
sys.path.insert(0, os.path.join(ROOT, "plugins", "plugin-marketplace"))

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication, QScrollArea, QVBoxLayout, QWidget, QGridLayout

app = QApplication(sys.argv)


def make_meta(i, long_desc=True):
    return {
        "name": f"plugin-demo-{i:02d}",
        "description": "这是一个用于测试滚动性能的插件描述文本，长度中等偏长，用来模拟真实换行场景。" if long_desc else f"短描述 {i}",
        "version": f"1.{i}.0",
        "author": "tester",
        "downloads": 1234 + i,
        "categories": ["tool", "ui", "demo"] if i % 2 else ["tool"],
        "keywords": ["kw1", "kw2", "kw3"],
        "_marketplace": "official",
    }


def build_list_rows(n=50):
    from ui.cards import _PluginRow

    content = QWidget()
    lay = QVBoxLayout(content)
    lay.setContentsMargins(12, 8, 12, 8)
    lay.setSpacing(6)
    lay.setAlignment(Qt.AlignTop)
    for i in range(n):
        row = _PluginRow(make_meta(i), installed=(i % 3 == 0), parent=content, font_size=14)
        lay.addWidget(row)
    return content


def build_explore_grid(n=30):
    from ui.cards import _ExploreCard

    content = QWidget()
    root = QVBoxLayout(content)
    root.setContentsMargins(0, 0, 0, 0)
    grid = QGridLayout()
    grid.setSpacing(10)
    for c in range(3):
        grid.setColumnStretch(c, 1)
    for i in range(n):
        card = _ExploreCard(make_meta(i), installed=(i % 3 == 0), parent=content, font_size=14)
        grid.addWidget(card, i // 3, i % 3)
    root.addLayout(grid)
    root.addStretch(1)
    return content


def bench(content, label, frames=120, step=24):
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    scroll.setWidget(content)
    scroll.resize(1180, 760)
    scroll.show()
    app.processEvents()

    bar = scroll.verticalScrollBar()
    bar.setValue(0)
    app.processEvents()

    # 预热（样式解析 / 字体 / 缓存）
    for _ in range(3):
        content.grab()
    app.processEvents()

    frame_times = []
    prof = cProfile.Profile()
    prof.enable()
    v = 0
    for f in range(frames):
        t0 = time.perf_counter()
        v += step
        if v > bar.maximum():
            v = 0
        bar.setValue(v)
        content.grab()  # 强制重绘
        app.processEvents()
        frame_times.append((time.perf_counter() - t0) * 1000)
    prof.disable()

    frame_times.sort()
    avg = statistics.mean(frame_times)
    p50 = frame_times[len(frame_times) // 2]
    p95 = frame_times[int(len(frame_times) * 0.95)]
    print(f"\n[{label}] frames={frames} step={step}px")
    print(f"  avg={avg:.2f}ms  p50={p50:.2f}ms  p95={p95:.2f}ms  max={frame_times[-1]:.2f}ms")

    s = io.StringIO()
    ps = pstats.Stats(prof, stream=s).sort_stats("cumulative")
    ps.print_stats(22)
    out = s.getvalue()
    lines = out.splitlines()
    start = next((i for i, l in enumerate(lines) if "ncalls" in l), 0)
    print("\n".join(lines[max(0, start - 2): start + 26]))
    scroll.hide()
    return avg


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    if "--patched" in sys.argv:
        from ui._icon_cache_patch import _apply_icon_cache_patch
        applied = _apply_icon_cache_patch()
        print(f"icon cache patch applied: {applied}")
    if which in ("all", "explore"):
        bench(build_explore_grid(), "探索页 _ExploreCard 网格(30)")
    if which in ("all", "list"):
        bench(build_list_rows(), "列表页 _PluginRow 行(50)")
    print("\n基准帧预算: 16.7ms (60fps)；超预算即掉帧")
