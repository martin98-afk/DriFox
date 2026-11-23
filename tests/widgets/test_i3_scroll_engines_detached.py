# -*- coding: utf-8 -*-
"""I3 回归：三处内滚容器摘 qfw 平滑引擎 + install_fast_wheel 单步到位行为

断言：
1. 三处落点源码不再实例化 SmoothScrollDelegate（静态守卫）
2. 落点 1/2 走 install_fast_wheel(step_ratio=0.4)；落点 3 额外断言
   setSmoothMode(NO_SMOOTH) 关引擎 + native_scrollbar=False（保 Fluent 条显隐链）
3. 行为：滚轮事件经 _FastWheelFilter 后 scrollbar value 立即单步到位
   （smooth_ms=0 无动画插值），高精度触控板 delta（非 120 倍数）放行走原生
"""

import sys
from pathlib import Path

import pytest

pytest.importorskip("PyQt5.QtWidgets")

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

_FILES = {
    "viewers": _REPO_ROOT / "app" / "widgets" / "card_viewers.py",
    # 落点 2（plugins/file-tree/ui/cards.py）按用户指令撤销：树视口保留原装
    # SmoothScrollDelegate（无性能问题），不纳入守卫。
    "input": _REPO_ROOT / "app" / "widgets" / "bottom_input_area.py",
}


def test_no_smooth_scroll_delegate_instantiation_left():
    """三处落点不得再实例化 SmoothScrollDelegate（注释提及不算）"""
    for name, path in _FILES.items():
        src = path.read_text(encoding="utf-8")
        for line in src.split("\n"):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            assert "SmoothScrollDelegate(" not in stripped, f"{name}: 仍残留 SmoothScrollDelegate 实例化: {stripped}"


def test_three_call_sites_wired_correctly():
    """落点参数断言：消息卡走接管器 step_ratio=0.4；输入框关引擎+native_scrollbar=False"""
    viewers = _FILES["viewers"].read_text(encoding="utf-8")
    assert "install_fast_wheel(self.text_edit, step_ratio=0.4)" in viewers
    inp = _FILES["input"].read_text(encoding="utf-8")
    assert inp.count("setSmoothMode(SmoothMode.NO_SMOOTH)") == 2, "输入框两方向平滑引擎均须关闭"
    assert "install_fast_wheel(self, step_ratio=0.4, native_scrollbar=False)" in inp


def test_fast_wheel_single_step_no_interpolation():
    """行为：滚轮事件→value 立即单步到位（无动画插值）；高精度 delta 放行"""
    from PyQt5.QtCore import QPoint, QPointF, Qt
    from PyQt5.QtGui import QWheelEvent
    from PyQt5.QtWidgets import QApplication, QScrollArea, QWidget

    from app.utils.scroll_tuning import install_fast_wheel

    app = QApplication.instance() or QApplication(["i3-fast-wheel"])
    area = QScrollArea()
    inner = QWidget()
    inner.setFixedSize(200, 4000)
    area.setWidget(inner)
    area.resize(200, 400)
    bar = area.verticalScrollBar()
    assert install_fast_wheel(area) is True
    flt = getattr(area, "_drifox_fast_wheel_filter")

    def wheel(notches: int) -> QWheelEvent:
        return QWheelEvent(
            QPointF(10, 10), QPointF(10, 10), QPoint(0, 0), QPoint(0, -120 * notches),
            Qt.NoButton, Qt.NoModifier, Qt.ScrollUpdate, False,
        )

    assert flt.eventFilter(area.viewport(), wheel(1)) is True
    step_value = bar.value() - bar.minimum()
    assert step_value > 0, "单格滚轮必须立即产生位移（一次到位）"
    assert bar.value() == bar.minimum() + step_value

    v_before = bar.value()
    assert flt.eventFilter(area.viewport(), wheel(2)) is True
    assert bar.value() == v_before + step_value * 2, "两格位移必须等于两倍单步（线性无插值残留）"

    # 高精度触控板 delta（非 120 整数倍）：过滤器放行，交给原生像素滚动
    hi_res = QWheelEvent(
        QPointF(10, 10), QPointF(10, 10), QPoint(0, -6), QPoint(0, -6),
        Qt.NoButton, Qt.NoModifier, Qt.ScrollUpdate, False,
    )
    assert flt.eventFilter(area.viewport(), hi_res) is False

    area.deleteLater()
