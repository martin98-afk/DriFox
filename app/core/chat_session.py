"""
ChatSession & SessionManager - 会话管理模块

负责 DriFox 聊天会话的创建、存储、压缩和生命周期管理。

主要组件：
- ChatSession: 单个会话的数据模型，包含消息历史、元数据和压缩状态
- SessionManager: 会话集合管理器，提供会话的增删改查、SQLite 持久化和自动压缩功能

设计要点：
- 内存缓存：SessionManager 在内存中缓存最近活跃的会话（默认最多 15 个）
- 持久化：会话通过 SQLite 数据库长期存储，加载时采用懒加载策略
- 压缩合并：支持多轮对话的历史压缩合并（保留摘要或固定尾部）
"""

import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from PyQt5.QtCore import QObject

from app.core.message_content import consolidate_messages
from app.core.token_estimator import count_messages_tokens

# 内存泄漏修复：会话消息数软限制
MAX_SESSION_MESSAGES = 500

# 默认最大缓存会话数（内存中同时保留的会话）
DEFAULT_MAX_CACHED_SESSIONS = 15


class ChatSession:
    def __init__(self, name: str = None, messages: Optional[List[Dict]] = None):
        self.session_id: str = uuid.uuid4().hex
        self.name = name or f"对话 {datetime.now().strftime('%m-%d %H:%M')}"
        self.messages: List[Dict[str, str]] = consolidate_messages(messages or [])
        # topic_summary 初始化为 name 的副本，确保首次保存时 title 字段不为空
        self.topic_summary: str = self.name
        self.user_edited_title: bool = False  # 用户是否手动编辑过标题
        self.created_at: str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.last_updated: str = self.created_at
        self.message_count: int = len(self.messages)
        self.compaction_state: Dict = self._default_compaction_state()
        self.compaction_cache: Dict = self._default_compaction_cache()
        self.system_prompt: str = ""
        self._system_prompt_agent: str = ""  # 缓存 system prompt 对应的 agent 名
        self.metadata: Dict[str, Any] = {}  # 扩展元数据（如模型/Agent 覆盖）
        self.context_usage: int = 0  # 消息列表估算 token 总数，保存时计算
        self.last_api_prompt_tokens: int = 0  # 最近一次 API 返回的 prompt_tokens，作为上下文占用权威值
        self.last_api_message_count: int = 0  # 上次 API 调用时的 session.messages 长度，用于增量估算
        self.last_api_prompt_from_usage: bool = False  # last_api_prompt_tokens 是否来自真实 API usage（非本地估算）
        # 🛡️ 首发项目快照：用户在哪个项目下首发的对话。
        # 用于"对话进行中切换项目导致落盘错存"bug 的兜底：
        # 一旦锁定不再改变，即使后续切换项目，会话仍归属首发项目。
        self.originating_project: str = ""

    @staticmethod
    def _default_compaction_state() -> Dict:
        return {
            "active": False,
            "source": "",
            "kind": "",
            "original_count": 0,
            "summarized_count": 0,
            "kept_count": 0,
            "summary_count": 0,
            "note": "",
        }

    @staticmethod
    def _default_compaction_cache() -> Dict:
        return {
            "active": False,
            "kind": "",
            "cutoff_index": 0,
            "source_message_count": 0,
            "summarized_count": 0,
            "tail_count": 0,
            "budget_tokens": 0,
            "summary_message": None,
            "generated_at": "",
        }

    def get_context_messages(self) -> List[Dict[str, str]]:
        return consolidate_messages(self.messages)

    def set_messages(self, messages: List[Dict], preserve_compaction: bool = False):
        self.messages = consolidate_messages(messages or [])
        if not preserve_compaction:
            self.reset_compaction_cache()
            self.reset_compaction_state()
        self._update_timestamp()

    def add_assistant_message(self, content: str, model_name: str = None, provider_name: str = None):
        msg = {
            "role": "assistant",
            "content": content,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        if model_name:
            msg["model_name"] = model_name
        if provider_name:
            msg["provider_name"] = provider_name
        self.messages.append(msg)
        # 追加操作不走全量 consolidate，由持久化层在 save 时统一做
        self._update_timestamp()

    def add_user_message(self, content, **kwargs):
        """添加用户消息，支持 str 和 list（multimodal content）"""
        msg = {"role": "user", "content": content, "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        if kwargs.get("params"):
            msg["params"] = kwargs["params"]
        hook_event = kwargs.get("_hook_event")
        if hook_event:
            msg["_hook_event"] = hook_event
        self.messages.append(msg)
        # 追加操作不走全量 consolidate，由持久化层在 save 时统一做
        self._update_timestamp()

    def _update_timestamp(self):
        self.last_updated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.message_count = len(self.messages)
        # 增量模式下避免每次 append 都遍历全量消息算 token；
        # context_usage 在 save 时由持久化层统一计算
        if self.context_usage == 0 and self.messages:
            self.context_usage = count_messages_tokens(self.messages)

    def set_topic_summary(self, summary: str):
        self.topic_summary = summary
        # 同步更新 name，使 DB 保存时 title 字段一致
        if summary and not summary.startswith("对话 "):
            self.name = summary

    def set_compaction_state(self, state: Optional[Dict] = None):
        merged = self._default_compaction_state()
        if state:
            merged.update(state)
        self.compaction_state = merged

    def reset_compaction_state(self):
        self.compaction_state = self._default_compaction_state()

    def set_compaction_cache(self, cache: Optional[Dict] = None):
        merged = self._default_compaction_cache()
        if cache:
            merged.update(cache)
        self.compaction_cache = merged

    def reset_compaction_cache(self):
        self.compaction_cache = self._default_compaction_cache()

    def invalidate_compaction(self):
        self.reset_compaction_cache()
        self.reset_compaction_state()

    def get_recent_messages(self, count: int = 10) -> List[Dict]:
        return self.messages[-count:] if self.messages else []

    def clear(self):
        self.messages.clear()
        self.topic_summary = ""
        self.invalidate_compaction()
        self._update_timestamp()

    def to_dict(self, consolidated: bool = True) -> Dict:
        """转换为字典

        Args:
            consolidated: 是否在导出时调用 consolidate_messages。
                          save 路径传 True，临时读取传 False 以节省性能。
        """
        msgs = consolidate_messages(self.messages) if consolidated else self.messages
        return {
            "session_id": self.session_id,
            "name": self.name,
            "messages": msgs,
            "topic_summary": self.topic_summary,
            "created_at": self.created_at,
            "last_updated": self.last_updated,
            "message_count": len(msgs),
            "compaction_state": self.compaction_state,
            "compaction_cache": self.compaction_cache,
            "system_prompt": self.system_prompt,
            "user_edited_title": self.user_edited_title,
            "metadata": self.metadata,
            "context_usage": self.context_usage,
            "last_api_prompt_tokens": self.last_api_prompt_tokens,
            "last_api_message_count": self.last_api_message_count,
            "last_api_prompt_from_usage": self.last_api_prompt_from_usage,
            "originating_project": self.originating_project,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "ChatSession":
        # 使用 data 中的 name 或 topic_summary 初始化（两者在 DB 层已统一）
        raw_name = data.get("name") or data.get("topic_summary") or data.get("name", "")
        session = cls(name=raw_name, messages=data.get("messages", []))
        session.session_id = data.get("session_id") or session.session_id
        # 通过 set_topic_summary 同步 name 和 topic_summary
        summary = data.get("topic_summary", "")
        if summary:
            session.set_topic_summary(summary)
        session.created_at = data.get("created_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        session.last_updated = data.get("last_updated", session.created_at)
        session.message_count = len(session.messages)
        session.set_compaction_state(data.get("compaction_state"))
        session.set_compaction_cache(data.get("compaction_cache"))
        session.system_prompt = data.get("system_prompt", "") or ""
        session.metadata = data.get("metadata", {}) or {}
        session.user_edited_title = data.get("user_edited_title", False)
        session.context_usage = data.get("context_usage", 0)
        session.last_api_prompt_tokens = data.get("last_api_prompt_tokens", 0)
        session.last_api_message_count = data.get("last_api_message_count", 0)
        session.last_api_prompt_from_usage = data.get("last_api_prompt_from_usage", False)
        session.originating_project = data.get("originating_project", "") or ""
        return session

    def set_user_edited_title(self, edited: bool = True):
        """标记标题已被用户编辑，后续不会自动覆盖"""
        self.user_edited_title = edited


class SessionManager(QObject):
    def __init__(self, max_cached: int = DEFAULT_MAX_CACHED_SESSIONS):
        super().__init__()
        self.sessions: List[ChatSession] = []
        self._last_access: Dict[str, float] = {}  # session_id -> last access time (timestamp)
        self.max_cached_sessions: int = max_cached
        self.current_index = -1

    def create_new_session(self) -> ChatSession:
        session = ChatSession()
        self.sessions.append(session)
        self.current_index = len(self.sessions) - 1
        self._touch_session(session.session_id)
        self._evict_if_needed()

        return session

    def get_current_session(self) -> Optional[ChatSession]:
        if 0 <= self.current_index < len(self.sessions):
            session = self.sessions[self.current_index]
            self._touch_session(session.session_id)
            return session
        return None

    def _touch_session(self, session_id: str):
        """更新会话最后访问时间（限制 last_access 字典防无限增长）"""
        import time

        self._last_access[session_id] = time.time()
        # 防内存泄漏：只保留活跃记录（最多 max_cached 的 2 倍）
        if len(self._last_access) > self.max_cached_sessions * 2:
            stale = [sid for sid in self._last_access
                     if sid not in {s.session_id for s in self.sessions}]
            for sid in stale:
                self._last_access.pop(sid, None)

    def _evict_if_needed(self):
        """如果超过最大缓存数，淘汰最久未访问的非当前会话（O(n) 优化版）"""
        n_sessions = len(self.sessions)
        if n_sessions <= self.max_cached_sessions:
            return

        # 获取当前会话ID，不淘汰当前会话
        current_session = self.get_current_session()
        current_id = current_session.session_id if current_session else None

        # 建立 session_id → index 的快速映射（O(n)）
        session_index = {s.session_id: i for i, s in enumerate(self.sessions)}

        # 找出所有非当前会话，按访问时间排序（最久未访问在前）
        candidates = [
            (sid, t)
            for sid, t in self._last_access.items()
            if sid != current_id and sid in session_index
        ]
        if not candidates:
            return

        # 按访问时间升序排列，最早的排前面
        candidates.sort(key=lambda x: x[1])

        # 淘汰最久未访问的，直到满足最大缓存限制
        to_remove = n_sessions - self.max_cached_sessions
        removed = 0
        for sid, _ in candidates:
            if removed >= to_remove:
                break
            idx = session_index.get(sid)
            if idx is None:
                continue
            self.sessions.pop(idx)
            self._last_access.pop(sid, None)
            removed += 1
            # 调整当前索引
            if idx <= self.current_index and self.current_index > 0:
                self.current_index -= 1
            # 后续索引前移，更新映射
            session_index = {s.session_id: i for i, s in enumerate(self.sessions)}

    def set_max_cached_sessions(self, max_cached: int):
        """设置最大缓存会话数"""
        self.max_cached_sessions = max_cached
        self._evict_if_needed()

    def switch_to_session(self, index: int):
        if 0 <= index < len(self.sessions):
            self.current_index = index
            # 更新访问时间
            session = self.sessions[index]
            self._touch_session(session.session_id)
            # 如果超过最大缓存数，淘汰最久未访问的非当前会话
            self._evict_if_needed()

    def get_session_names(self) -> List[str]:
        return [s.name for s in self.sessions]

    def set_session_from_messages(self, messages: List[Dict]):
        if self.current_index < 0:
            self.current_index = 0
        if self.current_index >= len(self.sessions):
            self.sessions.append(ChatSession(messages=messages.copy()))
        else:
            self.sessions[self.current_index] = ChatSession(messages=messages.copy())

    def set_current_session(self, session: ChatSession):
        if self.current_index < 0:
            self.sessions.append(session)
            self.current_index = len(self.sessions) - 1
            self._touch_session(session.session_id)
            self._evict_if_needed()
            return
        if self.current_index >= len(self.sessions):
            self.sessions.append(session)
            self.current_index = len(self.sessions) - 1
            self._touch_session(session.session_id)
            self._evict_if_needed()
            return
        self.sessions[self.current_index] = session
        self._touch_session(session.session_id)
        self._evict_if_needed()

    def delete_session(self, index: int) -> bool:
        if 0 <= index < len(self.sessions):
            session = self.sessions[index]
            # 从访问记录中移除
            self._last_access.pop(session.session_id, None)
            self.sessions.pop(index)
            if self.current_index >= len(self.sessions):
                self.current_index = len(self.sessions) - 1
            return True
        return False

    def get_all_sessions(self) -> List[ChatSession]:
        return self.sessions.copy()
