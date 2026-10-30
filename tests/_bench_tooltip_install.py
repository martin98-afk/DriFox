# -*- coding: utf-8 -*-
"""临时微基准：hover tooltip filter 安装成本 A/B（不入库）

跑法：.venv/Scripts/python.exe tests/_bench_tooltip_install.py

同一进程内对比「懒建定时器（新）」vs「构造即建 2 个 QTimer（旧）」，
各跑 5 轮取中位数，剔除机器负载噪声。
"""

import os
import statistics
import sys
import time
import weakref

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["DRIFOX_DATA_DIR"] = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".drifox-test", "bench_tip")

from PyQt5.QtCore import QObject, QTimer
from PyQt5.QtWidgets import QApplication, QPushButton, QWidget

app = QApplication.instance() or QApplication([])

N = 210  # 与新建标签页实测控件量一致


class _LegacyFilter(QObject):
    """旧实现等价体：构造期创建 _timer + _guard 两个 QTimer 并连接"""

    def __init__(self, parent, text, delay_ms=400):
        super().__init__(parent)
        self._parent = weakref.ref(parent)
        self._text = text
        self._tooltip = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(delay_ms)
        self._timer.timeout.connect(self._noop)
        self._guard = QTimer(self)
        self._guard.setSingleShot(False)
        self._guard.setInterval(120)
        self._guard.timeout.connect(self._noop)
        parent.installEventFilter(self)
        parent.destroyed.connect(self._cleanup)

    def _noop(self):
        pass

    def _cleanup(self, *_a):
        pass


def make_hosts(n):
    host = QWidget()
    btns = []
    for i in range(n):
        b = QPushButton(f"btn{i}", host)
        b.setToolTip(f"tip{i}")
        btns.append(b)
    return host, btns


def bench(label, install_fn, rounds=5):
    vals = []
    for _ in range(rounds):
        host, btns = make_hosts(N)
        # 清掉 setToolTip 自动安装的 filter，保证从零开始
        from app.widgets.simple_hover_tooltip import _filters

        _filters.clear()
        t0 = time.perf_counter()
        install_fn(btns)
        vals.append((time.perf_counter() - t0) * 1000)
        host.deleteLater()
        app.processEvents()
    print(f"  {label:22s} 中位数={statistics.median(vals):6.2f}ms  全部=[{', '.join(f'{v:.1f}' for v in vals)}]")
    return statistics.median(vals)


def main():
    from app.widgets.simple_hover_tooltip import _filters, _HoverTooltipFilter

    def install_legacy(btns):
        for b in btns:
            _LegacyFilter(b, b.toolTip())

    def install_new(btns):
        for b in btns:
            _HoverTooltipFilter(b, b.toolTip())

    # 预热（首次会触发 Qt 内部各类初始化）
    _h1, _b1 = make_hosts(10)
    install_legacy(_b1)
    _filters.clear()
    _h2, _b2 = make_hosts(10)
    install_new(_b2)
    _filters.clear()

    print(f"控件数 N={N}（与新建标签页实测 210 一致）")
    old = bench("旧：构造即建 timer", install_legacy)
    new = bench("新：懒建 timer", install_new)
    _filters.clear()
    if new > 0:
        print(f"  降幅: {(1 - new / old) * 100:.0f}%（{old:.1f}ms → {new:.1f}ms，每次新建省 {old - new:.1f}ms）")

    # 附带：计时器数量差异（对象树规模）
    host, btns = make_hosts(N)
    _filters.clear()
    install_legacy(btns)
    n_old = len(host.findChildren(QTimer))
    _filters.clear()
    host.deleteLater()
    app.processEvents()
    host, btns = make_hosts(N)
    install_new(btns)
    n_new = len(host.findChildren(QTimer))
    print(f"  QTimer 子对象数：旧={n_old} 新={n_new}")


if __name__ == "__main__":
    main()
