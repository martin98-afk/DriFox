# -*- coding: utf-8 -*-
"""
Gateway 会话管理器

管理企业微信/钉钉的独立会话，与桌面端会话隔离。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from loguru import logger

from app.gateway.base import Platform, MessageEvent
from app.utils.utils import get_app_data_dir



@dataclass
class GatewaySession:
    """Gateway 会话"""
    session_id: str
    platform: Platform
    user_id: str
    chat_id: str
    user_name: str = ""
    created_at: datetime = field(default_factory=datetime.now)
    last_active: datetime = field(default_factory=datetime.now)
    message_count: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    @property
    def display_name(self) -> str:
        """显示名称"""
        if self.user_name:
            return f"{self.user_name} ({self.platform.value})"
        return f"{self.user_id[:8]}... ({self.platform.value})"


class GatewaySessionManager:
    """
    Gateway 会话管理器
    
    管理企业微信/钉钉的独立会话：
    - 每个用户创建一个会话
    - 会话与桌面端完全隔离
    - 支持 /new, /reset 等命令
    """
    
    def __init__(self, data_dir: Optional[Path] = None):
        """
        初始化会话管理器
        
        Args:
            data_dir: 数据目录，默认为 ~/.drifox/gateway
        """
        if data_dir is None:
            data_dir = get_app_data_dir() / "gateway"
        self._data_dir = data_dir
        self._data_dir.mkdir(parents=True, exist_ok=True)
        
        # 内存中的会话: session_id -> GatewaySession
        self._sessions: Dict[str, GatewaySession] = {}
        
        # 平台用户索引: platform -> user_id -> session_id
        self._user_sessions: Dict[Platform, Dict[str, str]] = {
            Platform.WECOM: {},
            Platform.DINGTALK: {},
        }
        
        # 回调: 创建新会话时调用
        self._on_create_session: Optional[Callable[[GatewaySession], None]] = None
        
        # 加载现有会话
        self._load_sessions()
    
    def _load_sessions(self) -> None:
        """加载会话列表"""
        sessions_file = self._data_dir / "sessions.json"
        if sessions_file.exists():
            try:
                import json
                with open(sessions_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                
                for session_id, info in data.items():
                    session = GatewaySession(
                        session_id=session_id,
                        platform=Platform(info["platform"]),
                        user_id=info["user_id"],
                        chat_id=info["chat_id"],
                        user_name=info.get("user_name", ""),
                        created_at=datetime.fromisoformat(info.get("created_at", datetime.now().isoformat())),
                        last_active=datetime.fromisoformat(info.get("last_active", datetime.now().isoformat())),
                        message_count=info.get("message_count", 0),
                    )
                    self._sessions[session_id] = session
                    
                    # 建立索引
                    user_sessions = self._user_sessions.get(session.platform)
                    if user_sessions is not None:
                        user_sessions[session.user_id] = session_id
                        
            except Exception as e:
                logger.warning(f"[GatewaySession] Failed to load sessions: {e}")
    
    def _save_sessions(self) -> None:
        """保存会话列表"""
        sessions_file = self._data_dir / "sessions.json"
        try:
            import json
            data = {}
            for session_id, session in self._sessions.items():
                data[session_id] = {
                    "platform": session.platform.value,
                    "user_id": session.user_id,
                    "chat_id": session.chat_id,
                    "user_name": session.user_name,
                    "created_at": session.created_at.isoformat(),
                    "last_active": session.last_active.isoformat(),
                    "message_count": session.message_count,
                }
            
            with open(sessions_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[GatewaySession] Failed to save sessions: {e}")
    
    def _generate_session_id(self, platform: Platform, user_id: str) -> str:
        """生成会话 ID"""
        import hashlib
        timestamp = str(int(time.time() * 1000))
        raw = f"{platform.value}:{user_id}:{timestamp}"
        return hashlib.md5(raw.encode()).hexdigest()[:16]
    
    def get_or_create_session(self, event: MessageEvent) -> GatewaySession:
        """
        获取或创建会话
        
        Args:
            event: 消息事件
            
        Returns:
            GatewaySession
        """
        platform = event.platform
        user_id = event.user_id
        chat_id = event.chat_id
        
        # 查找现有会话
        user_sessions = self._user_sessions.get(platform, {})
        session_id = user_sessions.get(user_id)
        
        if session_id and session_id in self._sessions:
            session = self._sessions[session_id]
            session.last_active = datetime.now()
            session.message_count += 1
            if event.user_name and not session.user_name:
                session.user_name = event.user_name
            return session
        
        # 创建新会话
        session_id = self._generate_session_id(platform, user_id)
        session = GatewaySession(
            session_id=session_id,
            platform=platform,
            user_id=user_id,
            chat_id=chat_id,
            user_name=event.user_name or "",
        )
        
        self._sessions[session_id] = session
        
        # 建立索引
        if platform not in self._user_sessions:
            self._user_sessions[platform] = {}
        self._user_sessions[platform][user_id] = session_id
        
        # 保存
        self._save_sessions()
        
        # 回调
        if self._on_create_session:
            try:
                self._on_create_session(session)
            except Exception as e:
                logger.warning(f"[GatewaySession] Create callback error: {e}")
        
        logger.info(f"[GatewaySession] Created session {session_id} for {user_id} on {platform.value}")
        
        return session
    
    def get_session(self, session_id: str) -> Optional[GatewaySession]:
        """获取会话"""
        return self._sessions.get(session_id)
    
    def get_session_by_platform_user(self, platform: Platform, user_id: str) -> Optional[GatewaySession]:
        """通过平台和用户 ID 获取会话"""
        user_sessions = self._user_sessions.get(platform, {})
        session_id = user_sessions.get(user_id)
        if session_id:
            return self._sessions.get(session_id)
        return None
    
    def delete_session(self, session_id: str) -> bool:
        """
        删除会话
        
        Returns:
            True 如果删除成功
        """
        if session_id not in self._sessions:
            return False
        
        session = self._sessions[session_id]
        
        # 移除索引
        user_sessions = self._user_sessions.get(session.platform)
        if user_sessions:
            user_sessions.pop(session.user_id, None)
        
        # 删除会话文件
        session_file = self._data_dir / f"{session_id}.json"
        if session_file.exists():
            session_file.unlink()
        
        # 移除会话
        del self._sessions[session_id]
        
        # 保存
        self._save_sessions()
        
        logger.info(f"[GatewaySession] Deleted session {session_id}")
        return True
    
    def reset_session(self, session_id: str) -> bool:
        """
        重置会话（清除历史但不删除）
        """
        session = self._sessions.get(session_id)
        if not session:
            return False
        
        # 删除会话文件
        session_file = self._data_dir / f"{session_id}.json"
        if session_file.exists():
            session_file.unlink()
        
        session.message_count = 0
        session.last_active = datetime.now()
        
        logger.info(f"[GatewaySession] Reset session {session_id}")
        return True
    
    def list_sessions(self, platform: Optional[Platform] = None) -> List[GatewaySession]:
        """
        列出会话
        
        Args:
            platform: 可选，按平台过滤
            
        Returns:
            会话列表
        """
        sessions = list(self._sessions.values())
        
        if platform:
            sessions = [s for s in sessions if s.platform == platform]
        
        # 按最后活跃时间排序
        sessions.sort(key=lambda s: s.last_active, reverse=True)
        
        return sessions
    
    def list_sessions_by_chat(self, platform: Platform, chat_id: str) -> List[GatewaySession]:
        """列出指定聊天的所有会话"""
        return [
            s for s in self._sessions.values()
            if s.platform == platform and s.chat_id == chat_id
        ]
    
    def set_create_callback(self, callback: Callable[[GatewaySession], None]) -> None:
        """设置创建会话回调"""
        self._on_create_session = callback
    
    @property
    def session_count(self) -> int:
        """会话数量"""
        return len(self._sessions)
    
    @property
    def session_count_by_platform(self) -> Dict[Platform, int]:
        """各平台会话数量"""
        return {
            platform: len([s for s in self._sessions.values() if s.platform == platform])
            for platform in [Platform.WECOM, Platform.DINGTALK]
        }