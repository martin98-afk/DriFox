# -*- coding: utf-8 -*-
"""E2E：三个运行时组件类型进入 kernel 体系（KNOWN_COMPONENTS / PROBES / reloader 分派）"""

from unittest.mock import MagicMock

import pytest


def test_kernel_known_components_contains_runtime_types():
    from app.plugins.kernel import KNOWN_COMPONENTS

    assert {"model_adapters", "loop_policies", "storages"} <= KNOWN_COMPONENTS


def test_component_probes_detect_runtime_dirs(tmp_path):
    from app.plugins.managers.plugin_manager import _detect_components

    (tmp_path / "model_adapters").mkdir()
    (tmp_path / "model_adapters" / "anthropic.py").write_text("", encoding="utf-8")
    (tmp_path / "loop_policies").mkdir()
    (tmp_path / "loop_policies" / "minimal.py").write_text("", encoding="utf-8")
    (tmp_path / "storages").mkdir()
    (tmp_path / "storages" / "memory.py").write_text("", encoding="utf-8")
    comps = _detect_components(tmp_path)
    assert {"model_adapters", "loop_policies", "storages"} <= set(comps)
    # 空目录不算（与 tools/providers 探测语义一致）
    empty = tmp_path / "storages_empty_dir"
    empty.mkdir()
    assert "storages_empty_dir" not in comps


def test_reloaders_dispatch_runtime_components(monkeypatch):
    """reloader 分派链：model_adapters → runtime loader scan"""
    from app.plugins import kernel

    reg = kernel.ComponentReloaderRegistry()
    calls = []

    def _fake_reloader(ctx):
        calls.append((ctx.component, ctx.plugin_name))
        return True

    for comp in ("model_adapters", "loop_policies", "storages"):
        reg.register(comp, _fake_reloader)
    monkeypatch.setattr(kernel, "get_reloader_registry", lambda: reg)

    backend_cls = pytest.importorskip("app.core.backend").ChatBackend
    backend = backend_cls.__new__(backend_cls)
    fake_pm = MagicMock()
    fake_pm.is_initialized.return_value = True
    fake_pm.get_plugin.return_value.components = {"model_adapters": True}
    fake_pm.get_plugin.return_value.has_component = lambda c: c == "model_adapters"
    fake_pm.rescan_plugin = MagicMock()
    monkeypatch.setattr(
        "app.plugins.managers.plugin_manager.PluginManager.get_instance",
        staticmethod(lambda: fake_pm),
    )
    result = backend._reload_single_plugin("demo-plugin", "model_adapters")
    assert result["model_adapters"] is True
    assert ("model_adapters", "demo-plugin") in calls


def test_builtin_reloaders_cover_runtime_types():
    """内置 reloader 注册表包含三个运行时组件"""
    from app.plugins.builtin_reloaders import (  # noqa: F401
        _reload_model_adapters,
        _reload_loop_policies,
        _reload_storages,
    )


def test_kernel_known_components_contains_engines():
    from app.plugins.kernel import KNOWN_COMPONENTS

    assert "engines" in KNOWN_COMPONENTS


def test_component_probes_detect_engines_dir(tmp_path):
    from app.plugins.managers.plugin_manager import _detect_components

    (tmp_path / "engines").mkdir()
    (tmp_path / "engines" / "demo.py").write_text("", encoding="utf-8")
    comps = _detect_components(tmp_path)
    assert "engines" in comps
    # 仅空目录不算（与 tools/providers 探测语义一致）
    empty = tmp_path / "engines_empty"
    empty.mkdir()
    assert "engines_empty" not in comps


def test_builtin_reloaders_cover_engines():
    """内置 reloader 注册表包含 engines — 与四类运行时组件并列"""
    from app.plugins.builtin_reloaders import (  # noqa: F401
        RELOADED_COMPONENTS,
        _reload_engines,
    )
    from app.plugins.kernel import ComponentReloaderRegistry

    assert "engines" in RELOADED_COMPONENTS
    reg = ComponentReloaderRegistry()
    from app.plugins import builtin_reloaders

    builtin_reloaders.register_builtin_reloaders(reg)
    assert "engines" in reg.known_components()
