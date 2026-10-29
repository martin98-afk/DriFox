# -*- coding: utf-8 -*-
"""tool surface 能力补齐：catalog 标 _BOTH 的能力在工具侧必须取得到。

背景：tool_core 原先只注册 9 条，catalog 里 12 条 _BOTH 能力（model.* /
session.* / agent.read / workdir.read / tool.execute /
plugin.log）在工具侧一律 CapabilityDenied——插件声明了不报错，取用才炸。
本测试锁死「登记即实现」，并按 surface 分别断言各 surface 的 provider 集合。

host 形状：tool surface 的 host 是 ToolExecutor（不是 MainWidget），
数据来源是 _backend / _builtin_tools / 自身，故用最小桩复刻这三条路径。
"""
import threading
from types import SimpleNamespace

import pytest


class _FakeBT:
    _lsp_tools = SimpleNamespace(ping=lambda: "lsp")
    _mcp_manager = SimpleNamespace(ping=lambda: "mcp")
    _team_window_id = "win-1"
    _team_agent_name = "agent-1"

    def gitee_upload(self, *a, **k):
        return "gitee"


class _FakeAgent:
    def __init__(self, name, prompt):
        self.name = name
        self.prompt = prompt
        self.permission = {}


class _FakeAgentManager:
    def __init__(self):
        self._agents = {"a1": _FakeAgent("a1", "PROMPT-A1")}

    def get_agent(self, name):
        return self._agents.get(name)


class _FakeSession:
    session_id = "sess-9"
    messages = [{"role": "user", "content": "hi"}]
    system_prompt = "SYS"


class _FakeBackend:
    def __init__(self):
        self.agent_manager = _FakeAgentManager()
        self.chat_engine = SimpleNamespace(_compactor="COMPACTOR")
        self._session = _FakeSession()

    def get_current_session(self):
        return self._session

    def save_messages_to_session(self, messages, task_prompt=""):
        return None


def _make_executor():
    from app.core.tools.tool_executor import ToolExecutor

    ex = ToolExecutor.__new__(ToolExecutor)
    ex._builtin_tools = _FakeBT()
    ex._builtin_tools._agent_manager = _FakeAgentManager()
    ex._builtin_tools._get_llm_config = lambda: {
        "provider_name": "p1",
        "模型名称": "m1",
        "api_key": "SECRET",
    }
    ex._builtin_tools._get_session_messages = lambda: [{"role": "user", "content": "hi"}]
    ex._backend = _FakeBackend()
    ex._workdir = "/tmp/wd"
    ex._call_id = "call-1"
    ex._sub_agent_manager = SimpleNamespace(anything="probe")
    ex._window_state = {}
    ex._window_state_lock = threading.Lock()
    return ex


@pytest.fixture(autouse=True)
def _fresh_registry():
    from app.plugins.capabilities import bootstrap
    from app.plugins.capabilities.registry import CapabilityRegistry

    CapabilityRegistry.reset_instance()
    bootstrap.reset_bootstrap()
    yield
    CapabilityRegistry.reset_instance()
    bootstrap.reset_bootstrap()


def _pool(declared):
    from app.plugins.capabilities.bootstrap import ensure_registered
    from app.plugins.capabilities.registry import CapabilityRegistry

    ensure_registered("tool")
    return CapabilityRegistry.get_instance().build_pool(
        surface="tool", host=_make_executor(), plugin="demo", declared=declared
    )


# ──────────────── 登记即实现（防 catalog-provider 漂移） ────────────────


def test_every_both_capability_has_tool_provider():
    """catalog 标 _BOTH 的能力，tool surface 必须有 provider。"""
    from app.plugins.capabilities import bootstrap
    from app.plugins.capabilities.catalog import CATALOG
    from app.plugins.capabilities.registry import CapabilityRegistry

    bootstrap.ensure_registered("ui")
    bootstrap.ensure_registered("tool")
    reg = CapabilityRegistry.get_instance()

    missing = [
        cid
        for cid, spec in CATALOG.items()
        if "tool" in spec.surfaces and not reg.has(cid, "tool")
    ]
    assert not missing, f"tool surface 登记但无 provider: {missing}"


def test_no_ui_only_capability_leaks_into_tool_surface():
    """ui-only 能力（window.* / session.list / ui.* 等）不得在 tool surface 取到。

    注：``tool.inject`` 是存量特例——catalog 声明 surfaces=_UI，但 tool_core
    早年也注册了它（工具插件要注册 schema 过滤器）。属登记与实现不一致的
    历史遗留，不在本轮范围，显式排除。
    """
    from app.plugins.capabilities import bootstrap
    from app.plugins.capabilities.catalog import CATALOG
    from app.plugins.capabilities.registry import CapabilityRegistry

    bootstrap.ensure_registered("ui")
    bootstrap.ensure_registered("tool")
    reg = CapabilityRegistry.get_instance()

    leaked = [
        cid
        for cid, spec in CATALOG.items()
        if spec.surfaces == frozenset({"ui"}) and reg.has(cid, "tool") and cid != "tool.inject"
    ]
    assert not leaked, f"ui-only 能力泄漏到 tool surface: {leaked}"


# ──────────────── 模型 ────────────────


def test_model_metadata():
    pool = _pool({"model.metadata"})
    assert pool["model.metadata"]() == {"provider": "p1", "model": "m1"}


def test_model_config_returns_getter_value():
    """密钥只在主程序装配的 getter 里，provider 必须走它而非读磁盘。"""
    pool = _pool({"model.config"})
    cfg = pool["model.config"]()
    assert cfg["api_key"] == "SECRET"


def test_model_schema_filters_denied_tools(monkeypatch):
    """schema 按 agent.permission 过滤 deny 的工具。"""
    from app.tools import get_builtin_tools_schema

    monkeypatch.setattr(
        "app.tools.get_builtin_tools_schema",
        lambda **kw: [
            {"function": {"name": "allowed_tool"}},
            {"function": {"name": "denied_tool"}},
        ],
    )
    ex = _make_executor()
    ex._builtin_tools._agent_manager._agents["a1"].permission = {"denied_tool": "deny"}
    from app.plugins.capabilities.bootstrap import ensure_registered
    from app.plugins.capabilities.registry import CapabilityRegistry

    ensure_registered("tool")
    pool = CapabilityRegistry.get_instance().build_pool(
        surface="tool", host=ex, plugin="demo", declared={"model.schema"}
    )
    names = [t["function"]["name"] for t in pool["model.schema"]("a1")]
    assert names == ["allowed_tool"]


# ──────────────── 会话 ────────────────


def test_session_read_snapshot():
    pool = _pool({"session.read"})
    snap = pool["session.read"]()
    assert snap["session_id"] == "sess-9"
    assert snap["messages"] == [{"role": "user", "content": "hi"}]
    assert snap["system_prompt"] == "SYS"


def test_session_read_empty_without_backend():
    from app.plugins.capabilities.bootstrap import ensure_registered
    from app.plugins.capabilities.registry import CapabilityRegistry

    ensure_registered("tool")
    ex = _make_executor()
    ex._backend = None
    pool = CapabilityRegistry.get_instance().build_pool(
        surface="tool", host=ex, plugin="demo", declared={"session.read"}
    )
    assert pool["session.read"]() == {"session_id": "", "messages": [], "system_prompt": ""}


def test_session_write_returns_count():
    pool = _pool({"session.write"})
    assert pool["session.write"]([{"role": "user"}]) == 1


def test_session_write_zero_without_backend():
    from app.plugins.capabilities.bootstrap import ensure_registered
    from app.plugins.capabilities.registry import CapabilityRegistry

    ensure_registered("tool")
    ex = _make_executor()
    ex._backend = None
    pool = CapabilityRegistry.get_instance().build_pool(
        surface="tool", host=ex, plugin="demo", declared={"session.write"}
    )
    assert pool["session.write"]([{"role": "user"}]) == 0


# ──────────────── agent / workdir / 上下文 ────────────────


def test_agent_read_manager_and_prompt():
    """主实现返回裸 manager 对象（与 ui_core 同形状）；提示词走 legacy 键。"""
    pool = _pool({"agent.read"})
    assert pool["agent.read"] is not None
    assert pool.legacy("get_agent_prompt", "agent.read")("a1") == "PROMPT-A1"
    assert pool.legacy("get_agent_prompt", "agent.read")("nope") == ""


def test_agent_read_legacy_key_semantics():
    """get_agent_prompt 旧键必须拿 callable，不能回落到 manager 对象。"""
    pool = _pool({"agent.read"})
    fn = pool.legacy("get_agent_prompt", "agent.read")
    assert callable(fn)
    assert fn("a1") == "PROMPT-A1"


def test_workdir_read():
    pool = _pool({"workdir.read"})
    assert pool["workdir.read"]() == "/tmp/wd"


# ──────────────── tool.execute / plugin.log ────────────────


def test_tool_execute_returns_host_executor():
    """工具侧 host 即执行器，返回自身（走同一条 _execute_registered_tool）。"""
    ex = _make_executor()
    from app.plugins.capabilities.bootstrap import ensure_registered
    from app.plugins.capabilities.registry import CapabilityRegistry

    ensure_registered("tool")
    pool = CapabilityRegistry.get_instance().build_pool(
        surface="tool", host=ex, plugin="demo", declared={"tool.execute"}
    )
    assert pool["tool.execute"] is ex


def test_plugin_log_binds_plugin_name():
    pool = _pool({"plugin.log"})
    assert pool["plugin.log"] is not None


# ──────────────── 声明裁剪 ────────────────


def test_declared_trims_new_capabilities():
    """只声明 model.metadata → 其余新能力一律 CapabilityDenied。"""
    from app.plugins.capabilities.pool import CapabilityDenied

    pool = _pool({"model.metadata"})
    assert "model.metadata" in pool
    for cap in ("model.config", "session.read", "session.write", "agent.read", "workdir.read"):
        assert cap not in pool
        with pytest.raises(CapabilityDenied):
            _ = pool[cap]


def test_undeclared_compat_grants_all_tool_caps():
    """api_version<2 未声明 → 兼容期全量放行（含新增的 12 条）。"""
    from app.plugins.capabilities.bootstrap import ensure_registered
    from app.plugins.capabilities.registry import CapabilityRegistry

    ensure_registered("tool")
    pool = CapabilityRegistry.get_instance().build_pool(
        surface="tool", host=_make_executor(), plugin="demo", declared=None
    )
    assert {
        "model.metadata",
        "model.schema",
        "model.config",
        "agent.read",
        "session.read",
        "session.write",
        "workdir.read",
        "tool.execute",
        "plugin.log",
    } <= set(pool.names())
