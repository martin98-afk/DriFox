# -*- coding: utf-8 -*-
"""
Gateway 配置管理

使用统一的 Settings 配置管理。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from app.gateway.base import Platform, PlatformConfig


def get_gateway_config() -> "GatewayConfigHelper":
    """获取 Gateway 配置辅助类"""
    return GatewayConfigHelper()


class GatewayConfigHelper:
    """
    Gateway 配置辅助类
    
    封装统一的 Settings 配置，提供便捷的访问接口。
    """
    
    @staticmethod
    def get_platform_config(platform: Platform) -> PlatformConfig:
        """
        获取平台配置
        
        Args:
            platform: 平台枚举
            
        Returns:
            PlatformConfig
        """
        from app.utils.config import Settings
        cfg = Settings.get_instance()
        
        if platform == Platform.WECOM:
            return PlatformConfig(
                enabled=cfg.gateway_wecom_enabled.value,
                platform=Platform.WECOM,
                bot_id=cfg.gateway_wecom_bot_id.value,
                secret=cfg.gateway_wecom_secret.value,
                websocket_url=cfg.gateway_wecom_websocket_url.value,
            )
        elif platform == Platform.DINGTALK:
            return PlatformConfig(
                enabled=cfg.gateway_dingtalk_enabled.value,
                platform=Platform.DINGTALK,
                client_id=cfg.gateway_dingtalk_client_id.value,
                client_secret=cfg.gateway_dingtalk_client_secret.value,
            )
        elif platform == Platform.TELEGRAM:
            return PlatformConfig(
                enabled=cfg.gateway_telegram_enabled.value,
                platform=Platform.TELEGRAM,
                token=cfg.gateway_telegram_token.value,
                extra={
                    "require_mention": cfg.gateway_telegram_require_mention.value,
                },
            )
        elif platform == Platform.DISCORD:
            return PlatformConfig(
                enabled=cfg.gateway_discord_enabled.value,
                platform=Platform.DISCORD,
                token=cfg.gateway_discord_token.value,
                extra={
                    "require_mention": cfg.gateway_discord_require_mention.value,
                },
            )
        elif platform == Platform.WHATSAPP:
            return PlatformConfig(
                enabled=cfg.gateway_whatsapp_enabled.value,
                platform=Platform.WHATSAPP,
                extra={
                    "account_sid": cfg.gateway_whatsapp_account_sid.value,
                    "auth_token": cfg.gateway_whatsapp_auth_token.value,
                    "from_number": cfg.gateway_whatsapp_from_number.value,
                },
            )
        elif platform == Platform.FEISHU:
            return PlatformConfig(
                enabled=cfg.gateway_feishu_enabled.value,
                platform=Platform.FEISHU,
                extra={
                    "app_id": cfg.gateway_feishu_app_id.value,
                    "app_secret": cfg.gateway_feishu_app_secret.value,
                },
            )
        elif platform == Platform.SLACK:
            return PlatformConfig(
                enabled=cfg.gateway_slack_enabled.value,
                platform=Platform.SLACK,
                extra={
                    "bot_token": cfg.gateway_slack_bot_token.value,
                    "app_token": cfg.gateway_slack_app_token.value,
                },
            )
        else:
            return PlatformConfig(enabled=False, platform=platform)
    
    @staticmethod
    def set_platform_config(platform: Platform, config: PlatformConfig) -> None:
        """
        设置平台配置
        
        Args:
            platform: 平台枚举
            config: 平台配置
        """
        from app.utils.config import Settings
        cfg = Settings.get_instance()
        
        if platform == Platform.WECOM:
            cfg.set(cfg.gateway_wecom_enabled, config.enabled, save=True)
            if config.bot_id is not None:
                cfg.set(cfg.gateway_wecom_bot_id, config.bot_id, save=True)
            if config.secret is not None:
                cfg.set(cfg.gateway_wecom_secret, config.secret, save=True)
            if config.websocket_url is not None:
                cfg.set(cfg.gateway_wecom_websocket_url, config.websocket_url, save=True)
        elif platform == Platform.DINGTALK:
            cfg.set(cfg.gateway_dingtalk_enabled, config.enabled, save=True)
            if config.client_id is not None:
                cfg.set(cfg.gateway_dingtalk_client_id, config.client_id, save=True)
            if config.client_secret is not None:
                cfg.set(cfg.gateway_dingtalk_client_secret, config.client_secret, save=True)
        elif platform == Platform.TELEGRAM:
            cfg.set(cfg.gateway_telegram_enabled, config.enabled, save=True)
            if config.token is not None:
                cfg.set(cfg.gateway_telegram_token, config.token, save=True)
            if config.extra:
                cfg.set(cfg.gateway_telegram_require_mention, config.extra.get("require_mention", True), save=True)
        elif platform == Platform.DISCORD:
            cfg.set(cfg.gateway_discord_enabled, config.enabled, save=True)
            if config.token is not None:
                cfg.set(cfg.gateway_discord_token, config.token, save=True)
            if config.extra:
                cfg.set(cfg.gateway_discord_require_mention, config.extra.get("require_mention", True), save=True)
        elif platform == Platform.WHATSAPP:
            cfg.set(cfg.gateway_whatsapp_enabled, config.enabled, save=True)
            if config.extra:
                if config.extra.get("account_sid") is not None:
                    cfg.set(cfg.gateway_whatsapp_account_sid, config.extra["account_sid"], save=True)
                if config.extra.get("auth_token") is not None:
                    cfg.set(cfg.gateway_whatsapp_auth_token, config.extra["auth_token"], save=True)
                if config.extra.get("from_number") is not None:
                    cfg.set(cfg.gateway_whatsapp_from_number, config.extra["from_number"], save=True)
        elif platform == Platform.FEISHU:
            cfg.set(cfg.gateway_feishu_enabled, config.enabled, save=True)
            if config.extra:
                if config.extra.get("app_id") is not None:
                    cfg.set(cfg.gateway_feishu_app_id, config.extra["app_id"], save=True)
                if config.extra.get("app_secret") is not None:
                    cfg.set(cfg.gateway_feishu_app_secret, config.extra["app_secret"], save=True)
        elif platform == Platform.SLACK:
            cfg.set(cfg.gateway_slack_enabled, config.enabled, save=True)
            if config.extra:
                if config.extra.get("bot_token") is not None:
                    cfg.set(cfg.gateway_slack_bot_token, config.extra["bot_token"], save=True)
                if config.extra.get("app_token") is not None:
                    cfg.set(cfg.gateway_slack_app_token, config.extra["app_token"], save=True)
    
    @staticmethod
    def is_platform_enabled(platform: Platform) -> bool:
        """检查平台是否启用"""
        from app.utils.config import Settings
        cfg = Settings.get_instance()
        
        if platform == Platform.WECOM:
            return cfg.gateway_wecom_enabled.value
        elif platform == Platform.DINGTALK:
            return cfg.gateway_dingtalk_enabled.value
        elif platform == Platform.TELEGRAM:
            return cfg.gateway_telegram_enabled.value
        elif platform == Platform.DISCORD:
            return cfg.gateway_discord_enabled.value
        elif platform == Platform.WHATSAPP:
            return cfg.gateway_whatsapp_enabled.value
        elif platform == Platform.FEISHU:
            return cfg.gateway_feishu_enabled.value
        elif platform == Platform.SLACK:
            return cfg.gateway_slack_enabled.value
        return False
    
    @staticmethod
    def set_platform_enabled(platform: Platform, enabled: bool) -> None:
        """设置平台启用状态"""
        from app.utils.config import Settings
        cfg = Settings.get_instance()
        
        if platform == Platform.WECOM:
            cfg.set(cfg.gateway_wecom_enabled, enabled, save=True)
        elif platform == Platform.DINGTALK:
            cfg.set(cfg.gateway_dingtalk_enabled, enabled, save=True)
        elif platform == Platform.TELEGRAM:
            cfg.set(cfg.gateway_telegram_enabled, enabled, save=True)
        elif platform == Platform.DISCORD:
            cfg.set(cfg.gateway_discord_enabled, enabled, save=True)
        elif platform == Platform.WHATSAPP:
            cfg.set(cfg.gateway_whatsapp_enabled, enabled, save=True)
        elif platform == Platform.FEISHU:
            cfg.set(cfg.gateway_feishu_enabled, enabled, save=True)
        elif platform == Platform.SLACK:
            cfg.set(cfg.gateway_slack_enabled, enabled, save=True)