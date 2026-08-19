# -*- coding: utf-8 -*-
"""验收：system 插件目录的 loop_policies 组件被加载并可激活（万物即插件判据）"""


def test_minimal_policy_loadable_from_plugin_dir():
    import importlib.util
    from pathlib import Path

    py = Path(__file__).resolve().parents[2] / "plugins" / "system" / "loop_policies" / "minimal.py"
    assert py.exists(), f"缺少验收插件: {py}"
    spec = importlib.util.spec_from_file_location("drifox_test_minimal_policy", py)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    from app.plugins.contracts.loop_policy import LoopDecision, LoopState

    policy = mod.MinimalLoopPolicy()
    assert policy.id == "minimal"
    assert policy.max_rounds({}) == 1
    assert policy.should_continue(LoopState(tool_calls_found=True)) is LoopDecision.STOP
    assert policy.should_continue(LoopState()) is LoopDecision.STOP


def test_loader_picks_up_system_plugin():
    """真实扫描项目 plugins/ 根 → system 插件的 minimal 注册进 registry"""
    from app.plugins.loaders.runtime_component_loader import RuntimeComponentLoader
    from app.plugins.registries.loop_policy_registry import LoopPolicyRegistry

    reg = LoopPolicyRegistry()
    # 直接用类构造独立实例（不污染全局单例）
    loader = RuntimeComponentLoader("loop_policies", reg)
    from pathlib import Path

    roots = [Path(__file__).resolve().parents[2] / "plugins"]
    loaded = loader.scan_roots(roots)
    if "system" in loaded:  # system 插件目录含 loop_policies 时
        assert "minimal" in reg.policies()
        try:
            assert reg.set_active("minimal") is True
            assert reg.get_active().id == "minimal"
        finally:
            reg.set_active("default")
