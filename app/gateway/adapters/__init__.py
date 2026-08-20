# -*- coding: utf-8 -*-
"""
Gateway 平台适配器

每个平台的 import 用 try/except 包裹，让单一平台缺包时：
- 不影响其他平台注册
- 不影响整个 gateway 子包加载
- 缺失的平台符号在 __all__ 中暴露为 None，由调用方通过 check_xxx_requirements() 判定

历史教训（2026-06-16）：曾因 dingtalk.py 模块顶层 eager import dingtalk_stream，
导致 `import app.gateway.adapters.dingtalk` 在缺包时抛 ModuleNotFoundError，
进而让 `app.gateway.adapters` 整个包加载失败，连带 manager / config / 整个
ChatBackend._init_gateway_async 失败。现已修复：平台模块改为真延迟导入，
本文件再加一层防御性 try/except 作为纵深。
"""
import importlib

from loguru import logger


def _try_import(platform_name: str, module_path: str, names: list) -> list:
    """
    从指定模块导入一组符号。

    Args:
        platform_name: 平台名（用于日志）
        module_path: 完整模块路径，如 "app.gateway.adapters.dingtalk"
        names: 期望导入的符号名列表

    Returns:
        与 names 等长的列表：成功位置为符号本身，失败位置为 None。
        整个模块导入失败时返回全 None 列表。
    """
    try:
        module = importlib.import_module(module_path)
    except ImportError as e:
        logger.warning(
            "[Gateway] {} 适配器模块加载失败（依赖缺失）: {}",
            platform_name, e,
        )
        return [None] * len(names)
    return [getattr(module, name, None) for name in names]


# 企业微信
(
    WeComAdapter,
    check_wecom_requirements,
) = _try_import(
    "WeCom", "app.gateway.adapters.wecom",
    ["WeComAdapter", "check_wecom_requirements"],
)

# 钉钉
(
    DingTalkAdapter,
    check_dingtalk_requirements,
) = _try_import(
    "DingTalk", "app.gateway.adapters.dingtalk",
    ["DingTalkAdapter", "check_dingtalk_requirements"],
)

# Telegram（E2 Task 4：适配器迁出至 plugins/system/gateways/telegram.py；
# 此处仍占位 None 以保持 __all__ 符号稳定 — Task 5 统一清理）
(TelegramAdapter, check_telegram_requirements,) = (None, None)

# Discord
(
    DiscordAdapter,
    check_discord_requirements,
) = _try_import(
    "Discord", "app.gateway.adapters.discord",
    ["DiscordAdapter", "check_discord_requirements"],
)

# 飞书
(
    FeishuAdapter,
    check_feishu_requirements,
) = _try_import(
    "Feishu", "app.gateway.adapters.feishu",
    ["FeishuAdapter", "check_feishu_requirements"],
)

# Slack
(SlackAdapter,) = _try_import(
    "Slack", "app.gateway.adapters.extra",
    ["SlackAdapter"],
)


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
