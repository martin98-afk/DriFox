# -*- coding: utf-8 -*-
"""团队工具快照可见性测试（#10 review 设计：用例 15-16）。

会话工具快照 key=(session_id, current_agent, in_team)，in_team 实时查
TeamManager.is_team_member(builtin_tools._team_window_id)。本文件在
注册表层面锁定该契约：team_manage / team_send_message 均对非成员可见
（create 不再入队，外部窗口需直接派任务），key 仍按 in_team 区分。
"""

import tempfile
from pathlib import Path

import pytest


@pytest.fixture()
def registry_env(monkeypatch):
    tmp = Path(tempfile.mkdtemp(prefix="drifox_snap_test_"))
    from app.core.team import team_manager as tmm

    monkeypatch.setattr(tmm.TeamManager, "_get_teams_dir", staticmethod(lambda: tmp))
    tmm.TeamManager.reset_instance()

    from app.tools.registry import ToolRegistry

    reg = ToolRegistry.get_instance()
    reg.clear()

    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "team_tools_snap",
        str(Path(__file__).resolve().parent.parent.parent / "plugins" / "system-team" / "tools" / "team_tools.py"),
    )
    tt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tt)

    class _Proxy:
        def register(self, name, schema, **kwargs):
            kwargs.setdefault("source", "plugin:test")
            return reg.register(name, schema, **kwargs)

    tt.register(_Proxy())
    yield {"reg": reg, "tm": tmm.TeamManager.get_instance(), "tt": tt}
    reg.clear()
    tmm.TeamManager.reset_instance()


def _visible_tools(reg: "ToolRegistry", in_team: bool) -> set:
    """复刻 agent.py 的 team_only schema 过滤语义（不 import agent.py）。"""
    team_only = reg.team_only_tools()
    return {name for name in reg.names() if in_team or name not in team_only}


def _snapshot_key(session_id: str, agent: str, in_team: bool):
    return (session_id, agent, in_team)


def test_snapshot_tools_visible_without_join(registry_env):
    """新契约：team_manage 与 team_send_message 都对非成员可见。

    create 不再把调用窗口入队，非成员会话必须能直接给成员派任务，
    因此两者都从 team_only 集合移除；join 前后工具集不翻转。
    """
    reg, tm = registry_env["reg"], registry_env["tm"]
    wid = "win_71"

    key1 = _snapshot_key("sess-a", "build", tm.is_team_member(wid))
    snap1 = _visible_tools(reg, key1[2])
    assert "team_manage" in snap1
    assert "team_send_message" in snap1  # 非成员也可见（外部发起派单）

    tm.join_team(wid, "build", run_id=tm.start_team_run(force=True))
    key2 = _snapshot_key("sess-a", "build", tm.is_team_member(wid))
    snap2 = _visible_tools(reg, key2[2])

    assert key2[2] is True
    assert snap2 == snap1


def test_snapshot_key_distinct_by_in_team(registry_env):
    """同 (session, agent) 不同 in_team 的 key 必须不相等（缓存重建依据）。"""
    k_off = _snapshot_key("sess-a", "build", False)
    k_on = _snapshot_key("sess-a", "build", True)
    assert k_off != k_on
    assert k_on[2] is True and k_off[2] is False


def test_team_only_set_matches_contract(registry_env):
    reg = registry_env["reg"]
    team_only = reg.team_only_tools()
    # 新契约：两个团队工具都对非成员可见（create 后原窗口直接派任务）
    assert "team_send_message" not in team_only
    assert "team_manage" not in team_only
