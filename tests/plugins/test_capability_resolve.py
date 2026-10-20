# -*- coding: utf-8 -*-
"""caps_for / pool_for —— 无 ctx 场景取能力的统一入口。

覆盖三件事：
1. 授权仍按 manifest 声明裁剪（不因为走 caps_for 就拿到声明外的能力）
2. host 解析：插件绑定窗口 > 活跃窗口（后台线程不再恒 host=None）
3. surface 推断：plugins/<name>/tools/ 与 hooks/ 下的调用走 tool surface
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def reg(monkeypatch):
    """干净的能力注册表 + 已注册 provider 的 bootstrap 就绪态。

    不手工 register provider：caps_for 内部会调 ensure_registered，若 bootstrap
    _READY 为 False 它会把真实 provider 注册进来（与生产一致），这里只保证
    从干净状态开始、结束后还原。
    """
    from app.plugins.capabilities import bootstrap, resolve
    from app.plugins.capabilities.registry import CapabilityRegistry

    resolve.reset_hosts()
    CapabilityRegistry.reset_instance()
    bootstrap.reset_bootstrap()
    yield CapabilityRegistry.get_instance()
    resolve.reset_hosts()
    CapabilityRegistry.reset_instance()
    bootstrap.reset_bootstrap()


def _fake_pm(monkeypatch, caps, api_version=2):
    """让 PluginManager.get_plugin 返回指定 manifest。"""
    from app.plugins.managers import plugin_manager

    info = SimpleNamespace(
        manifest={"capabilities": caps, "api_version": api_version},
        path=str(_ROOT / "plugins" / "demo"),
    )
    monkeypatch.setattr(plugin_manager.PluginManager, "get_plugin", lambda self, name: info, raising=False)


def _fake_window(workdir="", session_id=""):
    """带 window.current / session.read 所需字段的替身窗口。

    真实 provider 只 getattr 若干字段，故 SimpleNamespace 足够。
    """
    return SimpleNamespace(
        _window_id="win-1",
        _current_project="demo",
        _current_workdir={"demo": workdir},
        _current_session_id=session_id,
        backend=SimpleNamespace(get_current_session=lambda: None),
        session_manager=None,
    )


@pytest.fixture()
def fake_tm(monkeypatch):
    """让 window.current provider 的 TabManagerWindow 单例解析返回假窗口。"""
    from app.plugins.capabilities.providers import ui_core

    holder = {}

    def _install(win):
        holder["win"] = win
        monkeypatch.setattr(ui_core, "_resolve_tab_window", lambda: win, raising=False)

    yield _install
    holder.clear()


def test_declared_capability_resolves_with_bound_host(reg, fake_tm, monkeypatch):
    from app.plugins.capabilities import resolve

    _fake_pm(monkeypatch, ["window.current"])
    tm = SimpleNamespace(get_current_window=lambda: _fake_window(workdir="/tmp/proj"))
    fake_tm(tm)
    resolve.note_host("demo", _fake_window(workdir="/tmp/proj"))

    snap = resolve.caps_for("window.current", plugin="demo")()
    assert snap is not None
    assert snap["workdir"] == "/tmp/proj"


def test_undeclared_capability_returns_none(reg, monkeypatch):
    """声明裁剪依然生效：没声明就拿不到（不是抛异常，是 None）。"""
    from app.plugins.capabilities import resolve

    _fake_pm(monkeypatch, ["window.current"])
    resolve.note_host("demo", _fake_window(session_id="s-1"))

    assert resolve.caps_for("session.read", plugin="demo") is None


def test_host_binding_used_for_host_bound_capability(reg, fake_tm, monkeypatch):
    """input.read 这类吃 host 的能力：必须拿到 note_host 登记的窗口，而非活跃窗口。

    这是 caps_for 相对旧样板（host=None）的实质修正：后台线程终于能摸到窗口。
    """
    from app.plugins.capabilities import resolve

    _fake_pm(monkeypatch, ["input.read"])
    bound = _fake_window()
    bound.input_area = SimpleNamespace(toPlainText=lambda: "来自绑定窗口")
    resolve.note_host("demo", bound)

    read = resolve.caps_for("input.read", plugin="demo")
    assert read is not None
    assert read()["text"] == "来自绑定窗口"


def test_window_current_prefers_host_over_active(reg, fake_tm, monkeypatch):
    """window.current 必须回答 host 窗口快照；tm 选中窗口仅作无 host 时的兜底。

    回归：浮动卡开在窗口 A，用户切到标签 B——记忆/工作台页面曾读到 B 窗口
    的项目（旧 provider 无视 host，恒取 tm.get_current_window）。
    """
    from app.plugins.capabilities import resolve

    _fake_pm(monkeypatch, ["window.current"])
    win_a = _fake_window(workdir="/proj-a", session_id="s-a")
    win_b = _fake_window(workdir="/proj-b", session_id="s-b")
    fake_tm(SimpleNamespace(get_current_window=lambda: win_b))
    resolve.note_host("demo", win_a)

    snap = resolve.caps_for("window.current", plugin="demo")()
    assert snap is not None
    assert snap["workdir"] == "/proj-a"
    assert snap["session_id"] == "s-a"


def test_window_current_ignores_non_window_host(reg, fake_tm, monkeypatch):
    """host 是带 _window_id 但无 backend 的容器假窗口（如 __global__ 容器）时，
    必须视为无效回退 tm 当前选中——只看 _window_id 会拿到空快照串错窗口。
    """
    from app.plugins.capabilities import resolve

    _fake_pm(monkeypatch, ["window.current"])
    fake_container = SimpleNamespace(_window_id="__global__")  # 无 backend
    real_win = _fake_window(workdir="/current-tab")
    fake_tm(SimpleNamespace(get_current_window=lambda: real_win))

    pool = resolve.pool_for(surface="ui", host=fake_container, plugin="demo")
    snap = pool["window.current"]()
    assert snap is not None
    assert snap["workdir"] == "/current-tab"


def test_pool_for_explicit_host_wins(reg, fake_tm, monkeypatch):
    """pool_for 显式 host 优先于全局解析链：卡片实例归属窗口语义。

    多窗口各一份卡片实例时，卡片必须按实例归属取 window.current，
    而非 note_host 最后登记窗口（全局单值与多实例归属天然错配）。
    """
    from app.plugins.capabilities import resolve

    _fake_pm(monkeypatch, ["window.current"])
    win_a = _fake_window(workdir="/proj-a", session_id="s-a")
    fake_tm(SimpleNamespace(get_current_window=lambda: _fake_window(workdir="/proj-b")))

    pool = resolve.pool_for(host=win_a)
    assert pool is not None and "window.current" in pool
    snap = pool["window.current"]()
    assert snap["workdir"] == "/proj-a"
    assert snap["session_id"] == "s-a"


def test_host_falls_back_to_active_window(reg, fake_tm, monkeypatch):
    """无绑定时退到活跃窗口 —— 原来 host=None 时这里必然拿到空。"""
    from app.plugins.capabilities import resolve
    from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

    _fake_pm(monkeypatch, ["window.current"])
    win = _fake_window(workdir="/active")
    fake_tm(SimpleNamespace(get_current_window=lambda: win))
    real = UIPluginRegistry.get_instance()
    prev = getattr(real, "_main_widget", None)
    real.set_main_widget(win)
    try:
        snap = resolve.caps_for("window.current", plugin="demo")()
    finally:
        real.set_main_widget(prev)
    assert snap is not None and snap["workdir"] == "/active"


def test_ungated_capability_always_available(reg, monkeypatch):
    from app.plugins.capabilities import resolve

    _fake_pm(monkeypatch, [])
    kv = resolve.caps_for("state.kv", plugin="demo")
    assert kv is not None and "get" in kv


def test_surface_inferred_from_tools_dir(reg, monkeypatch, tmp_path):
    """plugins/<name>/tools/ 下的调用必须走 tool surface。"""
    import importlib.util

    _fake_pm(monkeypatch, ["tool.read"])
    tools_dir = tmp_path / "plugins" / "demo" / "tools"
    tools_dir.mkdir(parents=True)
    probe = tools_dir / "_caps_probe.py"
    probe.write_text(
        "def probe(plugin='demo'):\n"
        "    from app.plugins.sdk import caps_for\n"
        "    api = caps_for('tool.read', plugin=plugin)\n"
        "    return api() if api is not None else None\n",
        encoding="utf-8",
    )
    spec = importlib.util.spec_from_file_location("demo_caps_surface_probe", probe)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    api = mod.probe(plugin="demo")
    assert api is not None and "names" in api


def test_surface_inferred_from_hooks_dir(reg, monkeypatch, tmp_path):
    """hooks/ 下的调用推断为 tool surface：tool-only 能力在 hook 内可用。"""
    import importlib.util

    _fake_pm(monkeypatch, ["tool.read"])
    hooks_dir = tmp_path / "plugins" / "demo" / "hooks"
    hooks_dir.mkdir(parents=True)
    probe = hooks_dir / "_caps_probe.py"
    probe.write_text(
        "def probe(plugin='demo'):\n"
        "    from app.plugins.sdk import caps_for\n"
        "    api = caps_for('tool.read', plugin=plugin)\n"
        "    return api() if api is not None else None\n",
        encoding="utf-8",
    )
    spec = importlib.util.spec_from_file_location("demo_caps_hooks_probe", probe)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    api = mod.probe(plugin="demo")
    assert api is not None and "names" in api


def test_hooks_dir_ui_only_capability_denied(reg, monkeypatch, tmp_path):
    """hooks/ 推断为 tool surface 后，ui-only 能力拿不到（语义边界固化）。"""
    import importlib.util

    _fake_pm(monkeypatch, ["ui.notify", "tool.read"])
    hooks_dir = tmp_path / "plugins" / "demo" / "hooks"
    hooks_dir.mkdir(parents=True)
    probe = hooks_dir / "_caps_probe.py"
    probe.write_text(
        "def probe(plugin='demo'):\n"
        "    from app.plugins.sdk import caps_for\n"
        "    return caps_for('ui.notify', plugin=plugin)\n",
        encoding="utf-8",
    )
    spec = importlib.util.spec_from_file_location("demo_caps_hooks_ui_probe", probe)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.probe(plugin="demo") is None


def test_pool_for_returns_view(reg, fake_tm, monkeypatch):
    from app.plugins.capabilities import resolve

    _fake_pm(monkeypatch, ["window.current", "session.read"])
    win = _fake_window(workdir="/w")
    fake_tm(SimpleNamespace(get_current_window=lambda: win))
    resolve.note_host("demo", win)

    pool = resolve.pool_for(plugin="demo")
    assert pool is not None
    assert "window.current" in pool
    assert "session.read" in pool
    assert pool["window.current"]()["workdir"] == "/w"


def test_forget_host_clears_binding(reg, monkeypatch):
    from app.plugins.capabilities import resolve

    _fake_pm(monkeypatch, ["window.current"])
    win = _fake_window(workdir="/w")
    resolve.note_host("demo", win)
    resolve.forget_host("demo", win)
    assert resolve._plugin_hosts.get("demo") is None


def test_caller_plugin_inferred_from_plugins_dir(reg, monkeypatch, tmp_path):
    """caps_for 不传 plugin 时按调用方文件路径推断（plugins/<name>/...）。

    marketplace 的 config.read 调用点就依赖这个：它在 ui/ 子目录下，同样能
    推断出 plugin-marketplace。
    """
    import importlib.util

    from app.plugins.capabilities import resolve

    _fake_pm(monkeypatch, ["config.read"])
    nested = tmp_path / "plugins" / "demo-market" / "ui"
    nested.mkdir(parents=True)
    probe = nested / "_probe.py"
    probe.write_text(
        "def probe():\n    from app.plugins.sdk import caps_for\n    return caps_for('config.read')\n",
        encoding="utf-8",
    )
    spec = importlib.util.spec_from_file_location("demo_market_probe", probe)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    read = mod.probe()
    assert read is not None, "未推断出插件名导致声明解析失败"
    # 非敏感标量可读
    assert read("marketplace_allowed_git_hosts", default=[]) == []
    # 密钥类字段一律拒绝
    assert read("api_key", default="X") == "X"
    assert resolve.declared_for(plugin="demo-market") is not None


def test_declared_for_reports_manifest(reg, monkeypatch):
    from app.plugins.capabilities import resolve

    _fake_pm(monkeypatch, ["window.current"])
    declared = resolve.declared_for(plugin="demo")
    assert declared is not None and "window.current" in declared
