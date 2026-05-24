# -*- coding: utf-8 -*-
"""
企业微信平台适配器

使用企业微信 AI Bot WebSocket Gateway 进行消息收发。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from typing import Any, Dict, List, Optional

import aiohttp
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
    cache_file_from_bytes,
)


# 企业微信 WebSocket 地址
DEFAULT_WS_URL = "wss://openws.work.weixin.qq.com"

# 协议命令
CMD_SUBSCRIBE = "aibot_subscribe"
CMD_CALLBACK = "aibot_msg_callback"
CMD_SEND = "aibot_send_msg"
CMD_RESPONSE = "aibot_respond_msg"
CMD_PING = "ping"
CMD_UPLOAD_MEDIA = "aibot_upload_media"

# 配置常量
CONNECT_TIMEOUT = 20.0
REQUEST_TIMEOUT = 15.0
HEARTBEAT_INTERVAL = 30.0
RECONNECT_BACKOFF = [2, 5, 10, 30, 60]
MAX_MESSAGE_LENGTH = 4000


class WeComAdapter(BasePlatformAdapter):
    """
    企业微信 AI Bot 适配器
    
    通过 WebSocket 长连接与企业微信 AI Bot Gateway 通信。
    
    配置项:
        - bot_id: 机器人 ID
        - secret: 密钥
        - websocket_url: WebSocket 地址 (可选)
    """
    
    platform = Platform.WECOM
    name = "WeCom"
    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH
    
    def __init__(self, config: PlatformConfig, **kwargs):
        super().__init__(config, **kwargs)
        
        self._bot_id = config.bot_id or ""
        self._secret = config.secret or ""
        self._ws_url = config.websocket_url or DEFAULT_WS_URL
        
        # WebSocket 连接
        self._session: Optional[Any] = None
        self._ws: Optional[Any] = None
        self._http_client: Optional[Any] = None
        
        # 任务
        self._listen_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        
        # 响应管理
        self._pending_responses: Dict[str, asyncio.Future] = {}
        
        # 设备 ID
        self._device_id = uuid.uuid4().hex
        
        # 重连退避
        self._backoff_idx = 0
    
    async def connect(self) -> bool:
        """连接到企业微信 WebSocket Gateway"""
        try:
            import aiohttp
            import httpx
        except ImportError:
            logger.error("[WeCom] aiohttp or httpx not installed. Run: pip install aiohttp httpx")
            return False
        
        if not self._bot_id or not self._secret:
            logger.error("[WeCom] bot_id and secret are required")
            return False
        
        try:
            # HTTP 客户端
            from app.gateway.platforms._http_client_limits import platform_httpx_limits
            
            limits = platform_httpx_limits()
            self._http_client = httpx.AsyncClient(
                timeout=30.0,
                follow_redirects=True,
                limits=limits if limits else httpx.Limits(),
            )
            
            # WebSocket 连接
            self._session = aiohttp.ClientSession(trust_env=True)
            self._ws = await self._session.ws_connect(
                self._ws_url,
                heartbeat=HEARTBEAT_INTERVAL * 2,
                timeout=CONNECT_TIMEOUT,
            )
            
            # 订阅
            req_id = self._new_req_id("subscribe")
            await self._send_json({
                "cmd": CMD_SUBSCRIBE,
                "headers": {"req_id": req_id},
                "body": {
                    "bot_id": self._bot_id,
                    "secret": self._secret,
                    "device_id": self._device_id,
                }
            })
            
            # 等待订阅确认
            response = await self._wait_response(req_id, timeout=CONNECT_TIMEOUT)
            if not response:
                raise RuntimeError("Subscription timeout")
            
            errcode = response.get("errcode", 0)
            if errcode not in {0, None}:
                raise RuntimeError(f"Subscription failed: {response.get('errmsg', 'unknown')}")
            
            logger.info("[WeCom] Connected successfully")
            
            # 标记为已连接
            self._connected = True
            
            # 启动监听和心跳
            self._listen_task = asyncio.create_task(self._listen_loop())
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            
            return True
            
        except Exception as e:
            logger.error("[WeCom] Connection failed: %s", e, exc_info=True)
            self._last_error = f"连接失败: {e}"
            await self._cleanup()
            return False
    
    async def disconnect(self) -> None:
        """断开连接"""
        self._running = False
        
        # 取消任务
        for task in [self._listen_task, self._heartbeat_task]:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        
        await self._cleanup()
        logger.info("[WeCom] Disconnected")
    
    async def _cleanup(self) -> None:
        """清理资源"""
        self._pending_responses.clear()
        
        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._ws = None
        
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
        
        if self._http_client:
            await self._http_client.aclose()
        self._http_client = None
    
    async def _send_json(self, payload: Dict[str, Any]) -> None:
        """发送 JSON 消息"""
        if not self._ws or self._ws.closed:
            raise RuntimeError("WebSocket not connected")
        await self._ws.send_json(payload)
    
    async def _send_request(self, cmd: str, body: Dict[str, Any], timeout: float = REQUEST_TIMEOUT) -> Dict[str, Any]:
        """发送请求并等待响应"""
        if not self._ws or self._ws.closed:
            raise RuntimeError("WebSocket not connected")
        
        req_id = self._new_req_id(cmd)
        future = asyncio.get_running_loop().create_future()
        self._pending_responses[req_id] = future
        
        try:
            await self._send_json({"cmd": cmd, "headers": {"req_id": req_id}, "body": body})
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending_responses.pop(req_id, None)
    
    async def _wait_response(self, req_id: str, timeout: float = CONNECT_TIMEOUT) -> Optional[Dict[str, Any]]:
        """等待指定 req_id 的响应"""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return None
            msg = await asyncio.wait_for(self._ws.receive(), timeout=remaining)
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    payload = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                
                # Ping 处理
                if payload.get("cmd") == CMD_PING:
                    await self._send_json({"cmd": CMD_PING, "headers": {}, "body": {}})
                    continue
                
                headers = payload.get("headers", {})
                if headers.get("req_id") == req_id:
                    return payload
            elif msg.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR}:
                return None
    
    async def _listen_loop(self) -> None:
        """监听消息循环"""
        self._backoff_idx = 0
        
        while self._running:
            try:
                await self._read_messages()
                self._backoff_idx = 0
            except asyncio.CancelledError:
                return
            except Exception as e:
                if not self._running:
                    return
                logger.warning("[WeCom] WebSocket error: %s", e)
                
                delay = RECONNECT_BACKOFF[min(self._backoff_idx, len(RECONNECT_BACKOFF) - 1)]
                self._backoff_idx += 1
                
                logger.info("[WeCom] Reconnecting in %ds...", delay)
                await asyncio.sleep(delay)
                
                try:
                    await self._reconnect()
                except Exception as e:
                    logger.warning("[WeCom] Reconnect failed: %s", e)
    
    async def _read_messages(self) -> None:
        """读取 WebSocket 消息"""
        while self._running and self._ws and not self._ws.closed:
            msg = await self._ws.receive()
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    payload = json.loads(msg.data)
                    await self._dispatch_payload(payload)
                except json.JSONDecodeError:
                    logger.debug("[WeCom] Failed to parse message")
                except Exception as e:
                    logger.error("[WeCom] Error dispatching message: %s", e, exc_info=True)
            elif msg.type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR}:
                raise RuntimeError("WebSocket closed")
    
    async def _dispatch_payload(self, payload: Dict[str, Any]) -> None:
        """分发消息"""
        cmd = payload.get("cmd", "")
        headers = payload.get("headers", {})
        req_id = headers.get("req_id", "")
        
        # 响应等待中的请求
        if req_id and req_id in self._pending_responses:
            future = self._pending_responses[req_id]
            if not future.done():
                future.set_result(payload)
            return
        
        # 消息回调
        if cmd == CMD_CALLBACK:
            await self._on_message(payload)
    
    async def _on_message(self, payload: Dict[str, Any]) -> None:
        """处理收到的消息"""
        body = payload.get("body", {})
        if not isinstance(body, dict):
            return
        
        msg_id = body.get("msgid") or ""
        sender = body.get("from", {}) if isinstance(body.get("from"), dict) else {}
        sender_id = str(sender.get("userid", "")).strip()
        if not msg_id:
            text = body.get("text", "") or ""
            msg_id = f"{sender_id}:{text[:50]}"
        chat_id = str(body.get("chatid", sender_id)).strip()
        
        if not chat_id:
            return
        
        is_group = str(body.get("chattype", "")).lower() == "group"
        msgtype = str(body.get("msgtype", "")).lower()
        
        # 提取文本
        text_parts = []
        if msgtype == "text":
            text_block = body.get("text", {})
            if isinstance(text_block, dict):
                text = text_block.get("content", "").strip()
                if text:
                    text_parts.append(text)
        elif msgtype == "voice":
            # 语音消息 - 获取文本内容
            voice_block = body.get("voice", {})
            if isinstance(voice_block, dict):
                voice_text = voice_block.get("content", "").strip()
                if voice_text:
                    text_parts.append(voice_text)
        elif msgtype == "appmsg":
            # 应用消息 - 获取标题
            appmsg = body.get("appmsg", {})
            if isinstance(appmsg, dict):
                title = appmsg.get("title", "").strip()
                if title:
                    text_parts.append(title)
        
        # 提取回复上下文
        reply_text = None
        quote = body.get("quote")
        if isinstance(quote, dict):
            quote_type = str(quote.get("msgtype", "")).lower()
            if quote_type == "text":
                quote_text = quote.get("text", {})
                if isinstance(quote_text, dict):
                    reply_text = quote_text.get("content", "").strip()
            elif quote_type == "voice":
                quote_voice = quote.get("voice", {})
                if isinstance(quote_voice, dict):
                    reply_text = quote_voice.get("content", "").strip()
        
        text = "\n".join(text_parts).strip()
        
        # 群聊时去除 @mention 前缀
        if is_group and text:
            text = re.sub(r"^@\S+\s*", "", text).strip()
        
        # 提取媒体
        media_urls = []
        media_types = []
        
        if msgtype == "image":
            image = body.get("image", {})
            if isinstance(image, dict):
                url = image.get("url", "")
                if url:
                    media_urls.append(url)
                    media_types.append("image")
        
        if msgtype == "file":
            file_info = body.get("file", {})
            if isinstance(file_info, dict):
                filename = file_info.get("filename", "file")
                url = file_info.get("url", "")
                if url:
                    media_urls.append(url)
                    media_types.append(filename)
        
        if not text and not media_urls:
            logger.debug("[WeCom] Empty message skipped")
            return
        
        # 构建消息事件
        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT if text else MessageType.FILE,
            message_id=msg_id,
            chat_id=chat_id,
            user_id=sender_id,
            user_name=sender_id,
            platform=Platform.WECOM,
            chat_type="group" if is_group else "dm",
            media_urls=media_urls,
            media_types=media_types,
            metadata={"reply_to": reply_text} if reply_text else {},
        )
        
        await self.handle_message(event)
    
    async def _heartbeat_loop(self) -> None:
        """心跳循环"""
        try:
            while self._running:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                if self._ws and not self._ws.closed:
                    try:
                        await self._send_json({"cmd": CMD_PING, "headers": {"req_id": self._new_req_id("ping")}, "body": {}})
                    except Exception as e:
                        logger.debug("[WeCom] Heartbeat failed: %s", e)
        except asyncio.CancelledError:
            pass
    
    async def _reconnect(self) -> None:
        """重新连接"""
        await self._cleanup()
        success = await self.connect()
        if success:
            logger.info("[WeCom] Reconnected successfully")
            self._connected = True
    
    async def send(self, chat_id: str, content: str, **kwargs) -> SendResult:
        """发送消息"""
        if not self._ws or self._ws.closed:
            return SendResult(success=False, error="Not connected", retryable=True)
        
        try:
            # 截断长消息
            chunks = self.truncate_message(content)
            
            for i, chunk in enumerate(chunks):
                # 企业微信使用 markdown 格式
                response = await self._send_request(CMD_SEND, {
                    "bot_id": self._bot_id,
                    "chat_id": chat_id,
                    "msg_type": "markdown",
                    "content": chunk,
                })
                
                if i == 0 and response:
                    errcode = response.get("errcode", 0)
                    if errcode != 0:
                        return SendResult(
                            success=False,
                            error=response.get("errmsg", "Send failed"),
                            retryable=errcode in {40001, 40014},  # token 相关错误可重试
                        )
            
            return SendResult(success=True)
            
        except asyncio.TimeoutError:
            return SendResult(success=False, error="Request timeout", retryable=True)
        except Exception as e:
            logger.error("[WeCom] Send failed: %s", e, exc_info=True)
            return SendResult(success=False, error=str(e), retryable=True)
    
    async def send_image(self, chat_id: str, image_path: str, **kwargs) -> SendResult:
        """发送图片"""
        if not self._ws or self._ws.closed:
            return SendResult(success=False, error="Not connected", retryable=True)
        
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
            
            # 上传媒体
            with open(image_path, "rb") as f:
                file_data = f.read()
            
            # 上传到企业微信
            upload_response = await self._send_request("aibot_upload_media", {
                "bot_id": self._bot_id,
                "file_name": Path(image_path).name,
                "file_size": len(file_data),
            })
            
            media_id = upload_response.get("body", {}).get("media_id")
            if not media_id:
                return SendResult(success=False, error="Failed to upload media")
            
            # 发送图片消息
            result = await self._send_request(CMD_SEND, {
                "bot_id": self._bot_id,
                "chat_id": chat_id,
                "msg_type": "image",
                "media_id": media_id,
            })
            
            return SendResult(
                success=result.get("errcode", 0) == 0,
                error=result.get("errmsg") if result.get("errcode", 0) != 0 else None,
            )
            
        except Exception as e:
            logger.error("[WeCom] Send image failed: %s", e, exc_info=True)
            return SendResult(success=False, error=str(e), retryable=True)
    
    async def send_file(self, chat_id: str, file_path: str, **kwargs) -> SendResult:
        """发送文件"""
        # 实现类似 send_image，但使用 file 类型
        if not self._ws or self._ws.closed:
            return SendResult(success=False, error="Not connected", retryable=True)
        
        try:
            from pathlib import Path
            
            file_path = str(file_path)
            if file_path.startswith("http"):
                # 下载远程文件
                import httpx
                cache_dir = get_cache_dir("files")
                cache_dir.mkdir(parents=True, exist_ok=True)
                
                async with httpx.AsyncClient(timeout=60.0) as client:
                    response = await client.get(file_path)
                    response.raise_for_status()
                    
                    filename = Path(file_path).name or "file"
                    filepath = cache_dir / filename
                    filepath.write_bytes(response.content)
                    file_path = str(filepath)
            
            with open(file_path, "rb") as f:
                file_data = f.read()
            
            # 上传媒体
            upload_response = await self._send_request("aibot_upload_media", {
                "bot_id": self._bot_id,
                "file_name": Path(file_path).name,
                "file_size": len(file_data),
            })
            
            media_id = upload_response.get("body", {}).get("media_id")
            if not media_id:
                return SendResult(success=False, error="Failed to upload media")
            
            # 发送文件消息
            result = await self._send_request(CMD_SEND, {
                "bot_id": self._bot_id,
                "chat_id": chat_id,
                "msg_type": "file",
                "media_id": media_id,
            })
            
            return SendResult(
                success=result.get("errcode", 0) == 0,
                error=result.get("errmsg") if result.get("errcode", 0) != 0 else None,
            )
            
        except Exception as e:
            logger.error("[WeCom] Send file failed: %s", e, exc_info=True)
            return SendResult(success=False, error=str(e), retryable=True)
    
    async def get_chat_info(self, chat_id: str) -> ChatInfo:
        """获取聊天信息"""
        # 企业微信 AI Bot 不提供直接的聊天信息查询
        return ChatInfo(
            name=chat_id,
            type="dm" if not chat_id.startswith("R:") else "group",
            chat_id=chat_id,
        )
    
    @staticmethod
    def _new_req_id(prefix: str) -> str:
        """生成请求 ID"""
        return f"{prefix}-{uuid.uuid4().hex}"


def check_wecom_requirements() -> bool:
    """检查企业微信依赖是否满足"""
    try:
        import aiohttp
        import httpx
        return True
    except ImportError:
        return False