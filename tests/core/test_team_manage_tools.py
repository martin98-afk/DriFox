# -*- coding: utf-8 -*-
"""team_manage 工具层测试（#4/#10：可见性 + create/add/remove + 群发单发）。

用例映射（review 18 条设计）：1, 2, 3, 4, 5, 6, 7, 8, 9, 17, 18。
隔离：TeamManager._get_teams_dir 指向临时目录；caps_for 按需指向真 facade 或 None。
"""

import json
import tempfile
from pathlib import Path

import pytest

_TEST_DIR = Path(__file__).resolve().parent


@pytest.fixture()
def team_env(monkeypatch):
    """隔离 TeamManager 存储 + 按能力 ID 装配 caps_for。"""
    tmp = Path(tempfile.mkdtemp(prefix="drifox_tm_test_"))
    from app.core.team import team_manager as tmm

    monkeypatch.setattr(tmm.TeamManager, "_get_teams_dir", staticmethod(lambda: tmp))
    tmm.TeamManager.reset_instance()

    from app.plugins.capabilities.facets import build_facet
    import app.plugins.sdk as sdk

    enabled = {"team.read", "team.send", "team.manage"}

    def fake_caps_for(cap_id, plugin="", surface=""):
        if cap_id in enabled:
            return build_facet(cap_id)
        return None  # team.spawn / agent.read 默认缺失（按用例开启）

    monkeypatch.setattr(sdk, "caps_for", fake_caps_for)

    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "team_tools_test", str(_TEST_DIR.parent.parent / "plugins" / "system-tools" / "tools" / "team_tools.py")
    )
    tt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tt)

    yield {"tt": tt, "tm": tmm.TeamManager.get_instance(), "enabled": enabled}

    tmm.TeamManager.reset_instance()


def _member_ctx(wid="win_90", agent="leader"):
    return {"team_window_id": wid, "team_agent_name": agent}


# ── 用例 1：可见性（team_manage 全员可见；team_send_message 团队专用）──

def test_team_manage_visible_to_non_member(team_env, monkeypatch):
    from app.tools.registry import ToolRegistry

    tt = team_env["tt"]
    reg = ToolRegistry.get_instance()
    reg.clear()
    try:
        tt.register(_Proxy(reg))  # 走插件代理语义（source=plugin:*）
        tools = reg.team_only_tools()
        assert "team_manage" not in tools
        assert "team_send_message" in tools
        # 工具确实注册成功
        assert reg.get("team_manage") is not None
        assert reg.get("team_send_message") is not None
    finally:
        reg.clear()


class _Proxy:
    """模拟 _PluginRegistryProxy：强制 source="plugin:test"（绕开 builtin 护栏）。"""

    def __init__(self, registry):
        self._registry = registry

    def register(self, name, schema, **kwargs):
        kwargs.setdefault("source", "plugin:test")
        return self._registry.register(name, schema, **kwargs)


# ── 用例 2/3：create ──

def test_team_manage_action_create_basic(team_env):
    tt, tm = team_env["tt"], team_env["tm"]
    r = tt._team_manage_impl(
        _member_ctx(),
        action="create",
        team_name="g1",
        team_role="dev",
        members=[{"agent_name": "build", "description": "干活"}],
    )
    assert r.success, r.error
    run_id = tm.get_team_run_id()
    assert run_id
    # leader 身份固定为 "leader"（P0①）
    leader_wid = tm.get_leader_window_id()
    assert leader_wid == "win_90"
    assert tm.get_member_agent_name(leader_wid) == "leader"
    # 槽位 runtime 标记
    slot = tm.get_template_for_run_id(run_id)
    assert slot and slot.get("runtime") is True and slot.get("name") == "g1"


def test_team_manage_action_create_registers_leader_as_member(team_env):
    tt, tm = team_env["tt"], team_env["tm"]
    r = tt._team_manage_impl(_member_ctx("win_91"), action="create", team_name="g2", team_role="x")
    assert r.success
    assert tm.is_team_member("win_91")
    assert any(m["agent_name"] == "leader" for m in tm.get_members())


# ── 用例 4：add（team.spawn 缺失 → 结构化未就绪，不写 members）──

def test_team_manage_action_add_reports_spawn_unavailable(team_env):
    tt, tm = team_env["tt"], team_env["tm"]
    tt._team_manage_impl(_member_ctx(), action="create", team_name="g1", team_role="dev")
    tm.join_team("win_91", "build", run_id=tm.get_team_run_id())
    before = len(tm.get_members())
    r = tt._team_manage_impl(
        {"team_window_id": "win_91", "team_agent_name": "build"}, action="add", agent_name="plan", role="规划"
    )
    assert not r.success
    assert "team.spawn" in r.error
    assert "submitted" in r.error  # 结构化回执
    assert len(tm.get_members()) == before  # 不写 members


# ── 用例 5：schema 契约（remove 参数声明；team_manage 无 team_only）──

def test_team_manage_schema_contract(team_env):
    tt = team_env["tt"]
    schema = tt._TEAM_MANAGE_SCHEMA["function"]
    assert schema["name"] == "team_manage"
    props = schema["parameters"]["properties"]
    assert set(props) >= {"action", "team_name", "team_role", "members", "agent_name", "role"}
    assert schema["parameters"]["required"] == ["action"]
    enum = props["action"]["enum"]
    assert set(enum) == {"list", "create", "add", "remove", "leave"}
    # role 在 add 语境必填（description 声明）；send schema 单发/群发口径
    send_props = tt._TEAM_SEND_SCHEMA["function"]["parameters"]["properties"]
    assert send_props["to_agent"]["type"] == ["string", "array"]


# ── 用例 6-9：单发与群发 ──

def _prepare_team(tm):
    run_id = tm.get_team_run_id() or tm.start_team_run(force=True)
    tm.join_team("win_90", "leader", run_id=run_id)
    tm.join_team("win_91", "build", run_id=run_id)
    tm.join_team("win_92", "review", run_id=run_id)
    # 群发/门禁前提：登记 leader（模拟 team_manage(action=create) 的登记）
    with tm._data_lock:
        data = tm._get_team_data(tm.DEFAULT_TEAM)
        data["leader_window_id"] = "win_90"
        tm._save_team_data(tm.DEFAULT_TEAM)


def test_single_send_reaches_mailbox(team_env):
    tt, tm = team_env["tt"], team_env["tm"]
    _prepare_team(tm)
    mid = build_send(tm).send_task(
        from_window="win_90", from_agent="leader", to_identifier="build@win_91", task_description="任务A"
    )
    assert mid
    mails = tm.get_mailbox_mails("win_91")
    assert len(mails) == 1 and mails[0]["body"] == "任务A"


def build_send(tm):
    from app.plugins.capabilities.facets import build_facet

    return build_facet("team.send")()


def test_single_send_unknown_target_returns_none(team_env):
    tt, tm = team_env["tt"], team_env["tm"]
    _prepare_team(tm)
    assert build_send(tm).send_task(
        from_window="win_90", from_agent="leader", to_identifier="ghost", task_description="x"
    ) is None


def test_broadcast_reports_per_target_and_skips_offline(team_env):
    tt, tm = team_env["tt"], team_env["tm"]
    _prepare_team(tm)
    mg = _facade("team.manage")
    res = mg.send_task_broadcast("s", "body", targets=["build", "ghost"])
    assert len(res["sent"]) == 1 and res["sent"][0]["identifier"] == "build@win_91"
    assert any(t == "ghost" for t, _ in res["skipped"])


def test_broadcast_star_excludes_self(team_env):
    tt, tm = team_env["tt"], team_env["tm"]
    _prepare_team(tm)
    res = _facade("team.manage").send_task_broadcast("s", "body", targets=["*"])
    ids = {s["identifier"] for s in res["sent"]}
    assert ids == {"build@win_91", "review@win_92"}


def _facade(cap_id):
    from app.plugins.capabilities.facets import build_facet

    return build_facet(cap_id)()


# ── 用例 17：非成员 action=list → 友好文案（success=True）──

def test_team_manage_action_list_non_member(team_env):
    tt = team_env["tt"]
    r = tt._team_manage_impl({}, action="list")
    assert r.success is True
    assert "不在任何团队" in r.content


# ── 用例 18：已在团队中 create → 拒绝且不重复建 run ──

def test_team_manage_action_create_when_already_in_team(team_env):
    tt, tm = team_env["tt"], team_env["tm"]
    tt._team_manage_impl(_member_ctx(), action="create", team_name="g1", team_role="dev")
    old_run = tm.get_team_run_id()
    r = tt._team_manage_impl(_member_ctx(), action="create", team_name="g2", team_role="y")
    assert r.success is False
    assert ("已在团队" in r.error) or ('action="add"' in r.error)
    assert tm.get_team_run_id() == old_run  # 未重复建 run
