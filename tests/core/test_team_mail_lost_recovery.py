# -*- coding: utf-8 -*-
"""团队邮件丢失修复回归：流结束回滚 pending 后无人拉起（G1）/ 冷却窗口错过无重试（G3）

症状：发送方收到「任务邮件已发送」，接收成员毫无反应——邮件躺 pending，
直到成员下一次对话（worker 注入路径）才被捞起；空闲成员视角即「丢了」。

G1（主因）：_on_stream_finished 中 _check_and_process_pending（17197）先于
_finalize_injected_team_mails（17200）执行：流式末期注入的邮件此刻仍是
running 状态，check 落空；随后 finalize 把它回滚 pending，但回滚写文件触发
的 directoryChanged 被 P0-1 id 快照拦截（状态写回不回流）→ 无人拉起。
docstring（_finalize_injected_team_mails）本就声称"由流结束后的
_check_and_process_pending 重新排队处理"，实现缺了这一步。

G3（次因）：新邮件到达恰逢手动停止后 1s 冷却窗口 →
_check_and_process_pending 直接 return 且无重试 → 同样躺尸。
冷却语义（F1 P1-3）为"1s 内不拉起"，1s 后拉起是被接受的设计
（见 test_team_mail_stop_retrigger.py 用例 4b）。
"""

import time
from types import MethodType, SimpleNamespace

import pytest

from app.core import team_manager as tm_mod
from app.main_widget import OpenAIChatToolWindow


@pytest.fixture
def team_manager(tmp_path, monkeypatch):
    """隔离数据目录的真实 TeamManager（避免污染真实 teams 目录）"""
    monkeypatch.setattr(tm_mod.TeamManager, "_get_teams_dir", staticmethod(lambda: tmp_path / "teams"))
    tm_mod.TeamManager._instance = None
    tm = tm_mod.TeamManager.get_instance()
    yield tm
    tm_mod.TeamManager._instance = None


def _drop_mail(tm, window_id, mail_id="mail_1", status="running"):
    """直接向邮箱目录落一封 task 邮件（等价 send_task 落盘 + mark running）"""
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
    return mail


def _make_window(team_manager, **overrides):
    """最小 fake window：绑定真实的邮件收尾/重试方法，其余依赖打桩"""
    fake = SimpleNamespace(
        _is_destroyed=False,
        _team_processing=False,
        _is_streaming=False,
        _auto_compact_in_progress=False,
        _last_stop_time=0.0,
        _pending_recheck_scheduled=False,
        _known_mail_ids=set(),
        _window_id="win_02",
        _team_agent_name="bob",
        _team_watch_paths=set(),
        session_manager=SimpleNamespace(get_current_session=lambda: None),
        _injected_team_mails=[],
    )
    fake._get_team_manager = lambda: team_manager
    fake._stop_team_watcher = lambda: None
    fake._processed = []  # _process_team_task 拉起记录（供断言）
    fake._process_team_task = lambda mail: fake._processed.append(mail["id"])
    fake._inject_team_mail_as_hook = lambda mail: overrides.setdefault("_injected", []).append(mail["id"])
    for name in (
        "_snapshot_mail_ids",
        "_on_team_mailbox_changed",
        "_rearm_team_watcher",
        "_check_and_process_pending",
        "_finalize_injected_team_mails",
        "_finalize_single_team_mail",
        "_mail_was_responded",
        "_last_non_hook_assistant_text",
        "_schedule_pending_recheck",
        "_pending_recheck_fire",
    ):
        setattr(fake, name, MethodType(getattr(OpenAIChatToolWindow, name), fake))
    fake._team_fs_watcher = SimpleNamespace(directories=lambda: [], addPath=lambda p: None)
    return fake


class TestFinalizeRequeue:
    """G1：流结束 finalize 回滚 pending 后必须重新拉起"""

    def test_finalize_rollback_then_requeue(self, team_manager, monkeypatch):
        """流式末期注入的邮件未获 LLM 响应 → finalize 回滚 pending → 立即重新拉起"""
        win = _make_window(team_manager)
        mail = _drop_mail(team_manager, "win_02", status="running")
        win._injected_team_mails = [{"mail_id": mail["id"], "mail": mail, "injected_at": time.time()}]

        calls = []
        orig_check = win._check_and_process_pending

        def _spy():
            calls.append(1)
            orig_check()

        win._check_and_process_pending = _spy
        win._finalize_injected_team_mails()

        # 邮件已回滚 pending（未响应，session 为空）
        status_now = {m["id"]: m["status"] for m in team_manager.get_mailbox_mails("win_02")}
        assert status_now.get(mail["id"]) == "pending"
        # 核心断言：回滚后必须立即重新拉起（修复前：无任何调用，邮件躺尸）
        assert calls, "G1 修复缺失：finalize 回滚 pending 后未重新拉起，邮件躺尸（发送方已显示发送成功）"
        # 真实链路效果：pending 邮件经 _check_and_process_pending → _process_team_task 拉起
        assert win._processed == [mail["id"]], (
            f"回滚后 pending 邮件应被 _process_team_task 拉起处理，实际 {win._processed}"
        )


class TestCooldownRecheck:
    """G3：冷却窗口错过的 pending 检查需安排延迟重试"""

    def test_cooldown_schedules_recheck(self, team_manager, monkeypatch):
        """冷却 return 前安排 singleShot 重试（不真实等待 1.15s，拦截回调）"""
        win = _make_window(team_manager)
        _drop_mail(team_manager, "win_02", mail_id="mail_1", status="pending")
        win._last_stop_time = time.monotonic()  # 刚停止，冷却中

        from PyQt5.QtCore import QTimer

        fired = []
        monkeypatch.setattr(QTimer, "singleShot", staticmethod(lambda ms, cb: fired.append((ms, cb))))

        win._check_and_process_pending()
        # 冷却拦截（P1-3 语义保持）：不拉起
        assert win._processed == []
        # 但必须安排重试
        assert fired, "冷却拦截后必须安排 pending 重检（G3），否则邮件躺尸"
        ms, cb = fired[0]
        assert ms >= 1000, f"重试延迟必须超过冷却期 1s，实际 {ms}ms"

        # 模拟冷却过期后重试触发
        win._last_stop_time = time.monotonic() - 5.0
        win._pending_recheck_scheduled = False
        cb()
        assert win._processed == ["mail_1"], f"重试触发后应拉起 pending 邮件，实际 {win._processed}"

    def test_recheck_dedup(self, team_manager, monkeypatch):
        """重试排程防重入：冷却期内多次到达只排一次"""
        win = _make_window(team_manager)
        _drop_mail(team_manager, "win_02", mail_id="mail_1", status="pending")
        win._last_stop_time = time.monotonic()

        from PyQt5.QtCore import QTimer

        fired = []
        monkeypatch.setattr(QTimer, "singleShot", staticmethod(lambda ms, cb: fired.append((ms, cb))))

        win._check_and_process_pending()
        win._check_and_process_pending()
        assert len(fired) == 1, f"重试排程应防重入，实际排了 {len(fired)} 次"

    def test_recheck_fire_destroyed_noop(self, team_manager, monkeypatch):
        """窗口已销毁时重试回调不做事"""
        win = _make_window(team_manager)
        win._is_destroyed = True
        # 不应抛异常
        win._pending_recheck_fire()
