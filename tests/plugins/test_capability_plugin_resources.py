# -*- coding: utf-8 -*-
"""plugin.config / secret / net.http / task.background 四条能力。

背景：这四条以前插件只能直连宿主内部模块（PluginConfigStore / SecretStore /
httpx / threading），没有审计、卸载收不干净、各写一套。收口后各自的边界：
- plugin.config 只允许写 config_schema 已声明字段
- secret 的 account 强制带 plugin:<name>: 前缀，跨插件不可读
- net.http 只放行 http/https，响应体有上限
- task.background 的 on_done/on_error 经 post_to_main 回主线程
"""
import threading
from types import SimpleNamespace

import pytest

from app.plugins.capabilities.providers import ui_core

CTX = SimpleNamespace(plugin="demo")


# ── plugin.config ──────────────────────────────────────────

class _FakeStore:
    def __init__(self):
        self.values = {"mode": "fast"}
        self.writes = []

    def get(self, plugin, key):
        return self.values.get(key)

    def effective_values(self, plugin):
        return dict(self.values)

    def set_values(self, plugin, values):
        self.writes.append((plugin, dict(values)))
        self.values.update(values)
        return True


@pytest.fixture()
def store(monkeypatch):
    fake = _FakeStore()
    monkeypatch.setattr("app.plugins.managers.plugin_config_store.PluginConfigStore", lambda: fake)
    return fake


def test_plugin_config_read_single_and_all(store):
    read = ui_core._plugin_config_read(None, CTX)
    assert read("mode") == "fast"
    assert read("missing", "fallback") == "fallback"
    assert read() == {"mode": "fast"}


def test_plugin_config_write_rejects_undeclared(store):
    write = ui_core._plugin_config_write(None, CTX)
    assert write("mode", "slow") is True
    assert store.writes == [("demo", {"mode": "slow"})]
    assert write("ghost", 1) is False
    assert len(store.writes) == 1


def test_plugin_config_write_without_plugin_name(monkeypatch):
    write = ui_core._plugin_config_write(None, SimpleNamespace(plugin=""))
    assert write("mode", "x") is False


# ── secret ─────────────────────────────────────────────────

class _FakeSecret:
    def __init__(self):
        self.data = {}

    def get(self, account):
        return self.data.get(account, "")

    def set(self, account, value):
        self.data[account] = value
        return True

    def delete(self, account):
        self.data.pop(account, None)


@pytest.fixture()
def keyring(monkeypatch):
    fake = _FakeSecret()
    monkeypatch.setattr("app.utils.secret_store.SecretStore", lambda: fake)
    return fake


def test_secret_roundtrip_uses_plugin_prefixed_account(keyring):
    write = ui_core._secret_write(None, CTX)
    read = ui_core._secret_read(None, CTX)

    assert write("token", "abc") is True
    assert keyring.data == {"plugin:demo:token": "abc"}
    assert read("token") == "abc"


def test_secret_isolation_between_plugins(keyring):
    ui_core._secret_write(None, CTX)("token", "abc")
    other = ui_core._secret_read(None, SimpleNamespace(plugin="other"))

    assert other("token", "none") == "none"


def test_secret_empty_value_deletes(keyring):
    write = ui_core._secret_write(None, CTX)
    write("token", "abc")
    assert write("token", "") is True
    assert keyring.data == {}


# ── net.http ───────────────────────────────────────────────

class _Resp:
    status_code = 200
    headers = {"content-type": "text/plain"}
    encoding = "utf-8"

    def iter_bytes(self):
        yield b"hello"

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _Client:
    last = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def stream(self, method, url, **kwargs):
        _Client.last = {"method": method, "url": url, **kwargs}
        return _Resp()


def test_net_http_rejects_non_http_scheme():
    out = ui_core._net_http(None, CTX)("file:///etc/passwd")
    assert out == {"ok": False, "error": "仅支持 http/https"}


def test_net_http_success(monkeypatch):
    monkeypatch.setattr("httpx.Client", _Client)
    out = ui_core._net_http(None, CTX)("https://example.com/x", method="post", json_body={"a": 1})

    assert out["ok"] is True
    assert out["status"] == 200
    assert out["text"] == "hello"
    assert out["truncated"] is False
    assert _Client.last["method"] == "POST"
    assert _Client.last["json"] == {"a": 1}


def test_net_http_error_is_swallowed(monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("dns fail")

    monkeypatch.setattr("httpx.Client", boom)
    out = ui_core._net_http(None, CTX)("https://example.com")
    assert out["ok"] is False
    assert "dns fail" in out["error"]


# ── task.background ────────────────────────────────────────

@pytest.fixture()
def inline_post(monkeypatch):
    """post_to_main 在无 Qt 环境下不可用，测试里改为同步执行。"""
    calls = []

    def _post(fn):
        calls.append(fn)
        fn()
        return True

    monkeypatch.setattr("app.plugins.capabilities.providers.ui_core.post_to_main", _post)
    return calls


def test_task_background_runs_and_reports_progress(inline_post):
    done = []
    run = ui_core._task_background(None, CTX)

    def work(handle):
        handle.progress("50%")
        return 42

    task = run(work, name="demo-task", on_done=done.append)
    task._future.result(timeout=5)

    snap = task.snapshot()
    assert snap["status"] == "done"
    assert snap["progress"] == "50%"
    assert snap["name"] == "demo-task"
    assert done == [42]


def test_task_background_error_calls_on_error(inline_post):
    errors = []
    run = ui_core._task_background(None, CTX)

    def work(handle):
        raise ValueError("boom")

    task = run(work, on_error=errors.append)
    task._future.result(timeout=5)

    assert task.snapshot()["status"] == "error"
    assert "boom" in task.snapshot()["error"]
    assert len(errors) == 1


def test_task_background_cancel_marks_cancelled(inline_post):
    from concurrent.futures import CancelledError

    gate = threading.Event()
    run = ui_core._task_background(None, CTX)

    def work(handle):
        gate.wait(3)

    task = run(work)
    task.cancel()
    gate.set()
    try:
        task._future.result(timeout=5)
    except CancelledError:
        pass  # 任务还在队列里就被取消，future 直接落 cancelled

    assert task.snapshot()["status"] == "cancelled"


# ── tool.register / tool.unregister / plugin.deps ───────────

_SCHEMA = {"type": "function", "function": {"name": "demo_tool", "description": "d", "parameters": {"type": "object", "properties": {}}}}


@pytest.fixture()
def tool_registry():
    from app.tools.registry import ToolRegistry

    ToolRegistry.reset_instance()
    yield ToolRegistry.get_instance()
    ToolRegistry.reset_instance()


def test_tool_register_forces_plugin_source(tool_registry):
    register = ui_core._tool_register(None, CTX)

    assert register("demo_tool", _SCHEMA, impl=lambda: "ok", danger="safe") is True
    assert tool_registry.get("demo_tool").source == "plugin:demo"


def test_tool_register_requires_danger(tool_registry):
    register = ui_core._tool_register(None, CTX)

    # registry 强制插件工具显式声明 danger；能力层默认 safe 是显式传的，
    # 这里只验非法值被拒
    assert register("demo_tool", _SCHEMA, impl=lambda: "ok", danger="unknown") is False


def test_tool_unregister_only_own_tools(tool_registry):
    ui_core._tool_register(None, CTX)("demo_tool", _SCHEMA, impl=lambda: "ok")

    assert ui_core._tool_unregister(None, SimpleNamespace(plugin="other"))("demo_tool") is False
    assert ui_core._tool_unregister(None, CTX)("demo_tool") is True
    assert tool_registry.get("demo_tool") is None


def test_unregister_by_source_clears_all_owned(tool_registry):
    register = ui_core._tool_register(None, CTX)
    register("demo_tool", _SCHEMA, impl=lambda: "ok")
    register("demo_tool2", {**_SCHEMA, "function": {**_SCHEMA["function"], "name": "demo_tool2"}}, impl=lambda: "ok")

    assert tool_registry.unregister_by_source("plugin:demo") == 2
    assert tool_registry.get("demo_tool") is None


def test_plugin_deps_reports_missing_plugin(monkeypatch):
    class _PM:
        def get_plugin(self, name):
            return None

    monkeypatch.setattr(
        "app.plugins.managers.plugin_manager.PluginManager.get_instance",
        classmethod(lambda cls: _PM()),
    )
    out = ui_core._plugin_deps(None, CTX)()
    assert out == {"ok": False, "missing": [], "issues": ["插件未安装"]}


# ── 插件接入守卫 ────────────────────────────────────────────

def test_welcome_changelog_fetches_via_net_http():
    """欢迎页更新日志走能力池 net.http，不再自建 httpx client。"""
    from pathlib import Path

    src = (
        Path(__file__).resolve().parent.parent.parent
        / "plugins" / "welcome_changelog" / "ui" / "_fetcher.py"
    ).read_text(encoding="utf-8")

    assert '_caps("net.http")' in src
    assert "import httpx" not in src


def test_system_web_reads_config_via_capability():
    """搜索 Key 读取走 plugin.config.read，迁移逻辑除外（一次性历史搬移仍直连存储）。"""
    from pathlib import Path

    src = (
        Path(__file__).resolve().parent.parent.parent
        / "plugins" / "system-web" / "tools" / "web_tools.py"
    ).read_text(encoding="utf-8")

    assert 'caps_for("plugin.config.read")' in src
