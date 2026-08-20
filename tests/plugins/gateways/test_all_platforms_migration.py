# -*- coding: utf-8 -*-
"""
全部内置平台迁移收官后契约：manager/config 零平台分支 + adapters 目录已删。

E2 Task 5：wecom/dingtalk/discord/feishu/slack 五平台迁出主程序，
manager / config 全部走 registry 分派（不再 if-elif Platform.X）。
adapters/ 整目录删除。

E2 Task 6（主仓清理）：
- 6 platform 模块已迁至社区仓 drifox-plugins2/plugins/gateway-*/。
- 「6 def 齐备 + 全字段」断言已并入 test_telegram_migration.py
  TestAllDefsRegistered（DRIFOX_GATEWAY_PLUGINS2 门控，默认 skip）。
- 本文件保留 manager / config / adapters 三项契约，与 test_telegram_migration.py
  TestNoHardcodedPlatformBranches / TestAdaptersPackageDeleted 完全等价。
  重复是为每个文件自带完整契约；任一处漂移另一处也可见。
"""

from __future__ import annotations

import inspect
import os


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
        assert not os.path.exists("app/gateway/adapters"), "app/gateway/adapters/ 仍存在——Task 5 须整目录删除"


class TestSystemGatewaysDirDeleted:
    """plugins/system/gateways/ 已随 6 platform 模块迁至社区仓（E2 Task 6）"""

    def test_system_gateways_dir_absent(self):
        assert not os.path.exists("plugins/system/gateways"), (
            "plugins/system/gateways/ 仍存在——6 platform 模块须随仓迁出"
        )
