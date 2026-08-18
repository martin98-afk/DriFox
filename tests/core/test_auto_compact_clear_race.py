# -*- coding: utf-8 -*-
"""团队成员自动压缩竞态修复回归测试（main_widget.py）

背景 bug：成员窗口触发 auto-compact → 压缩清空与团队邮件自动重发 /
子智能体完成回调（_do_trigger_callback）撞车：
- 清空销毁在途流式卡片（_current_assistant_card 失效）→ 回复不可见
- _post_compact_guard 把清空后新 worker 的 finished_with_messages 误判为
  旧快照丢弃 → 大模型恢复结果永不显示/落库

修复点验证：
- F1：_on_compact_clear_finished 清空前停止在途流式 + 收尾团队邮件
- F2：_on_messages_updated 守卫按 _send_epoch / _post_compact_epoch 精确
  区分旧快照（丢弃）与新 worker（放行）
- F3：_check_and_process_pending 在 _auto_compact_in_progress 期间暂缓；
  清空完成后重启（_check_and_process_pending 被调用）

设计说明：与 tests/core/test_team_mail_f4_bugs.py 风格一致——
SimpleNamespace + MethodType 绑定真实方法，避免 Qt 实例化重依赖。
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


def _recorder():
    """返回 (calls:list, callable)——记录任意调用"""
    calls = []
    return calls, lambda *a, **k: calls.append((a, k))


# ══════════════════════════════════════════════════════════
# F2：压缩守卫按发送纪元区分旧快照 / 新 worker
# ══════════════════════════════════════════════════════════


def _mk_guard_fake(session_messages, send_epoch, compact_epoch):
    """构造能跑通 _on_messages_updated 守卫段的 fake 窗口"""
    from app.main_widget import OpenAIChatToolWindow

    set_calls, set_rec = _recorder()
    session = SimpleNamespace(
        session_id="sess_0001",
        messages=session_messages,
        set_messages=set_rec,
    )
    fake = SimpleNamespace(
        _is_destroyed=False,
        _post_compact_guard=True,
        _send_epoch=send_epoch,
        _post_compact_epoch=compact_epoch,
        session_manager=SimpleNamespace(get_current_session=lambda: session),
        _session_switched=False,
        _pending_session_hook=False,
        _truncation_sentinel=None,
        _pending_send_after_truncation=False,
        _pending_send_user_text=None,
        _history_preview_messages=None,
        _response_start_time=None,
        _current_provider_name="",
        _session_dirty=False,
        _refresh_context_usage_indicator=lambda: None,
    )
    fake._on_messages_updated = MethodType(OpenAIChatToolWindow._on_messages_updated, fake)
    fake.__dict__["_session"] = session
    return fake, set_calls


class TestCompactGuardEpoch:
    def test_guard_passes_new_worker_messages_after_post_compact_send(self, qapp):
        """🔴 核心回归：清空后有新发送（epoch 变化），新 worker 的结果必须放行

        修复前：guard 只看 len(messages) > len(session.messages)，清空后
        session=[]、新 worker 回传 [user, assistant] → 被误判旧快照丢弃，
        大模型恢复结果永不显示。
        """
        fake, set_calls = _mk_guard_fake(
            session_messages=[],
            send_epoch=2,  # 清空后发生了新发送
            compact_epoch=1,  # 清空时刻快照
        )
        incoming = [
            {"role": "user", "content": "📨 任务邮件"},
            {"role": "assistant", "content": "恢复结果"},
        ]
        fake._on_messages_updated(incoming)
        assert len(set_calls) == 1, "新 worker 消息应写入 session（放行）"
        assert fake._post_compact_guard is False

    def test_guard_blocks_old_worker_snapshot_without_new_send(self):
        """清空后无新发送（epoch 相等），旧 worker 延迟快照应被丢弃（原语义保持）"""
        fake, set_calls = _mk_guard_fake(
            session_messages=[],
            send_epoch=1,
            compact_epoch=1,  # 清空后没有任何新发送
        )
        incoming = [
            {"role": "user", "content": "旧提问"},
            {"role": "assistant", "content": "旧部分回复"},
            {"role": "user", "content": "旧提问2"},
        ]
        fake._on_messages_updated(incoming)
        assert len(set_calls) == 0, "旧 worker 快照不应覆写已清空的会话"
        assert fake._post_compact_guard is False, "守卫单次使用后自清零"

    def test_new_worker_messages_land_in_session(self, qapp):
        """放行的新 worker 消息内容完整落入 session.set_messages"""
        fake, set_calls = _mk_guard_fake(
            session_messages=[{"role": "user", "content": "📨 新邮件"}],
            send_epoch=5,
            compact_epoch=4,
        )
        incoming = [
            {"role": "user", "content": "📨 新邮件"},
            {"role": "assistant", "content": "大模型恢复结果"},
        ]
        fake._on_messages_updated(incoming)
        assert len(set_calls) == 1
        landed = set_calls[0][0][0]
        assert landed == incoming


# ══════════════════════════════════════════════════════════
# F3a：压缩进行中暂缓 pending 邮件自动拉起
# ══════════════════════════════════════════════════════════


class TestTeamMailDeferredDuringCompact:
    def test_check_and_process_pending_skipped_while_compacting(self):
        """压缩子智能体执行期间，pending 邮件不得被自动拉起（撞清空竞态源头）"""
        from app.main_widget import OpenAIChatToolWindow

        tm = MagicMock()
        fake = SimpleNamespace(
            _team_processing=False,
            _auto_compact_in_progress=True,
            _get_team_manager=lambda: tm,
        )
        fake._check_and_process_pending = MethodType(OpenAIChatToolWindow._check_and_process_pending, fake)
        fake._check_and_process_pending()
        tm.get_pending_tasks.assert_not_called(), "压缩期间不应拉取 pending 邮件"

    def test_check_and_process_pending_resumes_after_compact(self, team_manager):
        """压缩结束后（标志复位），pending 邮件恢复拉取"""
        from app.main_widget import OpenAIChatToolWindow

        # 落一封 pending 邮件到隔离邮箱
        mailbox = team_manager._mailbox_dir("default", "win_test")
        mailbox.mkdir(parents=True, exist_ok=True)
        team_manager._write_json(
            mailbox / "mail_1.json",
            {
                "id": "mail_1",
                "type": "task",
                "from_window": "win_01",
                "from_agent": "alice",
                "to_window": "win_test",
                "to_agent": "bob",
                "subject": "任务",
                "body": "任务内容",
                "status": "pending",
                "result": "",
                "created_at": 0,
            },
        )

        process_calls, process_rec = _recorder()
        fake = SimpleNamespace(
            _team_processing=False,
            _auto_compact_in_progress=False,  # 压缩已结束
            _window_id="win_test",
            _get_team_manager=lambda: team_manager,
            _is_streaming=False,
            _last_stop_time=0.0,
            _inject_team_mail_as_hook=MagicMock(),
            _process_team_task=process_rec,
        )
        fake._check_and_process_pending = MethodType(OpenAIChatToolWindow._check_and_process_pending, fake)
        fake._check_and_process_pending()
        assert len(process_calls) == 1, "压缩结束后 pending 邮件应恢复自动处理"


# ══════════════════════════════════════════════════════════
# F1 + F3b：_on_compact_clear_finished 清空前停流 + 完成后重启邮件
# ══════════════════════════════════════════════════════════


class TestCompactClearStopsInFlightStream:
    def _mk_clear_fake(self):
        """构造能跑通 _on_compact_clear_finished 主流程的 fake 窗口"""
        from app.main_widget import OpenAIChatToolWindow

        set_calls, set_rec = _recorder()
        session = SimpleNamespace(
            session_id="sess_0001",
            messages=[{"role": "user", "content": "旧对话"}],
            set_messages=set_rec,
            set_compaction_cache=lambda cache: None,
        )
        stop_calls, stop_rec = _recorder()
        mail_stop_calls, mail_stop_rec = _recorder()
        display_calls, display_rec = _recorder()
        resume_calls, resume_rec = _recorder()
        card = SimpleNamespace(
            stop_streaming_anim=lambda: None,
            finish_streaming=lambda: None,
        )
        executor = SimpleNamespace(_execution_error=None)

        fake = SimpleNamespace(
            _is_destroyed=False,
            _auto_compact_in_progress=True,
            backend=SimpleNamespace(
                sub_agent_manager=SimpleNamespace(
                    _running_tasks={"compact_1": executor},
                    _finished_tasks={},
                ),
                chat_engine=SimpleNamespace(alive=True),
                stop_streaming=stop_rec,
                trigger_session_event=lambda *a, **k: None,
            ),
            session_manager=SimpleNamespace(sessions=[session]),
            _is_streaming=True,  # 压缩执行期间已有新一轮流式在跑（竞态现场）
            _current_assistant_card=card,
            _set_ai_state=lambda *a, **k: None,
            _toggle_send_stop=lambda *a, **k: None,
            _sync_team_mail_on_stop=mail_stop_rec,
            _session_card_cache={},
            _current_session_id="sess_0001",
            _pending_session_hook=False,
            _display_current_session=display_rec,
            _check_and_process_pending=resume_rec,
            _send_epoch=3,
            _post_compact_epoch=-1,
        )
        fake._on_compact_clear_finished = MethodType(OpenAIChatToolWindow._on_compact_clear_finished, fake)
        fake.__dict__.update(
            _stop_calls=stop_calls,
            _mail_stop_calls=mail_stop_calls,
            _display_calls=display_calls,
            _resume_calls=resume_calls,
            _set_calls=set_calls,
            _session_obj=session,
        )
        return fake

    def test_clear_stops_stream_before_wipe(self):
        """🔴 F1：清空前必须停止在途流式 + 收尾团队邮件（否则流式卡片被销毁、回复不可见）"""
        fake = self._mk_clear_fake()
        fake._on_compact_clear_finished("compact_1", "摘要结果", "sess_0001")
        assert len(fake._stop_calls) == 1, "清空前应停止在途流式"
        assert len(fake._mail_stop_calls) == 1, "停流后应收尾团队邮件状态"
        assert fake._is_streaming is False

    def test_clear_records_epoch_snapshot_and_wipes_session(self):
        """F2 配套：清空时记录 _send_epoch 快照，session 被清空"""
        fake = self._mk_clear_fake()
        fake._on_compact_clear_finished("compact_1", "摘要结果", "sess_0001")
        assert fake._post_compact_epoch == 3, "应记录清空时刻发送纪元快照"
        assert fake._post_compact_guard is True
        wiped = fake._set_calls[0][0][0]
        assert wiped == [], "session 应被清空"

    def test_clear_resumes_team_mail_after_finish(self):
        """F3b：清空完成后重启 pending 团队邮件处理"""
        fake = self._mk_clear_fake()
        fake._on_compact_clear_finished("compact_1", "摘要结果", "sess_0001")
        assert len(fake._resume_calls) == 1, "清空完成后应重启团队邮件处理"
        assert fake._auto_compact_in_progress is False

    def test_clear_captures_source_count_before_wipe(self):
        """F5：source_message_count 应为清空前的消息数（修复前恒为 0）"""
        from app.main_widget import OpenAIChatToolWindow

        cache_holder = {}

        fake = self._mk_clear_fake()
        fake._session_obj.set_compaction_cache = lambda c: cache_holder.update(c)
        fake._on_compact_clear_finished("compact_1", "摘要结果", "sess_0001")
        assert cache_holder.get("source_message_count") == 1, "应捕获清空前消息数（1 条旧对话）"
