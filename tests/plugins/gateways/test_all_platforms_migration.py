# -*- coding: utf-8 -*-
"""
全部内置平台迁移收官：6 def 齐备 + 主程序零平台分支断言。

E2 Task 5：wecom/dingtalk/discord/feishu/slack 五平台迁出主程序，
manager / config 全部走 registry 分派（不再 if-elif Platform.X）。
adapters/ 整目录删除。
"""

from __future__ import annotations

import importlib
import inspect

BUILTIN_IDS = ["wecom", "dingtalk", "telegram", "discord", "feishu", "slack"]


def _load_all():
    """手动执行 system 插件各 gateway 模块的 register（loader 集成环境下自动完成）"""
    from app.plugins.registries.gateway_platform_registry import GatewayPlatformRegistry

    reg = GatewayPlatformRegistry.get_instance()
    for pid in BUILTIN_IDS:
        mod = importlib.import_module(f"plugins.system.gateways.{pid}")
        mod.register(reg)
    return reg


class TestAllDefsRegistered:
    def test_six_defs_with_full_callbacks(self):
        reg = _load_all()
        try:
            for pid in BUILTIN_IDS:
                d = reg.get(pid)
                assert d is not None, f"{pid} 未注册"
                assert d.adapter_factory is not None, f"{pid} 缺 adapter_factory"
                assert d.config_builder is not None, f"{pid} 缺 config_builder"
                assert d.config_writer is not None, f"{pid} 缺 config_writer"
                assert d.build_config_values is not None, f"{pid} 缺 build_config_values"
                assert d.validate_config is not None, f"{pid} 缺 validate_config"
                assert d.display_name, f"{pid} 缺 display_name"
        finally:
            # 清理：通过 id 直接 pop（手动 register source=""）
            with reg._lock:
                for pid in BUILTIN_IDS:
                    reg._defs.pop(pid, None)


class TestNoHardcodedPlatformBranches:
    def test_manager_no_platform_if_chain(self):
        """manager.py 不再硬编码 'if/elif platform == Platform.<X>' 段"""
        src = inspect.getsource(__import__("app.gateway.manager", fromlist=["x"]))
        assert "if platform == Platform." not in src, "manager.py 仍存在 'if platform == Platform.X' 硬编码分支"
        assert "elif platform == Platform." not in src, "manager.py 仍存在 'elif platform == Platform.X' 硬编码分支"

    def test_config_no_platform_if_chain(self):
        """config.py 不再硬编码 'if/elif platform == Platform.<X>' 段"""
        src = inspect.getsource(__import__("app.gateway.config", fromlist=["x"]))
        assert "if platform == Platform." not in src, "config.py 仍存在 'if platform == Platform.X' 硬编码分支"
        assert "elif platform == Platform." not in src, "config.py 仍存在 'elif platform == Platform.X' 硬编码分支"

    def test_adapters_package_deleted(self):
        """app/gateway/adapters/ 整目录已删除"""
        import os

        assert not os.path.exists("app/gateway/adapters"), "app/gateway/adapters/ 仍存在——Task 5 须整目录删除"

    def test_all_platforms_from_registry(self):
        """registry.list_platforms() 包含全部 6 内置 id"""
        reg = _load_all()
        try:
            ids = {d.platform_id for d in reg.list_platforms()}
            assert set(BUILTIN_IDS) <= ids, f"registry 缺失内置平台：{set(BUILTIN_IDS) - ids}"
        finally:
            with reg._lock:
                for pid in BUILTIN_IDS:
                    reg._defs.pop(pid, None)
