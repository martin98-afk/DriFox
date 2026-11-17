# -*- coding: utf-8 -*-
"""🚪 外部消息统一经 leader 回归测试（team_send_message 改投路由）。

契约：非成员窗口（from_agent=user）的任何消息不得直投内部成员——
- 单发成员 → 自动改投 leader 邮箱（正文标注原目标），leader 转交
- 单发 leader 本身 → 直投（leader 即外部入口）
- 群发（list/"*"）→ 整体打包一封改投 leader 分发
- 团队未登记 leader → 回退直投并提示
- 团队内部成员间交流不受限
"""

import tempfile
from pathlib import Path

import pytest

@pytest.fixture()
def route_env(monkeypatch):
    tmp = Path(tempfile.mkdtemp(prefix="drifox_route_test_"))
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
        "team_tools_route",
        str(Path(__file__).resolve().parent.parent.parent / "plugins" / "system-team" / "tools" / "team_tools.py"),
    )
    tt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tt)

    # 标准团队：win_90=leader（已登记），win_91=build，win_92=review；win_95=外部窗口
    tm = tmm.TeamManager.get_instance()
    run_id = tm.start_team_run(force=True)
    tm.join_team("win_90", "leader", run_id=run_id, team_label="g")
    tm.join_team("win_91", "build", run_id=run_id, team_label="g")
    tm.join_team("win_92", "review", run_id=run_id, team_label="g")
    with tm._data_lock:
        data = tm._get_team_data(tm.DEFAULT_TEAM)
        data["leader_window_id"] = "win_90"
        tm._save_team_data(tm.DEFAULT_TEAM)

    yield {"tt": tt, "tm": tm}

    tmm.TeamManager.reset_instance()


def _external_ctx():
    return {"team_window_id": "win_95", "team_agent_name": "build"}


def test_external_single_to_member_is_rerouted_to_leader(route_env):
    """外部单发成员 → 改投 leader 邮箱，目标成员邮箱保持为空。"""
    tt, tm = route_env["tt"], route_env["tm"]
    r = tt._team_send_message(_external_ctx(), "build", "外部任务")
    assert r.success, r.error
    assert "改投" in r.content and "经 leader" in r.content
    assert tm.get_mailbox_mails("win_91") == []
    mails = tm.get_mailbox_mails("win_90")
    assert len(mails) == 1
    mail = mails[0]
    assert mail["from_agent"] == "user"
    assert mail["from_window"] == "win_95"
    assert mail["to_agent"] == "leader"
    assert mail["to_window"] == "win_90"
    assert "原目标: build@win_91" in mail["body"]
    assert "外部任务" in mail["body"]


def test_external_single_to_leader_goes_direct(route_env):
    """外部单发 leader 本身 → 直投，正文原文无改投标记。"""
    tt, tm = route_env["tt"], route_env["tm"]
    r = tt._team_send_message(_external_ctx(), "leader", "给 leader 的话")
    assert r.success, r.error
    mails = tm.get_mailbox_mails("win_90")
    assert len(mails) == 1
    assert mails[0]["body"] == "给 leader 的话"
    assert mails[0]["from_agent"] == "user"


def test_external_broadcast_is_rerouted_to_leader(route_env):
    """外部群发（list 与 "*"）→ 整体一封改投 leader，其他成员邮箱为空。"""
    tt, tm = route_env["tt"], route_env["tm"]
    r = tt._team_send_message(_external_ctx(), ["build", "review"], "广播内容")
    assert r.success, r.error
    assert "改投" in r.content
    assert tm.get_mailbox_mails("win_91") == []
    assert tm.get_mailbox_mails("win_92") == []
    mails = tm.get_mailbox_mails("win_90")
    assert len(mails) == 1
    assert "原目标: build, review" in mails[0]["body"]
    assert "广播内容" in mails[0]["body"]

    r2 = tt._team_send_message(_external_ctx(), "*", "全员广播")
    assert r2.success, r2.error
    mails2 = tm.get_mailbox_mails("win_90")
    assert len(mails2) == 2
    assert "原目标: 全队(*)" in mails2[-1]["body"]
    assert "全员广播" in mails2[-1]["body"]


def test_member_to_member_stays_direct(route_env):
    """团队成员间交流不受限：单发直投目标成员，不经 leader。"""
    tt, tm = route_env["tt"], route_env["tm"]
    r = tt._team_send_message(
        {"team_window_id": "win_91", "team_agent_name": "build"}, "review", "内部协作"
    )
    assert r.success, r.error
    assert tm.get_mailbox_mails("win_90") == []
    mails = tm.get_mailbox_mails("win_92")
    assert len(mails) == 1
    assert mails[0]["body"] == "内部协作"
    assert mails[0]["from_agent"] == "build"


def test_member_broadcast_stays_direct(route_env):
    """成员群发不受限：逐目标直投。"""
    tt, tm = route_env["tt"], route_env["tm"]
    r = tt._team_send_message(
        {"team_window_id": "win_90", "team_agent_name": "leader"}, "*", "全员注意"
    )
    assert r.success, r.error
    assert "群发完成：成功 2 封" in r.content
    assert len(tm.get_mailbox_mails("win_91")) == 1
    assert len(tm.get_mailbox_mails("win_92")) == 1


def test_external_fallback_when_no_leader_registered(route_env):
    """团队未登记 leader → 外部消息回退直投并提示。"""
    tt, tm = route_env["tt"], route_env["tm"]
    with tm._data_lock:
        data = tm._get_team_data(tm.DEFAULT_TEAM)
        data["leader_window_id"] = ""
        tm._save_team_data(tm.DEFAULT_TEAM)
    r = tt._team_send_message(_external_ctx(), "build", "无 leader 场景")
    assert r.success, r.error
    assert "未登记 leader" in r.content
    mails = tm.get_mailbox_mails("win_91")
    assert len(mails) == 1
    assert mails[0]["body"] == "无 leader 场景"
    assert mails[0]["from_agent"] == "user"


def test_external_to_unknown_target_fails(route_env):
    """外部发给不存在的目标 → 报「未找到目标」，leader 邮箱无邮件。"""
    tt, tm = route_env["tt"], route_env["tm"]
    r = tt._team_send_message(_external_ctx(), "ghost", "找不到人")
    assert not r.success
    assert "未找到目标" in (r.error or "")
    assert tm.get_mailbox_mails("win_90") == []
