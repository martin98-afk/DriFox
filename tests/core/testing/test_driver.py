# -*- coding: utf-8 -*-
"""驱动库单测：selector 匹配 / bus 投递 / ARM 拒绝 / 观测降级。

运行约束（S2 P2b 结论）：offscreen 纯 widgets 环境，不依赖 WebEngine。
"""

from __future__ import annotations

import threading

import pytest


@pytest.fixture()
def armed(qtbot):
    from tools.ui_driver import setArmed

    setArmed(True)
    yield
    setArmed(False)


def _make_tree(qtbot):
    """构造 三层控件树：window → grid → label/button/lineEdit。"""
    from PyQt5.QtWidgets import QGridLayout, QLineEdit, QPushButton, QWidget

    from app.widgets.elided_label import _ElidedLabel

    win = QWidget()
    grid = QGridLayout(win)
    btn = QPushButton("发送")
    btn.setObjectName("send_btn")
    label = _ElidedLabel("任务 <2> 进行中")
    edit = QLineEdit()
    edit.setObjectName("input_box")
    grid.addWidget(label, 0, 0)
    grid.addWidget(btn, 1, 0)
    grid.addWidget(edit, 2, 0)
    qtbot.addWidget(win)
    win.show()
    return win, label, btn, edit


class TestQuery:
    def test_find_by_object_name(self, qtbot, armed):
        from tools.ui_driver import find

        win, _label, _btn, _edit = _make_tree(qtbot)
        hit = find({"objectName": "send_btn"}, root=win)
        assert hit is not None and hit.objectName() == "send_btn"

    def test_find_by_cls_and_text(self, qtbot, armed):
        from tools.ui_driver import find

        win, label, _btn, _edit = _make_tree(qtbot)
        hit = find({"cls": "_ElidedLabel", "text": "任务"}, root=win)
        assert hit is label

    def test_find_miss_returns_none(self, qtbot, armed):
        from tools.ui_driver import find

        win, _label, _btn, _edit = _make_tree(qtbot)
        assert find({"objectName": "__nope__"}, root=win) is None

    def test_tree_token_friendly(self, qtbot, armed):
        from tools.ui_driver import tree

        win, _label, _btn, _edit = _make_tree(qtbot)
        snap = tree(win, depth=2)
        assert snap is not None
        assert snap["cls"] == "QWidget"
        assert snap["children_count"] >= 3
        assert "children" in snap


class TestBus:
    def test_not_armed_rejects(self, qtbot):
        from tools.ui_driver import is_armed, setArmed
        from tools.ui_driver.bus import DriverNotArmedError, invoke

        setArmed(False)
        with pytest.raises(DriverNotArmedError):
            invoke(lambda: 1)
        assert is_armed() is False

    def test_invoke_main_thread_execution(self, qtbot, armed):
        from tools.ui_driver import find
        from tools.ui_driver.bus import invoke

        win, _label, _btn, edit = _make_tree(qtbot)
        errors: list[Exception] = []
        results: list[str] = []

        def _worker():
            try:
                # 工作线程里摸控件：经 invoke 投递主线程改文本并回读（必修 1/2 覆盖）
                def _do():
                    edit.setText("via-bus")
                    return edit.text()

                results.append(invoke(_do))

                def _boom():
                    raise ValueError("boom")

                invoke(_boom)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        t = threading.Thread(target=_worker)
        t.start()
        # 主线程必须泵事件（queued 请求靠主线程派发）；join() 不泵会饿死投递
        qtbot.waitUntil(lambda: not t.is_alive(), timeout=30000)
        assert results == ["via-bus"], f"跨线程投递结果异常: {results}"
        assert len(errors) == 1 and isinstance(errors[0], ValueError), f"异常未跨线程重抛: {errors}"
        assert edit.text() == "via-bus"
        assert find({"objectName": "input_box"}, root=win) is edit

    def test_invoke_timeout(self, qtbot, armed):
        """timeout_ms 生效：主线程不泵事件（本用例主线程纯轮询 sleep）时抛 DriverTimeoutError。"""
        import time as _time

        from tools.ui_driver.bus import DriverTimeoutError, invoke

        errors: list[Exception] = []

        def _worker():
            try:
                invoke(lambda: None, timeout_ms=200)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        t = threading.Thread(target=_worker)
        t.start()
        deadline = _time.monotonic() + 30
        while t.is_alive() and _time.monotonic() < deadline:
            _time.sleep(0.05)  # 主线程不泵：queued 请求不派发 → 必走 200ms 超时分支
        assert not t.is_alive(), "超时路径死锁"
        assert errors and isinstance(errors[0], DriverTimeoutError), f"应超时: {errors}"


class TestActionsObserve:
    def test_click_and_type(self, qtbot, armed):
        from tools.ui_driver import click, type_text

        win, _label, btn, edit = _make_tree(qtbot)
        assert click(btn) is True
        assert type_text(edit, "hello") is True
        assert edit.text() == "hello"
        assert click(edit) is False  # QLineEdit 无 click()

    def test_scroll_missing_bar(self, qtbot, armed):
        from tools.ui_driver import scroll

        win, _label, _btn, _edit = _make_tree(qtbot)
        assert scroll(win, 100) is False

    def test_screenshot_bytes(self, qtbot, armed):
        from tools.ui_driver import screenshot

        win, _label, _btn, _edit = _make_tree(qtbot)
        data = screenshot(win)
        assert isinstance(data, bytes) and data[:8] == b"\x89PNG\r\n\x1a\n"

    def test_memory_shape(self, qtbot, armed):
        from tools.ui_driver import memory

        snap = memory()
        assert snap["main_ws_mb"] is None or isinstance(snap["main_ws_mb"], float)
        assert "containers" in snap

    def test_state_degrades_gracefully(self, qtbot, armed):
        from tools.ui_driver import state

        snap = state()
        assert isinstance(snap, dict)
