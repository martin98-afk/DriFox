# app/core/conversation/core.py
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from app.core.chat_session import SessionManager
from app.core.history_compactor import HistoryCompactor
from app.core.conversation.config import PermissionCache
from app.core.context_builder import ContextBudgetAllocator


@dataclass
class ConversationCore:
    """共享对话基础设施包
    
    聚合四个组件为单一实体，消除 ChatEngine / GatewayEngine 中的重复声明。
    每个消费者拥有自己的 ConversationCore 实例，Session 互相隔离。
    """
    session_manager: SessionManager
    compactor: HistoryCompactor
    permission_cache: PermissionCache
    context_builder: ContextBudgetAllocator

    @classmethod
    def create(
        cls,
        get_model_config: Callable[[], Dict[str, Any]],
        agent_manager: Any,
        backend: Any,
        session_manager: Optional[SessionManager] = None,
    ) -> "ConversationCore":
        """便捷工厂方法

        Args:
            get_model_config: 获取模型配置的回调
            agent_manager: AgentManager 实例
            backend: 后端实例
            session_manager: 外部传入的 SessionManager（如 ChatEngine 的共享管理器），
                            None 时内部创建新实例
        """
        if session_manager is None:
            session_manager = SessionManager()
        compactor = HistoryCompactor(get_model_config, agent_manager)
        permission_cache = PermissionCache()
        context_builder = ContextBudgetAllocator(
            agent_manager=agent_manager,
            compactor=compactor,
            backend=backend,
        )
        return cls(
            session_manager=session_manager,
            compactor=compactor,
            permission_cache=permission_cache,
            context_builder=context_builder,
        )
