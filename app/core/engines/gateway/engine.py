# -*- coding: utf-8 -*-
"""
GatewayEngine — Gateway 专用引擎，与 UI 的 ChatEngine 完全独立

Issue #144: 多用户并发对话支持

职责：
1. 独立管理 Gateway 的 ChatSession（不碰 UI 的 SessionManager.current_index）
2. 处理 Gateway 命令（/help, /new, /model, /agent, /session 等）
3. 处理 AI 对话（每个用户独立 Executor，支持并发）
4. 将 Gateway 会话保存到 SQLite（供 UI 历史列表查看）

设计原则：
- 与 ChatEngine 完全独立：各自有自己的 SessionManager、_is_streaming、_current_worker
- 单例模式：由 Backend 持有，多个 UI 窗口共享同一个 GatewayEngine
- ✅ 每个用户独立 ConversationCore + ConversationExecutor（支持并发）
- 共享 ToolExecutor / AgentManager（无状态组件）
- 空闲 Executor 可被定期清理
"""
import time
from typing import Callable, Dict, List, Optional, Any

from PyQt5.QtCore import QObject, pyqtSignal
from loguru import logger

from app.core.chat_session import ChatSession, SessionManager
from app.core.conversation.core import ConversationCore
from app.core.conversation.config import ConversationConfig, PermissionStrategy
from app.core.conversation.executor import ConversationExecutor
from app.core.conversation.adapters import GatewayConversationAdapter
from app.core.engines.base import BaseEngine
from app.tools import get_builtin_tools_schema
from app.utils.config import Settings


def _noop(*args, **kwargs):
    """空操作回调"""
    pass


# 默认空闲超时时间（秒）
DEFAULT_IDLE_TIMEOUT_SECONDS = 300  # 5 分钟


class GatewayEngine(QObject, BaseEngine):
    """Gateway 专用引擎，与 UI 的 ChatEngine 完全独立"""

    # 状态信号
    worker_started = pyqtSignal()
    worker_finished = pyqtSignal(str)  # response text
    worker_error = pyqtSignal(str)     # error message

    # 全局单例
    _global_instance: Optional["GatewayEngine"] = None

    def __init__(
        self,
        get_model_config: Callable[[], Dict[str, Any]],
        tool_executor: Any = None,
        agent_manager: Any = None,
        session_store: Any = None,
        parent: QObject = None,
    ):
        # 防止重复构造单例
        if self._global_instance is not None and self is not self._global_instance:
            raise RuntimeError("GatewayEngine is singleton, use GatewayEngine.get_instance()")

        # 先调用 QObject.__init__（会通过 MRO 触发 BaseEngine.__init__()，使用默认值）
        super().__init__(parent)

        # ===== 共享的无状态组件 =====
        self._get_model_config = get_model_config
        self._tool_executor = tool_executor
        self._agent_manager = agent_manager
        self._session_store = session_store

        # ===== 修改点①：每个用户独立 ConversationCore + ConversationExecutor =====
        # key: session.session_id → ConversationCore
        self._user_cores: Dict[str, ConversationCore] = {}
        # key: session.session_id → ConversationExecutor
        self._user_executors: Dict[str, ConversationExecutor] = {}
        # key: session.session_id → GatewayConversationAdapter
        self._user_adapters: Dict[str, GatewayConversationAdapter] = {}
        # key: session.session_id → List[tuple]（每个用户的私有消息队列）
        self._user_pending_queues: Dict[str, List[tuple]] = {}
        # key: session.session_id → 最后活动时间（用于空闲清理）
        self._user_last_active: Dict[str, float] = {}

        # ===== 全局会话索引（跨所有用户的会话查询） =====
        # 维护一个统一的 session_id → ChatSession 映射
        self._all_sessions: Dict[str, ChatSession] = {}

        # ===== Gateway 会话级配置 =====
        self._current_agent: Optional[str] = "plan"

        # 注册为全局单例
        GatewayEngine._global_instance = self

    @classmethod
    def get_instance(
        cls,
        get_model_config: Callable[[], Dict[str, Any]] = None,
        tool_executor: Any = None,
        agent_manager: Any = None,
        session_store: Any = None,
    ) -> "GatewayEngine":
        """获取全局单例"""
        if cls._global_instance is not None:
            return cls._global_instance
        if get_model_config is None:
            raise ValueError("First call to get_instance() must provide get_model_config")
        instance = cls(get_model_config, tool_executor, agent_manager, session_store)
        cls._global_instance._global_instance = cls._global_instance
        return instance

    # ==================== 修改点②：新增 Executor 工厂方法 ====================

    def _get_or_create_executor(self, session_id: str) -> ConversationExecutor:
        """获取或创建用户专属的 ConversationExecutor

        每个用户（GatewaySession）获得独立的 ConversationCore 和 ConversationExecutor，
        从而实现真正的并行对话，互不阻塞。
        """
        if session_id not in self._user_executors:
            # 为该用户创建独立的 ConversationCore
            core = ConversationCore.create(
                get_model_config=self._get_model_config,
                agent_manager=self._agent_manager,
                backend=None,
            )
            self._user_cores[session_id] = core

            # 为该用户创建独立的 ConversationExecutor
            config = ConversationConfig(
                permission_strategy=PermissionStrategy.AGENT_CONFIG,
            )
            executor = ConversationExecutor(
                core=core,
                config=config,
                tool_executor=self._tool_executor,   # 共享（无状态）
                agent_manager=self._agent_manager,   # 共享（无状态）
            )
            self._user_executors[session_id] = executor

            # 为该用户创建独立的 GatewayConversationAdapter
            adapter = GatewayConversationAdapter(
                core=core,
                executor=executor,
            )
            self._user_adapters[session_id] = adapter

            # 初始化用户的私有消息队列
            self._user_pending_queues[session_id] = []

            # 标记活动时间
            self._touch_user(session_id)

            logger.debug(f"[GatewayEngine] Created per-user executor for session={session_id[:12]}")

        return self._user_executors[session_id]

    def _touch_user(self, session_id: str):
        """更新用户最后活动时间"""
        self._user_last_active[session_id] = time.time()

    # ==================== BaseEngine 接口实现 ====================

    def get_current_session(self) -> Optional[ChatSession]:
        """获取当前 Gateway 会话（保留向后兼容）"""
        # 如果有多个用户，返回最近活动的会话
        if not self._all_sessions:
            return None
        # 按最后活动时间降序排列
        active = sorted(
            self._all_sessions.values(),
            key=lambda s: self._user_last_active.get(s.session_id, 0),
            reverse=True,
        )
        return active[0] if active else None

    # ==================== 会话管理 ====================

    def add_session(self, session: ChatSession) -> None:
        """添加 Gateway 会话并注册到全局索引"""
        self._all_sessions[session.session_id] = session
        # 为该会话创建 Core + Executor（懒加载）
        self._get_or_create_executor(session.session_id)
        self._save_to_store(session)

    def find_session(self, session_id: str) -> Optional[ChatSession]:
        """查找 Gateway 会话"""
        session = self._all_sessions.get(session_id)
        if session:
            self._touch_user(session_id)
        return session

    def switch_to_session(self, session_id: str) -> Optional[ChatSession]:
        """切换当前 Gateway 会话（不影响 UI）"""
        session = self._all_sessions.get(session_id)
        if session:
            self._touch_user(session_id)
        return session

    def get_all_sessions(self) -> List[ChatSession]:
        """获取所有 Gateway 会话"""
        return list(self._all_sessions.values())

    # ==================== 入口 ====================

    def process(
        self,
        session: ChatSession,
        text: str,
        callbacks: Optional[Dict[str, Callable]] = None,
    ) -> bool:
        callback = callback or {}
