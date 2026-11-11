# -*- coding: utf-8 -*-
"""T3+T4 回归：欢迎守卫排除 hook 注入消息 + 拦截分支消息卡兜底渲染

== 背景（2026-10-29）==
团队成员窗口 create_session() 同步触发 SessionStart hook，注入
{"role":"user","_hook_event":"SessionStart"} 进 session.messages：
- T3 修复前：欢迎守卫误判「会话已有消息」→ return → 主区域永久空白
- T3 修复：守卫排除 _hook_event 消息（TeamMail 例外，R6/R7 既定语义）
- T4 修复：守卫合法拦截（确有真消息）但主区域可能已被迟到 welcome 槽位
  场景清空 → 兜底走会话渲染汇聚点 _display_current_session()

== 测试策略 ==
OpenAIChatToolWindow.__new__ + 逐属性 stub（避开 QWebEngine 真实初始化，
沿用 test_session_save_team_welcome.py 先例）：
1. [SessionStart, TeamMail] → 拦截 + 兜底：消息卡路径、无 welcome 卡、
   _displayed_session_id 置位
2. 纯 [SessionStart] → 守卫放行 → welcome 正常渲染（T3 不误伤普通新窗口）
3. _displayed_session_id == 当前会话 → 迟到槽位放弃，不重复兜底
4. _welcome_fallback_busy=True → 直接 return，防重入
"""

from unittest.mock import MagicMock, patch

HOOK_MSG = {"role": "user", "content": "startup", "_hook_event": "SessionStart"}
TEAM_MAIL_MSG = {"role": "user", "content": "📨 任务邮件", "_hook_event": "TeamMail"}


def _make_session(messages, sid="s_team"):
    s = MagicMock()
    s.session_id = sid
    s.messages = messages
    s.topic_summary = ""
    s.name = "团队会话"
    return s


def _make_window(session):
    from app.main_widget import OpenAIChatToolWindow

    inst = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    inst._window_id = "win_test"
    inst._is_streaming = False
    inst._welcome_build_hold = False
    inst._welcome_fallback_busy = False
    inst._displayed_session_id = None
    inst.session_manager = MagicMock()
    inst.session_manager.get_current_session.return_value = session
    return inst


def _stub_display_deps(inst, sid="s_team"):
    """stub _display_current_session 全量重建路径的依赖；_load_message_batch 用
    极简替身保留真方法开头的关键副作用（置 _displayed_session_id / _current_session_id），
    懒渲染/滚动细节不在本测范围。"""
    inst._clear_undo_store_for_session_switch = MagicMock()
    inst._sync_dialog_title = MagicMock()
    inst.title_edit = MagicMock()
    inst._current_session_id = None
    inst._restore_cached_session_cards = MagicMock(return_value=False)
    inst._clear_chat_area = MagicMock()
    inst._build_user_prefix_cache = MagicMock()
    inst._initial_visible_batch_count = 10
    inst._sync_batch_structures = MagicMock()
    inst._push_workbench_updates = MagicMock()
    inst._sync_todo_panel = MagicMock()

    def _fake_load(initial=False):
        cur = inst.session_manager.get_current_session()
        inst._displayed_session_id = cur.session_id
        inst._current_session_id = cur.session_id

    inst._load_message_batch = MagicMock(side_effect=_fake_load)


class TestWelcomeGuardFallback:
    @patch("app.main_widget.QTimer")
    def test_hook_plus_team_mail_renders_session_not_welcome(self, _mock_qtimer):
        """[SessionStart, TeamMail]：守卫拦截（TeamMail 算真消息）→ 兜底走汇聚点，
        渲染消息卡而非欢迎卡，_displayed_session_id 置位"""
        inst = _make_window(_make_session([HOOK_MSG, TEAM_MAIL_MSG]))
        _stub_display_deps(inst)
        inst._get_or_create_welcome_card = MagicMock()
        inst._welcome_rendered_once = False  # 预置走实例字典，避开未初始化 wrapper 的属性访问陷阱

        inst._show_initial_welcome()

        inst._get_or_create_welcome_card.assert_not_called()  # 无 welcome 卡
        assert inst._displayed_session_id == "s_team"  # 消息卡路径置位
        inst._clear_chat_area.assert_called()  # 兜底确实走了汇聚点重建
        assert inst._welcome_rendered_once is False  # welcome 路径未走

    @patch("app.main_widget.QTimer")
    def test_pure_hook_session_renders_welcome(self, _mock_qtimer):
        """纯 [SessionStart]：守卫放行 → welcome 正常渲染（T3 不误伤普通新窗口）"""
        inst = _make_window(_make_session([HOOK_MSG], sid="s_fresh"))
        inst._clear_chat_area = MagicMock()
        card = MagicMock()
        inst._get_or_create_welcome_card = MagicMock(return_value=card)
        inst._add_chat_widget = MagicMock()

        inst._show_initial_welcome()

        inst._get_or_create_welcome_card.assert_called_once()
        inst._add_chat_widget.assert_called_once_with(card)
        assert inst._welcome_rendered_once is True
        assert inst._displayed_session_id is None

    @patch("app.main_widget.QTimer")
    def test_same_session_displayed_skips_fallback(self, _mock_qtimer):
        """_displayed_session_id == 当前会话：迟到槽位放弃，不重复兜底"""
        inst = _make_window(_make_session([HOOK_MSG, TEAM_MAIL_MSG]))
        _stub_display_deps(inst)
        inst._displayed_session_id = "s_team"  # 已渲染过本会话

        inst._show_initial_welcome()

        inst._load_message_batch.assert_not_called()
        inst._clear_chat_area.assert_not_called()

    @patch("app.main_widget.QTimer")
    def test_fallback_busy_skips_reentry(self, _mock_qtimer):
        """_welcome_fallback_busy=True：直接 return，防重入"""
        inst = _make_window(_make_session([HOOK_MSG, TEAM_MAIL_MSG]))
        _stub_display_deps(inst)
        inst._welcome_fallback_busy = True  # 重入中

        inst._show_initial_welcome()

        inst._load_message_batch.assert_not_called()
        inst._clear_chat_area.assert_not_called()

    @patch("app.main_widget.QTimer")
    def test_fallback_busy_reset_after_display(self, _mock_qtimer):
        """兜底完成后旗标必须复位（finally），后续迟到槽位仍能判同放弃而非卡死"""
        inst = _make_window(_make_session([HOOK_MSG, TEAM_MAIL_MSG]))
        _stub_display_deps(inst)

        inst._show_initial_welcome()

        assert inst._welcome_fallback_busy is False

    @patch("app.main_widget.QTimer")
    def test_fallback_display_exception_not_leaked(self, _mock_qtimer):
        """兜底渲染抛异常（如 TypeError）：不外泄（防直达事件循环 qFatal）+ 旗标复位"""
        inst = _make_window(_make_session([HOOK_MSG, TEAM_MAIL_MSG]))
        _stub_display_deps(inst)
        # 让汇聚点真方法在重建路径上抛 TypeError
        inst._restore_cached_session_cards = MagicMock(side_effect=TypeError("boom"))

        inst._show_initial_welcome()  # 不应抛

        assert inst._welcome_fallback_busy is False  # 旗标仍复位
