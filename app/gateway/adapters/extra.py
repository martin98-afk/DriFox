# -*- coding: utf-8 -*-
"""
WhatsApp 平台适配器 (通过 Twilio)

使用 Twilio API 接收和发送 WhatsApp 消息。
"""
from __future__ import annotations

import asyncio
import json
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
TWILIO_AVAILABLE = False


def check_whatsapp_requirements() -> bool:
    """检查 WhatsApp (Twilio) 依赖是否可用"""
    global TWILIO_AVAILABLE
    if TWILIO_AVAILABLE:
        return True
    try:
        from twilio.rest import Client
        from twilio.twiml import MessagingResponse
        TWILIO_AVAILABLE = True
        return True
    except ImportError:
        logger.error("[WhatsApp] twilio not installed. Run: pip install twilio")
        return False


# class WhatsAppAdapter(BasePlatformAdapter):
#     """
#     WhatsApp 适配器 (通过 Twilio)
#
#     支持：
#     - 私聊消息
#     - 媒体消息 (图片、音频、视频)
#     - 24小时回复窗口
#     """
#
#     MAX_MESSAGE_LENGTH = 1600  # WhatsApp 消息长度限制
#
#     def __init__(self, config: PlatformConfig):
#         super().__init__(config, Platform.WHATSAPP)
#
#         self._client = None
#         self._from_number = None
#
#         # Twilio 配置
#         self._account_sid = config.extra.get("account_sid") or os.getenv("TWILIO_ACCOUNT_SID")
#         self._auth_token = config.extra.get("auth_token") or os.getenv("TWILIO_AUTH_TOKEN")
#         self._from_number = config.extra.get("from_number") or os.getenv("TWILIO_WHATSAPP_FROM")
#
#         # 会话存储 (Twilio 需要 24 小时内回复)
#         self._sessions: Dict[str, Dict] = {}
#
#     async def connect(self) -> bool:
#         """连接到 Twilio"""
#         if not check_whatsapp_requirements():
#             logger.error("[WhatsApp] Dependencies not available")
#             return False
#
#         if not self._account_sid or not self._auth_token:
#             logger.error("[WhatsApp] Twilio account_sid and auth_token are required")
#             return False
#
#         try:
#             from twilio.rest import Client
#             self._client = Client(self._account_sid, self._auth_token)
#
#             # 验证凭证
#             self._client.api.accounts(self._account_sid).fetch()
#
#             self._connected = True
#             logger.info("[WhatsApp] Connected to Twilio")
#             return True
#
#         except Exception as e:
#             logger.error("[WhatsApp] Failed to connect: %s", e)
#             return False
#
#     async def disconnect(self) -> None:
#         """断开连接"""
#         self._client = None
#         self._connected = False
#         logger.info("[WhatsApp] Disconnected")
#
#     async def send(
#         self,
#         chat_id: str,
#         content: str,
#         reply_to: Optional[str] = None,
#         metadata: Optional[Dict[str, Any]] = None,
#     ) -> SendResult:
#         """发送消息"""
#         if not self._client:
#             return SendResult(success=False, error="Not connected")
#
#         if not self._from_number:
#             return SendResult(success=False, error="from_number not configured")
#
#         try:
#             # 格式化内容
#             formatted = self._format_whatsapp_text(content)
#
#             # 分割长消息
#             if len(formatted) > self.MAX_MESSAGE_LENGTH:
#                 chunks = self._split_message(formatted)
#             else:
#                 chunks = [formatted]
#
#             message_ids = []
#             for i, chunk in enumerate(chunks):
#                 # 添加消息编号 (如果是多条)
#                 if len(chunks) > 1:
#                     chunk = f"[{i+1}/{len(chunks)}] {chunk}"
#
#                 message = self._client.messages.create(
#                     body=chunk,
#                     from_=f"whatsapp:{self._from_number}",
#                     to=f"whatsapp:{chat_id}",
#                 )
#                 message_ids.append(message.sid)
#
#             return SendResult(
#                 success=True,
#                 message_id=message_ids[0] if message_ids else None,
#                 raw_response={"sids": message_ids},
#             )
#
#         except Exception as e:
#             logger.error("[WhatsApp] Send failed: %s", e)
#             return SendResult(success=False, error=str(e))
#
#     def _format_whatsapp_text(self, content: str) -> str:
#         """格式化 WhatsApp 文本"""
#         if not content:
#             return content
#
#         # 移除 Markdown 特殊字符
#         special_chars = ['*', '_', '~', '`', '>']
#         result = content
#         for char in special_chars:
#             result = result.replace(char, '')
#
#         return result
#
#     def _split_message(self, content: str) -> List[str]:
#         """分割消息"""
#         if len(content) <= self.MAX_MESSAGE_LENGTH:
#             return [content]
#
#         chunks = []
#         # 按句子分割
#         sentences = content.split('. ')
#         current = ""
#
#         for sentence in sentences:
#             if len(current) + len(sentence) + 2 <= self.MAX_MESSAGE_LENGTH:
#                 current += (". " if current else "") + sentence
#             else:
#                 if current:
#                     chunks.append(current)
#                 current = sentence
#
#         if current:
#             chunks.append(current)
#
#         return chunks if chunks else [content]
#
#     async def send_image(
#         self,
#         chat_id: str,
#         image_url: str,
#         caption: Optional[str] = None,
#         reply_to: Optional[str] = None,
#         metadata: Optional[Dict[str, Any]] = None,
#     ) -> SendResult:
#         """发送图片"""
#         if not self._client:
#             return SendResult(success=False, error="Not connected")
#
#         try:
#             kwargs = {
#                 "from_": f"whatsapp:{self._from_number}",
#                 "to": f"whatsapp:{chat_id}",
#             }
#
#             if image_url.startswith("http"):
#                 kwargs["media_url"] = [image_url]
#             else:
#                 # 本地文件需要上传到公开 URL
#                 return SendResult(success=False, error="Local file not supported, use HTTP URL")
#
#             if caption:
#                 kwargs["body"] = caption
#
#             message = self._client.messages.create(**kwargs)
#             return SendResult(success=True, message_id=message.sid)
#
#         except Exception as e:
#             logger.error("[WhatsApp] Send image failed: %s", e)
#             return SendResult(success=False, error=str(e))
#
#     def handle_webhook(self, data: Dict[str, Any]) -> None:
#         """
#         处理 Twilio Webhook 回调
#
#         这个方法应该被 webhook 端点调用
#         """
#         if not data:
#             return
#
#         # 提取消息数据
#         from_number = data.get("From", "")
#         to_number = data.get("To", "")
#         body = data.get("Body", "")
#         num_media = int(data.get("NumMedia", 0))
#
#         # 清理号码
#         chat_id = from_number.replace("whatsapp:", "")
#
#         # 确定消息类型
#         if body and body.startswith('/'):
#             msg_type = MessageType.COMMAND
#         elif num_media > 0:
#             media_type = data.get("MediaContentType0", "")
#             if media_type.startswith("image/"):
#                 msg_type = MessageType.PHOTO
#             elif media_type.startswith("audio/"):
#                 msg_type = MessageType.AUDIO
#             elif media_type.startswith("video/"):
#                 msg_type = MessageType.VIDEO
#             else:
#                 msg_type = MessageType.DOCUMENT
#         else:
#             msg_type = MessageType.TEXT
#
#         # 提取媒体 URL
#         media_urls = []
#         media_types = []
#         for i in range(num_media):
#             media_url = data.get(f"MediaUrl{i}", "")
#             media_content_type = data.get(f"MediaContentType{i}", "")
#             if media_url:
#                 media_urls.append(media_url)
#                 media_types.append(media_content_type)
#
#         # 构建事件
#         event = MessageEvent(
#             text=body or "",
#             message_type=msg_type,
#             message_id=data.get("MessageSid", ""),
#             chat_id=chat_id,
#             user_id=chat_id,
#             user_name=chat_id,  # WhatsApp 没有用户名字段
#             platform=Platform.WHATSAPP,
#             chat_type="dm",
#             media_urls=media_urls,
#             media_types=media_types,
#             metadata={"from_number": from_number, "to_number": to_number},
#         )
#
#         # 在新的事件循环中处理
#         asyncio.create_task(self.handle_message(event))
#
#     async def send_typing(self, chat_id: str, metadata: Optional[Dict[str, Any]] = None) -> None:
#         """WhatsApp 不支持 typing 指示器"""
#         pass
#
#     async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
#         """获取聊天信息"""
#         return {"name": chat_id, "type": "dm"}


# ============================================================
# Slack 适配器
# ============================================================

class SlackAdapter(BasePlatformAdapter):
    """
    Slack 适配器
    
    使用 Slack WebSocket 模式进行消息收发。
    """
    
    MAX_MESSAGE_LENGTH = 4000
    
    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.SLACK)
        
        self._bot_token = config.extra.get("bot_token") or os.getenv("SLACK_BOT_TOKEN")
        self._app_token = config.extra.get("app_token") or os.getenv("SLACK_APP_TOKEN")
        self._client = None
    
    async def connect(self) -> bool:
        """连接到 Slack"""
        if not self._bot_token:
            logger.error("[Slack] bot_token is required")
            return False
        
        try:
            from slack_sdk import WebClient
            from slack_sdk.socket_mode import SocketModeClient
            
            # 创建 Web API 客户端
            self._client = WebClient(token=self._bot_token)
            
            # 验证 token
            self._client.auth_test()
            
            self._connected = True
            logger.info("[Slack] Connected successfully")
            return True
            
        except Exception as e:
            logger.error("[Slack] Failed to connect: %s", e)
            return False
    
    async def disconnect(self) -> None:
        """断开连接"""
        if self._client:
            try:
                if hasattr(self._client, '_session'):
                    import asyncio
                    # 清理资源
                    pass
            except Exception as e:
                logger.warning("[Slack] Error during disconnect: %s", e)
        
        self._connected = False
        logger.info("[Slack] Disconnected")
    
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
            # 格式化内容 (使用 Block Kit 格式)
            formatted = self._format_slack_text(content)
            
            # 分割长消息
            if len(formatted) > self.MAX_MESSAGE_LENGTH:
                chunks = self._split_message(formatted)
            else:
                chunks = [formatted]
            
            message_ids = []
            
            for i, chunk in enumerate(chunks):
                kwargs = {
                    "channel": chat_id,
                    "text": chunk,
                }
                
                # 添加线程回复
                if reply_to and i == 0:
                    kwargs["thread_ts"] = reply_to
                
                response = self._client.chat_postMessage(**kwargs)
                message_ids.append(response["ts"])
            
            return SendResult(
                success=True,
                message_id=message_ids[0] if message_ids else None,
            )
            
        except Exception as e:
            logger.error("[Slack] Send failed: %s", e)
            return SendResult(success=False, error=str(e))
    
    def _format_slack_text(self, content: str) -> str:
        """格式化 Slack 文本"""
        if not content:
            return content
        
        # Slack 文本格式
        result = content
        # 转换 Markdown 到 Slack 格式
        result = result.replace("**", "*")  # 粗体
        result = result.replace("__", "_")  # 斜体
        
        return result
    
    def _split_message(self, content: str) -> List[str]:
        """分割消息"""
        if len(content) <= self.MAX_MESSAGE_LENGTH:
            return [content]
        
        chunks = []
        paragraphs = content.split('\n\n')
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
            # 上传图片到 Slack
            if image_url.startswith("http"):
                # 下载并上传
                import httpx
                import tempfile
                import os
                
                async with httpx.AsyncClient() as client:
                    response = await client.get(image_url, timeout=30.0)
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as f:
                        f.write(response.content)
                        temp_path = f.name
                
                try:
                    response = self._client.files_upload_v2(
                        channel=chat_id,
                        file=temp_path,
                        title=caption or "Image",
                    )
                    return SendResult(
                        success=True,
                        message_id=response.get("file", {}).get("id", ""),
                    )
                finally:
                    os.unlink(temp_path)
            else:
                # 本地文件
                response = self._client.files_upload_v2(
                    channel=chat_id,
                    file=image_url,
                    title=caption or "Image",
                )
                return SendResult(
                    success=True,
                    message_id=response.get("file", {}).get("id", ""),
                )
            
        except Exception as e:
            logger.error("[Slack] Send image failed: %s", e)
            return SendResult(success=False, error=str(e))
    
    async def send_typing(self, chat_id: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """发送 typing 状态"""
        if self._client:
            try:
                self._client.api_call(
                    "chat.postMessage",
                    channel=chat_id,
                    text="...",
                    unflink=True,
                )
            except Exception:
                pass
    
    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """获取聊天信息"""
        if not self._client:
            return {"name": "Unknown", "type": "dm"}
        
        try:
            response = self._client.conversations_info(channel=chat_id)
            channel = response.get("channel", {})
            return {
                "name": channel.get("name", chat_id),
                "type": "dm" if channel.get("is_im") else "group",
            }
        except Exception as e:
            logger.error("[Slack] get_chat_info failed: %s", e)
            return {"name": str(chat_id), "type": "dm"}