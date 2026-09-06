# -*- coding: utf-8 -*-
"""G1：hook_policies 链路补全验证。

三用例：kernel 集合含 hook_policies / builtin_reloaders 与 kernel 一致性 /
watchfiles 归因模拟（hook_policies 段变更 → 组件识别命中）。
"""
from pathlib import Path

from app.plugins.builtin_reloaders import RELOADED_COMPONENTS
from app.plugins.kernel import COMPONENT_ORDER, KNOWN_COMPONENTS


def test_kernel_sets_contain_hook_policies():
    """kernel.KNOWN_COMPONENTS 与 COMPONENT_ORDER 均含 hook_policies。"""
    assert "hook_policies" in KNOWN_COMPONENTS
    assert "hook_policies" in COMPONENT_ORDER
    # 归因链依赖物理目录探测：plugin_manager._COMPONENT_PROBES 也须有谓词
    from app.plugins.managers.plugin_manager import _COMPONENT_PROBES

    assert "hook_policies" in _COMPONENT_PROBES
    probe_dir = Path(__file__).parent / "_probe_hook_policies"
    (probe_dir / "hook_policies").mkdir(parents=True, exist_ok=True)
    (probe_dir / "hook_policies" / "demo.py").write_text("x = 1\n", encoding="utf-8")
    try:
        from app.plugins.managers.plugin_manager import _detect_components

        assert _detect_components(probe_dir).get("hook_policies") is True
    finally:
        import shutil

        shutil.rmtree(probe_dir, ignore_errors=True)


def test_reloaded_components_match_kernel():
    """builtin_reloaders.RELOADED 与 kernel.KNOWN_COMPONENTS 一致（单一事实源）。"""
    assert RELOADED_COMPONENTS == KNOWN_COMPONENTS
    # reloader 注册表有 hook_policies 实现（注册后 registry 能分派）
    from app.plugins.builtin_reloaders import register_builtin_reloaders
    from app.plugins.kernel import get_reloader_registry

    registry = get_reloader_registry()
    register_builtin_reloaders(registry)
    assert "hook_policies" in registry.known_components()


def test_watchfiles_attribution_identifies_hook_policies():
    """watchfiles 归因模拟：hook_policies 段变更路径 → 识别为可重载组件。"""
    from app.core.plugin_host_service import PluginHostService

    svc = PluginHostService.__new__(PluginHostService)
    plugin_dir = Path("D:/work/DriFox/plugins/system").resolve()
    changes = [
        ("Change.added", str(plugin_dir / "hook_policies" / "team_member.py")),
        ("Change.modified", str(plugin_dir / "hooks" / "hooks.json")),
    ]
    # QObject 子类无法 __new__ 后直接绑定调用（super-class init 未执行），
    # 改用 __dict__ 取 unbound 函数——方法体不依赖 self 状态
    identify = PluginHostService.__dict__["_identify_all_components_from_changes"]
    got = identify(None, changes, {str(plugin_dir).lower(): "system"}, "system")
    assert "hook_policies" in got
    assert "hooks" in got
