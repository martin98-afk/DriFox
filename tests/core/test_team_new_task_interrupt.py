# -*- coding: utf-8 -*-
"""回归：新建任务打断流式时团队邮件锁不可死锁（_team_processing 必须复位）。

复现场景：成员 B 正在处理任务邮件（_team_processing=True + running 邮件），
此时点「新建任务」→ _handle_team_new_task 对每个成员调 _create_new_session，
其中 stop_streaming 中断流式。取消路径下 worker 不发射 finished_with_content，
_on_stream_finished 永不触发，原本 _team_processing 会永久卡 True、running 邮件
永久卡住 → A 后续发来的新邮件被 _check_and_process_pending 的 _team_processing
守卫跳过，B 完全收不到。

修复：_create_new_session 的 stop 分支补 _sync_team_mail_on_stop 复位锁并回滚邮件。
"""

from types import MethodType, SimpleNamespace

import pytest

from app.core import team_manager as tm_mod
from app.main_widget import OpenAIChatToolWindow


@pytest.fixture
def team_manager(tmp_path, monkeypatch):
    monkeypatch.setattr(tm_mod.TeamManager, "_get_teams_dir", staticmethod(lambda: tmp_path / "teams"))
    tm_mod.TeamManager._instance = None
    tm = tm_mod.TeamManager.get_instance()
    yield tm
    tm_mod.TeamManager._instance = None


class _Null:
    """任意未显式设置的属性/方法调用都安全 no-op（返回自身，支持链式访问）。"""

    def __call__(self, *a, **k):
        return self

    def __getattr__(self, n):
        return self


class _Stub:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def __getattr__(self, n):
        return _Null()


def _drop_running_mail(tm, window_id, mail_id="mail_1"):
    mailbox = tm._mailbox_dir("default", window_id)
    mailbox.mkdir(parents=True, exist_ok=True)
    mail = {
        "id": mail_id,
        "type": "task",
        "from_window": "win_A",
        "from_agent": "alice",
        "to_window": window_id,
        "to_agent": "bob",
        "subject": "任务",
        "body": "任务内容",
        "status": "running",
        "result": "",
        "created_at": 0,
    }
    tm._write_json(mailbox / f"{mail_id}.json", mail)
    return mail


def test_new_task_interrupt_unblocks_team_mail(qapp, team_manager):
    """成员流式处理中被新建任务打断 → 团队邮件锁必须复位、running 邮件回滚。"""
    running_mail = _drop_running_mail(team_manager, "win_B")
    # 无响应 → _mail_was_responded 返回 False → 回滚 pending（不误标 done）
    sess = SimpleNamespace(messages=[{"role": "user", "content": "x"}])
    backend = SimpleNamespace(
        chat_engine=object(),
        stop_streaming=lambda: True,
        create_session=lambda: SimpleNamespace(session_id="new"),
        set_session_context=lambda x: None,
        clear_todo_list=lambda: None,
    )
    win = _Stub(
        _is_destroyed=False,
        _exclusive_ui_modes=set(),
        _is_streaming=True,  # 触发打断分支
        _team_processing=True,  # 正在处理邮件（卡锁前置状态）
        _current_team_mail={"mail": running_mail},
        _window_id="win_B",
        _session_dirty=False,
        backend=backend,
        _get_team_manager=lambda: team_manager,
        session_manager=SimpleNamespace(get_current_session=lambda: sess),
        _current_session_id="old",
        _injected_team_mails=[],  # 显式空列表，避免 __getattr__ 返回不可迭代的 _Null
    )
    # 绑定真实的修复相关方法，其余 self 调用由 _Null 安全 no-op
    win._create_new_session = MethodType(OpenAIChatToolWindow._create_new_session, win)
    win._sync_team_mail_on_stop = MethodType(OpenAIChatToolWindow._sync_team_mail_on_stop, win)
    win._finalize_single_team_mail = MethodType(OpenAIChatToolWindow._finalize_single_team_mail, win)
    win._mail_was_responded = MethodType(OpenAIChatToolWindow._mail_was_responded, win)
    win._last_non_hook_assistant_text = MethodType(
        OpenAIChatToolWindow._last_non_hook_assistant_text, win
    )

    win._create_new_session()

    assert win._team_processing is False, (
        "新建会话打断流式必须复位团队邮件处理锁，否则后续邮件永久被 _check_and_process_pending 拦截"
    )
    mails = team_manager.get_mailbox_mails("win_B")
    assert mails and mails[0]["status"] == "pending", "被中断的 running 邮件应回滚为 pending（不卡 running）"
    assert win._current_team_mail is None
