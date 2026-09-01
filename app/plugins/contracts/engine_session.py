# -*- coding: utf-8 -*-
"""EngineSession 契约 — 插件自定义对话方式的最通用驱动原语（EP3）。

设计原则：**不预设对话流程，最大化自由度**。
- turn() 只做「执行一轮 + 同步等待」一件事，其余全部开放：
  * messages：自由构建（system/user 简写仅为便利）
  * tools：每轮自由指定（阶段化工具集 / 空工具纯问答）
  * callbacks：ChatWorker 全量回调透传（流式/工具调用/推理内容…）
  * auto_history：可选的多轮上下文累积（默认关闭——调用方自管上下文）
- 逃生舱：core / executor 公开，插件可直接操作完整对话执行栈
  （自定义等待策略、跨轮持有 executor 状态等极端场景）
- hook 规范化：hook_policy 在会话创建时声明（默认 NONE），全局 hooks
  是否参与由引擎决定，插件循环不再被动触发。

用法示例：
    session = services["create_engine_session"]("my-engine")
    r = session.turn(user="你好", timeout=60)                # 最简一轮
    r = session.turn(messages=msgs, tools=phase_tools,       # 全控一轮
                     callbacks={"content_received": on_chunk})
    session.executor.execute(...)                            # 逃生舱

kwargs：create_engine_session(name, **kwargs) 透传给 EngineSessionImpl，
常用项：model_config_override / hook_policy / hook_policy_id /
loop_policy_id（引擎级循环策略 id，按 id 取对象，不改全局激活槽）。

守卫：tests/plugins/test_hook_policy_and_chat_client.py
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Protocol, runtime_checkable


@runtime_checkable
class ChatResultLike(Protocol):
    """turn() 返回的结果对象（dataclass，字段只读语义）"""

    text: str  # 最终 assistant 文本（无则为空串）
    error: Optional[str]  # 错误信息（None = 无错）
    cancelled: bool  # 是否被 cancel() 取消
    timed_out: bool  # 是否超时（timeout 到期仍未完成）
    messages: List[Dict[str, Any]]  # 本轮完整消息流（含工具调用轮次）

    @property
    def ok(self) -> bool:
        """成功 = 无错误且未取消且未超时"""
        ...


@runtime_checkable
class EngineSession(Protocol):
    """插件对话引擎会话 — 同步阻塞驱动原语

    线程模型：turn() 阻塞直至完成/超时/取消。插件应在自己的后台线程
    （QThread 等）中调用；回调在 worker 线程执行，UI 更新需经 Qt 信号转发。
    """

    engine_name: str

    @property
    def history(self) -> List[Dict[str, Any]]:
        """多轮上下文累积视图（auto_history=True 的轮次自动追加；可直接读写）"""
        ...

    @property
    def core(self) -> Any:
        """逃生舱：ConversationCore（SessionManager/Compactor/ContextBuilder）"""
        ...

    @property
    def executor(self) -> Any:
        """逃生舱：ConversationExecutor（execute/cancel_worker/stop）"""
        ...

    def turn(
        self,
        messages: Optional[List[Dict[str, Any]]] = None,
        *,
        system: Optional[str] = None,
        user: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        callbacks: Optional[Dict[str, Callable]] = None,
        timeout: float = 300.0,
        auto_history: bool = False,
    ) -> Any:
        """执行一轮对话（阻塞）

        Args:
            messages: OpenAI 格式完整消息列表（调用方全权管理上下文）
            system/user: 简写（messages 为空时组装单轮消息）
            tools: 工具 schema 列表（默认无工具）
            callbacks: ChatWorker 回调透传（content_received/tool_call_started/
                tool_result_received/reasoning_content_received/…）。
                finished/error 由会话内部持有（同步等待必需），同名键会被覆盖。
            timeout: 等待上限秒数（reasoning model 建议 ≥300）
            auto_history: True 时本轮输入消息与响应自动追加到 history
                （默认 False——上下文由调用方管理）

        Returns:
            ChatResultLike：text/error/cancelled/timed_out/messages/ok
        """
        ...

    def cancel(self) -> None:
        """非阻塞取消当前 turn()（turn() 随后以 cancelled=True 返回）"""
        ...

    def cleanup(self) -> None:
        """释放执行器资源（插件停用时调用）"""
        ...
