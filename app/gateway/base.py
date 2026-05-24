# -*- coding: utf-8 -*-
"""
Gateway 基础模块 - 抽象基类和数据结构
"""
from __future__ import annotations

import asyncio
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TypeVar
from loguru import logger

from app.utils.utils import get_app_data_dir

T = TypeVar("T")


class Platform(Enum):
    """支持的通讯平台"""
    WECOM = "wecom"
    DINGTALK = "dingtalk"
    TELEGRAM = "telegram"
    DISCORD = "discord"
    WHATSAPP = "whatsapp"
    FEISHU = "feishu"
    SLACK = "slack"


class MessageType(Enum):
    """消息类型"""
    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    FILE = "file"
    COMMAND = "command"  # /command 样式命令


@dataclass
class MessageEvent:
    """
    标准化的消息事件
    
    所有平台适配器接收的消息都会被转换为这个统一格式。
    """
    # 消息内容
    text: str = ""
    message_type: MessageType = MessageType.TEXT
    
    # 来源信息
    message_id: str = ""
    chat_id: str = ""
    user_id: str = ""
    user_name: str = ""
    platform: Platform = Platform.WECOM
    chat_type: str = "dm"  # dm / group
    
    # 媒体附件
    media_urls: List[str] = field(default_factory=list)
    media_types: List[str] = field(default_factory=list)
    
    # 时间戳
    timestamp: datetime = field(default_factory=datetime.now)
    
    # 额外数据
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    @property
    def is_command(self) -> bool:
        """检查是否为命令消息"""
        return self.text.startswith("/")
    
    @property
    def command_name(self) -> Optional[str]:
        """获取命令名称"""
        if not self.is_command:
            return None
        parts = self.text.split(maxsplit=1)
        raw = parts[0][1:].lower() if parts else None
        if raw and "@" in raw:
            raw = raw.split("@", 1)[0]
        return raw
    
    @property
    def command_args(self) -> str:
        """获取命令参数"""
        if not self.is_command:
            return ""
        parts = self.text.split(maxsplit=1)
        return parts[1] if len(parts) > 1 else ""


@dataclass
class SendResult:
    """发送结果"""
    success: bool
    message_id: Optional[str] = None
    error: Optional[str] = None
    retryable: bool = False  # 是否可重试


@dataclass
class ChatInfo:
    """聊天信息"""
    name: str = ""
    type: str = "dm"  # dm / group
    chat_id: str = ""
    user_id: str = ""
    user_name: str = ""
    avatar: Optional[str] = None


@dataclass 
class PlatformConfig:
    """平台配置"""
    enabled: bool = False
    platform: Platform = Platform.WECOM
    
    # 企业微信配置
    bot_id: Optional[str] = None
    secret: Optional[str] = None
    websocket_url: Optional[str] = None
    
    # 钉钉配置
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    
    # 通用配置（适用于 Telegram、Discord 等）
    token: Optional[str] = None
    
    # 通用配置
    extra: Dict[str, Any] = field(default_factory=dict)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any], platform: Platform) -> "PlatformConfig":
        """从字典创建配置"""
        config = cls(enabled=data.get("enabled", False), platform=platform)
        
        if platform == Platform.WECOM:
            config.bot_id = data.get("bot_id") or os.getenv("WECOM_BOT_ID")
            config.secret = data.get("secret") or os.getenv("WECOM_SECRET")
            config.websocket_url = data.get("websocket_url") or os.getenv(
                "WECOM_WEBSOCKET_URL", 
                "wss://openws.work.weixin.qq.com"
            )
        elif platform == Platform.DINGTALK:
            config.client_id = data.get("client_id") or os.getenv("DINGTALK_CLIENT_ID")
            config.client_secret = data.get("client_secret") or os.getenv("DINGTALK_CLIENT_SECRET")
        
        config.extra = data.get("extra", {})
        return config


# 消息处理器类型
MessageHandler = Callable[[MessageEvent], Any]


class BasePlatformAdapter(ABC):
    """
    平台适配器抽象基类
    
    所有平台适配器必须继承此类并实现抽象方法。
    平台适配器负责：
    1. 连接到通讯平台
    2. 接收消息
    3. 发送消息/媒体
    4. 管理连接状态
    """
    
    # 子类应该设置这些类属性
    platform: Platform = Platform.WECOM
    name: str = "Base Platform"
    
    # 最大消息长度（子平台可覆盖）
    MAX_MESSAGE_LENGTH: int = 4096
    
    def __init__(self, config: PlatformConfig, message_handler: Optional[MessageHandler] = None):
        """
        初始化适配器
        
        Args:
            config: 平台配置
            message_handler: 消息处理回调
        """
        self.config = config
        self._message_handler = message_handler
        self._running = False
        self._connected = False
        self._last_error: Optional[str] = None
        
        # 会话活跃状态
        self._active_sessions: Dict[str, asyncio.Event] = {}
        self._pending_messages: Dict[str, MessageEvent] = {}
        
        # 消息去重：记录当前正在处理的消息 ID，防止平台重复投递
        self._active_message_ids: Dict[str, str] = {}  # session_key -> message_id
    
    @property
    def is_connected(self) -> bool:
        """检查是否已连接"""
        return self._connected
    
    @property
    def last_error(self) -> Optional[str]:
        """获取最后的错误信息"""
        return self._last_error
    
    def set_message_handler(self, handler: MessageHandler) -> None:
        """设置消息处理器"""
        self._message_handler = handler
    
    @abstractmethod
    async def connect(self) -> bool:
        """
        连接到平台
        
        Returns:
            True 如果连接成功
        """
        pass
    
    @abstractmethod
    async def disconnect(self) -> None:
        """断开与平台的连接"""
        pass
    
    @abstractmethod
    async def send(self, chat_id: str, content: str, **kwargs) -> SendResult:
        """
        发送文本消息
        
        Args:
            chat_id: 聊天ID
            content: 消息内容
            **kwargs: 额外参数
            
        Returns:
            SendResult
        """
        pass
    
    async def send_image(self, chat_id: str, image_path: str, **kwargs) -> SendResult:
        """
        发送图片
        
        默认实现为发送图片路径作为文本。子平台可覆盖实现原生发送。
        """
        return SendResult(
            success=False,
            error="Platform does not support native image sending"
        )
    
    async def send_file(self, chat_id: str, file_path: str, **kwargs) -> SendResult:
        """
        发送文件
        
        默认实现为发送文件路径作为文本。子平台可覆盖实现原生发送。
        """
        return SendResult(
            success=False,
            error="Platform does not support native file sending"
        )
    
    async def send_voice(self, chat_id: str, audio_path: str, **kwargs) -> SendResult:
        """
        发送语音
        
        默认实现为发送音频路径作为文本。子平台可覆盖实现原生发送。
        """
        return SendResult(
            success=False,
            error="Platform does not support native voice sending"
        )
    
    @abstractmethod
    async def get_chat_info(self, chat_id: str) -> ChatInfo:
        """
        获取聊天信息
        
        Args:
            chat_id: 聊天ID
            
        Returns:
            ChatInfo
        """
        pass
    
    async def handle_message(self, event: MessageEvent) -> None:
        """
        处理接收到的消息
        
        模板方法：处理消息并调用注册的 handler。
        子平台应该在消息接收时调用此方法。
        """
        if not self._message_handler:
            logger.warning(f"[{self.name}] No message handler configured")
            return
        
        session_key = self._get_session_key(event)
        
        # 消息去重：如果正在处理的消息 ID 与新消息相同，丢弃重复投递
        active_msg_id = self._active_message_ids.get(session_key)
        if active_msg_id and event.message_id and active_msg_id == event.message_id:
            logger.info(f"[{self.name}] Dropping duplicate message {event.message_id[:12]} for session: {session_key}")
            return
        
        # 检查是否有活跃会话
        if session_key in self._active_sessions:
            # 活跃会话中：排队消息，但跳过与当前处理中相同 ID 的重复消息
            if event.message_id and active_msg_id == event.message_id:
                logger.info(f"[{self.name}] Dropping queued duplicate {event.message_id[:12]} for busy session: {session_key}")
                return
            logger.info(f"[{self.name}] Queuing message for busy session: {session_key}")
            self._pending_messages[session_key] = event
            self._active_sessions[session_key].set()
            return
        
        # 启动新会话处理
        self._active_sessions[session_key] = asyncio.Event()
        if event.message_id:
            self._active_message_ids[session_key] = event.message_id
        try:
            await self._message_handler(event)
        except Exception as e:
            logger.error(f"[{self.name}] Error handling message: {e}", exc_info=True)
        finally:
            self._active_sessions.pop(session_key, None)
            self._active_message_ids.pop(session_key, None)
            
            # 处理待处理消息（跳过与刚完成的重复消息）
            if session_key in self._pending_messages:
                pending = self._pending_messages.pop(session_key)
                # 去重检查：排队的消息是否与刚处理完的消息相同
                if (event.message_id and pending.message_id
                        and event.message_id == pending.message_id):
                    logger.info(f"[{self.name}] Dropping pending duplicate {pending.message_id[:12]} after session completed: {session_key}")
                    return
                await self.handle_message(pending)
    
    def _get_session_key(self, event: MessageEvent) -> str:
        """获取会话键"""
        return f"{event.platform.value}:{event.chat_id}"
    
    def truncate_message(self, content: str, max_length: Optional[int] = None) -> List[str]:
        """
        截断长消息
        
        Args:
            content: 消息内容
            max_length: 最大长度
            
        Returns:
            消息块列表
        """
        limit = max_length or self.MAX_MESSAGE_LENGTH
        if len(content) <= limit:
            return [content]
        
        chunks = []
        remaining = content
        while remaining:
            chunk = remaining[:limit]
            remaining = remaining[limit:]
            chunks.append(chunk)
        
        return chunks
    
    async def start(self) -> bool:
        """
        启动适配器
        
        Returns:
            True 如果启动成功
        """
        if self._running:
            return True
        
        success = await self.connect()
        if success:
            self._running = True
            self._connected = True
            logger.info(f"[{self.name}] Started")
        return success
    
    async def stop(self) -> None:
        """
        停止适配器
        """
        if not self._running:
            return
        
        self._running = False
        await self.disconnect()
        self._connected = False
        self._active_sessions.clear()
        self._pending_messages.clear()
        self._active_message_ids.clear()
        logger.info(f"[{self.name}] Stopped")


def get_cache_dir(name: str) -> Path:
    """获取缓存目录"""
    cache_dir = get_app_data_dir() / "gateway" / "cache"
    cache_dir = cache_dir / name
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir

def cache_image_from_bytes(data: bytes, ext: str = ".jpg") -> str:
    """缓存图片字节到本地文件"""
    import uuid
    cache_dir = get_cache_dir("images")
    filename = f"img_{uuid.uuid4().hex[:12]}{ext}"
    filepath = cache_dir / filename
    filepath.write_bytes(data)
    return str(filepath)


def cache_file_from_bytes(data: bytes, filename: str) -> str:
    """缓存文件字节到本地"""
    import uuid
    cache_dir = get_cache_dir("files")
    safe_name = Path(filename).name
    cached_name = f"doc_{uuid.uuid4().hex[:12]}_{safe_name}"
    filepath = cache_dir / cached_name
    filepath.write_bytes(data)
    return str(filepath)