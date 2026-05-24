# -*- coding: utf-8 -*-
"""
Gateway 平台适配器
"""
from app.gateway.adapters.wecom import WeComAdapter, check_wecom_requirements
from app.gateway.adapters.dingtalk import DingTalkAdapter, check_dingtalk_requirements
from app.gateway.adapters.telegram import TelegramAdapter, check_telegram_requirements
from app.gateway.adapters.discord import DiscordAdapter, check_discord_requirements
from app.gateway.adapters.feishu import FeishuAdapter, check_feishu_requirements
from app.gateway.adapters.extra import SlackAdapter

# 检查依赖函数
__all__ = [
    # 企业微信
    "WeComAdapter",
    "check_wecom_requirements",
    # 钉钉
    "DingTalkAdapter",
    "check_dingtalk_requirements",
    # Telegram
    "TelegramAdapter",
    "check_telegram_requirements",
    # Discord
    "DiscordAdapter",
    "check_discord_requirements",
    # 飞书
    "FeishuAdapter",
    "check_feishu_requirements",
    # Slack
    "SlackAdapter",
]