# -*- coding: utf-8 -*-
"""
Discord 平台适配器

使用 discord.py 库进行消息收发。
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, List, Optional

from loguru import logger

from app.gateway.base import (
    BasePlatformAdapter,
    Platform,
    PlatformConfig,
    MessageEvent,
    MessageType,
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
        if message.content and message.content.startswith('/'):
            msg_type = MessageType.COMMAND
        elif message.attachments:
            if any(a.content_type and a.content_type.startswith('image/') for a in message.attachments):
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
        """从 Discord 消息构建 MessageEvent"""
        channel = message.channel
        
        # 确定聊天类型
        if isinstance(channel, discord.DMChannel):
            chat_type = "dm"
            chat_id = str(channel.id)
        elif isinstance(channel, discord.Thread):
            chat_type = "thread"
            chat_id = str(channel.parent_id)
        else:
            chat_type = "group"
            chat_id = str(channel.id)
        
        # 提取媒体 URL
        media_urls = []
        media_types = []
        for attachment in message.attachments:
            if attachment.content_type:
                media_types.append(attachment.content_type)
                media_urls.append(attachment.url)
        
        # 线程 ID
        thread_id = None
        if isinstance(channel, discord.Thread):
            thread_id = str(channel.id)
        
        return MessageEvent(
            text=message.content or "",
            message_type=msg_type,
            message_id=str(message.id),
            chat_id=chat_id,
            user_id=str(message.author.id),
            user_name=str(message.author),
            platform=Platform.DISCORD,
            chat_type=chat_type,
            media_urls=media_urls,
            media_types=media_types,
            metadata={
                "thread_id": thread_id,
                "guild_id": str(message.guild.id) if message.guild else None,
            } if thread_id or message.guild else {},
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
            import discord
            
            # 获取频道
            channel = self._client.get_channel(int(chat_id))
            if not channel:
                return SendResult(success=False, error="Channel not found")
            
            # 分割长消息
            chunks = self._split_message(content)
            
            message_ids = []
            prev_msg = None
            
            for i, chunk in enumerate(chunks):
                kwargs = {"content": chunk}
                
                # 如果是第一条消息且有回复目标
                if i == 0 and reply_to:
                    try:
                        kwargs["reference"] = await channel.fetch_message(int(reply_to))
                    except Exception:
                        pass
                
                msg = await channel.send(**kwargs)
                message_ids.append(str(msg.id))
                prev_msg = msg
            
            return SendResult(
                success=True,
                message_id=message_ids[0] if message_ids else None,
            )
            
        except Exception as e:
            logger.error(f"[Discord] Send failed: {e}")
            return SendResult(success=False, error=str(e))
    
    def _split_message(self, content: str) -> List[str]:
        """分割消息"""
        if len(content) <= self.MAX_MESSAGE_LENGTH:
            return [content]
        
        chunks = []
        lines = content.split('\n')
        current = ""
        
        for line in lines:
            if len(current) + len(line) + 1 <= self.MAX_MESSAGE_LENGTH:
                current += ("\n" if current else "") + line
            else:
                if current:
                    chunks.append(current)
                # 如果单行超过限制，强制分割
                while len(line) > self.MAX_MESSAGE_LENGTH:
                    chunks.append(line[:self.MAX_MESSAGE_LENGTH])
                    line = line[self.MAX_MESSAGE_LENGTH:]
                current = line
        
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
            channel = self._client.get_channel(int(chat_id))
            if not channel:
                return SendResult(success=False, error="Channel not found")
            
            kwargs = {}
            if caption:
                kwargs["content"] = caption
            
            if image_url.startswith("http"):
                kwargs["files"] = []  # 需要下载后发送
            else:
                # 本地文件
                import discord
                kwargs["file"] = discord.File(image_url)
            
            msg = await channel.send(**kwargs)
            return SendResult(success=True, message_id=str(msg.id))
            
        except Exception as e:
            logger.error(f"[Discord] Send image failed: {e}")
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
            channel = self._client.get_channel(int(chat_id))
            if channel:
                return {
                    "name": channel.name or str(chat_id),
                    "type": "dm" if isinstance(channel, discord.DMChannel) else "group",
                }
        except Exception as e:
            logger.error(f"[Discord] get_chat_info failed: {e}")
        
        return {"name": str(chat_id), "type": "dm"}