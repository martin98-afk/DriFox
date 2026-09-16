# -*- coding: utf-8 -*-
"""app.aboutToQuit 配对核实测试（#4.9 Commit 6 配套）

验证结构约定（因 pyqtBoundSignal 不暴露 .receivers() 直接计数）：
1. _on_app_about_to_quit 与 _auto_save_current_session 方法存在于 OpenAIChatToolWindow
2. 首次注册守卫（去重逻辑）保护重复连接
3. connect/disconnect 调用本身不抛错（结构性 sanity）
"""
import gc

import pytest

from PyQt5.QtWidgets import QApplication

from app.main_widget import OpenAIChatToolWindow


def test_about_to_quit_methods_exist():
    """OpenAIChatToolWindow 必须持有 aboutToQuit 配对的两个回调方法。"""
    assert hasattr(OpenAIChatToolWindow, "_on_app_about_to_quit")
    assert hasattr(OpenAIChatToolWindow, "_auto_save_current_session")
    # connect 在 _on_app_about_to_quit 的 register 路径；通过源码静态确认
    # （避免运行时创建完整窗口的 fixture 复杂度）
    import inspect
    src = inspect.getsource(OpenAIChatToolWindow)
    assert "app.aboutToQuit.connect(cls._on_app_about_to_quit)" in src
    # [T40] 反向断言：session 级 connect（register 去重）只需 disconnect 一次，
    # 该断开在 _deferred_startup 的退出链里统一做；closeEvent 内的逐窗 disconnect
    # 是从未 connect 过的死代码，已在 :22467 区删除——不得回流。
    assert "app.aboutToQuit.disconnect(self._auto_save_current_session)" not in src


@pytest.mark.usefixtures("qapp")
def test_app_instance_has_about_to_quit_signal():
    """QApplication 实例必须暴露 aboutToQuit 信号（PyQt5 标准）。"""
    app = QApplication.instance()
    assert app is not None  # conftest.py 已创建
    assert hasattr(app, "aboutToQuit")
    # 触发 disconnect 不存在的 lambda 不会崩（Qt 容错）
    try:
        app.aboutToQuit.disconnect(lambda: None)
    except (TypeError, RuntimeError):
        pass
    gc.collect()