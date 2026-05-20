# -*- coding: utf-8 -*-
"""
对话引擎基类 — 统一所有引擎的接口契约

所有对话引擎（UI/Gateway/AutoLoop）继承此基类，
确保对外暴露一致的属性与方法，方便后续扩展新引擎。

注意：不使用 ABC 以避免与 PyQt5 QObject 的元类冲突。
各引擎通过重写方法提供实现，不实现时抛出 NotImplementedError。
"""
from typing import Any, Dict, List, Optional


class BaseEngine:
    """对话引擎基类

    提供对 ConversationCore + ConversationExecutor 的统一封装，
    各引擎只需实现自身差异化的业务逻辑。
    """

    def __init__(self, conversation_core: Any = None, conversation_executor: Any = None):
        self._conversation_core = conversation_core
        self._conversation_executor = conversation_executor

    # ========== 公共属性 ==========

    @property
    def is_streaming(self) -> bool:
        """当前是否正在流式输出"""
        return self._conversation_executor.is_streaming

    @property
    def conversation_core(self) -> Any:
        """获取 ConversationCore 实例"""
        return self._conversation_core

    @property
    def conversation_executor(self) -> Any:
        """获取 ConversationExecutor 实例"""
        return self._conversation_executor

    # ========== 公共方法 ==========

    def stop(self) -> List[Dict]:
        """停止当前执行，返回被中断的消息列表"""
        return self._conversation_executor.stop()

    def cleanup(self):
        """清理执行器资源"""
        self._conversation_executor.cleanup()

    # ========== 各引擎需实现的接口 ==========

    def get_current_session(self):
        """获取当前会话

        Returns:
            ChatSession 或 None
        """
        raise NotImplementedError("子类必须实现 get_current_session()")
