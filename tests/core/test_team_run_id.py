# -*- coding: utf-8 -*-
"""方案 A 阶段 2：团队运行标识（run_id）注入测试

覆盖：
1. TeamManager.start_team_run 生成 run_id 并持久化到 team.json 顶层（幂等复用）
2. get_team_run_id 读取；老团队无 run_id 返回空串
3. 成员清理（_cleanup_stale_members）不丢失 run_id（模板级而非成员级）
4. main_widget 团队加载后 _team_run_id 注入（通过 _handle_team_load 路径静态校验
   与 _do_join_team 语义）

团队会话自动保存落库（save_session 带 team 三参数 → DB 行）已由
tests/core/test_session_team_columns.py 覆盖，本文件聚焦 run_id 生成/持久化。
"""

from pathlib import Path

import pytest

from app.core import team_manager as tm_mod

# 🛡️ 模块导入时缓存真实 get_instance（test_team_template.py 部分用例直接赋值
# TeamManager.get_instance 为 _FakeTM 且不恢复，属既有隔离缺陷；此处提前
# 缓存真实引用，fixture 中强制恢复，避免本文件用例被污染类方法误导）。
_ORIG_GET_INSTANCE = tm_mod.TeamManager.__dict__["get_instance"]


@pytest.fixture
def fresh_tm(tmp_path, monkeypatch):
    """指向 tmp_path 的全新 TeamManager 实例（隔离，不污染真实 ~/.drifox/）。"""
    monkeypatch.setattr(tm_mod.TeamManager, "get_instance", _ORIG_GET_INSTANCE)
    monkeypatch.setattr(tm_mod.TeamManager, "_get_teams_dir", staticmethod(lambda: tmp_path))
    tm_mod.TeamManager._instance = None
    tm = tm_mod.TeamManager.get_instance()
    yield tm
    tm_mod.TeamManager._instance = None


def _team_file(tm, team_name: str) -> Path:
    return tm._team_file(team_name)


class TestStartTeamRun:
    def test_start_team_run_generates_and_persists(self, fresh_tm, tmp_path):
        """start_team_run 生成 uuid4 并写入 team.json 顶层。"""
        run_id = fresh_tm.start_team_run()
        assert run_id, "run_id 不应为空"
        assert len(run_id) == 32, "run_id 应为 uuid4 hex（32 字符）"

        data = fresh_tm._read_json(_team_file(fresh_tm, fresh_tm.DEFAULT_TEAM))
        assert data.get("run_id") == run_id, "run_id 应持久化到 team.json 顶层"

    def test_start_team_run_idempotent(self, fresh_tm):
        """同一团队重复 start_team_run 复用同一 run_id（不刷新运行标识）。"""
        run_id_1 = fresh_tm.start_team_run()
        run_id_2 = fresh_tm.start_team_run()
        assert run_id_1 == run_id_2, "同一团队应复用同一 run_id"

    def test_start_team_run_force_generates_new(self, fresh_tm):
        """force=True 无条件生成新 run_id 并落盘（恢复路径使用）。

        回归保护：一键恢复是一次新的团队运行，必须与历史 run_id 区分，
        否则恢复出的新会话仍归属旧 run_id，历史面板分组/恢复再次串台。
        """
        run_id_1 = fresh_tm.start_team_run()
        run_id_2 = fresh_tm.start_team_run(force=True)
        assert run_id_1 != run_id_2, "force=True 应生成全新 run_id"
        data = fresh_tm._read_json(_team_file(fresh_tm, fresh_tm.DEFAULT_TEAM))
        assert data.get("run_id") == run_id_2, "force 生成的新 run_id 应落盘"

    def test_start_team_run_force_idempotent_without_force(self, fresh_tm):
        """force=False（默认）对已有 run_id 幂等不变。"""
        run_id_1 = fresh_tm.start_team_run()
        run_id_2 = fresh_tm.start_team_run(force=False)
        assert run_id_1 == run_id_2, "默认（非 force）应复用已有 run_id"

    def test_get_team_run_id_returns_value(self, fresh_tm):
        """get_team_run_id 返回已持久化的 run_id。"""
        run_id = fresh_tm.start_team_run()
        assert fresh_tm.get_team_run_id() == run_id

    def test_get_team_run_id_empty_for_old_team(self, fresh_tm, tmp_path):
        """老团队（无 run_id）返回空串——不生成、不注入，行为与现状一致。"""
        assert fresh_tm.get_team_run_id() == ""
        # team.json 中不应出现 run_id 键
        data = fresh_tm._read_json(_team_file(fresh_tm, fresh_tm.DEFAULT_TEAM))
        assert "run_id" not in data

    def test_run_id_survives_stale_member_cleanup(self, fresh_tm, tmp_path):
        """run_id 在成员清理后仍保留（模板级字段而非成员级）。

        回归保护：_cleanup_stale_members 删除失效成员时只动 members 字典，
        绝不能连带把 run_id 清掉——否则团队会话与 run 的关联随成员清理丢失。
        """
        run_id = fresh_tm.start_team_run()

        # 加入两个成员，同步活跃窗口集合后清理其中一个
        fresh_tm.set_active_window_ids({"win_01", "win_02"})
        fresh_tm.join_team("win_01", "build")
        fresh_tm.join_team("win_02", "plan")

        # 窗口 win_02 关闭 → 活跃集合只剩 win_01，触发清理
        fresh_tm.set_active_window_ids({"win_01"})
        fresh_tm._cleanup_stale_members(fresh_tm.DEFAULT_TEAM)

        members = fresh_tm.get_members()
        assert {m["window_id"] for m in members} == {"win_01"}
        assert fresh_tm.get_team_run_id() == run_id, "成员清理不应丢失 run_id"

        # 直接检查文件，确认 run_id 仍在顶层
        data = fresh_tm._read_json(_team_file(fresh_tm, fresh_tm.DEFAULT_TEAM))
        assert data.get("run_id") == run_id

    def test_join_team_does_not_create_run_id(self, fresh_tm, tmp_path):
        """仅手动 join_team（未 start_team_run）不产生 run_id（老团队兼容）。"""
        fresh_tm.set_active_window_ids({"win_01"})
        fresh_tm.join_team("win_01", "build")
        assert fresh_tm.get_team_run_id() == ""


class TestTeamRunInjectPoints:
    """main_widget 注入点静态校验（不实例化完整 UI，避免 Qt 依赖）。"""

    def test_main_widget_has_team_run_id_attr(self):
        """OpenAIChatToolWindow 应声明 _team_run_id 窗口属性。"""
        src = Path(__file__).resolve().parent.parent.parent / "app" / "main_widget.py"
        text = src.read_text(encoding="utf-8")
        assert "self._team_run_id" in text, "main_widget 缺少 _team_run_id 窗口属性"

    def test_handle_team_load_injects_run_id(self):
        """团队加载路径应 start_team_run 并给新窗口注入 run_id。

        T5 重构：run_id 注入迁至公共创建方法 _spawn_team_member_window
        （_handle_team_load → _spawn_team_members → _spawn_team_member_window）。
        """
        src = Path(__file__).resolve().parent.parent.parent / "app" / "main_widget.py"
        text = src.read_text(encoding="utf-8")

        # _handle_team_load 仍负责 start_team_run（生成/复用 run_id）
        start = text.find("    def _handle_team_load(")
        assert start >= 0
        body_end = len(text)
        for probe in ("\n    def ", "\n    class "):
            idx = text.find(probe, start + 10)
            if idx >= 0:
                body_end = min(body_end, idx)
        body = text[start:body_end]
        assert "start_team_run" in body, "_handle_team_load 未调用 start_team_run"

        # 新窗口 run_id 注入在 _spawn_team_member_window（创建链路语义所在）
        start = text.find("    def _spawn_team_member_window(")
        assert start >= 0, "缺少 _spawn_team_member_window 公共创建方法"
        body_end = len(text)
        for probe in ("\n    def ", "\n    class "):
            idx = text.find(probe, start + 10)
            if idx >= 0:
                body_end = min(body_end, idx)
        body = text[start:body_end]
        assert "win._team_run_id" in body, "_spawn_team_member_window 未给新窗口注入 _team_run_id"
        assert "get_team_run_id" in body, "_spawn_team_member_window 应复用团队 run_id（禁止 force 新 run_id）"

    def test_do_join_team_reads_run_id(self):
        """_do_join_team 应读取团队已有 run_id 赋给窗口。"""
        src = Path(__file__).resolve().parent.parent.parent / "app" / "main_widget.py"
        text = src.read_text(encoding="utf-8")
        start = text.find("    def _do_join_team(")
        assert start >= 0
        body_end = len(text)
        for probe in ("\n    def ", "\n    class "):
            idx = text.find(probe, start + 10)
            if idx >= 0:
                body_end = min(body_end, idx)
        body = text[start:body_end]
        assert "get_team_run_id" in body, "_do_join_team 未读取团队 run_id"
        assert "self._team_run_id" in body, "_do_join_team 未赋值 _team_run_id"

    def test_auto_save_passes_team_kwargs(self):
        """_auto_save_current_session 的 save 调用应透传团队三参数。"""
        src = Path(__file__).resolve().parent.parent.parent / "app" / "main_widget.py"
        text = src.read_text(encoding="utf-8")
        start = text.find("    def _auto_save_current_session(")
        assert start >= 0
        body_end = len(text)
        for probe in ("\n    def ", "\n    class "):
            idx = text.find(probe, start + 10)
            if idx >= 0:
                body_end = min(body_end, idx)
        body = text[start:body_end]
        assert "team_run_id=self._team_run_id" in body, "save_session 未透传 team_run_id"
        assert "team_name=self._team_name" in body, "save_session 未透传 team_name"
        assert "agent_name=self._team_agent_name" in body, "save_session 未透传 agent_name"
        assert "team_kwargs" in body, "update_session 未按保留现值语义处理团队字段"
