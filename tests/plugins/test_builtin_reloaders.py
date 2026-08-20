# -*- coding: utf-8 -*-
"""内置 reloader 注册测试 — monkeypatch 各子系统，断言分派正确、不真实加载"""

import pytest

from app.plugins import builtin_reloaders
from app.plugins.kernel import ComponentReloaderRegistry, ReloadContext


@pytest.fixture(autouse=True)
def _reset_builtin_registered():
    """重置内置 reloader 注册守卫（避免 id 复用导致 register_builtin_reloaders 早退）

    builtin_reloaders._BUILTIN_REGISTERED 用 id(reg) 判定是否已注册。
    测试间若 reg 被 GC、id 被新 reg 复用，后续测试的 register_builtin_reloaders
    会误判为已注册而早退，导致新 reg 实际为空集 → reg.reload() 全部 False 跳过。
    """
    builtin_reloaders._BUILTIN_REGISTERED.clear()
    yield
    builtin_reloaders._BUILTIN_REGISTERED.clear()


def _make_reg() -> ComponentReloaderRegistry:
    reg = ComponentReloaderRegistry()
    builtin_reloaders.register_builtin_reloaders(reg)
    return reg


def test_all_components_registered():
    reg = _make_reg()
    assert reg.known_components() == builtin_reloaders.RELOADED_COMPONENTS


def test_themes_reloader_calls_theme_manager(monkeypatch):
    called = {}

    class _FakeThemeManager:
        def reload(self):
            called["reload"] = True

    import app.utils.theme_manager as tm_mod

    monkeypatch.setattr(tm_mod, "theme_manager", _FakeThemeManager())
    monkeypatch.setattr("app.utils.config.update_theme_options", lambda: called.update(options=True))

    reg = _make_reg()
    ok = reg.reload(ReloadContext("p", plugin=None, component="themes", is_new_plugin=False))
    assert ok is True and called == {"reload": True, "options": True}


def test_skills_reloader_invalidates_cache(monkeypatch):
    called = []
    import app.plugins.builtin_reloaders as br

    monkeypatch.setattr(br, "invalidate_skills_cache", lambda: called.append(1))

    reg = _make_reg()
    ok = reg.reload(ReloadContext("p", plugin=None, component="skills", is_new_plugin=False))
    assert ok is True and called == [1]


def test_idempotent_register():
    reg = ComponentReloaderRegistry()
    builtin_reloaders.register_builtin_reloaders(reg)
    builtin_reloaders.register_builtin_reloaders(reg)  # 二次注册不抛
    assert reg.known_components() == builtin_reloaders.RELOADED_COMPONENTS


# ════════════════════════════════════════════════════════════════════════
# 删除路径（ctx.plugin is None）单测 — 验证副作用调用正确
# ════════════════════════════════════════════════════════════════════════


class _FakeAgentManager:
    """最小化 AgentManager fake：仅捕获 hooks/agents 清理调用"""

    def __init__(self):
        self.cleanup_calls: list[str] = []
        self.unregister_calls: list[str] = []
        self.reload_hooks_calls: list[str] = []
        self.reload_agents_calls: list[str] = []
        self._hook_manager = self  # 内嵌 hook_manager 简化

    def cleanup_plugin_artifacts(self, plugin_name: str):
        self.cleanup_calls.append(plugin_name)

    def reload_plugin_agents(self, plugin_name: str) -> int:
        self.reload_agents_calls.append(plugin_name)
        return 1

    def reload_plugin_hooks(self, plugin_name: str) -> bool:
        self.reload_hooks_calls.append(plugin_name)
        return True

    def unregister_skill_hooks(self, plugin_name: str):
        self.unregister_calls.append(plugin_name)


def _bind_runtime(monkeypatch, agent_manager: _FakeAgentManager):
    """把 fake AgentManager 注入 builtin_reloaders._RUNTIME"""
    builtin_reloaders.bind_runtime(agent_manager)
    monkeypatch.setattr(builtin_reloaders, "_RUNTIME", builtin_reloaders._RUNTIME)


def test_delete_agents_path_calls_cleanup_not_reload_agent_commands(monkeypatch):
    """agents 删除分支：仅清理 artifacts，不触发 reload_agent_commands。

    旧 backend elif 语义：agents 命中后不走 commands 分支。
    现 reloader 删除分支内部又调 reload_agent_commands → 与 commands reloader 双触发。
    修复后：删除分支不调 reload_agent_commands（命令清理由 commands reloader 统一）。
    """
    agent_calls = []
    # builtin_reloaders 模块顶部已 import reload_agent_commands — 直接 patch 模块属性
    monkeypatch.setattr(
        builtin_reloaders,
        "reload_agent_commands",
        lambda: agent_calls.append(1),
    )
    am = _FakeAgentManager()
    _bind_runtime(monkeypatch, am)

    reg = _make_reg()
    result = reg.reload(ReloadContext("p1", plugin=None, component="agents", is_new_plugin=False))

    # 副作用：仅 cleanup_plugin_artifacts 被调用
    assert am.cleanup_calls == ["p1"]
    assert am.reload_agents_calls == []  # 删除分支不调 reload_plugin_agents
    # 关键断言：删除分支不调 reload_agent_commands
    assert agent_calls == [], f"agents 删除分支不应触发 reload_agent_commands，实际调用 {len(agent_calls)} 次"
    # 返回值：agents 删除归零
    assert result == 0


def test_delete_hooks_only_path_unregisters_skill_hooks(monkeypatch):
    """hooks-only 删除分支：直接 unregister_skill_hooks（不走 cleanup_plugin_artifacts）。"""
    am = _FakeAgentManager()
    _bind_runtime(monkeypatch, am)

    reg = _make_reg()
    ok = reg.reload(ReloadContext("p2", plugin=None, component="hooks", is_new_plugin=False))

    # hooks-only 路径：不调 cleanup（避免与 agents 重叠），仅 unregister
    assert am.unregister_calls == ["p2"]
    assert am.cleanup_calls == []
    assert ok is True


def test_delete_ui_path_unloads_plugin(monkeypatch):
    """ui 删除分支：UIPluginRegistry.unload_plugin(plugin_name)。"""
    unloaded: list[str] = []

    class _FakeUIRegistry:
        _instance = None

        @classmethod
        def get_instance(cls):
            return cls._instance or cls()

        def unload_plugin(self, plugin_name: str):
            unloaded.append(plugin_name)
            return True

    fake = _FakeUIRegistry()
    _FakeUIRegistry._instance = fake
    monkeypatch.setattr(
        "app.plugins.registries.ui_plugin_registry.UIPluginRegistry",
        _FakeUIRegistry,
    )

    reg = _make_reg()
    ok = reg.reload(ReloadContext("p3", plugin=None, component="ui", is_new_plugin=False))

    assert unloaded == ["p3"]
    assert ok is True


def test_delete_lsp_path_removes_plugin_servers(monkeypatch):
    """lsp 删除分支：get_lsp_manager().remove_plugin_servers(plugin_name)，返回计数。"""
    removed: list[str] = []

    class _FakeLSPManager:
        def remove_plugin_servers(self, plugin_name: str) -> int:
            removed.append(plugin_name)
            return 2  # 假设该插件有 2 个 LSP 服务器

    import app.core.lsp.lsp_manager as lsp_mod

    monkeypatch.setattr(lsp_mod, "get_lsp_manager", lambda: _FakeLSPManager())

    reg = _make_reg()
    ok = reg.reload(ReloadContext("p4", plugin=None, component="lsp", is_new_plugin=False))

    assert removed == ["p4"]
    assert ok is True  # removed > 0


def test_delete_commands_path_reloads_all_commands(monkeypatch):
    """commands 删除分支：调 reload_all_commands（与 reload 路径一致）。"""
    all_calls: list[int] = []

    # builtin_reloaders 在模块顶部已 import reload_all_commands — 直接 patch 模块属性
    monkeypatch.setattr(
        builtin_reloaders,
        "reload_all_commands",
        lambda: all_calls.append(1),
    )

    reg = _make_reg()
    ok = reg.reload(ReloadContext("p5", plugin=None, component="commands", is_new_plugin=False))

    assert all_calls == [1]
    assert ok is True


# ─────────────────────────────────────────────────────────────────────────────
# gateways 分支：卸载/热更新路径（先关闭 gateway → 清 module → unregister/重建）
# ─────────────────────────────────────────────────────────────────────────────


class _FakeGatewayManager:
    """最小化 PlatformManager fake：捕获 stop / rebuild 调用"""

    def __init__(self):
        self.stopped: list[str] = []
        self.rebuilt: list[tuple] = []

    def stop_plugin_platforms(self, plugin_name: str) -> None:
        self.stopped.append(plugin_name)

    def rebuild_plugin_platforms(self, plugin_name: str, restart_if_running: bool = True) -> None:
        self.rebuilt.append((plugin_name, restart_if_running))


class _FakeGatewayWatcher:
    def __init__(self):
        self.scans: int = 0
        self.unloaded: list[str] = []
        self.reloaded: list[str] = []

    def scan_now(self) -> None:
        self.scans += 1

    def unload_plugin(self, plugin_name: str) -> None:
        self.unloaded.append(plugin_name)

    def reload_plugin(self, plugin_name: str) -> None:
        self.reloaded.append(plugin_name)


def test_delete_gateways_path_stops_and_unregisters(monkeypatch):
    """gateways 删除分支：先 stop 该插件平台 + 清 module + unload_plugin(精准注销)；
    不触发 rebuild（删除路径用旧 def 无法重建）。"""
    mgr = _FakeGatewayManager()
    watcher = _FakeGatewayWatcher()
    monkeypatch.setattr("app.gateway.manager.get_platform_manager", lambda: mgr)
    monkeypatch.setattr(
        "app.plugins.loaders.runtime_component_loader.ensure_gateway_watcher",
        lambda: watcher,
    )
    # 注入待清理的 module 引用，验证 _purge_gateway_plugin_modules 生效
    import sys

    sys.modules["drifox_rt_gateways_gwplug_wecom"] = object()

    reg = _make_reg()
    ok = reg.reload(ReloadContext("gwplug", plugin=None, component="gateways", is_new_plugin=False))

    assert ok is True
    assert mgr.stopped == ["gwplug"]  # 先关闭 gateway
    assert watcher.unloaded == ["gwplug"], "删除路径应精准 unload_plugin"
    assert watcher.scans == 0, "删除路径不应全量 scan_now"
    assert mgr.rebuilt == []  # 删除路径不重建
    assert "drifox_rt_gateways_gwplug_wecom" not in sys.modules  # module 已清


def test_update_gateways_path_stops_and_rebuilds(monkeypatch):
    """gateways 更新分支：先 stop + 清 module + reload_plugin(精准重载) + 用新 def 重建 adapter。"""
    mgr = _FakeGatewayManager()
    watcher = _FakeGatewayWatcher()
    monkeypatch.setattr("app.gateway.manager.get_platform_manager", lambda: mgr)
    monkeypatch.setattr(
        "app.plugins.loaders.runtime_component_loader.ensure_gateway_watcher",
        lambda: watcher,
    )

    class _FakePlugin:
        def has_component(self, c):
            return False

    reg = _make_reg()
    ok = reg.reload(ReloadContext("gwplug", plugin=_FakePlugin(), component="gateways", is_new_plugin=False))

    assert ok is True
    assert mgr.stopped == ["gwplug"]  # 先关闭
    assert watcher.reloaded == ["gwplug"], "更新路径应精准 reload_plugin"
    assert watcher.scans == 0, "更新路径不应全量 scan_now"
    assert mgr.rebuilt == [("gwplug", True)]  # 更新路径重建（restart_if_running=True）


# ─────────────────────────────────────────────────────────────────────────────
# tools 分支：删除路径精准卸载（unload_plugin），更新路径精准重载（reload_plugin）
# ─────────────────────────────────────────────────────────────────────────────


class _FakeToolWatcher:
    """最小化 PluginToolWatcher fake：捕获 unload_plugin / reload_plugin / scan_now / _notify_reloaded"""

    def __init__(self):
        self.unloaded: list[str] = []
        self.reloaded: list[str] = []
        self.scans: int = 0
        self.notified: int = 0

    def unload_plugin(self, plugin_name: str) -> None:
        self.unloaded.append(plugin_name)

    def reload_plugin(self, plugin_name: str) -> None:
        self.reloaded.append(plugin_name)

    def scan_now(self) -> None:
        self.scans += 1

    def _notify_reloaded(self) -> None:
        self.notified += 1


def test_delete_tools_path_calls_unload_not_scan_now(monkeypatch):
    """tools 删除分支：精准 unload_plugin，不调 scan_now

    回归锁定：此前 _reload_tools 对删除路径也调 scan_now 全量重扫，
    导致删一个插件却卸载并重载全部工具（含 system）。
    """
    watcher = _FakeToolWatcher()
    monkeypatch.setattr(
        "app.plugins.loaders.plugin_tool_loader.ensure_plugin_tool_watcher",
        lambda: watcher,
    )

    reg = _make_reg()
    ok = reg.reload(ReloadContext("workbuddy", plugin=None, component="tools", is_new_plugin=False))

    assert ok is True
    assert watcher.unloaded == ["workbuddy"], "删除路径应调 unload_plugin(plugin_name)"
    assert watcher.scans == 0, "删除路径不应调 scan_now（避免全量重载）"
    assert watcher.reloaded == [], "删除路径不应调 reload_plugin"
    assert watcher.notified == 0, "删除路径（非轮询）不应通知监听"


def test_update_tools_path_calls_reload_not_scan_now(monkeypatch):
    """tools 更新/新增分支：精准 reload_plugin + _notify_reloaded，不调 scan_now

    回归锁定：此前更新路径也调 scan_now 全量重扫（装/改一个插件同样
    卸载并重载全部工具），现改为只重载目标插件。
    """
    watcher = _FakeToolWatcher()
    monkeypatch.setattr(
        "app.plugins.loaders.plugin_tool_loader.ensure_plugin_tool_watcher",
        lambda: watcher,
    )

    class _FakePlugin:
        def has_component(self, c):
            return False

    reg = _make_reg()
    ok = reg.reload(ReloadContext("workbuddy", plugin=_FakePlugin(), component="tools", is_new_plugin=False))

    assert ok is True
    assert watcher.reloaded == ["workbuddy"], "更新路径应调 reload_plugin(plugin_name)"
    assert watcher.scans == 0, "更新路径不应调 scan_now（避免全量重载）"
    assert watcher.notified == 1, "更新路径应通知监听"
    assert watcher.unloaded == [], "更新路径不应调 unload_plugin"
