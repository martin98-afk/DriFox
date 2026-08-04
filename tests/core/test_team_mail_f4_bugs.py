# -*- coding: utf-8 -*-
"""F4：main_widget.py 团队邮件路径 4 个 bug 修复测试

覆盖：
- Bug1（🔴 round_index 口径）：_get_current_user_round_index 计数 TeamMail
  （与全仓口径统一），ui_helpers.find_user_round_index 同步排除非 TeamMail hook
- Bug2（🔴 团队邮件死锁）：_process_team_task 被 _on_send_clicked 提前 return
  拦截（未进入流式）→ 锁复位 + 邮件回 pending；延迟兜底 guard 同样复位
- Bug3（🟡 误标终态）：_on_task_stream_finished 按 _mail_was_responded 收尾，
  未响应 → 回滚 pending 而非无条件 done
- Bug5（🟡 关闭死锁）：closeEvent 源码含 _sync_team_mail_on_stop 调用
  （流式中关闭窗口不卡 running）

设计说明：qapp 由 pytest-qt 提供；重依赖用 __new__/SimpleNamespace + MethodType
绑定真实方法（与 tests/core/test_team_mail_stop_retrigger.py 风格一致）；
TeamManager 用 tmp_path 隔离真实 teams 目录。
"""

from types import MethodType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.core import team_manager as tm_mod


@pytest.fixture
def team_manager(tmp_path, monkeypatch):
    """隔离数据目录的真实 TeamManager（避免污染真实 teams 目录）"""
    monkeypatch.setattr(tm_mod.TeamManager, "_get_teams_dir", staticmethod(lambda: tmp_path / "teams"))
    tm_mod.TeamManager._instance = None
    tm = tm_mod.TeamManager.get_instance()
    yield tm
    tm_mod.TeamManager._instance = None


def _drop_mail(tm, window_id, mail_id="mail_1", status="pending"):
    """直接向邮箱目录落一封 task 邮件（等价 send_task 的落盘）"""
    mailbox = tm._mailbox_dir("default", window_id)
    mailbox.mkdir(parents=True, exist_ok=True)
    mail = {
        "id": mail_id,
        "type": "task",
        "from_window": "win_01",
        "from_agent": "alice",
        "to_window": window_id,
        "to_agent": "bob",
        "subject": "任务",
        "body": "任务内容",
        "status": status,
        "result": "",
        "created_at": 0,
    }
    tm._write_json(mailbox / f"{mail_id}.json", mail)
    return mail_id


def _mk_session(messages):
    return SimpleNamespace(messages=messages)


# ══════════════════════════════════════════════════════════
# Bug1：round_index 口径统一（TeamMail 计入 user round）
# ══════════════════════════════════════════════════════════


class TestRoundIndexConsistency:
    def test_get_current_user_round_index_counts_team_mail(self):
        """TeamMail 后的下一条 user round_index 应包含 TeamMail（修复前少 1）"""
        from app.main_widget import OpenAIChatToolWindow

        fake = SimpleNamespace()
        fake.session_manager = SimpleNamespace(
            get_current_session=lambda: _mk_session(
                [
                    {"role": "user", "content": "A"},
                    {"role": "user", "content": "📨 邮件", "_hook_event": "TeamMail"},
                    {"role": "user", "content": "B"},
                    {"role": "assistant", "content": "回复"},
                ]
            )
        )
        fake._get_current_user_round_index = MethodType(OpenAIChatToolWindow._get_current_user_round_index, fake)
        # 已有 3 条 user（A=0, TeamMail=1, B=2）→ 下一条应是 3
        assert fake._get_current_user_round_index() == 3

    def test_get_current_user_round_index_skips_other_hooks(self):
        """非 TeamMail hook（SessionStart）不计入 round（口径不变）"""
        from app.main_widget import OpenAIChatToolWindow

        fake = SimpleNamespace()
        fake.session_manager = SimpleNamespace(
            get_current_session=lambda: _mk_session(
                [
                    {"role": "user", "content": "<hook>", "_hook_event": "SessionStart"},
                    {"role": "user", "content": "A"},
                ]
            )
        )
        fake._get_current_user_round_index = MethodType(OpenAIChatToolWindow._get_current_user_round_index, fake)
        assert fake._get_current_user_round_index() == 1

    def test_find_user_round_index_counts_team_mail_skips_other_hooks(self):
        """find_user_round_index：TeamMail 计入 round，SessionStart 跳过（与全仓口径一致）"""
        from app.widgets.ui_helpers import find_user_round_index

        session = _mk_session(
            [
                {"role": "user", "content": "A", "timestamp": "t1"},
                {"role": "user", "content": "<hook>", "timestamp": "t2", "_hook_event": "SessionStart"},
                {"role": "user", "content": "📨 邮件", "timestamp": "t3", "_hook_event": "TeamMail"},
                {"role": "user", "content": "B", "timestamp": "t4"},
            ]
        )
        # 定位 B：A=0, TeamMail=1, B=2（SessionStart 不计）
        assert find_user_round_index(session, "B", "t4") == 2

    def test_find_user_round_index_still_locates_without_hooks(self):
        """无 hook 的普通会话定位 round 不变（回归）"""
        from app.widgets.ui_helpers import find_user_round_index

        session = _mk_session(
            [
                {"role": "user", "content": "A", "timestamp": "t1"},
                {"role": "user", "content": "B", "timestamp": "t2"},
            ]
        )
        assert find_user_round_index(session, "B", "t2") == 1


# ══════════════════════════════════════════════════════════
# Bug2：团队邮件锁永久死锁 → _process_team_task 拦截路径释放
# ══════════════════════════════════════════════════════════


class TestTeamMailLockRelease:
    def _make_window(self, team_manager):
        from app.main_widget import OpenAIChatToolWindow

        fake = SimpleNamespace(
            _window_id="win_02",
            _team_processing=True,
            _current_team_mail=None,
            _is_streaming=False,
            _is_destroyed=False,
        )
        fake._get_team_manager = lambda: team_manager
        fake._process_team_task = MethodType(OpenAIChatToolWindow._process_team_task, fake)
        fake._rollback_team_mail_processing = MethodType(OpenAIChatToolWindow._rollback_team_mail_processing, fake)
        fake._delayed_team_mail_lock_guard = MethodType(OpenAIChatToolWindow._delayed_team_mail_lock_guard, fake)
        return fake

    def test_send_blocked_releases_lock_and_requeues(self, team_manager):
        """_on_send_clicked 提前 return（模型无效等，未进入流式）→ 锁复位 + 邮件回 pending"""
        fake = self._make_window(team_manager)
        # mock _on_send_clicked：模拟 send 前被拦截（不进入流式）
        fake._on_send_clicked = lambda *a, **k: None

        mail_id = _drop_mail(team_manager, "win_02")
        fake._process_team_task({"id": mail_id, "body": "任务内容", "from_agent": "alice", "from_window": "win_01"})

        assert fake._team_processing is False, "拦截后锁应复位（团队邮件系统不得死锁）"
        assert fake._current_team_mail is None, "邮件上下文应清空"
        mails = team_manager.get_mailbox_mails("win_02")
        assert mails[0]["status"] == "pending", f"邮件应回滚 pending（可重新排队），实际 {mails[0]['status']}"

    def test_send_streaming_keeps_lock(self, team_manager):
        """正常进入流式 → 锁保持（由流式结束 _on_task_stream_finished 释放）"""
        from unittest.mock import patch

        from PyQt5.QtCore import QTimer

        fake = self._make_window(team_manager)

        def _fake_send(*a, **k):
            fake._is_streaming = True  # 模拟 _on_send_clicked 同步置流式

        fake._on_send_clicked = _fake_send

        mail_id = _drop_mail(team_manager, "win_02")
        # 拦截兜底 QTimer（避免测试排队执行 guard）
        with patch.object(QTimer, "singleShot"):
            fake._process_team_task(
                {"id": mail_id, "body": "任务内容", "from_agent": "alice", "from_window": "win_01"}
            )

        assert fake._team_processing is True, "正常进入流式时锁应保持（流式结束释放）"
        mails = team_manager.get_mailbox_mails("win_02")
        assert mails[0]["status"] == "running", "进入流式后邮件应保持 running"

    def test_delayed_guard_releases_when_stream_died(self, team_manager):
        """兜底：延迟 guard 发现锁持有且未流式 → 复位（覆盖 _do_deferred_send 异步失败）"""
        fake = self._make_window(team_manager)
        mail_id = _drop_mail(team_manager, "win_02")
        fake._current_team_mail = {"mail": {"id": mail_id}}

        fake._delayed_team_mail_lock_guard()

        assert fake._team_processing is False, "guard 应复位锁"
        assert fake._current_team_mail is None
        mails = team_manager.get_mailbox_mails("win_02")
        assert mails[0]["status"] == "pending", "guard 应将未处理邮件回滚 pending"

    def test_delayed_guard_noop_when_streaming(self, team_manager):
        """兜底：流式中 guard 不动作（正常路径由流式结束释放）"""
        fake = self._make_window(team_manager)
        mail_id = _drop_mail(team_manager, "win_02")
        fake._current_team_mail = {"mail": {"id": mail_id}}
        fake._is_streaming = True
        team_manager.mark_mail_running(mail_id, "win_02")  # 模拟进入流式时已标 running

        fake._delayed_team_mail_lock_guard()

        assert fake._team_processing is True, "流式中 guard 不应误复位"
        mails = team_manager.get_mailbox_mails("win_02")
        assert mails[0]["status"] == "running"


# ══════════════════════════════════════════════════════════
# Bug3：_on_task_stream_finished 按响应状态收尾（不误标 done）
# ══════════════════════════════════════════════════════════


class TestTaskStreamFinishedFinalize:
    def _make_window(self, team_manager, responded: bool):
        from app.main_widget import OpenAIChatToolWindow

        fake = SimpleNamespace(
            _window_id="win_02",
            _team_processing=True,
            _current_team_mail=None,
            _is_streaming=False,
        )
        fake._get_team_manager = lambda: team_manager
        fake.session_manager = SimpleNamespace(get_current_session=lambda: _mk_session([]))
        fake._mail_was_responded = staticmethod(lambda s, m: responded)
        fake._last_non_hook_assistant_text = staticmethod(lambda s: "AI 回复")
        fake._finalize_single_team_mail = MethodType(OpenAIChatToolWindow._finalize_single_team_mail, fake)
        fake._on_task_stream_finished = MethodType(OpenAIChatToolWindow._on_task_stream_finished, fake)
        fake._check_and_process_pending = lambda: None
        return fake

    def test_unresponded_requeues_pending(self, team_manager):
        """流式结束但邮件未被 LLM 响应（仅工具调用/截断）→ 回滚 pending 非 done（Bug3）"""
        fake = self._make_window(team_manager, responded=False)
        mail_id = _drop_mail(team_manager, "win_02")
        fake._current_team_mail = {"mail": {"id": mail_id, "from_agent": "alice", "from_window": "win_01"}}

        fake._on_task_stream_finished()

        assert fake._team_processing is False
        mails = team_manager.get_mailbox_mails("win_02")
        assert mails[0]["status"] == "pending", f"未响应邮件应回滚 pending，实际 {mails[0]['status']}"

    def test_responded_marks_done(self, team_manager):
        """流式结束且邮件已被 LLM 响应 → 标 done（结果取最后 assistant 文本）"""
        fake = self._make_window(team_manager, responded=True)
        mail_id = _drop_mail(team_manager, "win_02")
        fake._current_team_mail = {"mail": {"id": mail_id, "from_agent": "alice", "from_window": "win_01"}}

        fake._on_task_stream_finished()

        mails = team_manager.get_mailbox_mails("win_02")
        assert mails[0]["status"] == "done", f"已响应邮件应标 done，实际 {mails[0]['status']}"
        assert mails[0]["result"] == "AI 回复"

    def test_no_current_mail_noop(self, team_manager):
        """无 _current_team_mail 时静默 no-op"""
        fake = self._make_window(team_manager, responded=True)
        fake._current_team_mail = None
        fake._on_task_stream_finished()  # 不应抛异常
        assert fake._team_processing is True  # 锁未被触碰


# ══════════════════════════════════════════════════════════
# Bug5：closeEvent 调 _sync_team_mail_on_stop（源码静态断言）
# ══════════════════════════════════════════════════════════


class TestCloseEventSyncsTeamMail:
    def test_close_event_calls_sync_team_mail_on_stop(self):
        """closeEvent 方法体必须调用 _sync_team_mail_on_stop（流式中关闭不卡 running）"""
        from pathlib import Path

        src = Path(__file__).resolve().parent.parent.parent / "app" / "main_widget.py"
        text = src.read_text(encoding="utf-8")
        start = text.find("    def closeEvent(self, event):")
        assert start >= 0
        body_end = len(text)
        for probe in ("\n    def ", "\n    class "):
            idx = text.find(probe, start + 10)
            if idx >= 0:
                body_end = min(body_end, idx)
        body = text[start:body_end]
        assert "_sync_team_mail_on_stop" in body, "closeEvent 未调用 _sync_team_mail_on_stop（Bug5）"
        # 收尾必须发生在停 watcher 之前（否则邮箱目录被移除后收尾丢失事件语义）
        assert body.find("_sync_team_mail_on_stop") < body.find("_stop_team_watcher"), (
            "收尾应在停 watcher 之前执行"
        )
