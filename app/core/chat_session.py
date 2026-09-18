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

import time
import uuid
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from loguru import logger
from PyQt5.QtCore import QObject

from app.core.message_content import consolidate_messages
from app.core.infra.token_estimator import count_messages_tokens

# 内存泄漏修复：会话消息数软限制
MAX_SESSION_MESSAGES = 500

# 默认最大缓存会话数（内存中同时保留的会话）
DEFAULT_MAX_CACHED_SESSIONS = 15

# 常驻消息体的会话数（当前会话 + 最近访问的前 N-1 个）。
# 其余会话只保留元数据，消息体释放后按需从 SQLite 重载。
# [MEM] 实测（tools/diag_session_mem_probe.py，dev 库 283MB / 1721 会话）：
#   15 个最大会话全量常驻 = RSS +126.8MB（单会话最大 +26.9MB / 228 条）；
#   单会话反序列化重载 22-56ms，用户无感。
# 保留 3 个常驻 → 稳态约 55-60MB，省 ~70MB，且会话对象与索引结构不变。
DEFAULT_KEEP_MESSAGES = 3


class ChatSession:
    def __init__(self, name: str = None, messages: Optional[List[Dict]] = None):
        self.session_id: str = uuid.uuid4().hex
        self.name = name or f"对话 {datetime.now().strftime('%m-%d %H:%M')}"
        # 消息体惰性重载器：由 SessionManager 注入（按 session_id 从 SQLite 重读）
        self._messages_loader: Optional[Callable[[str], Optional[List[Dict]]]] = None
        self._messages_released: bool = False
        # 赋值走 property setter（不加类型注解，避免与 property 声明冲突）
        self.messages = consolidate_messages(messages or [])
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
        # [PERF T33] 发送前处理缓存版本号：消息列表内容/顺序任何变化都自增，
        # 供 ContextBudgetAllocator 的 consolidate+token 估算缓存做失效判定。
        # 缓存本身不持久化（to_dict 不输出），进程内复用。
        self._messages_version: int = 0
        self._send_prep_cache: Optional[Dict] = None

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

    # ── 消息体：惰性重载（内存治理）──────────────────────────────
    @property
    def messages(self) -> List[Dict[str, str]]:
        """消息列表。已释放时经 loader 自动重载，对调用方透明。"""
        if self._messages_released:
            self._reload_messages()
        return self._messages

    @messages.setter
    def messages(self, value: Optional[List[Dict]]) -> None:
        self._messages = value or []
        self._messages_released = False
        # [PERF T33] 赋值路径统一失效（覆盖 set_messages 之外的直写点，
        # 如 main_widget 分支会话创建）。__init__ 首次赋值时版本字段尚未创建，
        # 用 hasattr 防御（该路径必然冷启动，缓存本就是 None）。
        if hasattr(self, "_messages_version"):
            self.bump_messages_version()

    @property
    def messages_released(self) -> bool:
        """消息体是否已释放（仅保留元数据，访问时重载）。"""
        return self._messages_released

    def _reload_messages(self) -> None:
        """从 loader 重载消息体。失败或 loader 不可用时保持 released，不静默变空。"""
        loader = self._messages_loader
        if loader is None:
            return
        try:
            loaded = loader(self.session_id)
        except Exception as e:
            logger.warning(f"[ChatSession] 重载会话消息失败 {self.session_id[:8]}: {e}")
            return
        if loaded is None:
            # 重载器暂不可用（如 HistoryManager 尚未就绪）：保持已释放状态，
            # 不写成空消息（空消息一旦被 save 覆盖 SQLite 就是丢历史）。
            return
        self._messages = consolidate_messages(loaded)
        self._messages_released = False
        self.bump_messages_version()

    def ensure_messages(self) -> None:
        """确保消息体已加载（已释放则重载），幂等。"""
        if self._messages_released:
            self._reload_messages()

    def release_messages(self) -> int:
        """释放消息体（保留元数据与 message_count），返回释放的条数。

        前置条件：已注入 loader，否则拒绝释放（数据不可恢复就不释放）。
        """
        if self._messages_released or not self._messages:
            return 0
        if self._messages_loader is None:
            return 0
        released = len(self._messages)
        self.message_count = released
        self._messages = []
        self._messages_released = True
        # [PERF T33] 释放后内容不再可用（下次读会从 SQLite 重载），
        # 只清缓存不 bump：bump 会让「释放→重载」同一内容产生无谓的版本抖动。
        self._send_prep_cache = None
        return released

    def get_context_messages(self) -> List[Dict[str, str]]:
        return consolidate_messages(self.messages)

    def set_messages(self, messages: List[Dict], preserve_compaction: bool = False):
        # 赋值经 property setter，已自带 bump_messages_version（T33）
        self.messages = consolidate_messages(messages or [])
        if not preserve_compaction:
            self.reset_compaction_cache()
            self.reset_compaction_state()
        self._update_timestamp()

    def bump_messages_version(self) -> None:
        """消息版本自增 + 发送前缓存失效（消息写入路径统一调用）。"""
        self._messages_version += 1
        self._send_prep_cache = None

    def add_assistant_message(self, content: str, model_name: str = None, provider_name: str = None):
        msg = {
            "role": "assistant",
            "content": content,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            # 毫秒级时间戳：``timestamp`` 只有秒级精度，同秒连发的多条消息
            # 排不出先后（轨迹/耗时分析需要）。消费方：agent_trace 插件。
            "ts_ms": int(time.time() * 1000),
        }
        if model_name:
            msg["model_name"] = model_name
        if provider_name:
            msg["provider_name"] = provider_name
        self.messages.append(msg)
        # 追加操作不走全量 consolidate，由持久化层在 save 时统一做
        self.bump_messages_version()
        self._update_timestamp()

    def add_user_message(self, content, **kwargs):
        """添加用户消息，支持 str 和 list（multimodal content）"""
        msg = {
            "role": "user",
            "content": content,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "ts_ms": int(time.time() * 1000),  # 毫秒级，同秒连发时用于排序（见上）
        }
        if kwargs.get("params"):
            msg["params"] = kwargs["params"]
        hook_event = kwargs.get("_hook_event")
        if hook_event:
            msg["_hook_event"] = hook_event
        # 图片附件路径标记：仅记录用户主动上传/粘贴的图片，供 UI 恢复会话时
        # 渲染气泡上方缩略图预览（消息级字段，API 序列化只取 role/content，不会泄漏）
        atts = kwargs.get("_image_attachments")
        if isinstance(atts, list) and atts:
            msg["_image_attachments"] = [str(p) for p in atts if p]
        # 原始输入元数据：全量附件路径 + 占位符形式正文（发送构建前的 toPlainText），
        # 供「撤销到这里」回填时保真还原 chips 与胶囊（消息级字段，不泄漏给 API）
        input_atts = kwargs.get("_input_attachments")
        if isinstance(input_atts, list) and input_atts:
            msg["_input_attachments"] = [str(p) for p in input_atts if p]
        raw_text = kwargs.get("_raw_input_text")
        if isinstance(raw_text, str) and raw_text:
            msg["_raw_input_text"] = raw_text
        self.messages.append(msg)
        # 追加操作不走全量 consolidate，由持久化层在 save 时统一做
        self.bump_messages_version()
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
        self._messages_released = False
        self.bump_messages_version()
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
    def __init__(self, max_cached: int = DEFAULT_MAX_CACHED_SESSIONS,
                 keep_messages: int = DEFAULT_KEEP_MESSAGES):
        super().__init__()
        self.sessions: List[ChatSession] = []
        self._last_access: Dict[str, float] = {}  # session_id -> last access time (timestamp)
        self.max_cached_sessions: int = max_cached
        self.keep_messages: int = keep_messages
        self._messages_loader: Optional[Callable[[str], Optional[List[Dict]]]] = None
        self.current_index = -1

    # ── 消息体惰性重载（内存治理）──────────────────────────────
    def set_messages_loader(self, loader) -> None:
        """注入消息体重载器。未注入时一律不释放（数据不可恢复就不释放）。"""
        self._messages_loader = loader
        for s in self.sessions:
            s._messages_loader = loader

    def _attach_loader(self, session: ChatSession) -> None:
        session._messages_loader = self._messages_loader

    def _release_stale_messages(self) -> None:
        """释放非活跃会话的消息体，只保留当前会话与最近 keep_messages 个。"""
        if self._messages_loader is None:
            return
        current = self.get_current_session()
        current_id = current.session_id if current else None
        candidates = [
            s for s in self.sessions
            if s.session_id != current_id and not s.messages_released and s._messages
        ]
        if len(candidates) <= self.keep_messages:
            return
        # 按最后访问时间升序：最久未访问的先释放
        candidates.sort(key=lambda s: self._last_access.get(s.session_id, 0.0))
        for s in candidates[: len(candidates) - self.keep_messages]:
            released = s.release_messages()
            if released:
                logger.debug(f"[Memory] 释放非活跃会话消息 {s.session_id[:8]} ({released} 条)")

    def create_new_session(self) -> ChatSession:
        session = ChatSession()
        self._attach_loader(session)
        self.sessions.append(session)
        self.current_index = len(self.sessions) - 1
        self._touch_session(session.session_id)
        self._evict_if_needed()
        self._release_stale_messages()

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


    def switch_to_session(self, index: int):
        if 0 <= index < len(self.sessions):
            self.current_index = index
            # 更新访问时间
            session = self.sessions[index]
            self._touch_session(session.session_id)
            # 消息体可能已被释放，切换后立即重载（实测 22-56ms）
            session.ensure_messages()
            # 如果超过最大缓存数，淘汰最久未访问的非当前会话
            self._evict_if_needed()
            self._release_stale_messages()

    def get_session_names(self) -> List[str]:
        return [s.name for s in self.sessions]


    def set_current_session(self, session: ChatSession):
        # 三个分支（首次 / 索引越界 / 覆盖当前位）语义一致：append 或覆盖后设为当前，
        # 末尾统一执行淘汰与非活跃消息体释放。
        self._attach_loader(session)
        if self.current_index < 0 or self.current_index >= len(self.sessions):
            self.sessions.append(session)
            self.current_index = len(self.sessions) - 1
        else:
            self.sessions[self.current_index] = session
        self._touch_session(session.session_id)
        self._evict_if_needed()
        self._release_stale_messages()

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
