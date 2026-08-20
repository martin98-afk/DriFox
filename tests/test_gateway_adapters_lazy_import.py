# -*- coding: utf-8 -*-
"""
回归测试：Gateway 框架加载链路不受平台适配器缺失影响。

历史背景（2026-06-16）：
- bug: app/gateway/adapters/dingtalk.py 在模块顶层调用
  `_ensure_dingtalk_imports()` 和 `_patch_dingtalk_stream_logging()`，
  这两个调用都是无 try/except 的 eager import。
- 现象：环境里若未安装 dingtalk-stream（gateway extra 未装），
  `import app.gateway.adapters.dingtalk` 抛 ModuleNotFoundError，
  进而让 adapters 包、manager、config、整个 ChatBackend._init_gateway_async
  链路全部失败。
- 修复（多次迭代）：
  1. dingtalk.py 改为真延迟导入；
  2. adapters/__init__.py 每个平台用 importlib + try/except 包裹；
  3. adapters/ 整目录删除，平台模块迁至 plugins/system/gateways/<id>.py；
  4. （当前）plugins/system/gateways/ 整目录迁至社区仓 drifox-plugins2，
     主仓零 platform 模块。

E2 Task 6 适配（主仓清理）：
- 全部 platform 模块断言已随平台插件迁往社区仓 drifox-plugins2
  （plugins/gateway-{telegram,wecom,dingtalk,discord,feishu,slack}/）。
- 本文件保留的「主程序加载链不受 platform 适配器缺失影响」不变量测试：
  manager / config 可导入；registry 在无任何 platform 插件时仍为空且不报错。
- 钉钉 send_image 死代码锁已随钉钉适配器迁出。
"""

import importlib
import sys

import pytest


class TestGatewaySubpackageLoading:
    """主程序 gateway 子包加载链路在缺平台插件时也应工作"""

    def test_gateway_config_importable(self):
        """app.gateway.config 可加载（无 platform 适配器时仍正常）"""
        sys.modules.pop("app.gateway.config", None)
        config = importlib.import_module("app.gateway.config")
        assert hasattr(config, "get_gateway_config")

    def test_gateway_manager_importable(self):
        """app.gateway.manager 可加载（无 platform 适配器时仍正常）"""
        sys.modules.pop("app.gateway.manager", None)
        manager = importlib.import_module("app.gateway.manager")
        assert hasattr(manager, "create_platform_manager")


class TestRegistryEmptyWithoutBuiltinPlatforms:
    """主仓不再自带任何 platform adapter，registry 启动应为空（社区插件由用户仓根注入）"""

    def test_registry_default_empty(self):
        """fresh GatewayPlatformRegistry 不含任何内置平台 id"""
        from app.plugins.registries.gateway_platform_registry import GatewayPlatformRegistry

        reg = GatewayPlatformRegistry()
        ids = {d.platform_id for d in reg.list_platforms()}
        # 主仓零 platform 适配器（6 个内置平台已全部迁往社区仓）
        assert ids == set(), f"主仓不应再有内置 platform 定义，实际: {ids}"

    def test_registry_unregister_unknown_is_noop(self):
        """unregister_source 不存在的 plugin 静默成功，不抛异常"""
        from app.plugins.registries.gateway_platform_registry import GatewayPlatformRegistry

        reg = GatewayPlatformRegistry()
        # 不应抛任何异常
        reg.unregister_source("plugin:nonexistent")
        assert reg.list_platforms() == []
