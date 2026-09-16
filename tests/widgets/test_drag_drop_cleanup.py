# -*- coding: utf-8 -*-
"""拖放 OLE 注册清理与托盘清理回归测试（T14）。

背景（2026-09-16 WER 口径 28.4% CoreMessaging failfast 族）：
QWidget 析构后若 OLE 端仍持有其拖放注册，系统会在 COM 回调里触达已释放的
drop target；托盘图标（Shell_NotifyIcon）注册未摘除同理会向销毁中的宿主投递
tray 消息。两族都表现为退出/关闭期 failfast，且因发生在 native 层而无
Python 现场。本文件用替身对象断言「关闭/退出路径确实摘除了注册」。

覆盖：
* 各拖放热区的 closeEvent 确实关掉 acceptDrops / 拖拽模式；
* TrayManager.cleanup 的 hide + deleteLater + 幂等（已删对象不抛）；
* aboutToQuit 链上托盘清理**先于**会话保存等后续步骤执行，且失败不中断。

运行::

    python -m pytest tests/widgets/test_drag_drop_cleanup.py -v
"""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtGui import QCloseEvent  # noqa: E402


def _close_event() -> QCloseEvent:
    """真 QCloseEvent：部分 closeEvent 会交给 super()，替身对象类型不匹配。"""
    return QCloseEvent()


# ─── B. 拖放 OLE 注册清理 ───────────────────────────────────────────


def test_sendable_text_edit_close_event_disables_drops(qapp):
    from app.widgets.bottom_input_area import SendableTextEdit

    edit = SendableTextEdit()
    assert edit.acceptDrops() is True, "前置：输入框默认接受拖放"
    edit.closeEvent(_close_event())
    assert edit.acceptDrops() is False


def test_project_selector_card_close_event_disables_all_drops(qapp):
    from app.widgets.cards.settings.project_selector_card import ProjectSelectorCardContent

    content = ProjectSelectorCardContent()
    assert content.acceptDrops() is True, "前置：卡本体接受拖放"
    assert content._content_widget.acceptDrops() is True
    assert content._scroll_area.acceptDrops() is True

    content.closeEvent(_close_event())

    assert content.acceptDrops() is False
    assert content._content_widget.acceptDrops() is False
    assert content._scroll_area.acceptDrops() is False


def test_model_list_editor_close_event_disables_internal_move(qapp):
    from PyQt5.QtWidgets import QListWidget

    from app.widgets.model_list_edit_dialog import ModelListEditorWidget

    editor = ModelListEditorWidget(["m1", "m2"])
    assert editor.listWidget.dragDropMode() == QListWidget.InternalMove, "前置：允许内部拖拽排序"
    editor.closeEvent(_close_event())
    assert editor.listWidget.dragDropMode() == QListWidget.NoDragDrop


def test_chat_area_module_drop_zone_cleared_by_host_close_event():
    """对话区拖放热区由宿主窗口清理。

    ChatAreaModule 是 UIModule（非 QWidget），Qt 不会调用它的 closeEvent；
    chat_container 的拖放注册只能在宿主窗口关闭时摘除。
    """
    from app.main_widget import OpenAIChatToolWindow

    host = MagicMock()
    host.chat_container = MagicMock()

    OpenAIChatToolWindow._release_drop_targets(host)

    host.chat_container.setAcceptDrops.assert_called_once_with(False)


def test_input_card_module_drop_zones_cleared_by_host_close_event():
    """输入卡与附件区拖放热区同样由宿主窗口清理。"""
    from app.main_widget import OpenAIChatToolWindow

    host = MagicMock()
    host.chat_container = MagicMock()
    host._input_card = MagicMock()
    host._attach_container = MagicMock()

    OpenAIChatToolWindow._release_drop_targets(host)

    host._input_card.setAcceptDrops.assert_called_once_with(False)
    host._attach_container.setAcceptDrops.assert_called_once_with(False)


def test_release_drop_targets_tolerates_missing_widgets():
    """热区尚未装配（None）或访问抛错时不得中断关闭流程。"""
    from app.main_widget import OpenAIChatToolWindow

    host = MagicMock(spec=[])  # 无任何热区属性
    OpenAIChatToolWindow._release_drop_targets(host)  # 不抛即通过

    host2 = MagicMock()
    host2.chat_container = MagicMock()
    host2.chat_container.setAcceptDrops.side_effect = RuntimeError("deleted")
    OpenAIChatToolWindow._release_drop_targets(host2)  # 容错

    # class _HostStub 不再需要（上面已改成直接调 _release_drop_targets）


# ─── C. 托盘 Shell_NotifyIcon 清理 ──────────────────────────────────


def test_tray_manager_cleanup_hides_icon():
    from app.tray_manager import TrayManager

    tm = TrayManager.__new__(TrayManager)  # 避开单例 __init__ 的真托盘创建
    icon = MagicMock()
    tm._tray_icon = icon

    tm.cleanup()

    icon.hide.assert_called_once()
    icon.deleteLater.assert_called_once()
    assert tm._tray_icon is None


def test_tray_manager_cleanup_swallows_deleted_icon():
    """已销毁（sip.isdeleted）的图标：跳过操作，置空且不抛。"""
    from PyQt5 import sip

    from app.tray_manager import TrayManager

    tm = TrayManager.__new__(TrayManager)
    icon = MagicMock()
    tm._tray_icon = icon

    _orig_isdeleted = sip.isdeleted
    sip.isdeleted = lambda obj: True
    try:
        tm.cleanup()
    finally:
        sip.isdeleted = _orig_isdeleted

    icon.hide.assert_not_called()
    assert tm._tray_icon is None


def test_about_to_quit_calls_tray_cleanup_first(monkeypatch):
    """托盘清理必须先于 stop_subagent_log_cleanup 与会话保存执行。

    顺序即语义：托盘注册摘除越早，退出期 Shell 回调窗口越小。
    """
    from app import tray_manager as tray_mod
    from app.main_widget import OpenAIChatToolWindow
    from app.core import window_registry as wr_mod

    calls: list = []

    fake_tm = MagicMock()
    fake_tm.cleanup.side_effect = lambda: calls.append("tray_cleanup")
    monkeypatch.setattr(tray_mod.TrayManager, "get_instance", classmethod(lambda cls: fake_tm))
    monkeypatch.setattr(
        OpenAIChatToolWindow,
        "stop_subagent_log_cleanup",
        classmethod(lambda cls: calls.append("stop_log_cleanup")),
    )
    monkeypatch.setattr(wr_mod, "alive_window_instances", lambda: [])

    OpenAIChatToolWindow._on_app_about_to_quit()

    assert calls[0] == "tray_cleanup", f"托盘清理未最先执行: {calls}"
    assert "stop_log_cleanup" in calls


def test_about_to_quit_continues_after_tray_cleanup_failure(monkeypatch):
    """托盘清理抛异常不得中断退出保存链（退出期批量保存是用户数据保障）。"""
    from app import tray_manager as tray_mod
    from app.core import window_registry as wr_mod
    from app.main_widget import OpenAIChatToolWindow

    calls: list = []

    fake_tm = MagicMock()
    fake_tm.cleanup.side_effect = RuntimeError("tray boom")
    monkeypatch.setattr(tray_mod.TrayManager, "get_instance", classmethod(lambda cls: fake_tm))
    monkeypatch.setattr(
        OpenAIChatToolWindow,
        "stop_subagent_log_cleanup",
        classmethod(lambda cls: calls.append("stop_log_cleanup")),
    )
    monkeypatch.setattr(wr_mod, "alive_window_instances", lambda: [])

    OpenAIChatToolWindow._on_app_about_to_quit()

    assert "stop_log_cleanup" in calls, "托盘清理失败后退出保存链被中断"
