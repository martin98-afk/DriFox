# -*- coding: utf-8 -*-
"""team_manage leader 门禁测试（#4/#10 review 设计：用例 10-14）。

remove 与群发仅 leader；leave 后 leader 自动接任（joined_at 最早存活）。
"""

import tempfile
from pathlib import Path

import pytest


@pytest.fixture()
def team_env(monkeypatch):
    tmp = Path(tempfile.mkdtemp(prefix="drifox_gate_test_"))
    from app.core.team import team_manager as tmm

    monkeypatch.setattr(tmm.TeamManager, "_get_teams_dir", staticmethod(lambda: tmp))
    tmm.TeamManager.reset_instance()

    from app.plugins.capabilities.facets import build_facet
    import app.plugins.sdk as sdk

    def fake_caps_for(cap_id, plugin="", surface=""):
        if cap_id in ("team.read", "team.send", "team.manage"):
            return build_facet(cap_id)
        return None

    monkeypatch.setattr(sdk, "caps_for", fake_caps_for)

    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "team_tools_gate",
        str(Path(__file__).resolve().parent.parent.parent / "plugins" / "system-team" / "tools" / "team_tools.py"),
    )
    tt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tt)

    # 标准团队：win_90=leader（最早），win_91=build，win_92=review
    tm = tmm.TeamManager.get_instance()
    run_id = tm.start_team_run(force=True)
    tm.join_team("win_90", "leader", run_id=run_id, team_label="g")
    tm.join_team("win_91", "build", run_id=run_id, team_label="g")
    tm.join_team("win_92", "review", run_id=run_id, team_label="g")
    tm.remove_member  # noqa: B018  (存在性自检)
    with_team_leader(tm)

    yield {"tt": tt, "tm": tm, "run_id": run_id}

    tmm.TeamManager.reset_instance()


def with_team_leader(tm):
    """把 win_90 标为 leader（模拟 team_manage(action=create) 的登记）。"""
    with tm._data_lock:
        data = tm._get_team_data(tm.DEFAULT_TEAM)
        data["leader_window_id"] = "win_90"
        tm._save_team_data(tm.DEFAULT_TEAM)


def test_remove_denied_for_non_leader(team_env):
    tt = team_env["tt"]
    r = tt._team_manage_impl({"team_window_id": "win_91", "team_agent_name": "build"}, action="remove", agent_name="review")
    assert not r.success
    assert "leader" in r.error


def test_broadcast_denied_for_non_leader(team_env):
    tt, tm = team_env["tt"], team_env["tm"]
    r = tt._team_send_message(
        {"team_window_id": "win_91", "team_agent_name": "build"},
        ["review@win_92"],
        "群发尝试",
    )
    assert not r.success
    assert "leader" in r.error


def test_remove_same_name_members_all(team_env):
    tt, tm = team_env["tt"], team_env["tm"]
    tm.join_team("win_93", "build", run_id=team_env["run_id"], team_label="g")
    r = tt._team_manage_impl({"team_window_id": "win_90", "team_agent_name": "leader"}, action="remove", agent_name="build")
    assert r.success
    assert "2" in r.content
    remaining = {m["window_id"] for m in tm.get_members()}
    assert "win_91" not in remaining and "win_93" not in remaining


def test_remove_cannot_remove_self(team_env):
    tt = team_env["tt"]
    r = tt._team_manage_impl({"team_window_id": "win_90", "team_agent_name": "leader"}, action="remove", agent_name="leader")
    assert not r.success
    assert "不能移除自己" in r.error


def test_leader_leave_succession_to_earliest_survivor(team_env):
    tt, tm = team_env["tt"], team_env["tm"]
    r = tt._team_manage_impl({"team_window_id": "win_90", "team_agent_name": "leader"}, action="leave")
    assert r.success
    # 存活成员 joined_at 最早的是 win_91（build）
    assert tm.get_leader_window_id() == "win_91"
    assert tm.is_team_member("win_91")


def test_last_member_leave_clears_team(team_env):
    tt, tm = team_env["tt"], team_env["tm"]
    tm.remove_member(window_id="win_91")
    tm.remove_member(window_id="win_92")
    r = tt._team_manage_impl({"team_window_id": "win_90", "team_agent_name": "leader"}, action="leave")
    assert r.success
    assert tm.get_leader_window_id() == ""
    assert tm.get_members() == []
