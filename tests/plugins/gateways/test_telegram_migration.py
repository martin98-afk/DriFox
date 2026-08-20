# -*- coding: utf-8 -*-
"""Telegram 试点迁移：def 注册齐备 + manager/config 查表 + 行为等价。

E2 Task 4：TelegramAdapter 迁出 app/gateway/adapters/telegram.py 至
plugins/system/gateways/telegram.py（register 暴露 GatewayPlatformDef）。
manager/config 仅对 Telegram 走 registry 优先；其余 5 平台保留旧内置段。
"""
from __future__ import annotations

import pytest


class _FakeSettings:
    """Settings 桩：gateway_<platform>_<field> → .value 访问"""

    def __init__(self, gateway: dict):
        self._gw = gateway

    def __getattr__(self, item: str):
        if item.startswith("gateway_"):
            rest = item[len("gateway_"):]
            platform, _, field = rest.partition("_")
            val = self._gw.get(f"{platform}_{field}")
            return _Val(val)
        raise AttributeError(item)


class _Val:
    def __init__(self, v):
        self.value = v


@pytest.fixture()
def telegram_def_registered():
    """手动执行 system 插件 telegram 注册（集成环境由 loader 自动完成）"""
    import plugins.system.gateways.telegram as tg
    from app.plugins.registries.gateway_platform_registry import GatewayPlatformRegistry

    reg = GatewayPlatformRegistry.get_instance()
    tg.register(reg)
    yield reg
    # 清理：手动 register 用 source="" 注册（未走 proxy），按当前实现
    # unregister_source("") 会清掉所有 source 为空的项；为避免误清其他
    # 第三方平台 def（理论不应存在），这里改为按 id 移除。
    from app.plugins.contracts.gateway_platform import GatewayPlatformDef
    # 直接重置：frozen dataclass 的 dict 替换
    with reg._lock:
        reg._defs.pop("telegram", None)


class TestTelegramDef:
    def test_def_registered_with_full_callbacks(self, telegram_def_registered):
        d = telegram_def_registered.get("telegram")
        assert d is not None
        assert d.display_name == "Telegram"
        assert d.config_builder is not None
        assert d.config_writer is not None
        assert d.build_config_values is not None
        assert d.check_requirements is not None
        # 手动 register 时 source 仍为空字符串（未走 loader proxy 强制）
        assert d.source == ""


class TestConfigEquivalent:
    def test_build_reads_settings(self, telegram_def_registered, monkeypatch, tmp_path):
        """config_builder 等价于旧 config.py TELEGRAM 段（读 Settings）"""
        from app.gateway.base import Platform, PlatformConfig
        from app.gateway.config import GatewayConfigHelper

        monkeypatch.setattr(
            "app.utils.config.Settings.get_instance",
            lambda: _FakeSettings(
                gateway={
                    "telegram_enabled": True,
                    "telegram_token": "T123",
                    "telegram_require_mention": False,
                }
            ),
        )
        cfg = GatewayConfigHelper.get_platform_config(Platform.TELEGRAM)
        assert isinstance(cfg, PlatformConfig)
        assert cfg.platform == Platform.TELEGRAM
        assert cfg.enabled is True
        assert cfg.token == "T123"
        assert cfg.extra["require_mention"] is False


class TestManagerRegistryPriority:
    """manager._load_adapters 对已注册平台走 registry 优先（不再读旧 adapters 段）"""

    def test_telegram_registered_yields_def_path(self, telegram_def_registered, monkeypatch):
        """telegram 已注册 → manager 头部循环命中 platform_id='telegram'，
        不依赖 adapters 模块的 TelegramAdapter 符号（迁移后该符号为 None）。
        本测试验：def 已注册且 platform_id 可被 registry 路由查找到。
        """
        from app.plugins.registries.gateway_platform_registry import GatewayPlatformRegistry

        d = GatewayPlatformRegistry.get_instance().get("telegram")
        assert d is not None
        assert d.adapter_factory is not None
        # adapter_factory 必须是可调用的（lambda cfg: TelegramAdapter(cfg)）
        # 直接调用工厂会触发 import — 这里仅验存在
        assert callable(d.adapter_factory)