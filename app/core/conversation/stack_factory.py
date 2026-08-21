# -*- coding: utf-8 -*-
"""对话执行栈工厂（EP2：UI 插件 services["conversation_stack"]() 的返回类型）

本模块是 ``app/plugins/contracts/conversation_stack.py`` 的**薄壳实现**：
- ``ConversationStackImpl.create_core`` → ``ConversationCore.create``
- ``ConversationStackImpl.create_executor`` → ``ConversationExecutor(...)``

PluginSide（autoloop 等）经 ``ctx["services"]["conversation_stack"]()`` 拿到
本实例，调用 ``create_core / create_executor`` 构建执行栈，**不再**
``from app.core.conversation.core import ConversationCore`` deep import 主程序
内部路径——主程序重构不再断插件。

契约验证：``tests/plugins/test_conversation_stack_contract.py`` 已守卫真实
``ConversationCore.create`` 的参数集与契约对齐。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from app.core.conversation.config import ConversationConfig
from app.core.conversation.core import ConversationCore
from app.core.conversation.executor import ConversationExecutor


class ConversationStackImpl:
    """对话执行栈工厂薄壳（实现 ConversationStackFactory 契约）"""

    def create_core(
        self,
        get_model_config: Callable[[], Dict[str, Any]],
        agent_manager: Any = None,
        backend: Any = None,
        session_manager: Optional[Any] = None,
    ) -> ConversationCore:
        return ConversationCore.create(
            get_model_config=get_model_config,
            agent_manager=agent_manager,
            backend=backend,
            session_manager=session_manager,
        )

    def create_executor(
        self,
        core: ConversationCore,
        config: Optional[ConversationConfig] = None,
        tool_executor: Any = None,
        agent_manager: Any = None,
    ) -> ConversationExecutor:
        return ConversationExecutor(
            core=core,
            config=config,
            tool_executor=tool_executor,
            agent_manager=agent_manager,
        )