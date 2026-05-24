# -*- coding: utf-8 -*-
"""
飞书 (Feishu/Lark) 适配器

使用 lark_oapi SDK WebSocket 模式进行消息收发。
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any, Dict, List, Optional

from app.gateway.base import (
    BasePlatformAdapter,
    Platform,
    PlatformConfig,
    MessageEvent,
    MessageType,
    SendResult,
)

logger = logging.getLogger(__name__)

# 延迟导入
LARK_AVAILABLE = False


def check_feishu_requirements() -> bool:
    """检查飞书依赖是否可用"""
    global LARK_AVAILABLE
    if LARK_AVAILABLE:
        return True
    try:
        import lark_oapi
        from lark_oapi.ws import Client as FeishuWSClient
        from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
        LARK_AVAILABLE = True
        return True
    except ImportError:
        logger.warning("[Feishu] lark_oapi not installed. Run: pip install lark-oapi")
        return False


class FeishuAdapter(BasePlatformAdapter):
    """
    飞书 (Feishu/Lark) 适配器
    
    使用飞书开放平台 WebSocket 模式进行消息收发。
    需要安装 lark-oapi: pip install lark-oapi
    """
    
    MAX_MESSAGE_LENGTH = 2000
    
    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.FEISHU)
        
        self._app_id = config.extra.get("app_id") or ""
        self._app_secret = config.extra.get("app_secret") or ""
        self._encrypt_key = config.extra.get("encrypt_key") or ""
        self._verification_token = config.extra.get("verification_token") or ""
        
        self._ws_client = None
        self._running = False
        self._reconnect_attempts = 0
        self._max_reconnect_attempts = 5
        self._message_handler = None
        self._feishu_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
    
    def set_message_handler(self, handler) -> None:
        """设置消息处理器"""
        self._message_handler = handler
    
    async def connect(self) -> bool:
        """连接到飞书 WebSocket"""
        if not check_feishu_requirements():
            logger.error("[Feishu] Dependencies not available. Run: pip install lark-oapi")
            return False
        
        # 从配置重新获取（确保最新）
        from app.gateway.config import get_gateway_config
        cfg = get_gateway_config().get_platform_config(Platform.FEISHU)
        self._app_id = cfg.extra.get("app_id") or ""
        self._app_secret = cfg.extra.get("app_secret") or ""
        self._encrypt_key = cfg.extra.get("encrypt_key") or ""
        self._verification_token = cfg.extra.get("verification_token") or ""
        
        if not self._app_id or not self._app_secret:
            logger.error("[Feishu] app_id and app_secret are required")
            return False
        
        try:
            from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
            from lark_oapi.ws import Client as FeishuWSClient
            
            # 创建事件处理器
            if self._encrypt_key and self._verification_token:
                handler = EventDispatcherHandler.builder(
                    encrypt_key=self._encrypt_key,
                    verification_token=self._verification_token
                ).register_p2_im_message_receive_v1(
                    self._on_feishu_message
                ).build()
            else:
                # 不使用加密
                handler = EventDispatcherHandler.builder(
                    encrypt_key="dummy_key_for_non_encrypted",
                    verification_token="dummy_token_for_non_encrypted"
                ).register_p2_im_message_receive_v1(
                    self._on_feishu_message
                ).build()
            
            # 创建 WebSocket 客户端
            self._ws_client = FeishuWSClient(
                app_id=self._app_id,
                app_secret=self._app_secret,
                event_handler=handler,
            )
            
            # 在独立线程中启动（避免事件循环冲突）
            self._stop_event.clear()
            self._feishu_thread = threading.Thread(
                target=self._run_feishu_client,
                name="FeishuWSClient",
                daemon=True
            )
            self._feishu_thread.start()
            
            # 等待连接建立
            await asyncio.sleep(1)
            
            self._running = True
            self._connected = True
            
            logger.info("[Feishu] Connected successfully (WebSocket)")
            return True
            
        except Exception as e:
            logger.error("[Feishu] Failed to connect: %s", e)
            import traceback
            traceback.print_exc()
            return False
    
    def _run_feishu_client(self) -> None:
        """在独立线程中运行飞书客户端"""
        try:
            # 创建独立的事件循环
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
            # 运行客户端
            try:
                self._ws_client.start()
            except Exception as e:
                if "event loop" not in str(e).lower() and "running" not in str(e).lower():
                    logger.error("[Feishu] Client error: %s", e)
            finally:
                loop.close()
                
        except Exception as e:
            logger.error("[Feishu] Thread error: %s", e)
    
    def _on_feishu_message(self, data: Any) -> None:
        """处理接收到的飞书消息"""
        try:
            # 飞书 SDK 返回的是 P2ImMessageReceiveV1 对象，不是字典
            # 需要使用 hasattr 或 getattr 来访问属性
            
            # 获取消息对象
            message = getattr(data, 'message', None) or getattr(data, 'msg', None)
            sender = getattr(data, 'sender', None)
            
            if message is None:
                logger.debug("[Feishu] No message in callback data")
                return
            
            # 提取字段（使用 getattr 安全访问）
            message_id = str(getattr(message, 'message_id', '') or '')
            chat_id = str(getattr(message, 'chat_id', '') or '')
            chat_type = str(getattr(message, 'chat_type', 'p2p') or 'p2p')
            msg_type = str(getattr(message, 'msg_type', 'text') or 'text')
            body = getattr(message, 'body', {})
            
            # 解析内容
            text = ""
            if isinstance(body, dict):
                text = body.get("text", "") or ""
            elif hasattr(body, 'text'):
                text = str(body.text or "")
            else:
                text = str(body or "")
            
            # 过滤心跳消息
            if not text and msg_type == "post":
                return
            
            # 获取发送者信息
            user_id = ""
            user_name = ""
            if sender:
                sender_id = getattr(sender, 'sender_id', None)
                if sender_id:
                    user_id = str(getattr(sender_id, 'open_id', '') or getattr(sender_id, 'user_id', '') or '')
                user_name = str(getattr(sender, 'sender_name', '') or user_id)
            
            # 确定消息类型
            if text.startswith('/'):
                event_msg_type = MessageType.COMMAND
            elif msg_type == "image":
                event_msg_type = MessageType.IMAGE
            elif msg_type == "file":
                event_msg_type = MessageType.FILE
            else:
                event_msg_type = MessageType.TEXT
            
            # 构建事件
            event = MessageEvent(
                text=text,
                message_type=event_msg_type,
                message_id=message_id,
                chat_id=chat_id,
                user_id=user_id,
                user_name=user_name,
                platform=Platform.FEISHU,
                chat_type="dm" if chat_type == "p2p" else "group",
                metadata={"chat_id": chat_id},
            )
            
            # 处理消息
            if self._message_handler:
                try:
                    # 在新线程的上下文中处理
                    asyncio.run(self._message_handler(event))
                except Exception as e:
                    logger.error("[Feishu] Handle message error: %s", e)
            
        except Exception as e:
            logger.error("[Feishu] Parse message error: %s", e)
    
    async def disconnect(self) -> None:
        """断开连接"""
        self._running = False
        self._connected = False
        self._stop_event.set()
        
        if self._ws_client:
            try:
                self._ws_client.stop()
            except Exception as e:
                logger.warning("[Feishu] Error during disconnect: %s", e)
        
        logger.info("[Feishu] Disconnected")
    
    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """发送消息"""
        if not self._connected:
            return SendResult(success=False, error="Not connected")
        
        try:
            import httpx
            
            # 获取 token
            token = await self._get_access_token()
            if not token:
                return SendResult(success=False, error="Failed to get access token")
            
            # 分割长消息
            if len(content) > self.MAX_MESSAGE_LENGTH:
                chunks = self._split_message(content)
            else:
                chunks = [content]
            
            message_ids = []
            
            async with httpx.AsyncClient() as client:
                for i, chunk in enumerate(chunks):
                    if len(chunks) > 1:
                        chunk = f"[{i+1}/{len(chunks)}]\n{chunk}"
                    
                    headers = {
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    }
                    
                    json_data = {
                        "receive_id": chat_id,
                        "msg_type": "text",
                        "content": json.dumps({"text": chunk}),
                    }
                    
                    if reply_to and i == 0:
                        endpoint = f"https://open.feishu.cn/open-apis/im/v1/messages/{reply_to}/reply"
                    else:
                        endpoint = "https://open.feishu.cn/open-apis/im/v1/messages"
                    
                    response = await client.post(
                        endpoint,
                        params={"receive_id_type": "chat_id"},
                        headers=headers,
                        json=json_data,
                        timeout=30.0,
                    )
                    
                    if response.status_code == 200:
                        resp_data = response.json()
                        if resp_data.get("code") == 0:
                            message_ids.append(resp_data.get("data", {}).get("message_id", ""))
            
            return SendResult(
                success=True,
                message_id=message_ids[0] if message_ids else None,
            )
            
        except Exception as e:
            logger.error("[Feishu] Send failed: %s", e)
            return SendResult(success=False, error=str(e))
    
    async def _get_access_token(self) -> Optional[str]:
        """获取 tenant access token"""
        try:
            import httpx
            
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                    json={
                        "app_id": self._app_id,
                        "app_secret": self._app_secret,
                    },
                    timeout=30.0,
                )
                
                if response.status_code == 200:
                    data = response.json()
                    if data.get("code") == 0:
                        return data.get("tenant_access_token")
            
            return None
            
        except Exception as e:
            logger.error("[Feishu] Failed to get access token: %s", e)
            return None
    
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
        image_path: str,
        **kwargs
    ) -> SendResult:
        """发送图片"""
        return SendResult(success=False, error="Not implemented")
    
    async def send_file(
        self,
        chat_id: str,
        file_path: str,
        **kwargs
    ) -> SendResult:
        """发送文件"""
        return SendResult(success=False, error="Not implemented")
    
    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """获取聊天信息"""
        return {"name": chat_id, "type": "dm"}