# -*- coding: utf-8 -*-
"""[scroll-hot] 微基准：量化 _sync_scroll_maximum 的 TTL 命中 vs 未命中成本。

仅测量方法级开销（不拉起完整窗口），用于验证锚定期每拍成本下降幅度。
运行：python tools/bench_scroll_hot.py

方法（T35 重建，对齐对方 T23 需求原文）：
- 替身 widget 容器（无 WebEngine 子件）：QScrollArea + QWidget 容器装
  N 个 QLabel + QVBoxLayout；
- 每轮 ``layout.invalidate()`` 模拟生产热路径的布局脏态——**关键**：无此步
  会因 QLabel 的 sizeHint 缓存严重低估 miss 成本（sizeHint 只在布局脏时
  才真正重算）；
- 未命中（miss）：重置 ``_scroll_max_cache = None`` 后调用，走完整
  ``container.sizeHint()`` O(卡片数) 布局计算；
- 命中（hit）：预置 ``_scroll_max_cache = (now, val)``，TTL 窗口内直接复用。

⚠️ 被测逻辑以内联方式复制自 ``OpenAIChatToolWindow._sync_scroll_maximum``
（基线 a9b45c6f，TTL 常量同源：SCROLL_MAX_CACHE_TTL = 0.12）。不直接
import main_widget 的原因：独立脚本 import 它会级联插件链并在启动早期
原生崩溃（0xC0000409，另案），基准脚本必须独立可跑。逻辑变更时需手动
同步本文件（顶部 ``# [sync]`` 标记行）。
"""

import os
import sys
import time
from contextlib import contextmanager

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QCoreApplication, Qt
from PyQt5.QtWidgets import QApplication, QLabel, QScrollArea, QVBoxLayout, QWidget

# [sync] 与 app/main_widget.py:206 同源
SCROLL_MAX_CACHE_TTL = 0.12


def _app():
    QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)  # type: ignore[attr-defined]
    return QApplication.instance() or QApplication(sys.argv)


class _SyncScrollHost:
    """替身 host：提供 _sync_scroll_maximum 触达的最小属性面与逻辑副本。"""

    def __init__(self, scroll_area):
        self.chat_scroll_area = scroll_area
        self._scroll_max_cache = None
        self._programmatic_scroll_depth = 0

    @contextmanager
    def _programmatic_scroll(self):
        """与 OpenAIChatToolWindow._programmatic_scroll 同语义：标记程序置底。"""
        self._programmatic_scroll_depth += 1
        try:
            yield
        finally:
            self._programmatic_scroll_depth -= 1

    def _sync_scroll_maximum(self) -> int:
        """[sync] 逻辑内联自 OpenAIChatToolWindow._sync_scroll_maximum（a9b45c6f）。

        TTL 窗口内命中：跳过布局计算，返回「Qt 现值」与「上次抬高值」较大者；
        未命中：container.sizeHint() O(卡片数) 完整布局计算 + setMaximum 抬高。
        """
        scroll_bar = self.chat_scroll_area.verticalScrollBar()
        now = time.monotonic()
        cache = self._scroll_max_cache
        if cache is not None and (now - cache[0]) < SCROLL_MAX_CACHE_TTL:
            return max(scroll_bar.maximum(), cache[1])
        container = self.chat_scroll_area.widget()
        if container is not None:
            real = container.sizeHint().height() - self.chat_scroll_area.viewport().height()
            if real > scroll_bar.maximum():
                with self._programmatic_scroll():
                    scroll_bar.setMaximum(max(0, real))
        self._scroll_max_cache = (now, scroll_bar.maximum())
        return scroll_bar.maximum()


def _build_host(cards: int):
    """QScrollArea + 容器装 cards 个 QLabel（无 WebEngine）。"""
    area = QScrollArea()
    container = QWidget(area)
    lay = QVBoxLayout(container)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(4)
    for i in range(cards):
        lay.addWidget(QLabel(f"card-{i}: " + "x" * 120))
    area.setWidget(container)
    area.setWidgetResizable(True)
    area.resize(800, 600)
    return _SyncScrollHost(area), container


def _bench_round(host, container, rounds: int, hit: bool):
    """跑 rounds 次，返回 ms/次。hit=False 时每轮前置 invalidate + 清缓存（miss）。"""
    fn = host._sync_scroll_maximum
    t0 = time.perf_counter()
    for _ in range(rounds):
        if not hit:
            container.layout().invalidate()  # 布局脏态：强制 sizeHint 真重算
            host._scroll_max_cache = None
        fn()
    t1 = time.perf_counter()
    return (t1 - t0) * 1000.0 / rounds


def main():
    app = _app()
    print(f"[scroll-hot] SCROLL_MAX_CACHE_TTL={SCROLL_MAX_CACHE_TTL}s  offscreen={os.environ.get('QT_QPA_PLATFORM')}")

    cards = 100
    host, container = _build_host(cards)
    host.chat_scroll_area.setParent(None)
    host.chat_scroll_area.resize(800, 600)
    host.chat_scroll_area.show()
    app.processEvents()

    rounds = 200
    _bench_round(host, container, 20, hit=False)  # 预热

    miss_ms = _bench_round(host, container, rounds, hit=False)
    hit_ms = _bench_round(host, container, rounds, hit=True)
    drop = (1 - hit_ms / miss_ms) * 100 if miss_ms > 0 else 0.0
    print(f"[scroll-hot] cards={cards}: miss={miss_ms:.4f}ms/次, hit={hit_ms:.4f}ms/次, 降幅={drop:.1f}%")

    print("[scroll-hot] 规模扫描（每档 miss/hit/降幅）：")
    for n in (100, 300, 600, 1000):
        h2, c2 = _build_host(n)
        h2.chat_scroll_area.show()
        app.processEvents()
        _bench_round(h2, c2, 20, hit=False)  # 预热
        m2 = _bench_round(h2, c2, max(30, 600 // max(n // 100, 1)), hit=False)
        hh2 = _bench_round(h2, c2, 200, hit=True)
        d2 = (1 - hh2 / m2) * 100 if m2 > 0 else 0.0
        print(f"  cards={n:<5d} miss={m2:.4f}ms/次  hit={hh2:.4f}ms/次  降幅={d2:.1f}%")

    print("[scroll-hot] done")


if __name__ == "__main__":
    main()
