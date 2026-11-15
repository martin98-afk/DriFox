# -*- coding: utf-8 -*-
"""S7 五接线点护栏：热重载 diff / 全量重载 / 启停委托 / 卸载回收 / 容错契约。

全部用 spy/monkeypatch 断言调用与状态，不真重载插件盘面（_reload_single_plugin
等重载步骤一律替换为 no-op）。接线点（行号即当前工作树）：
- plugin_host_service.reload_plugin_subsystems：diff 点逐插件 invalidate（2699-2711）
- plugin_host_service._reload_all_subsystems：全量 invalidate("")+reset_bootstrap+
  forget_host（2843-2861）
- plugin_manager.enable_plugin/disable_plugin → 委托 invalidate_capability_declared
  （1335 / 1391 / 2059）
- plugin_host_service._release_plugin_host_resources：现状仅 release_plugin 无
  invalidate，删除场景由 diff removed 分支覆盖（M4-F1 防回归钉 disconnect 行为）。
"""
from types import SimpleNamespace

import pytest


# ── 测试替身 ──────────────────────────────────────────────────────────────

class _FakePM:
    """PluginManager 最小替身：只供 reload 链路读取 diff 与插件列表。"""

    def __init__(self, diff=None, plugins=None):
        self._diff = diff or {"changed": [], "added": [], "removed": []}
        self._plugins = list(plugins or [])

    def is_initialized(self):
        return True

    def rescan(self, force=False):
        return dict(self._diff)

    def list_plugins(self):
        return list(self._plugins)

    def get_lsp_configs(self):
        return []


def _bare_host_service():
    """绕过 __init__ 取 PluginHostService 裸实例（只设 reload 链需要的属性）。"""
    from app.core.services.plugin_host_service import PluginHostService

    svc = PluginHostService.__new__(PluginHostService)
    svc._agent_manager = None
    return svc


def _patch_reload_side_effects(monkeypatch):
    """把 reload 链里全部真重载/真系统副作用替换为 no-op（不碰盘面）。"""
    from app.core.services.plugin_host_service import PluginHostService

    monkeypatch.setattr(PluginHostService, "_reload_single_plugin", lambda self, *a, **k: {})
    monkeypatch.setattr(PluginHostService, "_detect_unloaded_components", lambda self, pm: {})

    import app.plugins.builtin_reloaders as br_mod

    monkeypatch.setattr(br_mod, "bind_runtime", lambda am: None)
    monkeypatch.setattr(br_mod, "register_builtin_reloaders", lambda registry: None)


def _patch_capability_registry(monkeypatch) -> list:
    """把 CapabilityRegistry 单例换成假对象，返回 invalidate_declared 调用记录。"""
    from app.plugins.capabilities.registry import CapabilityRegistry

    seen: list = []
    fake = SimpleNamespace(invalidate_declared=lambda name: seen.append(name))
    monkeypatch.setattr(CapabilityRegistry, "get_instance", classmethod(lambda cls: fake))
    return seen


def _patch_subsystem_reloads(monkeypatch):
    """替换 _reload_all_subsystems 里 commands/themes/LSP/MCP 的真实副作用。"""
    import app.core.commands.builtin_commands as bc_mod
    import app.utils.theme_manager as tm_mod
    import app.utils.config as cfg_mod
    import app.core.lsp.lsp_manager as lsp_mod
    import app.tools.mcp_tools as mcp_mod

    monkeypatch.setattr(bc_mod, "reload_all_commands", lambda: None)
    monkeypatch.setattr(tm_mod.theme_manager, "reload", lambda: None)
    monkeypatch.setattr(cfg_mod, "update_theme_options", lambda: None)

    class _FakeLsp:
        def __init__(self):
            self._clients = {}

        def initialize(self, *a, **k):
            pass

        def start_all_background(self):
            pass

    monkeypatch.setattr(lsp_mod, "get_lsp_manager", lambda: _FakeLsp())
    monkeypatch.setattr(mcp_mod.MCPClientManager, "get_instance", classmethod(lambda cls: None))


# ── M4-D 系：接线存在性 ──────────────────────────────────────────────────

def test_reload_diff_invalidates_declared_per_plugin(monkeypatch):
    """M4-D1：diff 点逐插件精细失效——changed 只含 A 时恰收到 "A"，不波及 B。"""
    from app.core.services.plugin_host_service import PluginHostService
    from app.plugins.managers.plugin_manager import PluginManager

    a = SimpleNamespace(name="A", components={}, manifest={})
    b = SimpleNamespace(name="B", components={}, manifest={})
    monkeypatch.setattr(
        PluginManager, "get_instance",
        classmethod(lambda cls: _FakePM({"changed": [a], "added": [], "removed": []}, [a, b])),
    )
    _patch_reload_side_effects(monkeypatch)
    seen = _patch_capability_registry(monkeypatch)

    result = _bare_host_service().reload_plugin_subsystems()

    assert seen == ["A"]  # 恰好 A，一次；B 在索引但不在 diff，不失效
    assert isinstance(result, dict)


def test_force_full_reload_resets_bootstrap_forgets_hosts_clears_all(monkeypatch):
    """M4-D2：全量重载 = invalidate("") + reset_bootstrap + 逐插件 forget_host。"""
    import app.plugins.capabilities.bootstrap as bootstrap_mod
    import app.plugins.capabilities.resolve as resolve_mod
    from app.core.services.plugin_host_service import PluginHostService
    from app.plugins.managers.plugin_manager import PluginManager

    a = SimpleNamespace(name="A", components={}, manifest={})
    b = SimpleNamespace(name="B", components={}, manifest={})
    monkeypatch.setattr(
        PluginManager, "get_instance",
        classmethod(lambda cls: _FakePM({"changed": [], "added": [], "removed": []}, [a, b])),
    )
    seen = _patch_capability_registry(monkeypatch)
    _patch_subsystem_reloads(monkeypatch)

    rb_calls: list = []
    real_reset = bootstrap_mod.reset_bootstrap

    def _spy_reset():
        rb_calls.append(1)
        real_reset()

    monkeypatch.setattr(bootstrap_mod, "reset_bootstrap", _spy_reset)
    fh_calls: list = []
    monkeypatch.setattr(resolve_mod, "forget_host", lambda plugin, host=None: fh_calls.append(plugin))

    # 预污染 bootstrap 就绪标记，验证 reset 真把它们拉回 False
    bootstrap_mod._READY["ui"] = True
    bootstrap_mod._READY["tool"] = True

    # result 须按真实调用方口径预填充（KNOWN_COMPONENTS 形状），实码末尾日志直取键
    import app.plugins.kernel as kernel_mod

    result = {k: (0 if k == "agents" else False) for k in kernel_mod.KNOWN_COMPONENTS}
    _bare_host_service()._reload_all_subsystems(_FakePM(plugins=[a, b]), result, tuple(kernel_mod.KNOWN_COMPONENTS))

    assert seen == [""]  # 全量语义：空插件名
    assert rb_calls == [1]
    assert bootstrap_mod._READY["ui"] is False
    assert bootstrap_mod._READY["tool"] is False
    assert sorted(fh_calls) == ["A", "B"]
    assert isinstance(result, dict)


def test_enable_disable_routes_through_pm_delegate(monkeypatch):
    """M4-D3：启停经 pm 委托 invalidate_capability_declared → registry.invalidate_declared。"""
    from app.plugins.capabilities.process_registry import ProcessRegistry
    from app.plugins.capabilities.registry import CapabilityRegistry
    from app.plugins.managers.plugin_manager import PluginManager

    # 真类裸实例 + 内存假配置态，避免构造真实 PluginManager
    pm = PluginManager.__new__(PluginManager)
    pm._plugins = {"demo": SimpleNamespace(name="demo")}
    enabled: set = set()
    disabled: set = set()

    monkeypatch.setattr(pm, "_get_enabled_set", lambda: enabled)

    def _save_enabled(s):
        snapshot = set(s)  # s 与外层 enabled 同对象，先快照再写回，防自引用清空
        enabled.clear()
        enabled.update(snapshot)

    monkeypatch.setattr(pm, "_save_enabled_set", _save_enabled)
    monkeypatch.setattr(pm, "_get_disabled_set", lambda: disabled)

    def _save_disabled(s):
        snapshot = set(s)
        disabled.clear()
        disabled.update(snapshot)

    monkeypatch.setattr(pm, "_save_disabled_set", _save_disabled)
    monkeypatch.setattr(pm, "invalidate_mcp_cache", lambda: None)
    monkeypatch.setattr(
        ProcessRegistry, "get_instance",
        classmethod(lambda cls: SimpleNamespace(kill_plugin=lambda name: 0)),
    )

    # 第一层 spy：pm 委托方法（包真实现）；第二层：registry 收到的 name
    delegate_seen: list = []
    real_delegate = PluginManager.invalidate_capability_declared

    def _spy_delegate(name):
        delegate_seen.append(name)
        real_delegate(pm, name)

    monkeypatch.setattr(pm, "invalidate_capability_declared", _spy_delegate)
    registry_seen = _patch_capability_registry(monkeypatch)

    pm.enable_plugin("demo", load_components=False)
    pm.disable_plugin("demo", load_components=False)

    assert delegate_seen == ["demo", "demo"]  # 启/停各恰一次
    assert registry_seen == ["demo", "demo"]  # 参数原样透传到 registry


def test_manifest_widening_lands_after_rescan_diff(monkeypatch, tmp_path):
    """M4-D4（端到端）：窄声明 → rescan diff 失效 → 宽授权落地（S1+S7 全链）。"""
    from app.core.services.plugin_host_service import PluginHostService
    from app.plugins.capabilities.registry import CapabilityRegistry
    from app.plugins.managers.plugin_manager import PluginManager

    infos = {"A": SimpleNamespace(manifest={"capabilities": ["session.read"]}, path=str(tmp_path))}
    plugin_a = SimpleNamespace(name="A", components={}, manifest={})
    monkeypatch.setattr(
        PluginManager, "get_instance",
        classmethod(lambda cls: SimpleNamespace(get_plugin=lambda n: infos.get(n))),
    )
    _patch_reload_side_effects(monkeypatch)

    reg = CapabilityRegistry()
    try:
        # reload 链必须与声明解析/build_pool 用同一张表，才能真失效
        monkeypatch.setattr(CapabilityRegistry, "get_instance", classmethod(lambda cls: reg))
        monkeypatch.setattr(
            PluginManager, "get_instance",
            classmethod(lambda cls: _ReloadFakePM(plugin_a, infos)),
        )

        reg.register("session.read", lambda h: "s")
        reg.register("model.config", lambda h: "m")

        narrow = reg.resolve_declared("A")
        pool1 = reg.build_pool(surface="ui", host="WIN", plugin="A")
        assert narrow == {"session.read"}
        assert pool1.names() == ["session.read"]

        # manifest 窄→宽，走 rescan diff 真实接线（changed=[A]）
        infos["A"].manifest = {"capabilities": ["session.read", "model.config"]}
        _bare_host_service().reload_plugin_subsystems()

        wide = reg.resolve_declared("A")
        pool2 = reg.build_pool(surface="ui", host="WIN", plugin="A")
        assert set(wide) == {"session.read", "model.config"}
        assert set(pool2.names()) == {"session.read", "model.config"}
    finally:
        reg.clear()


class _ReloadFakePM(_FakePM):
    """声明解析与 reload 链共用同一 fake：get_plugin 查内存 manifest，rescan 出 diff。"""

    def __init__(self, plugin_obj, infos, diff=None):
        super().__init__(diff=diff or {"changed": [plugin_obj], "added": [], "removed": []},
                         plugins=[plugin_obj])
        self._infos = infos

    def get_plugin(self, name):
        return self._infos.get(name)


# ── M4-F 系：失效路径防回归 ──────────────────────────────────────────────

def test_release_plugin_resources_still_disconnects_subscriptions(monkeypatch):
    """M4-F1：直调卸载资源回收，订阅 disconnect 不得回归（现状无 invalidate，不钉它）。"""
    import app.plugins.capabilities.process_registry as pr_mod
    import app.plugins.capabilities.registry as cap_reg_mod
    import app.plugins.registries.contribution_registry as contrib_mod
    from app.core.infra.ui_event_bus import UIEventBus
    from app.core.services.plugin_host_service import PluginHostService

    monkeypatch.setattr(
        pr_mod.ProcessRegistry, "get_instance",
        classmethod(lambda cls: SimpleNamespace(kill_plugin=lambda name: 0)),
    )
    monkeypatch.setattr(
        contrib_mod.ContributionRegistry, "get_instance",
        classmethod(lambda cls: SimpleNamespace(unregister_source=lambda name: None)),
    )
    monkeypatch.setattr(
        UIEventBus, "get_instance",
        classmethod(lambda cls: SimpleNamespace(unsubscribe_plugin=lambda name: None)),
    )
    reg = cap_reg_mod.CapabilityRegistry()
    monkeypatch.setattr(cap_reg_mod.CapabilityRegistry, "get_instance", classmethod(lambda cls: reg))

    class _Sig:
        def __init__(self):
            self.calls = []

        def disconnect(self, slot):
            self.calls.append(slot)

    sig = _Sig()
    reg.track_subscription("p", sig, "SLOT")

    PluginHostService._release_plugin_host_resources("p")

    assert sig.calls == ["SLOT"]  # 断言 disconnect 发生；invalidate 与否留给实现演进


def test_empty_diff_short_circuit_skips_invalidation(monkeypatch):
    """M4-F2：空 diff 短路——失效零调用，保住 spec 缓存命中收益。"""
    from app.core.services.plugin_host_service import PluginHostService
    from app.plugins.managers.plugin_manager import PluginManager

    monkeypatch.setattr(
        PluginManager, "get_instance",
        classmethod(lambda cls: _FakePM({"changed": [], "added": [], "removed": []}, [])),
    )
    _patch_reload_side_effects(monkeypatch)
    seen = _patch_capability_registry(monkeypatch)

    result = _bare_host_service().reload_plugin_subsystems()

    assert seen == []  # 无变更不空转失效；无条件失效会把 S3 收益打归零
    assert isinstance(result, dict)


def test_invalidation_failure_does_not_break_reload(monkeypatch):
    """M4-F3：失效挂了重载不能挂——实码 try/except 吞 + warning 的容错契约。"""
    from app.core.services.plugin_host_service import PluginHostService
    from app.plugins.managers.plugin_manager import PluginManager

    a = SimpleNamespace(name="A", components={}, manifest={})
    monkeypatch.setattr(
        PluginManager, "get_instance",
        classmethod(lambda cls: _FakePM({"changed": [a], "added": [], "removed": []}, [a])),
    )
    _patch_reload_side_effects(monkeypatch)

    from app.plugins.capabilities.registry import CapabilityRegistry

    def _boom(name):
        raise RuntimeError("invalidate exploded")

    fake = SimpleNamespace(invalidate_declared=_boom)
    monkeypatch.setattr(CapabilityRegistry, "get_instance", classmethod(lambda cls: fake))

    result = _bare_host_service().reload_plugin_subsystems()  # 不得外抛

    assert isinstance(result, dict)
    assert "ui" in result  # 主流程照常产出 KNOWN_COMPONENTS 形状的汇总
