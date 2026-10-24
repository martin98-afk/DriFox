# -*- coding: utf-8 -*-
"""InfoBar 死壳防线 + notify 主线程投递测试

背景（2026-10-24 崩溃）：后台线程经 services.notify 直弹 InfoBar 后被 GC，
InfoBarManager.infoBars[parent] 残留死壳；新 InfoBar 的 showEvent → add →
_pos 遍历兄弟条时访问死壳 height() 抛
``RuntimeError: wrapped C/C++ object of type InfoBar has been deleted``。

两层防护：
1. info_bar_guard.install_guard：monkeypatch InfoBarManager.add，入口清洗死壳
2. ui_core._ui_notify：后台线程调用时投递主线程执行
"""

import threading

import pytest
import shiboken6 as sip
from PySide6.QtCore import Qt
from qfluentwidgets import InfoBar, InfoBarIcon, InfoBarPosition
from qfluentwidgets.components.widgets.info_bar import InfoBarManager

from app.utils import info_bar_guard


@pytest.fixture
def parent_widget(qtbot):
    from PySide6.QtWidgets import QWidget

    w = QWidget()
    qtbot.addWidget(w)
    w.resize(400, 300)
    # 子控件 showEvent 只在父链可见时分发（add 在 showEvent 里注册），
    # 父不 show 则 manager 组永不建立
    w.show()
    return w


def _new_bottom_bar(parent):
    """duration=-1 常驻条（与崩溃场景同款：BOTTOM 位置多条共存）"""
    return InfoBar.new(
        InfoBarIcon.INFORMATION,
        "t",
        "c",
        orient=Qt.Orientation.Vertical,
        isClosable=False,
        duration=-1,
        position=InfoBarPosition.BOTTOM,
        parent=parent,
    )


# ── 防线：add 入口清洗死壳 ──────────────────────────────


def test_install_guard_idempotent():
    """重复安装不叠加 patch"""
    info_bar_guard.install_guard()
    once = InfoBarManager.add
    info_bar_guard.install_guard()
    assert InfoBarManager.add is once
    assert getattr(InfoBarManager.add, "_drifox_guarded", False)


def test_add_purges_dead_sibling_and_survives(qtbot, parent_widget):
    """死壳在列时新 InfoBar 弹出不崩，且死壳被摘除

    未修前的崩溃形态：第一条被绕过 closeEvent 析构（跨线程 GC / 连带删除），
    第二条 show → add → _pos 遍历兄弟死壳 → RuntimeError。
    """
    info_bar_guard.install_guard()
    manager = InfoBarManager.make(InfoBarPosition.BOTTOM)

    a = _new_bottom_bar(parent_widget)
    assert a in manager.infoBars[parent_widget]

    # 等效「绕过 closeEvent 的析构」：C++ 立即销毁，wrapper 变死壳
    sip.delete(a)

    # 未修前此行抛 RuntimeError（showEvent → add → _pos 摸死壳）
    b = _new_bottom_bar(parent_widget)
    qtbot.addWidget(b)

    assert a not in manager.infoBars[parent_widget]
    assert b in manager.infoBars[parent_widget]


def test_add_keeps_alive_siblings(qtbot, parent_widget):
    """存活兄弟条不受清洗影响，堆叠定位正常"""
    info_bar_guard.install_guard()
    manager = InfoBarManager.make(InfoBarPosition.BOTTOM)

    a = _new_bottom_bar(parent_widget)
    b = _new_bottom_bar(parent_widget)
    qtbot.addWidget(a)
    qtbot.addWidget(b)

    bars = manager.infoBars[parent_widget]
    assert a in bars and b in bars


# ── 治本：notify 后台线程投递主线程 ──────────────────────


def test_ui_notify_dispatches_to_main_thread(qtbot, monkeypatch):
    """后台线程调 notify 时 InfoBar 必须在主线程创建"""
    import app.plugins.capabilities.providers.ui_core as ui_core

    monkeypatch.setattr(ui_core, "_resolve_tab_window", lambda: None, raising=False)

    created_threads = []
    monkeypatch.setattr(
        InfoBar,
        "success",
        classmethod(lambda cls, *a, **k: created_threads.append(threading.current_thread())),
    )

    class _Host:
        def window(self):
            return None

    notify = ui_core._ui_notify(_Host())

    worker = threading.Thread(target=lambda: notify("t", "m"))
    worker.start()
    worker.join(timeout=5)

    # 后台线程内不得同步创建
    assert created_threads == []

    qtbot.waitUntil(lambda: len(created_threads) == 1, timeout=3000)
    assert created_threads[0] is threading.main_thread()


def test_post_to_main_runs_in_main_thread(qtbot):
    """post_to_main 投递的回调在主线程执行"""
    results = []
    assert info_bar_guard.post_to_main(lambda: results.append(threading.current_thread()))
    qtbot.waitUntil(lambda: len(results) == 1, timeout=3000)
    assert results[0] is threading.main_thread()
