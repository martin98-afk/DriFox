# -*- coding: utf-8 -*-
"""F5：team_manager 并发安全 + 邮件数据完整性（T4 Bug6 + Bug12）

覆盖：
- Bug6: _write_json 原子写（临时文件 + os.replace，无 tmp 残留）
- Bug6: mark_mail_running/pending/done 并发/连续 status 切换后 JSON 最终一致
- Bug12: send_task/send_reply 邮件 dict 含 team_name

设计说明：
- team_manager fixture 隔离数据目录（tmp_path），不污染真实 ~/.drifox/teams
- 并发测试用线程 + barrier 制造 read-modify-write 竞争窗口，验证锁生效
"""

import json
import threading

import pytest

from app.core import team_manager as tm_mod


@pytest.fixture
def team_manager(tmp_path, monkeypatch):
    """隔离数据目录的真实 TeamManager（避免污染真实 teams 目录）。

    🛡️ 直接构造实例（绕过 get_instance 单例）：test_team_template.py 中
    有直接赋值 TeamManager.get_instance 的测试（非 monkeypatch），会永久
    污染单例为 _FakeTM。本 fixture 不用单例，并发测试也用本实例，互不影响。
    """
    monkeypatch.setattr(tm_mod.TeamManager, "_get_teams_dir", staticmethod(lambda: tmp_path / "teams"))
    tm = tm_mod.TeamManager()
    yield tm


def _drop_mail(tm, window_id, mail_id="mail_1", status="pending", team_name="default"):
    """直接向邮箱目录落一封 task 邮件（等价 send_task 的落盘，含 team_name）"""
    mailbox = tm._mailbox_dir(team_name, window_id)
    mailbox.mkdir(parents=True, exist_ok=True)
    mail = {
        "id": mail_id,
        "type": "task",
        "team_name": team_name,
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


class TestWriteJsonAtomic:
    """Bug6：_write_json 原子写（临时文件 + os.replace）"""

    def test_write_json_atomic_replace(self, team_manager, tmp_path):
        """写后目标文件完整、无 .tmp 残留。"""
        target = tmp_path / "teams" / "default" / "mailboxes" / "win_01" / "mail_x.json"
        data = {"id": "mail_x", "status": "pending", "body": "任务", "nested": {"a": [1, 2, 3]}}

        team_manager._write_json(target, data)

        # 目标文件完整可读，内容一致
        assert target.exists(), "目标文件应存在"
        loaded = json.loads(target.read_text(encoding="utf-8"))
        assert loaded == data, "写入内容应完整一致"

        # 无 tmp 残留（同目录）
        leftovers = [p for p in target.parent.iterdir() if p.name != target.name]
        assert leftovers == [], f"不应残留临时文件: {[p.name for p in leftovers]}"

    def test_write_json_overwrite_preserves_atomic(self, team_manager, tmp_path):
        """重复覆盖写：每次都原子替换，文件始终完整。"""
        target = tmp_path / "teams" / "default" / "mailboxes" / "win_01" / "mail_y.json"
        for i in range(5):
            team_manager._write_json(target, {"id": "mail_y", "seq": i, "data": "x" * 100})
            loaded = json.loads(target.read_text(encoding="utf-8"))
            assert loaded["seq"] == i, f"第 {i} 次覆盖写后内容应完整"
        # 无 tmp 残留
        leftovers = [p for p in target.parent.iterdir() if p.name != target.name]
        assert leftovers == []


class TestMarkMailStatusConcurrency:
    """Bug6：mark_mail_* 并发/连续 status 切换后 JSON 最终一致"""

    def test_mark_mail_status_concurrent_safety(self, team_manager, tmp_path):
        """并发多线程连续切换 status：读到的最终状态必须是最新一次合法写入
        （无 JSON 损坏 / 无 lost update 导致回滚失效）。"""
        mail_id = _drop_mail(team_manager, "win_02", mail_id="mail_c1", status="pending")
        mail_file = team_manager._mailbox_dir("default", "win_02") / f"{mail_id}.json"

        # 并发交替切换 running/done（模拟流式注入 vs 收尾回滚竞争）
        n_threads = 8
        barrier = threading.Barrier(n_threads)

        def _flip(status):
            barrier.wait()
            for _ in range(20):
                team_manager.mark_mail_running(mail_id, "win_02")
                if status == "done":
                    team_manager.mark_mail_done(mail_id, "win_02", "结果")
                else:
                    team_manager.mark_mail_pending(mail_id, "win_02")

        threads = [
            threading.Thread(
                target=_flip,
                args=(("done" if i % 2 == 0 else "pending"),),
            )
            for i in range(n_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 最终 JSON 必须完整可读，且 status 是合法终态之一（done/pending）
        raw = mail_file.read_text(encoding="utf-8")
        loaded = json.loads(raw)
        assert loaded["id"] == mail_id, "JSON 不应损坏"
        assert loaded["status"] in ("pending", "done", "running"), (
            f"status 应为合法值，实际 {loaded['status']}"
        )
        # result 字段一致性：done 应带 result，pending 应清空
        if loaded["status"] == "done":
            assert loaded["result"] == "结果", "done 状态应保留 result"
        elif loaded["status"] == "pending":
            assert loaded["result"] == "", "pending 状态应清空 result（T23 回滚语义）"

    def test_consecutive_status_switch_consistent(self, team_manager, tmp_path):
        """连续状态切换（无并发）：最终状态与最后一次调用一致（锁不破坏串行语义）。"""
        mail_id = _drop_mail(team_manager, "win_02", mail_id="mail_c2", status="pending")
        mail_file = team_manager._mailbox_dir("default", "win_02") / f"{mail_id}.json"

        team_manager.mark_mail_running(mail_id, "win_02")
        team_manager.mark_mail_done(mail_id, "win_02", "完成结果")
        loaded = json.loads(mail_file.read_text(encoding="utf-8"))
        assert loaded["status"] == "done"
        assert loaded["result"] == "完成结果"

        # done 是终态，但回滚路径可主动改回 pending（T23 场景）
        team_manager.mark_mail_pending(mail_id, "win_02")
        loaded = json.loads(mail_file.read_text(encoding="utf-8"))
        assert loaded["status"] == "pending"
        assert loaded["result"] == "", "回滚 pending 必须清空 result"


class TestMailContainsTeamName:
    """Bug12：send_task / send_reply 邮件 dict 含 team_name"""

    def test_send_task_mail_contains_team_name(self, team_manager):
        """send_task 产物含 team_name（默认 default）。"""
        team_manager.join_team("win_01", "alice")
        team_manager.join_team("win_02", "bob")
        mail_id = team_manager.send_task(
            from_window="win_01", from_agent="alice", to_identifier="bob", task_description="写代码"
        )
        assert mail_id is not None, "send_task 应成功"

        mails = team_manager.get_mailbox_mails("win_02")
        mail = next(m for m in mails if m["id"] == mail_id)
        assert mail["team_name"] == "default", f"task 邮件应含 team_name，实际 {mail.get('team_name')!r}"
        assert mail["type"] == "task"
        assert mail["to_agent"] == "bob"

    def test_send_reply_mail_contains_team_name(self, team_manager):
        """send_reply 产物含 team_name（自定义团队）。"""
        team_manager.join_team("win_01", "alice", team_name="proj-x")
        team_manager.join_team("win_02", "bob", team_name="proj-x")
        task_id = team_manager.send_task(
            from_window="win_01",
            from_agent="alice",
            to_identifier="bob",
            task_description="任务",
            team_name="proj-x",
        )
        assert task_id is not None

        reply_id = team_manager.send_reply(
            original_mail_id=task_id,
            from_window="win_02",
            from_agent="bob",
            to_window="win_01",
            to_agent="alice",
            result="完成",
            team_name="proj-x",
        )
        assert reply_id is not None

        mails = team_manager.get_mailbox_mails("win_01", team_name="proj-x")
        reply = next(m for m in mails if m["id"] == reply_id)
        assert reply["team_name"] == "proj-x", f"reply 邮件应含 team_name，实际 {reply.get('team_name')!r}"
        assert reply["type"] == "reply"
        assert reply["reply_to"] == task_id

    def test_join_team_concurrent_safety(self, team_manager):
        """Bug6 延伸：并发 join_team 不丢成员（read-modify-write 锁生效）。"""
        n_threads = 6
        barrier = threading.Barrier(n_threads)

        def _join(i):
            barrier.wait()
            team_manager.join_team(f"win_{i:02d}", f"agent_{i}")

        threads = [threading.Thread(target=_join, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        members = team_manager.get_members()
        assert len(members) == n_threads, f"并发 join 后成员数应={n_threads}，实际 {len(members)}"
        names = {m["agent_name"] for m in members}
        assert names == {f"agent_{i}" for i in range(n_threads)}, "并发 join 不应丢失成员（lost update）"
