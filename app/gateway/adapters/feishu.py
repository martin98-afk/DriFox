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
        
        # 独立的事件循环，用于执行消息处理（不与 WS client 的循环冲突）
        self._handler_loop: Optional[asyncio.AbstractEventLoop] = None
        self._handler_loop_thread: Optional[threading.Thread] = None
    
    def set_message_handler(self, handler) -> None:
        """设置消息处理器"""
        self._message_handler = handler
    
    async def connect(self) -> bool:
        """连接到飞书 WebSocket"""
        if not check_feishu_requirements():
            logger.error("[Feishu] Dependencies not available. Run: pip install lark-oapi")
            self._last_error = "依赖不可用"
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
            self._last_error = "缺少 app_id 或 app_secret"
            return False
        
        try:
            import lark_oapi as lark
            from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
            from lark_oapi.ws import Client as FeishuWSClient
            from lark_oapi.core.const import FEISHU_DOMAIN, LARK_DOMAIN
            
            # 创建事件处理器
            # 关键：直接传递 encrypt_key 和 verification_token（即使是空字符串）
            # 不要传 dummy 值！SDK 的 _do_without_validation 方法（WebSocket 使用）
            # 会跳过验证/解密，所以空值完全 OK
            handler = EventDispatcherHandler.builder(
                self._encrypt_key or "",
                self._verification_token or "",
            ).register_p2_im_message_receive_v1(
                self._on_feishu_message
            ).build()
            
            # 创建 WebSocket 客户端 - 传入 domain 参数
            self._ws_client = FeishuWSClient(
                app_id=self._app_id,
                app_secret=self._app_secret,
                log_level=lark.LogLevel.INFO,
                event_handler=handler,
                domain=FEISHU_DOMAIN,  # 明确指定域名
            )
            
            # 启动独立的事件循环线程，用于执行消息处理回调
            # 注意：WS client 的 on_message 回调在 WS 线程的事件循环中执行，
            # 不能直接在回调里调用 asyncio.run()（会冲突），
            # 所以需要独立的 loop 来执行 message_handler
            self._start_handler_loop()
            
            # 在独立线程中启动 WS client
            self._stop_event.clear()
            self._feishu_thread = threading.Thread(
                target=self._run_feishu_client,
                name="FeishuWSClient",
                daemon=True
            )
            self._feishu_thread.start()
            
            # 等待连接建立
            await asyncio.sleep(2)
            
            self._running = True
            self._connected = True
            
            logger.info("[Feishu] Connected successfully (WebSocket)")
            return True
            
        except Exception as e:
            logger.error("[Feishu] Failed to connect: %s", e)
            import traceback
            traceback.print_exc()
            return False
    
    def _start_handler_loop(self) -> None:
        """启动独立的事件循环线程，用于调度消息处理回调"""
        if self._handler_loop is not None:
            return
        self._handler_loop = asyncio.new_event_loop()
        self._handler_loop_thread = threading.Thread(
            target=self._run_handler_loop,
            name="FeishuHandlerLoop",
            daemon=True,
        )
        self._handler_loop_thread.start()

    def _run_handler_loop(self) -> None:
        """运行消息处理事件循环"""
        asyncio.set_event_loop(self._handler_loop)
        self._handler_loop.run_forever()
    
    def _run_feishu_client(self) -> None:
        """在独立线程中运行飞书客户端

        参考 hermes-agent 的 _run_official_feishu_ws_client 实现。
        关键：
        1. 创建独立事件循环并设置到线程
        2. 同时设置 lark_oapi.ws.client 模块的 loop 变量
           （WS 客户端的 _reconnect / _ping_loop 等方法依赖此变量）
        """
        try:
            import lark_oapi.ws.client as ws_client_module
            
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
            # 关键！设置 ws 模块的全局 loop，否则 WS client 内部会使用
            # 模块导入时的原始事件循环（可能是主线程的），导致事件循环冲突
            ws_client_module.loop = loop
            
            # 运行客户端
            try:
                self._ws_client.start()
            except Exception as e:
                msg = str(e).lower()
                if "event loop" not in msg and "running" not in msg:
                    logger.error("[Feishu] Client error: %s", e)
                else:
                    logger.debug("[Feishu] Client stopped (expected): %s", e)
            finally:
                loop.close()
                
        except Exception as e:
            logger.error("[Feishu] Thread error: %s", e)
    
    def _on_feishu_message(self, data: Any) -> None:
        """处理接收到的飞书消息

        注意：lark_oapi SDK 的 WebSocket 模式通过 _do_without_validation 分发事件，
        回调接收到的 data 是 P2ImMessageReceiveV1 对象，结构为:
            data.event          -> P2ImMessageReceiveV1Data
                .message       -> EventMessage
                    .message_id, .chat_id, .chat_type, .message_type,
                    .content (JSON 字符串, 如 '{"text":"hello"}')
                .sender        -> EventSender
                    .sender_id  -> UserId {open_id, user_id, union_id}
                    .sender_type, .tenant_key
        """
        try:
            # 获取 event 包装层 (P2ImMessageReceiveV1Data)
            event_wrapper = getattr(data, 'event', None)
            if event_wrapper is None:
                logger.debug("[Feishu] No event wrapper in callback data")
                return

            # 获取消息对象
            message = getattr(event_wrapper, 'message', None)
            sender = getattr(event_wrapper, 'sender', None)

            if message is None:
                logger.debug("[Feishu] No message in callback data")
                return

            # 提取字段
            message_id = str(getattr(message, 'message_id', '') or '')
            chat_id = str(getattr(message, 'chat_id', '') or '')
            chat_type = str(getattr(message, 'chat_type', 'p2p') or 'p2p')

            # EventMessage 使用 message_type（而不是 msg_type），content 是 JSON 字符串
            msg_type = str(getattr(message, 'message_type', 'text') or 'text')
            content_str = str(getattr(message, 'content', '') or '')

            # 解析 content JSON 字符串
            text = ""
            try:
                if content_str:
                    content_data = json.loads(content_str)
                    if isinstance(content_data, dict):
                        text = content_data.get("text", "") or content_data.get("content", "") or ""
            except (json.JSONDecodeError, TypeError):
                # 如果不是 JSON，直接作为文本
                text = content_str

            # 获取发送者信息
            user_id = ""
            user_name = ""
            if sender:
                sender_id = getattr(sender, 'sender_id', None)
                if sender_id:
                    user_id = str(getattr(sender_id, 'open_id', '') or getattr(sender_id, 'user_id', '') or '')
                # EventSender 没有 sender_name，用 user_id 代替
                user_name = user_id

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
                    # 重要：WS client 回调运行在 WS 线程的事件循环中，
                    # 不能在此调用 asyncio.run()，会触发 RuntimeError。
                    # 使用独立的 handler_loop 来调度消息处理。
                    loop = self._handler_loop
                    if loop is not None and loop.is_running():
                        asyncio.run_coroutine_threadsafe(
                            self._message_handler(event),
                            loop,
                        )
                    else:
                        logger.error("[Feishu] Handler loop not running, dropping message")
                except Exception as e:
                    logger.error("[Feishu] Handle message error: %s", e)

        except Exception as e:
            logger.error("[Feishu] Parse message error: %s", e)
            import traceback
            traceback.print_exc()
    
    async def disconnect(self) -> None:
        """断开连接"""
        self._running = False
        self._connected = False
        self._stop_event.set()
        
        if self._ws_client:
            try:
                # 飞书 SDK 的 Client 可能使用不同方法停止
                # 方法1: stop() 方法
                if hasattr(self._ws_client, 'stop'):
                    self._ws_client.stop()
                # 方法2: close() 方法  
                elif hasattr(self._ws_client, 'close'):
                    self._ws_client.close()
                # 方法3: 直接设置运行标志
                elif hasattr(self._ws_client, '_running'):
                    self._ws_client._running = False
            except AttributeError:
                # Client 对象可能没有这些属性，忽略
                pass
            except Exception as e:
                logger.debug("[Feishu] Disconnect note: %s", e)
        
        # 停止 handler loop
        if self._handler_loop is not None and self._handler_loop.is_running():
            try:
                self._handler_loop.call_soon_threadsafe(self._handler_loop.stop)
            except Exception:
                pass
        self._handler_loop = None
        self._handler_loop_thread = None
        
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