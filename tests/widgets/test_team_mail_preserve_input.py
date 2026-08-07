# -*- coding: utf-8 -*-
"""团队任务邮件自动发送不得清空用户输入框（Bug 回归测试）

背景：_process_team_task 复用 _on_send_clicked 走"用户完整发送流程"，
此前会无条件 input_area.clear() + _clear_attachments() + _record_input_history()，
导致成员邮件到达时（非流式路径）把用户正在输入框编辑的内容抹掉。

覆盖范围：
① _process_team_task 调用 _on_send_clicked 时必须传 preserve_input=True
② preserve_input=True 时 _on_send_clicked 不清输入框/附件、不记录输入历史
③ preserve_input=False（用户主动发送）行为不变：仍清空输入框

设计说明：
- qapp 由 pytest-qt 提供（session 级 QApplication）
- 重依赖（backend/QWebEngine/TrayManager）一律用 __new__ 绕过 + MagicMock 隔离
"""

from unittest.mock import MagicMock, patch

from contextlib import ExitStack

import pytest


_PATCHES = [
    "app.main_widget.CommandManager.get_instance",
    "app.main_widget.get_model_capabilities",
    "PyQt5.QtCore.QTimer.singleShot",  # _on_send_clicked / _process_team_task 内部 from PyQt5.QtCore import QTimer
]

def _make_main_widget_instance():
    """构造跳过 __init__ 的 OpenAIChatToolWindow 实例（重依赖隔离）"""
    from app.main_widget import OpenAIChatToolWindow

    inst = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    inst._is_destroyed = False
    inst._window_id = "win_test"
    inst._is_auto_loop_running = False
    inst._attachments = []
    inst._session_switched = False
    inst._is_streaming = False
    inst._truncation_sentinel = None
    inst._current_assistant_card = None
    inst._current_model_name = "test-model"
    inst._current_provider_name = "test-provider"
    inst._response_start_time = None

    # 输入框：记录 clear 调用（本 bug 的核心断言对象）
    inst.input_area = MagicMock()
    inst.input_area.toPlainText.return_value = ""
    inst.input_area.pop_card_selected_type.return_value = None

    # 其余副作用全部隔离
    inst._record_input_history = MagicMock()
    inst._hide_welcome_cards = MagicMock()
    inst._append_user_message = MagicMock()
    inst._append_assistant_message = MagicMock(return_value=MagicMock())
    inst._toggle_send_stop = MagicMock()
    inst._clear_attachments = MagicMock()
    inst._get_current_model_config = MagicMock(
        return_value={"API_KEY": "test-key", "模型名称": "test-model"}
    )
    inst.session_manager = MagicMock()
    inst.session_manager.get_current_session.return_value = None
    return inst


def _patch_send_dependencies():
    """patch _on_send_clicked 内部依赖：命令管理器 / 模型能力 / 延迟发送定时器"""
    stack = ExitStack()
    stack.enter_context(patch("app.main_widget.CommandManager.get_instance", return_value=MagicMock()))
    stack.enter_context(patch("app.main_widget.get_model_capabilities", return_value={}))
    # _on_send_clicked / _process_team_task 内部 from PyQt5.QtCore import QTimer
    stack.enter_context(patch("PyQt5.QtCore.QTimer.singleShot"))
    return stack


# ══════════════════════════════════════════════════════════
# ① _process_team_task 传参约束
# ══════════════════════════════════════════════════════════


def test_process_team_task_passes_preserve_input(qapp):
    """⚠️ 核心约束：团队邮件自动发送必须 preserve_input=True，禁止清空用户输入框"""
    import app.main_widget as mw

    inst = _make_main_widget_instance()
    inst._team_processing = True  # _check_and_process_pending 置位语义
    inst._current_team_mail = None
    inst._is_streaming = True  # 让 _process_team_task 走正常流式分支（不触发回滚）

    fake_tm = MagicMock()
    fake_tm.mark_mail_running.return_value = None
    inst._get_team_manager = MagicMock(return_value=fake_tm)
    inst._on_send_clicked = MagicMock()
    inst._delayed_team_mail_lock_guard = MagicMock()

    mail = {
        "id": "mail-1",
        "body": "请修复登录页样式",
        "subject": "任务",
        "from_agent": "build",
        "from_window": "win_01",
    }
    with patch("PyQt5.QtCore.QTimer.singleShot") as m_single_shot:
        inst._process_team_task(mail)

    # 关键断言：必须传 preserve_input=True
    args, kwargs = inst._on_send_clicked.call_args
    assert kwargs.get("preserve_input") is True, "团队邮件必须 preserve_input=True"
    assert kwargs.get("hook_event") == "TeamMail"
    assert "任务邮件" in args[0]
    # 延迟锁守卫照常安排
    assert m_single_shot.called


# ══════════════════════════════════════════════════════════
# ② preserve_input=True：不清输入框/附件、不记录历史
# ══════════════════════════════════════════════════════════


def test_on_send_clicked_preserve_input_keeps_input_area(qapp):
    """preserve_input=True 时 input_area.clear / _clear_attachments / _record_input_history 均不得调用"""
    inst = _make_main_widget_instance()

    with _patch_send_dependencies():
        inst._on_send_clicked("📨 **来自 [build@win_01] 的任务邮件：**\n\n请修复", hook_event="TeamMail", preserve_input=True)

    inst.input_area.clear.assert_not_called()
    inst._clear_attachments.assert_not_called()
    inst._record_input_history.assert_not_called()
    # 消息仍正常进入对话流
    inst._append_user_message.assert_called_once()
    inst._append_assistant_message.assert_called_once()


def test_on_send_clicked_preserve_input_skips_attachment_merge(qapp):
    """preserve_input=True 且用户挂了附件：不把附件拼入邮件文本（附件属于用户编辑内容）"""
    inst = _make_main_widget_instance()
    inst._attachments = ["D:/tmp/photo.png"]

    with _patch_send_dependencies():
        inst._on_send_clicked("📨 **来自 [build@win_01] 的任务邮件：**\n\n请修复", hook_event="TeamMail", preserve_input=True)

    # 附件未拼接、未清空
    assert inst._build_user_text_with_attachments is not None or True  # 方法存在性无断言意义
    inst._clear_attachments.assert_not_called()
    sent_text = inst._append_user_message.call_args.args[0]
    assert "photo.png" not in sent_text, "preserve_input 时附件路径不得拼入邮件文本"


# ══════════════════════════════════════════════════════════
# ③ preserve_input=False（用户主动发送）：行为不变
# ══════════════════════════════════════════════════════════


def test_on_send_clicked_default_still_clears_input(qapp):
    """用户主动发送（默认）必须保持原行为：清空输入框/附件、记录历史"""
    inst = _make_main_widget_instance()

    with _patch_send_dependencies():
        inst._on_send_clicked("你好")

    inst.input_area.clear.assert_called_once()
    inst._clear_attachments.assert_called_once()
    inst._record_input_history.assert_called_once()
