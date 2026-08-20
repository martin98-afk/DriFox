# -*- coding: utf-8 -*-
"""
Discord 平台适配器（系统插件，万物即插件 Phase E）

使用 discord.py 库进行消息收发。

本文件原位于 app/gateway/adapters/discord.py（E2 Task 5 迁入）。
适配器实现保持原状（SDK 延迟导入纪律：模块顶层不 eager import 平台 SDK）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from loguru import logger

from app.gateway.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    Platform,
    PlatformConfig,
    SendResult,
)

# 延迟导入
DISCORD_AVAILABLE = False


def check_discord_requirements() -> bool:
    """检查 Discord 依赖是否可用"""
    global DISCORD_AVAILABLE
    if DISCORD_AVAILABLE:
        return True
    try:
        import discord

        DISCORD_AVAILABLE = True
        return True
    except ImportError:
        logger.error("[Discord] discord.py not installed. Run: pip install discord.py")
        return False


class DiscordAdapter(BasePlatformAdapter):
    """
    Discord 机器人适配器

    支持：
    - 私聊和服务器频道
    - 线程 (Threads)
    - 媒体消息
    - Slash Commands
    """

    MAX_MESSAGE_LENGTH = 2000  # Discord 消息长度限制

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.DISCORD)
        self._client = None
        self._guilds = {}

        # 群组触发配置
        self._require_mention = config.extra.get("require_mention", True)

    async def connect(self) -> bool:
        """连接到 Discord"""
        if not check_discord_requirements():
            logger.error("[Discord] Dependencies not available")
            self._last_error = "依赖不可用"
            return False

        if not self.config.token:
            logger.error("[Discord] No bot token configured")
            self._last_error = "未配置 Bot Token"
            return False

        try:
            import discord
            from discord import Intents

            # 创建意图
            intents = Intents.default()
            intents.message_content = True  # 需要开启 Message Content Intent
            intents.guilds = True
            intents.messages = True

            # 创建客户端
            self._client = discord.Client(intents=intents)

            # 注册事件
            @self._client.event
            async def on_message(message):
                await self._handle_message(message)

            @self._client.event
            async def on_ready():
                logger.info(f"[Discord] Logged in as {self._client.user}")
                self._connected = True

            # 登录
            await self._client.start(self.config.token)

        except Exception as e:
            logger.error(f"[Discord] Failed to connect: {e}")
            return False

    async def _handle_message(self, message):
        """处理收到的消息"""
        # 忽略机器人消息
        if message.author.bot:
            return

        # 忽略系统消息
        if message.type not in (
            None,
            discord.MessageType.default,
            discord.MessageType.reply,
        ):
            return

        # 检查是否需要 @ 机器人
        if self._require_mention and not self._bot_mentioned(message):
            return

        # 确定消息类型
        if message.content and message.content.startswith("/"):
            msg_type = MessageType.COMMAND
        elif message.attachments:
            if any(a.content_type and a.content_type.startswith("image/") for a in message.attachments):
                msg_type = MessageType.IMAGE
            else:
                msg_type = MessageType.FILE
        else:
            msg_type = MessageType.TEXT

        # 构建事件
        event = self._build_message_event(message, msg_type)
        await self.handle_message(event)

    def _bot_mentioned(self, message) -> bool:
        """检查消息是否 @ 机器人"""
        if not self._client or not self._client.user:
            return True  # 没有用户信息，默认处理

        # 检查是否直接回复机器人
        if message.reference:
            replied_msg = message.reference.cached_message
            if replied_msg and replied_msg.author.id == self._client.user.id:
                return True

        # 检查内容是否包含机器人 mention
        if hasattr(message, "mentions") and self._client.user in message.mentions:
            return True

        # 检查是否在 DM 中
        if isinstance(message.channel, discord.DMChannel):
            return True

        return False

    def _build_message_event(self, message, msg_type: MessageType) -> MessageEvent:
        """构建消息事件"""
        # 提取媒体
        media_urls = []
        media_types = []

        for attachment in message.attachments:
            if attachment.url:
                media_urls.append(attachment.url)
                media_types.append(attachment.content_type or "file")

        # 用户名
        user_name = message.author.display_name or message.author.name

        return MessageEvent(
            text=message.content or "",
            message_type=msg_type,
            message_id=str(message.id),
            chat_id=str(message.channel.id),
            user_id=str(message.author.id),
            user_name=user_name,
            platform=Platform.DISCORD,
            chat_type="dm" if isinstance(message.channel, discord.DMChannel) else "group",
            media_urls=media_urls,
            media_types=media_types,
            metadata={
                "guild_id": str(message.guild.id) if message.guild else "",
                "channel_name": message.channel.name,
            },
        )

    async def disconnect(self) -> None:
        """断开连接"""
        if self._client:
            try:
                await self._client.close()
            except Exception as e:
                logger.warning(f"[Discord] Error during disconnect: {e}")

        self._connected = False
        logger.info("[Discord] Disconnected")

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """发送消息"""
        if not self._client:
            return SendResult(success=False, error="Not connected")

        try:
            channel = self._client.get_channel(int(chat_id))
            if not channel:
                return SendResult(success=False, error=f"Channel {chat_id} not found")

            # 分割长消息
            if len(content) > self.MAX_MESSAGE_LENGTH:
                chunks = self._split_message(content)
            else:
                chunks = [content]

            message_ids = []

            for i, chunk in enumerate(chunks):
                if len(chunks) > 1:
                    chunk = f"[{i + 1}/{len(chunks)}] {chunk}"

                msg = await channel.send(content=chunk)
                message_ids.append(str(msg.id))

            return SendResult(
                success=True,
                message_id=message_ids[0] if message_ids else None,
            )

        except Exception as e:
            logger.error(f"[Discord] Send failed: {e}", exc_info=True)
            return SendResult(success=False, error=str(e))

    def _split_message(self, content: str) -> List[str]:
        """分割消息"""
        if len(content) <= self.MAX_MESSAGE_LENGTH:
            return [content]

        chunks = []
        paragraphs = content.split("\n\n")
        current = ""

        for para in paragraphs:
            if len(current) + len(para) + 2 <= self.MAX_MESSAGE_LENGTH:
                current += ("\n\n" if current else "") + para
            else:
                if current:
                    chunks.append(current)
                current = para

        if current:
            chunks.append(current)

        return chunks if chunks else [content]

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """发送图片"""
        if not self._client:
            return SendResult(success=False, error="Not connected")

        try:
            import discord

            channel = self._client.get_channel(int(chat_id))
            if not channel:
                return SendResult(success=False, error=f"Channel {chat_id} not found")

            # Discord embed 格式
            embed = discord.Embed()
            if image_url.startswith("http"):
                embed.set_image(url=image_url)
            else:
                # 本地路径
                embed.set_image(url=f"attachment://{image_url.split('/')[-1]}")

            if caption:
                embed.description = caption

            if image_url.startswith("http"):
                msg = await channel.send(embed=embed)
            else:
                # 本地文件 - 上传为附件
                import discord as dc

                file = dc.File(image_url)
                msg = await channel.send(file=file, embed=embed)

            return SendResult(success=True, message_id=str(msg.id))

        except Exception as e:
            logger.error(f"[Discord] Send image failed: {e}", exc_info=True)
            return SendResult(success=False, error=str(e))

    async def send_typing(self, chat_id: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """发送 typing 状态"""
        if self._client:
            try:
                channel = self._client.get_channel(int(chat_id))
                if channel:
                    async with channel.typing():
                        pass
            except Exception:
                pass  # Typing failures are non-fatal

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """获取聊天信息"""
        if not self._client:
            return {"name": "Unknown", "type": "dm"}

        try:
            import discord

            channel = self._client.get_channel(int(chat_id))
            if channel:
                return {
                    "name": channel.name or str(chat_id),
                    "type": "dm" if isinstance(channel, discord.DMChannel) else "group",
                }
        except Exception as e:
            logger.error(f"[Discord] get_chat_info failed: {e}")

        return {"name": str(chat_id), "type": "dm"}


# ── Phase E 插件注册 ────────────────────────────────────
# 配置读写回调走主程序 Settings（存量用户配置零迁移；Task 5 统一切 E1
# PluginConfigStore 时仅改本块闭包，调用方不动）。闭包内延迟 import
# Settings/PlatformConfig，避免模块顶层触发 PyQt5 / Settings 副作用。


def _build_config() -> "PlatformConfig":
    """读主程序 Settings 构造 Discord 配置（存量用户配置零迁移）"""
    from app.gateway.base import Platform, PlatformConfig
    from app.utils.config import Settings

    cfg = Settings.get_instance()
    return PlatformConfig(
        enabled=cfg.gateway_discord_enabled.value,
        platform=Platform.DISCORD,
        token=cfg.gateway_discord_token.value,
        extra={"require_mention": cfg.gateway_discord_require_mention.value},
    )


def _write_config(config: "PlatformConfig") -> None:
    from app.utils.config import Settings

    cfg = Settings.get_instance()
    cfg.set(cfg.gateway_discord_enabled, config.enabled, save=True)
    if config.token is not None:
        cfg.set(cfg.gateway_discord_token, config.token, save=True)
    if config.extra:
        cfg.set(
            cfg.gateway_discord_require_mention,
            config.extra.get("require_mention", True),
            save=True,
        )


def _build_config_values(values: dict, old_config) -> "PlatformConfig":
    """设置卡保存回调：表单值 → PlatformConfig（对齐旧 _on_save DISCORD 分支）"""
    from app.gateway.base import Platform, PlatformConfig

    extra = dict(old_config.extra) if old_config and old_config.extra else {}
    if "require_mention" in values:
        extra["require_mention"] = bool(values["require_mention"])
    return PlatformConfig(
        enabled=bool(values.get("enabled", False)),
        platform=Platform.DISCORD,
        token=values.get("token") or "",
        extra=extra,
    )


def register(registry) -> None:
    from app.plugins.contracts.gateway_platform import GatewayPlatformDef

    registry.register(
        GatewayPlatformDef(
            platform_id="discord",
            display_name="Discord",
            adapter_factory=lambda cfg: DiscordAdapter(cfg),
            check_requirements=check_discord_requirements,
            config_builder=_build_config,
            config_writer=_write_config,
            build_config_values=_build_config_values,
            validate_config=lambda cfg: (bool(cfg.token), "Token 未配置"),
            ui_order=40,
        )
    )
