# -*- coding: utf-8 -*-
"""F1：停止对话失效根治——状态写回不重触发 / 新邮件仍处理 / 取消排空 hook 队列 / 停止冷却

对应 T1 诊断报告修复方案 P0-1 / P0-2 / P1-3：
- P0-1：_on_team_mailbox_changed / _rearm_team_watcher 只对新邮件 id 响应，
  状态写回（mark_mail_running/pending/done 改写同一文件）不回流重触发；
- P0-2：_cancel_with_stop_hook 排空 _hook_message_queue 中的 TeamMail 残留；
- P1-3：_check_and_process_pending 停止后 1s 冷却期内不自动拉起 pending 邮件。
"""

import queue
import time
from types import MethodType, SimpleNamespace

import pytest

from app.core import team_manager as tm_mod
from app.core.workers.chat_worker import OpenAIChatWorker
from app.main_widget import OpenAIChatToolWindow


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


class TestWatcherNoRetriggerOnStatusWriteback:
    """P0-1：状态写回不触发重新处理（watcher 回流根治）"""

    def _make_window(self, team_manager):
        fake = SimpleNamespace(
            _is_destroyed=False,
            _known_mail_ids=set(),
            _last_stop_time=0.0,
            _window_id="win_02",
            _team_agent_name="bob",
            _team_watch_paths=set(),
        )
        fake._get_team_manager = lambda: team_manager
        fake._stop_team_watcher = lambda: None
        fake._snapshot_mail_ids = MethodType(OpenAIChatToolWindow._snapshot_mail_ids, fake)
        fake._on_team_mailbox_changed = MethodType(OpenAIChatToolWindow._on_team_mailbox_changed, fake)
        return fake

    def test_status_writeback_does_not_retrigger(self, team_manager):
        """用例1：状态写回（running/pending）不触发 _check_and_process_pending"""
        win = self._make_window(team_manager)
        mail_id = _drop_mail(team_manager, "win_02")
        win._known_mail_ids = win._snapshot_mail_ids()  # 模拟启动快照

        calls = []
        win._check_and_process_pending = lambda: calls.append("called")

        # 模拟流式注入（mark running 写回 → directoryChanged）
        team_manager.mark_mail_running(mail_id, "win_02")
        win._on_team_mailbox_changed("mailbox")
        # 模拟停止回滚（mark pending 写回 → directoryChanged）
        team_manager.mark_mail_pending(mail_id, "win_02")
        win._on_team_mailbox_changed("mailbox")

        assert calls == [], f"状态写回不应触发重新处理，实际触发 {len(calls)} 次"

    def test_new_mail_still_processed(self, team_manager):
        """用例2：新邮件文件出现仍触发处理（回归保护）"""
        win = self._make_window(team_manager)
        _drop_mail(team_manager, "win_02", mail_id="mail_1")
        win._known_mail_ids = win._snapshot_mail_ids()

        calls = []
        win._check_and_process_pending = lambda: calls.append("called")

        # 新邮件到达（新建文件 → directoryChanged）
        _drop_mail(team_manager, "win_02", mail_id="mail_2")
        win._on_team_mailbox_changed("mailbox")

        assert calls == ["called"], f"新邮件应触发处理，实际 {calls}"

    def test_rearm_poll_only_new_ids(self, team_manager):
        """用例2b：_rearm_team_watcher 补轮询同样只响应新邮件 id"""
        win = self._make_window(team_manager)
        win._team_fs_watcher = SimpleNamespace(directories=lambda: [], addPath=lambda p: None)  # 目录从 watcher 丢失
        win._rearm_team_watcher = MethodType(OpenAIChatToolWindow._rearm_team_watcher, win)
        _drop_mail(team_manager, "win_02", mail_id="mail_1")
        win._known_mail_ids = win._snapshot_mail_ids()

        calls = []
        win._check_and_process_pending = lambda: calls.append("called")

        # 状态写回后重挂：不应触发（停止回滚 pending 不被 5s 轮询重新拉起）
        team_manager.mark_mail_pending("mail_1", "win_02")
        win._rearm_team_watcher()
        assert calls == [], f"重挂轮询不应因状态写回触发，实际 {calls}"

        # 新邮件后重挂：应触发（期间到达的新邮件不遗漏）
        _drop_mail(team_manager, "win_02", mail_id="mail_2")
        win._rearm_team_watcher()
        assert calls == ["called"], f"重挂轮询应处理新邮件，实际 {calls}"


class TestCancelDrainsHookQueue:
    """P0-2：取消路径排空 _hook_message_queue 中的 TeamMail 残留"""

    def test_team_mail_drained_non_team_mail_kept(self):
        """用例3：TeamMail hook 被丢弃，非 TeamMail（SubAgentFinished）保留"""
        q = queue.Queue()
        q.put({"_hook_event": "TeamMail", "content": "📨 邮件"})
        q.put({"_hook_event": "SubAgentFinished", "content": "完成"})
        backend = SimpleNamespace(_hook_message_queue=q)
        tool_executor = SimpleNamespace(_backend=backend)

        worker = SimpleNamespace(tool_executor=tool_executor)
        worker._cancel_with_stop_hook = MethodType(OpenAIChatWorker._cancel_with_stop_hook, worker)
        worker._build_response_message_sequence = lambda: []  # partial 为空也执行排空
        worker._trigger_worker_hook = lambda *a, **k: None
        worker._emit_with_callback = lambda *a, **k: None

        worker._cancel_with_stop_hook([], [])

        rest = []
        while not q.empty():
            rest.append(q.get_nowait())
        assert len(rest) == 1, f"应只剩 1 条非 TeamMail，实际 {len(rest)}: {rest}"
        assert rest[0]["_hook_event"] == "SubAgentFinished", f"非 TeamMail 应保留: {rest}"

    def test_empty_queue_no_error(self):
        """用例3b：队列为空时排空逻辑不抛异常"""
        q = queue.Queue()
        backend = SimpleNamespace(_hook_message_queue=q)
        tool_executor = SimpleNamespace(_backend=backend)

        worker = SimpleNamespace(tool_executor=tool_executor)
        worker._cancel_with_stop_hook = MethodType(OpenAIChatWorker._cancel_with_stop_hook, worker)
        worker._build_response_message_sequence = lambda: []
        worker._trigger_worker_hook = lambda *a, **k: None
        worker._emit_with_callback = lambda *a, **k: None

        worker._cancel_with_stop_hook([], [])  # 不应抛异常
        assert q.empty()


class TestStopCooldown:
    """P1-3：停止后冷却期内不自动拉起 pending 邮件"""

    def _make_window(self, team_manager):
        fake = SimpleNamespace(
            _team_processing=False,
            _is_streaming=False,
            _last_stop_time=0.0,
            _window_id="win_02",
        )
        fake._get_team_manager = lambda: team_manager
        # G3 修复后冷却分支会安排重检：打桩隔离 QTimer（本用例只断言"冷却内不拉起"）
        fake._schedule_pending_recheck = lambda: None
        fake._check_and_process_pending = MethodType(OpenAIChatToolWindow._check_and_process_pending, fake)
        return fake

    def test_cooldown_blocks_auto_requeue(self, team_manager):
        """用例4：停止后 1s 冷却期内不自动拉起 pending 邮件"""
        win = self._make_window(team_manager)
        _drop_mail(team_manager, "win_02", mail_id="mail_1")
        win._last_stop_time = time.monotonic()  # 刚停止

        processed = []
        win._process_team_task = lambda mail: processed.append(mail["id"])
        win._inject_team_mail_as_hook = lambda mail: processed.append(("hook", mail["id"]))

        win._check_and_process_pending()
        assert processed == [], f"冷却期内不应处理 pending 邮件，实际 {processed}"

    def test_cooldown_expired_allows_processing(self, team_manager):
        """用例4b：冷却过期后恢复处理（用户后续对话仍能正常接手）"""
        win = self._make_window(team_manager)
        mail_id = _drop_mail(team_manager, "win_02", mail_id="mail_1")
        win._last_stop_time = time.monotonic() - 5.0  # 5s 前停止，已过期

        processed = []
        win._process_team_task = lambda mail: processed.append(mail["id"])
        win._inject_team_mail_as_hook = lambda mail: processed.append(("hook", mail["id"]))

        win._check_and_process_pending()
        assert processed == [mail_id], f"冷却过期后应处理 pending 邮件，实际 {processed}"
