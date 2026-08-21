# -*- coding: utf-8 -*-
"""ConversationStack 契约 — 对话执行栈的构建面声明（EP2 前置）。

现状：插件（autoloop 等）直接 ``from app.core.conversation.core import
ConversationCore`` deep import 主程序内部路径——主程序重构即断。
本契约声明插件所需的构建能力，为后续"插件经公开工厂取执行栈"铺路：
- 本版：声明 + 签名漂移守卫（tests 对比真实 create 参数集）
- 下版：services 增加 conversation_stack() 入口，插件撤销 deep import
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Protocol, runtime_checkable


@runtime_checkable
class ConversationStackFactory(Protocol):
    """对话执行栈构建契约（语义 = ConversationCore.create + ConversationExecutor）"""

    def create_core(
        self,
        get_model_config: Callable[[], Dict[str, Any]],
        agent_manager: Any = None,
        backend: Any = None,
        session_manager: Any = None,
    ) -> Any:
        """创建 ConversationCore（聚合 Compactor + PermissionCache + ContextBudgetAllocator）

        参数集与 app.core.conversation.core.ConversationCore.create 保持一致，
        漂移由 tests/plugins/test_conversation_stack_contract.py 守卫。
        """
        ...

    def create_executor(
        self,
        core: Any,
        config: Any = None,
        tool_executor: Any = None,
        agent_manager: Any = None,
    ) -> Any:
        """创建 ConversationExecutor（统一 Worker 执行）

        参数集与 app.core.conversation.executor.ConversationExecutor 保持一致。
        """
        ...
