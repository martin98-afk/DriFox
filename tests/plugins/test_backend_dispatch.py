# -*- coding: utf-8 -*-
"""backend 表分派测试 — 验证 _reload_single_plugin 走 kernel 注册表而非 8 分支"""

from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QObject

from app.core.plugin_host_service import PluginHostService


def _make_host():
    """构造脱离单例/生命周期约束的 PluginHostService 实例（纯插件分派测试用）"""
    svc = PluginHostService.__new__(PluginHostService)
    QObject.__init__(svc)
    svc._agent_manager = None
    return svc


@pytest.fixture()
def kernel_env(monkeypatch, tmp_path):
    """隔离环境：空 PluginManager + 独立 reloader 注册表

    注入方式：monkeypatch `app.plugins.kernel._registry` 为本次 fixture 的独立 reg，
    并在该 reg 上注册内置 reloader（builtin_reloaders 可能在 import 时已注册到旧表，
    这里直接绑到 fixture 的 reg，确保 registry.reload() 命中）。
    """
    from app.plugins import builtin_reloaders, kernel as kernel_mod
    from app.plugins.kernel import ComponentReloaderRegistry, ReloadContext

    reg = ComponentReloaderRegistry()
    monkeypatch.setattr(kernel_mod, "_registry", reg)
    # 把 builtin_reloaders 注册到这个 fixture 的 reg（而非 import 期可能已注册的旧 reg）
    builtin_reloaders.register_builtin_reloaders(reg)

    fake_plugin = MagicMock()
    fake_plugin.components = {"themes": True}
    fake_plugin.has_component = lambda c: fake_plugin.components.get(c, False)
    fake_plugin.path = tmp_path

    fake_pm = MagicMock()
    fake_pm.is_initialized.return_value = True
    fake_pm.get_plugin.return_value = fake_plugin
    fake_pm.rescan_plugin = MagicMock()

    return reg, fake_pm


def test_dispatch_via_registry(kernel_env, monkeypatch):
    """经注册表分派：themes 组件 → 自定义 reloader 被调用"""
    reg, fake_pm = kernel_env
    from app.core.backend import ChatBackend

    calls = []
    reg.register("themes", lambda ctx: calls.append(ctx.component) or True)

    backend = _make_host()  # 跳过 __init__（不做真实初始化）
    monkeypatch.setattr(
        "app.plugins.managers.plugin_manager.PluginManager.get_instance",
        staticmethod(lambda: fake_pm),
    )

    result = backend._reload_single_plugin("p", "themes")
    assert calls == ["themes"]
    assert result["themes"] is True


def test_dispatch_deleted_plugin_triggers_cleanup_path(kernel_env, monkeypatch):
    """插件删除（get_plugin → None）时各 reloader 收到 plugin=None 真实分派

    Task 6：删除清理段并入 reloader 分派 — 删除路径不再手写清理分支，
    而是遍历该插件原有组件 → 调 registry.reload(ReloadContext(plugin=None))。
    各 reloader 自行处理 ctx.plugin is None 语义（agents→cleanup_plugin_artifacts、
    hooks-only→unregister_skill_hooks、commands→reload_all_commands、themes→reload、
    skills→invalidate_cache、ui→unload_plugin、lsp→remove_plugin_servers，
    tools/providers 删除路径下跳过）。
    """
    reg, fake_pm = kernel_env
    # 删除前 fake_plugin（含 components 字典，提供 removed_components 数据源）
    fake_plugin = fake_pm.get_plugin.return_value
    fake_plugin.components = {"themes": True, "commands": True, "skills": True}
    fake_plugin.has_component = lambda c: fake_plugin.components.get(c, False)

    # 第一次 get_plugin（拿 plugin_before）→ fake_plugin；第二次（rescan 后）→ None
    fake_pm.get_plugin.side_effect = [fake_plugin, None]

    from app.core.backend import ChatBackend

    seen = []
    reg.register("themes", lambda ctx: seen.append(("themes", ctx.plugin is None)) or True)
    reg.register("commands", lambda ctx: seen.append(("commands", ctx.plugin is None)) or True)
    reg.register("skills", lambda ctx: seen.append(("skills", ctx.plugin is None)) or True)

    backend = _make_host()
    # 给 backend 实例补齐删除段依赖的属性（__new__ 跳过 __init__，默认无任何属性）
    backend._watcher_dedup_cache = {}
    backend._agent_manager = MagicMock()  # builtin_reloaders._RUNTIME 注入前占位（实际不被命中）
    monkeypatch.setattr(
        "app.plugins.managers.plugin_manager.PluginManager.get_instance",
        staticmethod(lambda: fake_pm),
    )

    backend._reload_single_plugin("p", "themes")

    # 真实分派断言：themes/commands/skills reloader 都在删除路径被调用且 plugin is None
    assert ("themes", True) in seen, f"themes reloader 应在删除路径被调用，实际 seen={seen}"
    assert ("commands", True) in seen, f"commands reloader 应在删除路径被调用，实际 seen={seen}"
    assert ("skills", True) in seen, f"skills reloader 应在删除路径被调用，实际 seen={seen}"


def test_unknown_component_skipped(kernel_env, monkeypatch):
    """未知组件：注册的 bogus reloader 完全不被调用（plugin=None 删除路径也不命中）"""
    reg, fake_pm = kernel_env
    from app.core.backend import ChatBackend

    # 故意注册一个 bogus 名字的 reloader — 验证注册表 dispatch 不会误派给它
    calls = []
    reg.register("bogus", lambda ctx: calls.append(ctx.component) or True)

    backend = _make_host()
    monkeypatch.setattr(
        "app.plugins.managers.plugin_manager.PluginManager.get_instance",
        staticmethod(lambda: fake_pm),
    )

    # 非删除路径：plugin 不为 None（默认 fixture）
    result = backend._reload_single_plugin("p", "bogus")
    assert result.get("themes") is False
    assert calls == [], "未知组件名 'bogus' 的 reloader 不应被调用"

    # 删除路径也不命中：plugin=None 时同样跳过 bogus reloader
    fake_pm.get_plugin.return_value = None
    backend._reload_single_plugin("p", "bogus")
    assert calls == [], "删除路径也不应触发未知 reloader"


def test_agents_dispatch_marks_hooks_and_commands(kernel_env, monkeypatch):
    """agents 联动标记：分派 agents 时 hooks/commands 一并视为已处理"""
    reg, fake_pm = kernel_env
    fake_plugin = fake_pm.get_plugin.return_value
    fake_plugin.components = {"agents": True, "hooks": True, "commands": True}
    fake_plugin.has_component = lambda c: fake_plugin.components.get(c, False)

    from app.core.backend import ChatBackend

    calls = []
    reg.register("agents", lambda ctx: calls.append(ctx.component) or 3)

    backend = _make_host()
    monkeypatch.setattr(
        "app.plugins.managers.plugin_manager.PluginManager.get_instance",
        staticmethod(lambda: fake_pm),
    )

    result = backend._reload_single_plugin("p", "agents")
    assert calls == ["agents"]
    assert result["agents"] == 3
    assert result["hooks"] is True  # 联动标记
    assert result["commands"] is True  # 联动标记


def test_delete_path_iterates_by_component_order(kernel_env, monkeypatch):
    """删除段遍历必须按 COMPONENT_ORDER（tuple）— 防止 KNOWN_COMPONENTS set 序不确定漏清理。

    Task 6 Fix Round 2：旧代码用 set 遍历，漏遍历某组件会跳过其清理分支（如 agents 后置 commands 顺序错了，
    会让 commands reloader 误跑在 cleanup 之后导致状态漂移）。本测试断言删除段遍历序与
    kernel.COMPONENT_ORDER 一致，且所有声明组件都收到 ctx.plugin is None 的清理调用。
    """
    from app.plugins import builtin_reloaders
    from app.plugins import kernel as kernel_mod

    reg, fake_pm = kernel_env
    # 关键：禁用 builtin 内置 reloader（避免 agents→commands 等副作用污染 seen 列表）
    # _BUILTIN_REGISTERED 为强引用列表（身份判断，防 GC 后 id 复用误判）
    builtin_reloaders._BUILTIN_REGISTERED = [
        r for r in builtin_reloaders._BUILTIN_REGISTERED if r is not reg
    ]

    fake_plugin = MagicMock()
    # 声明全部 11 类组件（含 team_templates 末尾），模拟复杂插件
    fake_plugin.components = {c: True for c in kernel_mod.COMPONENT_ORDER}
    fake_plugin.has_component = lambda c: fake_plugin.components.get(c, False)
    fake_plugin.path = MagicMock()
    # 第一次 get_plugin（取 plugin_before）→ fake_plugin；第二次（rescan 后）→ None
    fake_pm.get_plugin.side_effect = [fake_plugin, None]

    # 用本地 seen 收集所有 reloader 实际触发顺序
    seen: list[str] = []
    for comp in kernel_mod.COMPONENT_ORDER:
        reg.register(comp, lambda ctx, _c=comp: seen.append(_c) or True)

    from app.core.backend import ChatBackend

    backend = _make_host()
    backend._watcher_dedup_cache = {}
    monkeypatch.setattr(
        "app.plugins.managers.plugin_manager.PluginManager.get_instance",
        staticmethod(lambda: fake_pm),
    )

    backend._reload_single_plugin("p", "")  # component 空走删除段

    # 遍历序必须严格等于 COMPONENT_ORDER — 任何 set 序漂移都会让 assert 失败
    assert seen == list(kernel_mod.COMPONENT_ORDER), f"删除段遍历序应等于 COMPONENT_ORDER，实际 {seen}"
    # 所有声明组件都被处理
    assert set(seen) == kernel_mod.KNOWN_COMPONENTS


def test_reload_plugin_targeted_dispatches_manifest(kernel_env, monkeypatch):
    """reload_plugin_targeted(非空名) → _reload_single_plugin(name, "__manifest__")

    回归锁定：UI 安装/更新/启停后若调全量 reload_plugin_subsystems，
    会重载全部插件的 hooks/agents/commands；targeted 门面必须走精准路径。
    """
    reg, fake_pm = kernel_env
    from app.core.backend import ChatBackend

    backend = _make_host()
    calls: list = []
    monkeypatch.setattr(backend, "_reload_single_plugin", lambda n, c: calls.append((n, c)) or {})
    backend.reload_plugin_targeted("workbuddy")
    assert calls == [("workbuddy", "__manifest__")], f"应分派到 _reload_single_plugin(name, __manifest__)，实际 {calls}"


def test_reload_plugin_targeted_empty_falls_back_to_full(kernel_env, monkeypatch):
    """reload_plugin_targeted(空名) → 回退全量 reload_plugin_subsystems（防御）"""
    reg, fake_pm = kernel_env
    from app.core.backend import ChatBackend

    backend = _make_host()
    calls: list = []
    monkeypatch.setattr(backend, "reload_plugin_subsystems", lambda: calls.append(True) or {})
    backend.reload_plugin_targeted("")
    assert calls == [True], "空名应回退全量重载"


def test_reload_plugin_targeted_emits_plugin_changed(kernel_env, monkeypatch):
    """安装/更新/启停路径必须广播 plugin_changed（回归：twin-chat 安装后已开标签页不刷新）

    Installer 直接调 reload_plugin_targeted（非 watcher 链路），若无 emit，
    窗口收不到 ui=True → 已打开标签页输入区按钮/内容块不刷新；
    watcher 抑制解除后的 fallback 事件组件归类常为 root（ui=False）顶替不了。
    """
    reg, fake_pm = kernel_env
    from app.core.backend import ChatBackend

    fake_plugin = fake_pm.get_plugin.return_value
    fake_plugin.components = {"ui": True}
    fake_plugin.has_component = lambda c: fake_plugin.components.get(c, False)

    backend = _make_host()
    # __new__ 实例无 C++ 对象，访问 Qt 信号必抛 RuntimeError →
    # 在 emit_plugin_changed 层断言（信号→窗口槽链路由 test_input_button_hot_reload 覆盖）
    emitted: list = []
    monkeypatch.setattr(
        backend, "emit_plugin_changed", lambda r, n="", action=None: emitted.append((dict(r), n))
    )
    monkeypatch.setattr(ChatBackend, "_active_instances", [])
    monkeypatch.setattr(
        "app.plugins.managers.plugin_manager.PluginManager.get_instance",
        staticmethod(lambda: fake_pm),
    )
    # 隔离 UI 注册表单例：ui reloader 的 reload_plugin 不做真实加载
    from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

    monkeypatch.setattr(UIPluginRegistry, "get_instance", classmethod(lambda cls: MagicMock()))

    result = backend.reload_plugin_targeted("plug")

    assert result.get("ui") is True, f"manifest 分派应置 ui=True，实际 {result}"
    assert len(emitted) == 1, "targeted 重载后必须广播 plugin_changed 到窗口"
    annotated, name = emitted[0]
    assert annotated.get("ui") is True
    assert name == "plug"


def test_reload_plugin_subsystems_diff_precise(kernel_env, monkeypatch):
    """reload_plugin_subsystems 默认 diff 精准：added/changed 走 __manifest__，removed 走精准清理

    回归锁定：旧实现除"单一新增"外全部走全量（重载所有 agents/hooks/commands），
    卸载一个插件会波及全部插件；现改为逐插件精准处理。
    """
    reg, fake_pm = kernel_env
    from app.core.backend import ChatBackend

    class _FakeDiffPlugin:
        def __init__(self, name, components):
            self.name = name
            self.components = components

    fake_pm.rescan.return_value = {
        "added": [_FakeDiffPlugin("new-plug", {"agents": True, "tools": True})],
        "removed": [_FakeDiffPlugin("dead-plug", {"hooks": True, "ui": True})],
        "changed": [_FakeDiffPlugin("touched-plug", {"commands": True})],
    }

    backend = _make_host()
    backend._watcher_dedup_cache = {}
    seen: list = []
    cleanup_calls: list = []
    monkeypatch.setattr(
        "app.plugins.managers.plugin_manager.PluginManager.get_instance",
        staticmethod(lambda: fake_pm),
    )

    def _fake_single(name, comp):
        seen.append((name, comp))
        return {}

    def _fake_cleanup(name, comps, res, keys):
        cleanup_calls.append((name, dict(comps)))
        return res

    monkeypatch.setattr(backend, "_reload_single_plugin", _fake_single)
    monkeypatch.setattr(backend, "_cleanup_removed_plugin_components", _fake_cleanup)

    result = backend.reload_plugin_subsystems()

    # added/changed → 精准 __manifest__ 重载
    assert seen == [("new-plug", "__manifest__"), ("touched-plug", "__manifest__")], (
        f"added/changed 应走 __manifest__ 精准重载，实际 {seen}"
    )
    # removed → 精准清理（组件信息取自 diff 对象，而非已移出索引的 get_plugin）
    assert cleanup_calls == [("dead-plug", {"hooks": True, "ui": True})], f"removed 应走精准清理，实际 {cleanup_calls}"
    # 不触发全量子系统重载
    assert result["agents"] == 0 and result["commands"] is False


def test_reload_plugin_subsystems_no_diff_skips(kernel_env, monkeypatch):
    """reload_plugin_subsystems 无变更时不重载任何子系统（零浪费）"""
    reg, fake_pm = kernel_env
    from app.core.backend import ChatBackend

    fake_pm.rescan.return_value = {"added": [], "removed": [], "changed": []}
    monkeypatch.setattr(
        "app.plugins.managers.plugin_manager.PluginManager.get_instance",
        staticmethod(lambda: fake_pm),
    )

    backend = _make_host()
    backend._watcher_dedup_cache = {}
    reloaded: list = []
    monkeypatch.setattr(backend, "_reload_single_plugin", lambda n, c: reloaded.append(n) or {})
    monkeypatch.setattr(
        backend,
        "_cleanup_removed_plugin_components",
        lambda n, comps, res, keys: reloaded.append(n) or res,
    )

    result = backend.reload_plugin_subsystems()

    assert reloaded == [], "无变更不应触发任何重载"
    assert result["agents"] == 0 and result["commands"] is False


def test_reload_plugin_subsystems_force_full(kernel_env, monkeypatch):
    """reload_plugin_subsystems(force_full=True) 保留全量语义（设置按钮显式操作）"""
    reg, fake_pm = kernel_env
    from app.core.backend import ChatBackend

    fake_pm.rescan.return_value = {"added": [], "removed": [], "changed": []}
    monkeypatch.setattr(
        "app.plugins.managers.plugin_manager.PluginManager.get_instance",
        staticmethod(lambda: fake_pm),
    )

    backend = _make_host()
    backend._watcher_dedup_cache = {}
    called: list = []
    monkeypatch.setattr(backend, "_reload_all_subsystems", lambda pm, res, keys: called.append(True) or res)

    backend.reload_plugin_subsystems(force_full=True)
    assert called == [True], "force_full=True 应走全量重载"
