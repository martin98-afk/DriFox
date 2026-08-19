# -*- coding: utf-8 -*-
"""运行时组件加载器：扫描插件目录三类组件 → register(registry) → source 清理"""
import shutil
import sys
import threading

import pytest


def test_scan_and_register_loop_policy_plugin(tmp_path, plugin_enabled):
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
    restore = plugin_enabled("demo-plugin")

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
    restore()


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


def test_cross_root_user_overrides_system(tmp_path, monkeypatch, plugin_enabled):
    """跨根覆盖：两个 tmp 根（伪造 system / user），同 id 注册，后者覆盖前者。"""
    from app.plugins.loaders import runtime_component_loader as rcl
    from app.plugins.loaders.runtime_component_loader import RuntimeComponentLoader
    from app.plugins.registries.loop_policy_registry import LoopPolicyRegistry

    system_root = tmp_path / "system"
    user_root = tmp_path / "user"
    for root in (system_root, user_root):
        plugin_dir = root / "shared-plugin"
        (plugin_dir / "loop_policies").mkdir(parents=True)

    # system 插件：基线实现
    (system_root / "shared-plugin" / "loop_policies" / "shared.py").write_text(
        "# -*- coding: utf-8 -*-\n"
        "from app.plugins.contracts.loop_policy import LoopDecision\n\n"
        "class SystemSharedLoopPolicy:\n"
        '    id = "shared"\n'
        '    tag = "system"\n\n'
        "    def should_continue(self, state):\n"
        "        return LoopDecision.STOP\n\n"
        "    def max_rounds(self, llm_config):\n"
        "        return 1\n\n"
        "def register(registry):\n"
        "    registry.register(SystemSharedLoopPolicy())\n",
        encoding="utf-8",
    )
    # user 插件：同名 id 但 tag 标记为 user（用于断言 user 胜出）
    (user_root / "shared-plugin" / "loop_policies" / "shared.py").write_text(
        "# -*- coding: utf-8 -*-\n"
        "from app.plugins.contracts.loop_policy import LoopDecision\n\n"
        "class UserSharedLoopPolicy:\n"
        '    id = "shared"\n'
        '    tag = "user"\n\n'
        "    def should_continue(self, state):\n"
        "        return LoopDecision.STOP\n\n"
        "    def max_rounds(self, llm_config):\n"
        "        return 1\n\n"
        "def register(registry):\n"
        "    registry.register(UserSharedLoopPolicy())\n",
        encoding="utf-8",
    )

    # monkeypatch _root_kind：按 root 路径返回 system / user
    kind_map = {system_root: "system", user_root: "user"}

    def fake_root_kind(root):
        return kind_map.get(root, "system")

    monkeypatch.setattr(rcl, "_root_kind", fake_root_kind)
    restore = plugin_enabled("shared-plugin")

    reg = LoopPolicyRegistry()
    loader = RuntimeComponentLoader(
        comp_dir="loop_policies",
        registry=reg,
        register_attr="register",
        unregister_attr="unregister_source",
    )
    # system 先扫、user 后扫 → user 应当覆盖 system
    loaded = loader.scan_roots([system_root, user_root])
    assert loaded == {"shared-plugin"}

    policy = reg.policies()["shared"]
    assert policy.tag == "user", f"期望 user 覆盖 system，实际 tag={policy.tag}"
    restore()


def test_module_registered_in_sys_modules(tmp_path, plugin_enabled):
    """加载后 sys.modules 含 drifox_rt_<comp_dir>_<plugin>_<stem> 模块名。"""
    from app.plugins.loaders.runtime_component_loader import RuntimeComponentLoader
    from app.plugins.registries.loop_policy_registry import LoopPolicyRegistry

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
    restore = plugin_enabled("demo-plugin")

    reg = LoopPolicyRegistry()
    loader = RuntimeComponentLoader(
        comp_dir="loop_policies",
        registry=reg,
        register_attr="register",
        unregister_attr="unregister_source",
    )
    loader.scan_roots([tmp_path])

    expected_prefix = "drifox_rt_loop_policies_demo-plugin_"
    matched = [name for name in sys.modules if name.startswith(expected_prefix)]
    assert matched, f"sys.modules 中未找到 {expected_prefix}* 模块名"
    assert "minimal" in reg.policies()
    restore()


def test_scan_lock_concurrent(tmp_path, plugin_enabled):
    """两线程并发 scan_roots → 最终状态稳定，无重复 / 无遗漏。"""
    from app.plugins.loaders.runtime_component_loader import RuntimeComponentLoader
    from app.plugins.registries.loop_policy_registry import LoopPolicyRegistry

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
    restore = plugin_enabled("demo-plugin")

    reg = LoopPolicyRegistry()
    loader = RuntimeComponentLoader(
        comp_dir="loop_policies",
        registry=reg,
        register_attr="register",
        unregister_attr="unregister_source",
    )

    # 代码级断言：_scan_lock 存在且是 Lock 实例，并在 scan_roots 中使用
    import inspect

    assert hasattr(loader, "_scan_lock"), "RuntimeComponentLoader 缺少 _scan_lock"
    assert isinstance(loader._scan_lock, type(threading.Lock())), "_scan_lock 不是 Lock 实例"
    src = inspect.getsource(loader.scan_roots)
    assert "self._scan_lock" in src, "scan_roots 未使用 _scan_lock"

    # 行为级：两线程并发扫描 8 轮 → 最终 registry 只含一份 'minimal'
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait()
        for _ in range(8):
            loader.scan_roots([tmp_path])

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    policies = reg.policies()
    assert "minimal" in policies
    assert len(policies) == 1, f"并发 scan 后注册表应仅一份，实际 {len(policies)} 条"
    restore()