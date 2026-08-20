# -*- coding: utf-8 -*-
"""Telegram 试点迁移收官后验证：manager/config 零平台分支 + 6 def 齐备（社区仓）。

E2 Task 4：TelegramAdapter 迁出 app/gateway/adapters/telegram.py 至
plugins/system/gateways/telegram.py（register 暴露 GatewayPlatformDef）。
manager/config 仅对 Telegram 走 registry 优先；其余 5 平台保留旧内置段。

E2 Task 5：6 平台全部迁至 plugins/system/gateways/<id>.py。

E2 Task 6（主仓清理）：
- 6 platform 模块已迁至社区仓 drifox-plugins2/plugins/gateway-*/。
- 平台模块具体断言（TelegramAdapter 类、register 流程、display_name 全字段）
  随 platform 模块迁出；这些断言在 drifox-plugins2 仓 tests/ 下复刻。
- 本文件保留：
  - manager / config 零硬编码「if platform == Platform.X」分支断言
    （inspect.getsource 锁，与 platform 模块所在位置无关）
  - 「6 def 齐备」断言：默认 skip；环境变量 DRIFOX_GATEWAY_PLUGINS2 指向
    drifox-plugins2 仓根时启用，从该路径 sys.path 注入后再验证。
"""

from __future__ import annotations

import importlib
import inspect
import os

import pytest


class TestNoHardcodedPlatformBranches:
    """manager / config 不再硬编码 if/elif platform == Platform.<X> 段。"""

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


class TestAdaptersPackageDeleted:
    """adapters/ 整目录删除（E2 Task 5 收官）"""

    def test_adapters_package_deleted(self):
        import os

        assert not os.path.exists("app/gateway/adapters"), "app/gateway/adapters/ 仍存在——Task 5 须整目录删除"


# 6 个内置平台 id（与 drifox-plugins2/plugins/gateway-*/ 一一对应）
BUILTIN_IDS = ["wecom", "dingtalk", "telegram", "discord", "feishu", "slack"]


def _load_all_from_plugins2(plugins2_root: str):
    """从社区仓根加载 6 个 platform 模块并注册到 fresh registry"""
    from app.plugins.registries.gateway_platform_registry import GatewayPlatformRegistry

    if plugins2_root not in sys.path:
        sys.path.insert(0, plugins2_root)

    reg = GatewayPlatformRegistry()
    for pid in BUILTIN_IDS:
        mod = importlib.import_module(f"plugins.gateway_{pid}")
        mod.register(reg)
    return reg


import sys  # noqa: E402


@pytest.mark.skipif(
    not os.environ.get("DRIFOX_GATEWAY_PLUGINS2"),
    reason=(
        "6 platform 模块已迁至社区仓 drifox-plugins2。"
        "设置环境变量 DRIFOX_GATEWAY_PLUGINS2=<社区仓根绝对路径> 启用此断言。"
    ),
)
class TestAllDefsRegistered:
    """6 def 齐备 + 全字段断言（依赖 DRIFOX_GATEWAY_PLUGINS2 指向社区仓根）"""

    def test_six_defs_with_full_callbacks(self):
        plugins2_root = os.environ["DRIFOX_GATEWAY_PLUGINS2"]
        reg = _load_all_from_plugins2(plugins2_root)
        try:
            for pid in BUILTIN_IDS:
                d = reg.get(pid)
                assert d is not None, f"{pid} 未注册"
                assert d.adapter_factory is not None, f"{pid} 缺 adapter_factory"
                assert d.config_builder is not None, f"{pid} 缺 config_builder"
                assert d.config_writer is not None, f"{pid} 缺 config_writer"
                assert d.build_config_values is not None, f"{pid} 缺 build_config_values"
                assert d.check_requirements is not None, f"{pid} 缺 check_requirements"
                assert d.display_name, f"{pid} 缺 display_name"
        finally:
            with reg._lock:
                for pid in BUILTIN_IDS:
                    reg._defs.pop(pid, None)
