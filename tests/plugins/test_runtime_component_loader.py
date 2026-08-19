# -*- coding: utf-8 -*-
"""运行时组件加载器：扫描插件目录三类组件 → register(registry) → source 清理"""
import shutil

import pytest


def test_scan_and_register_loop_policy_plugin(tmp_path, monkeypatch):
    from app.plugins.loaders.runtime_component_loader import RuntimeComponentLoader
    from app.plugins.registries.loop_policy_registry import LoopPolicyRegistry

    # 造一个插件目录：loop_policies/minimal.py
    plugin_dir = tmp_path / "demo-plugin"
    (plugin_dir / "loop_policies").mkdir(parents=True)
    (plugin_dir / "loop_policies" / "minimal.py").write_text(
        "# -*- coding: utf-8 -*-\n"
        "from app.plugins.contracts.loop_policy import LoopDecision\n\n"
        "class MinimalLoopPolicy:\n"
        '    id = "minimal"\n\n'
        "    def should_continue(self, state):\n"
        "        return LoopDecision.STOP\n\n"
        "    def max_rounds(self, llm_config):\n"
        "        return 1\n\n"
        "def register(registry):\n"
        "    registry.register(MinimalLoopPolicy())\n",
        encoding="utf-8",
    )

    reg = LoopPolicyRegistry()
    loader = RuntimeComponentLoader(
        comp_dir="loop_policies",
        registry=reg,
        register_attr="register",
        unregister_attr="unregister_source",
    )
    loaded = loader.scan_roots([tmp_path])  # 显式传入扫描根（测试不依赖全局 plugins/）
    assert loaded == {"demo-plugin"}

    assert "minimal" in reg.policies()
    # 卸载语义：重扫时源插件已删除 → 注册清理
    shutil.rmtree(plugin_dir)
    loader.scan_roots([tmp_path])
    assert "minimal" not in reg.policies()


def test_warmup_registers_system_builtin_first():
    """warmup 后全局单例含内置实现（openai/default/sqlite 不丢）"""
    from app.plugins.builtin_runtime import ensure_builtin_runtime
    from app.plugins.loaders.runtime_component_loader import warmup_runtime_components

    ensure_builtin_runtime()
    warmup_runtime_components()
    from app.plugins.registries.model_adapter_registry import ModelAdapterRegistry
    from app.plugins.registries.loop_policy_registry import LoopPolicyRegistry
    from app.plugins.registries.storage_registry import StorageRegistry

    assert "openai" in ModelAdapterRegistry.get_instance().adapters()
    assert "default" in LoopPolicyRegistry.get_instance().policies()
    assert StorageRegistry.get_instance().get_active().id in ("sqlite",)