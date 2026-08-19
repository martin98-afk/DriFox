# -*- coding: utf-8 -*-
"""E2E：新增组件类型 = 注册一个 reloader，backend/plugin_manager 零改动。

模拟 Phase B 的场景：未来「窗口面板」类组件只需：
1. kernel 不改（KNOWN_COMPONENTS 由注册 API 动态扩展，见下）
2. 注册 reloader
本测试证明 watchfiles 路径识别 → 分派 → reloader 执行全链路通。

验收判据（对应 SDD 评价报告 G2）：
- backend._identify_all_components_from_changes 用 kernel 常量识别组件
- backend._reload_single_plugin 走 kernel reloader 注册表分派（无 if component == 字面量）
- 新组件类型 result key 动态存在于 result dict（不需改 backend.py 初始化）
"""

from unittest.mock import MagicMock

import pytest


def test_new_component_type_end_to_end(monkeypatch, tmp_path):
    """E2E：模拟新增组件类型 widgets，验证识别→分派→result 全链路"""
    from app.plugins import kernel
    from app.plugins.kernel import ComponentReloaderRegistry, ReloadContext

    # 1) 动态扩展组件类型（模拟插件注册新组件类型）
    kernel.KNOWN_COMPONENTS.add("widgets")
    try:
        # 2) 注册 reloader
        reg = ComponentReloaderRegistry()
        calls = []
        reg.register("widgets", lambda ctx: calls.append(ctx.plugin_name) or True)

        # 3) 路径识别链：backend._identify_all_components_from_changes 用 kernel 常量
        backend_cls = pytest.importorskip("app.core.backend").ChatBackend
        backend = backend_cls.__new__(backend_cls)

        # tmp_path 在 Windows 上需小写与 backend 内 os.path 比较语义一致
        plugin_prefixes = {str(tmp_path).lower(): "e2e-plugin"}
        changes = [(None, str(tmp_path / "widgets" / "panel.py"))]
        comps = backend._identify_all_components_from_changes(changes, plugin_prefixes, "e2e-plugin")
        assert "widgets" in comps, f"新组件类型 widgets 应被识别，实际 comps={comps}"

        # 4) 分派链：_reload_single_plugin 走注册表
        fake_plugin = MagicMock()
        fake_plugin.components = {"widgets": True}
        fake_plugin.has_component = lambda c: c == "widgets"
        fake_plugin.path = tmp_path

        fake_pm = MagicMock()
        fake_pm.is_initialized.return_value = True
        fake_pm.get_plugin.return_value = fake_plugin
        fake_pm.rescan_plugin = MagicMock()

        monkeypatch.setattr(
            "app.plugins.managers.plugin_manager.PluginManager.get_instance",
            staticmethod(lambda: fake_pm),
        )
        monkeypatch.setattr(kernel, "get_reloader_registry", lambda: reg)

        result = backend._reload_single_plugin("e2e-plugin", "widgets")
        assert calls == ["e2e-plugin"], f"widgets reloader 应被调用一次，实际 calls={calls}"
        assert result["widgets"] is True, f"result['widgets'] 应为 True，实际={result.get('widgets')}"
    finally:
        # 清理污染：保证不污染后续测试的 KNOWN_COMPONENTS 集合
        kernel.KNOWN_COMPONENTS.discard("widgets")
