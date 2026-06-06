# -*- coding: utf-8 -*-
"""
钉钉平台适配器

使用钉钉 Stream Mode SDK 进行实时消息接收。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from typing import Any, Dict, List, Optional

from loguru import logger

from app.gateway.base import (
    BasePlatformAdapter,
    Platform,
    PlatformConfig,
    MessageEvent,
    MessageType,
    SendResult,
    ChatInfo,
    get_cache_dir,
)

# 钉钉 SDK 组件（延迟导入）
ChatbotHandler = None
ChatbotMessage = None
AckMessage = None


def _ensure_dingtalk_imports():
    """确保钉钉 SDK 组件已导入"""
    global ChatbotHandler, ChatbotMessage, AckMessage
    if ChatbotHandler is None:
        from dingtalk_stream import ChatbotHandler as CH, ChatbotMessage as CM, AckMessage as AM
        ChatbotHandler = CH
        ChatbotMessage = CM
        AckMessage = AM


def _patch_dingtalk_stream_logging():
    """
    Monkey-patch dingtalk_stream.stream.DingTalkStreamClient.start()
    
    修复三方库 stream.py:89 的日志错误：
        self.logger.exception('unknown exception', e)
            # TypeError: not all arguments converted during string formatting
    修改为:
        self.logger.exception('unknown exception', exc_info=e)
    """
    import asyncio.exceptions
    import websockets
    import dingtalk_stream.stream as dstream
    from urllib.parse import quote_plus

    original_start = dstream.DingTalkStreamClient.start

    async def patched_start(self):
        """Fixed version of DingTalkStreamClient.start()"""
        self.pre_start()
        while True:
            try:
                connection = self.open_connection()
                if not connection:
                    self.logger.error('open connection failed')
                    await asyncio.sleep(10)
                    continue
                self.logger.info('endpoint is %s', connection)

                uri = f'{connection["endpoint"]}?ticket={quote_plus(connection["ticket"])}'
                async with websockets.connect(uri) as websocket:
                    self.websocket = websocket
                    asyncio.create_task(self.keepalive(websocket))
                    async for raw_message in websocket:
                        json_message = json.loads(raw_message)
                        asyncio.create_task(self.background_task(json_message))
            except KeyboardInterrupt:
                break
            except (asyncio.exceptions.CancelledError,
                    websockets.exceptions.ConnectionClosedError) as e:
                self.logger.error('[start] network exception, error=%s', e)
                await asyncio.sleep(10)
                continue
            except Exception as e:
                await asyncio.sleep(3)
                # 🔧 FIXED: exc_info=e 而非作为位置参数传入
                self.logger.exception('unknown exception', exc_info=e)
                continue
            finally:
                pass

    dstream.DingTalkStreamClient.start = patched_start


# 预导入钉钉 SDK 组件
_ensure_dingtalk_imports()


# 消息类型映射
DINGTALK_TYPE_MAPPING = {
    "picture": "image",
    "voice": "audio",
}

# 重连退避
RECONNECT_BACKOFF = [2, 5, 10, 30, 60]
MAX_MESSAGE_LENGTH = 20000


class DingTalkAdapter(BasePlatformAdapter):
    """
    钉钉 chatbot 适配器 (Stream Mode)
    
    使用 dingtalk-stream SDK 维持 WebSocket 长连接。
    
    配置项:
        - client_id: 应用 AppKey
        - client_secret: 应用 AppSecret
    """
    
    platform = Platform.DINGTALK
    name = "DingTalk"
    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH
    
    def __init__(self, config: PlatformConfig, **kwargs):
        super().__init__(config, **kwargs)
        
        self._client_id = config.client_id or ""
        self._client_secret = config.client_secret or ""
        
        # Stream 客户端
        self._stream_client: Optional[Any] = None
        self._stream_task: Optional[asyncio.Task] = None
        self._http_client: Optional[Any] = None
        
        # Session webhook 缓存
        self._session_webhooks: Dict[str, tuple] = {}  # chat_id -> (webhook, expire_time)
        
        # 重连退避
        self._backoff_idx = 0
    
    async def connect(self) -> bool:
        """连接到钉钉 Stream Mode"""
        try:
            from dingtalk_stream import DingTalkStreamClient, Credential
        except ImportError:
            logger.error("[DingTalk] dingtalk-stream not installed. Run: pip install 'dingtalk-stream>=0.20'")
            self._last_error = "依赖不可用"
            return False
        
        # 修复三方库 dingtalk-stream 的日志 bug（TypeError 噪声）
        _patch_dingtalk_stream_logging()
        
        if not self._client_id or not self._client_secret:
            logger.error("[DingTalk] client_id and client_secret are required")
            self._last_error = "缺少 client_id 或 client_secret"
            return False
        
        try:
            import httpx
            from app.gateway.platforms._http_client_limits import platform_httpx_limits
            
            limits = platform_httpx_limits()
            self._http_client = httpx.AsyncClient(
                timeout=30.0,
                limits=limits if limits else httpx.Limits(),
            )
            
            # 创建 Stream 客户端
            credential = Credential(self._client_id, self._client_secret)
            self._stream_client = DingTalkStreamClient(credential)
            
            # 注册处理器 - 使用字符串 topic
            handler = _IncomingHandler(self)
            self._stream_client.register_callback_handler(
                "/v1.0/im/bot/messages/get",  # 机器人消息 topic
                handler
            )
            
            # 启动
            self._stream_task = asyncio.create_task(self._run_stream())
            
            # 标记为已连接
            self._connected = True
            
            logger.info("[DingTalk] Connected successfully")
            return True
            
        except Exception as e:
            logger.error(f"[DingTalk] Connection failed: {e}", exc_info=True)
            await self._cleanup()
            return False
    
    async def _run_stream(self) -> None:
        """运行 Stream 客户端"""
        self._backoff_idx = 0
        _consecutive_timeouts = 0
        
        while self._running:
            try:
                await self._stream_client.start()
            except asyncio.CancelledError:
                return
            except (TimeoutError, asyncio.TimeoutError) as e:
                if not self._running:
                    return
                _consecutive_timeouts += 1
                if _consecutive_timeouts == 1:
                    logger.warning(
                        "[DingTalk] WebSocket connection timed out — check network / firewall. "
                        "DingTalk server may be unreachable."
                    )
                else:
                    logger.warning(f"[DingTalk] Timeout again ({_consecutive_timeouts}x): {e}")
            except Exception as e:
                if not self._running:
                    return
                _consecutive_timeouts = 0
                logger.warning(f"[DingTalk] Stream error: {e}")
            
            if not self._running:
                return
            
            delay = RECONNECT_BACKOFF[min(self._backoff_idx, len(RECONNECT_BACKOFF) - 1)]
            self._backoff_idx += 1
            
            logger.info(f"[DingTalk] Reconnecting in {delay}s...")
            await asyncio.sleep(delay)
    
    async def disconnect(self) -> None:
        """断开连接"""
        self._running = False
        
        if self._stream_task:
            self._stream_task.cancel()
            try:
                await asyncio.wait_for(self._stream_task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        
        await self._cleanup()
        logger.info("[DingTalk] Disconnected")
    
    async def _cleanup(self) -> None:
        """清理资源"""
        self._session_webhooks.clear()
        
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None
        
        self._stream_client = None
    
    async def _on_message(self, message: Any) -> None:
        """处理收到的消息"""
        msg_id = getattr(message, "message_id", None) or ""
        if not msg_id:
            # 钉钉 SDK 通常会提供 message_id（对应 msgId），
            # 如果缺失则用 conversation_id + sender_id + text 组合作为去重键
            sender_id = getattr(message, "sender_id", "") or ""
            text = self._extract_text(message) or ""
            msg_id = f"{sender_id}:{text[:50]}"
        conversation_id = getattr(message, "conversation_id", "") or ""
        conversation_type = getattr(message, "conversation_type", "1")
        is_group = str(conversation_type) == "2"
        
        sender_id = getattr(message, "sender_id", "") or ""
        sender_nick = getattr(message, "sender_nick", "") or sender_id
        
        chat_id = conversation_id or sender_id
        chat_type = "group" if is_group else "dm"
        
        # 存储 session webhook
        session_webhook = getattr(message, "session_webhook", None) or ""
        session_webhook_expired_time = getattr(message, "session_webhook_expired_time", 0) or 0
        
        if session_webhook and chat_id:
            self._session_webhooks[chat_id] = (session_webhook, session_webhook_expired_time)
            logger.debug(f"[DingTalk] Stored webhook for {str(chat_id)[:30]}: {str(session_webhook)[:50]}")
        
        # 提取文本
        text = self._extract_text(message)
        
        # 提取媒体
        msg_type, media_urls, media_types = self._extract_media(message)
        
        if not text and not media_urls:
            logger.debug("[DingTalk] Empty message skipped")
            return
        
        # 构建消息事件
        event = MessageEvent(
            text=text,
            message_type=msg_type,
            message_id=msg_id,
            chat_id=chat_id,
            user_id=sender_id,
            user_name=sender_nick,
            platform=Platform.DINGTALK,
            chat_type=chat_type,
            media_urls=media_urls,
            media_types=media_types,
        )
        
        await self.handle_message(event)
    
    def _on_message_sync(self, message: Any) -> None:
        """同步处理收到的消息（供 ChatbotHandler 调用）"""
        msg_id = getattr(message, "message_id", None) or ""
        if not msg_id:
            sender_id = getattr(message, "sender_id", "") or ""
            text = self._extract_text(message) or ""
            msg_id = f"{sender_id}:{text[:50]}"
        conversation_id = getattr(message, "conversation_id", "") or ""
        conversation_type = getattr(message, "conversation_type", "1")
        is_group = str(conversation_type) == "2"
        
        sender_id = getattr(message, "sender_id", "") or ""
        sender_nick = getattr(message, "sender_nick", "") or sender_id
        
        chat_id = conversation_id or sender_id
        chat_type = "group" if is_group else "dm"
        
        # 存储 session webhook
        session_webhook = getattr(message, "session_webhook", None) or ""
        session_webhook_expired_time = getattr(message, "session_webhook_expired_time", 0) or 0
        
        if session_webhook and chat_id:
            self._session_webhooks[chat_id] = (session_webhook, session_webhook_expired_time)
        
        # 提取文本
        text = self._extract_text(message)
        
        # 提取媒体
        msg_type, media_urls, media_types = self._extract_media(message)
        
        if not text and not media_urls:
            logger.debug("[DingTalk] Empty message skipped")
            return
        
        # 构建消息事件
        event = MessageEvent(
            text=text,
            message_type=msg_type,
            message_id=msg_id,
            chat_id=chat_id,
            user_id=sender_id,
            user_name=sender_nick,
            platform=Platform.DINGTALK,
            chat_type=chat_type,
            media_urls=media_urls,
            media_types=media_types,
        )
        
        # 在新事件循环中运行异步处理
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # 如果已经在运行，创建任务
                asyncio.create_task(self.handle_message(event))
            else:
                loop.run_until_complete(self.handle_message(event))
        except RuntimeError:
            # 没有事件循环，创建一个
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(self.handle_message(event))
            finally:
                loop.close()
    
    def _extract_text(self, message: Any) -> str:
        """提取文本内容"""
        text = getattr(message, "text", None) or ""
        
        # 处理 TextContent 对象
        if hasattr(text, "content"):
            content = (text.content or "").strip()
        elif isinstance(text, dict):
            content = text.get("content", "").strip()
        else:
            content = str(text).strip()
        
        if not content:
            rich_text = getattr(message, "rich_text_content", None) or getattr(message, "rich_text", None)
            if rich_text:
                rich_list = getattr(rich_text, "rich_text_list", None) or rich_text
                if isinstance(rich_list, list):
                    parts = []
                    for item in rich_list:
                        if isinstance(item, dict):
                            t = item.get("text") or item.get("content") or ""
                            if t:
                                parts.append(t)
                        elif hasattr(item, "text") and item.text:
                            parts.append(item.text)
                    content = " ".join(parts).strip()
        
        return content
    
    def _extract_media(self, message: Any) -> tuple:
        """提取媒体信息"""
        msg_type = MessageType.TEXT
        media_urls = []
        media_types = []
        
        # 图片
        image_content = getattr(message, "image_content", None)
        if image_content:
            download_code = getattr(image_content, "download_code", None)
            if download_code:
                media_urls.append(download_code)
                media_types.append("image")
                msg_type = MessageType.IMAGE
        
        # 富文本中的媒体
        rich_text = getattr(message, "rich_text_content", None) or getattr(message, "rich_text", None)
        if rich_text:
            rich_list = getattr(rich_text, "rich_text_list", None) or rich_text
            if isinstance(rich_list, list):
                for item in rich_list:
                    if isinstance(item, dict):
                        dl_code = item.get("downloadCode") or item.get("download_code") or ""
                        item_type = item.get("type", "")
                        if dl_code:
                            mapped = DINGTALK_TYPE_MAPPING.get(item_type, "file")
                            media_urls.append(dl_code)
                            media_types.append(mapped)
                            if mapped == "image":
                                msg_type = MessageType.IMAGE
                            elif mapped == "audio":
                                msg_type = MessageType.AUDIO
        
        return msg_type, media_urls, media_types
    
    async def send(self, chat_id: str, content: str, **kwargs) -> SendResult:
        """发送消息"""
        logger.debug("[DingTalk] send() called, chat_id=" + chat_id[:20] + f", content_len={len(content)}, webhooks={list(self._session_webhooks.keys())}")
        
        webhook_info = self._session_webhooks.get(chat_id)
        if not webhook_info:
            logger.warning(f"[DingTalk] No session webhook for chat_id={chat_id[:30]}")
            return SendResult(success=False, error="No session webhook available", retryable=True)
        
        webhook, expired_time = webhook_info
        
        if not self._http_client:
            return SendResult(success=False, error="HTTP client not initialized", retryable=False)
        
        try:
            # 钉钉 session_webhook API 格式
            payload = {
                "msgtype": "markdown",
                "markdown": {
                    "title": "DriFox",
                    "text": content
                }
            }
            
            response = await self._http_client.post(webhook, json=payload)
            
            if response.status_code == 200:
                return SendResult(success=True)
            else:
                return SendResult(
                    success=False,
                    error=f"HTTP {response.status_code}: {response.text}",
                    retryable=response.status_code >= 500
                )
                    
        except asyncio.TimeoutError:
            return SendResult(success=False, error="Request timeout", retryable=True)
        except Exception as e:
            logger.error(f"[DingTalk] Send failed: {e}", exc_info=True)
            return SendResult(success=False, error=str(e), retryable=True)
    
    async def send_image(self, chat_id: str, image_path: str, **kwargs) -> SendResult:
        """发送图片"""
        webhook_info = self._session_webhooks.get(chat_id)
        if not webhook_info:
            return SendResult(success=False, error="No session webhook available", retryable=True)
        
        webhook, _ = webhook_info
        
        try:
            from pathlib import Path
            import httpx
            
            image_path = str(image_path)
            
            # 如果是 URL，先下载
            if image_path.startswith("http"):
                cache_dir = get_cache_dir("images")
                cache_dir.mkdir(parents=True, exist_ok=True)
                
                async with httpx.AsyncClient(timeout=30.0) as client:
                    response = await client.get(image_path)
                    response.raise_for_status()
                    
                    ext = ".jpg"
                    if "." in image_path:
                        ext = "." + image_path.rsplit(".", 1)[-1].split("?")[0]
                    
                    filename = f"img_{uuid.uuid4().hex[:12]}{ext}"
                    filepath = cache_dir / filename
                    filepath.write_bytes(response.content)
                    image_path = str(filepath)
            
            # 读取图片数据
            with open(image_path, "rb") as f:
                image_data = f.read()
            
            # 钉钉需要先上传媒体获取 media_id
            # 注意：这里需要先获取 access_token
            # 简化实现：使用文件路径作为文本发送
            payload = {
                "msg": {
                    "msgtype": "text",
                    "text": {
                        "content": f"🖼️ Image: {Path(image_path).name}"
                    }
                }
            }
            
            async with self._http_client as client:
                response = await client.post(webhook, json=payload)
                
                if response.status_code == 200:
                    return SendResult(success=True)
                else:
                    return SendResult(
                        success=False,
                        error=f"HTTP {response.status_code}",
                        retryable=response.status_code >= 500
                    )
                    
        except Exception as e:
            logger.error(f"[DingTalk] Send image failed: {e}", exc_info=True)
            return SendResult(success=False, error=str(e), retryable=True)
    
    async def send_file(self, chat_id: str, file_path: str, **kwargs) -> SendResult:
        """发送文件"""
        webhook_info = self._session_webhooks.get(chat_id)
        if not webhook_info:
            return SendResult(success=False, error="No session webhook available", retryable=True)
        
        webhook, _ = webhook_info
        
        try:
            from pathlib import Path
            
            payload = {
                "msg": {
                    "msgtype": "text",
                    "text": {
                        "content": f"📎 File: {Path(file_path).name}"
                    }
                }
            }
            
            async with self._http_client as client:
                response = await client.post(webhook, json=payload)
                
                if response.status_code == 200:
                    return SendResult(success=True)
                else:
                    return SendResult(success=False, error=f"HTTP {response.status_code}")
                    
        except Exception as e:
            logger.error(f"[DingTalk] Send file failed: {e}", exc_info=True)
            return SendResult(success=False, error=str(e), retryable=True)
    
    async def get_chat_info(self, chat_id: str) -> ChatInfo:
        """获取聊天信息"""
        return ChatInfo(
            name=chat_id,
            type="dm",
            chat_id=chat_id,
        )


class _IncomingHandler(ChatbotHandler):
    """
    钉钉消息处理器
    
    继承自 dingtalk_stream.ChatbotHandler，处理机器人消息。
    """
    
    def __init__(self, adapter: DingTalkAdapter):
        from dingtalk_stream import ChatbotMessage
        super().__init__()
        self._adapter = adapter
        self._ChatbotMessage = ChatbotMessage
    
    async def process(self, callback) -> tuple:
        """
        处理消息回调
        
        Args:
            callback: 钉钉 SDK 的回调消息
            
        Returns:
            (status_code, response)
        """
        try:
            from dingtalk_stream import AckMessage
            
            # 获取消息数据
            data = callback.data if hasattr(callback, 'data') else callback
            
            # 创建 ChatbotMessage
            message = self._ChatbotMessage.from_dict(data)
            
            # 异步处理消息
            await self._adapter._on_message(message)
            
            # 返回成功状态
            return AckMessage.STATUS_OK, 'OK'
            
        except Exception as e:
            logger.error("[DingTalk] Handler process error: %s", e, exc_info=True)
            from dingtalk_stream import AckMessage
            return AckMessage.STATUS_FAIL, str(e)


def check_dingtalk_requirements() -> bool:
    """检查钉钉依赖是否满足"""
    try:
        from dingtalk_stream import DingTalkStreamClient, Credential
        import httpx
        return True
    except ImportError:
        return False