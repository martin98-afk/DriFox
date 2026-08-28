# -*- coding: utf-8 -*-
"""
TabManagerWindow 几何持久化回归测试

保护以下场景：
1. save → restore 不应该改变窗口位置（frame 位置稳定）
2. minimize → restore 后窗口不应向上偏移

根因历史：
- x()/y() 返回 FRAME 在屏幕上的位置（含标题栏）
- setGeometry(x, y, w, h) 把 (x, y) 当作 CLIENT 区域位置
- 旧实现用 x()/y() 保存，再用 setGeometry 还原 → 每次恢复
  窗口向上偏移 title bar 高度（约 65px）
"""

import json

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from app.utils.config import Settings
from app.widgets.tab_manager_window import TabManagerWindow


@pytest.fixture(autouse=True)
def reset_singleton():
    """每个测试前后重置单例"""
    from app.tray_manager import TrayManager

    TabManagerWindow._instance = None
    TrayManager.get_instance()._tab_manager_window = None
    yield
    TabManagerWindow._instance = None
    TrayManager.get_instance()._tab_manager_window = None


@pytest.fixture(autouse=True)
def reset_saved_geometry():
    """每个测试前后清理保存的几何数据"""
    settings = Settings.get_instance()
    original = settings.tab_manager_geometry.value
    settings.tab_manager_geometry.value = ""
    yield
    settings.tab_manager_geometry.value = original


def _frame_top_y(widget):
    """获取窗口 FRAME 在屏幕上的 Y 坐标（用户视觉上的窗口顶部）"""
    return widget.frameGeometry().y()


def _frame_height(widget):
    """获取窗口 FRAME 的高度（用户视觉上的窗口高度，含标题栏）"""
    return widget.frameGeometry().height()


def test_save_uses_geometry_not_frame_position(qtbot):
    """保存的坐标必须是 CLIENT 区域位置（与 setGeometry 语义一致）"""
    tm = TabManagerWindow.create_instance()
    qtbot.addWidget(tm)
    tm.show()
    qtbot.wait(100)

    # 把窗口移动到一个明显非默认位置
    tm.setGeometry(300, 250, 900, 700)
    QApplication.processEvents()

    g = tm.geometry()
    fg = tm.frameGeometry()

    # 触发保存（立即调用绕过防抖）
    tm._do_save_geometry()

    saved = json.loads(Settings.get_instance().tab_manager_geometry.value)

    # 保存的 x/y 必须等于 geometry() 的 x/y（即 client 位置），
    # 不能等于 frameGeometry() 的 x/y（即 frame 位置）
    assert saved["x"] == g.x(), (
        f"保存的 x 应为 client area x={g.x()}，但保存了 frame x={fg.x()}。"
        f"若保存 frame 位置再用 setGeometry 还原，每次都会向上偏移 title bar 高度。"
    )
    assert saved["y"] == g.y(), f"保存的 y 应为 client area y={g.y()}，但保存了 frame y={fg.y()}。"
    assert saved["w"] == g.width()
    assert saved["h"] == g.height()


def test_round_trip_preserves_frame_position(qtbot):
    """保存后再还原，frame 位置必须稳定（不应向上偏移）"""
    tm = TabManagerWindow.create_instance()
    qtbot.addWidget(tm)
    tm.show()
    qtbot.wait(100)

    # 把窗口放到 (300, 250)，模拟用户拖到该位置
    tm.setGeometry(300, 250, 900, 700)
    QApplication.processEvents()
    qtbot.wait(50)

    frame_y_before = _frame_top_y(tm)
    frame_h_before = _frame_height(tm)
    assert frame_y_before != 250  # frame_y 必定不等于 setGeometry.y

    # 触发保存
    tm._do_save_geometry()

    # 模拟窗口被最小化再恢复（OS 会触发 showEvent → _restore_geometry）
    # 关闭并重新打开，等价于 hide() + show() 触发的 showEvent 流程
    tm.hide()
    tm.show()
    qtbot.wait(100)
    QApplication.processEvents()

    # 关键断言：frame Y 不能偏移
    frame_y_after = _frame_top_y(tm)
    frame_h_after = _frame_height(tm)

    # setGeometry(...) 内部可能对几何做 1px 量级的取整，但 65px 量级的
    # 整体偏移属于此 bug 的特征，必须为 0
    assert abs(frame_y_after - frame_y_before) <= 1, (
        f"窗口 Y 位置偏移了 {frame_y_after - frame_y_before}px（期望 ≤1px）。"
        f"这是「保存 frame 位置却用 client 语义还原」导致的 title bar 偏移 bug。"
    )
    assert frame_h_after == frame_h_before, f"窗口高度变了 {frame_h_before} → {frame_h_after}"


def test_multiple_restores_do_not_drift(qtbot):
    """多次 minimize → restore 累积不应让窗口持续向上偏移"""
    tm = TabManagerWindow.create_instance()
    qtbot.addWidget(tm)
    tm.show()
    qtbot.wait(100)

    # 初始位置
    tm.setGeometry(500, 400, 1020, 762)
    QApplication.processEvents()
    qtbot.wait(50)
    tm._do_save_geometry()
    qtbot.wait(250)  # 等防抖完成

    initial_frame_y = _frame_top_y(tm)
    initial_frame_h = _frame_height(tm)

    # 反复触发 _restore_geometry（等价于多次 minimize+restore）
    for _ in range(5):
        tm.hide()
        tm.show()
        qtbot.wait(100)
        QApplication.processEvents()

    final_frame_y = _frame_top_y(tm)
    final_frame_h = _frame_height(tm)

    # 多次 restore 后的累积偏移必须为 0
    drift = final_frame_y - initial_frame_y
    assert abs(drift) <= 1, f"5 次 restore 后窗口累积向上漂移 {drift}px。这是 round-trip 坐标语义不一致导致的回归。"
    assert final_frame_h == initial_frame_h
