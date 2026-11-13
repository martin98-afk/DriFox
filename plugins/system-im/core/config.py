# -*- coding: utf-8 -*-
"""
Gateway 配置管理

E2 Task 5：四方法全部 registry 分派，删除全部 Platform.X if-elif 硬编码段。
内置平台 def.config_builder / config_writer 闭包读主程序 Settings（存量用户
配置零迁移）；第三方平台按各自 def 实现读写。
"""

from __future__ import annotations

from loguru import logger

from app.gateway.base import PlatformConfig, _platform_key


def get_gateway_config() -> "GatewayConfigHelper":
    """获取 Gateway 配置辅助类"""
    return GatewayConfigHelper()


class GatewayConfigHelper:
    """Gateway 配置辅助类（registry 分派，无平台 if-elif）"""

    @staticmethod
    def get_platform_config(platform) -> PlatformConfig:
        """获取平台配置（registry def.config_builder，无则返回 disabled 默认值）"""
        from app.plugins.registries.gateway_platform_registry import (
            GatewayPlatformRegistry,
        )

        d = GatewayPlatformRegistry.get_instance().get(_platform_key(platform))
        if d is not None and d.config_builder is not None:
            return d.config_builder()
        return PlatformConfig(enabled=False, platform=platform)

    @staticmethod
    def set_platform_config(platform, config: PlatformConfig) -> None:
        """设置平台配置（registry def.config_writer，缺失则 warning）"""
        from app.plugins.registries.gateway_platform_registry import (
            GatewayPlatformRegistry,
        )

        d = GatewayPlatformRegistry.get_instance().get(_platform_key(platform))
        if d is None:
            logger.warning(f"[GatewayConfig] 未注册平台: {_platform_key(platform)}")
            return
        if d.config_writer is None:
            logger.warning(f"[GatewayConfig] {d.display_name} 无 config_writer，跳过")
            return
        d.config_writer(config)

    @staticmethod
    def is_platform_enabled(platform) -> bool:
        """检查平台是否启用（读 def.config_builder().enabled，缺失则 False）"""
        cfg = GatewayConfigHelper.get_platform_config(platform)
        return bool(cfg.enabled)

    @staticmethod
    def set_platform_enabled(platform, enabled: bool) -> None:
        """设置平台启用状态（registry def.config_writer，缺失则 warning）"""
        from app.plugins.registries.gateway_platform_registry import (
            GatewayPlatformRegistry,
        )

        d = GatewayPlatformRegistry.get_instance().get(_platform_key(platform))
        if d is None:
            logger.warning(f"[GatewayConfig] 未注册平台: {_platform_key(platform)}")
            return
        if d.config_builder is None or d.config_writer is None:
            logger.warning(f"[GatewayConfig] {d.display_name} 缺 builder/writer，无法切换启用状态")
            return
        # 行为等价于旧 Settings 段：仅切换 enabled 字段，其它字段保留
        cfg = d.config_builder()
        cfg.enabled = bool(enabled)
        d.config_writer(cfg)
