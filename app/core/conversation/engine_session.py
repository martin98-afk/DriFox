# -*- coding: utf-8 -*-
"""EngineSession 实现 — 插件对话引擎的最通用同步驱动原语（EP3）。

契约：app/plugins/contracts/engine_session.py（EngineSession Protocol）
注入：main_widget._build_ui_services()["create_engine_session"]

设计：不预设对话流程。turn() = 执行一轮 + 同步等待 + 防御复位，
其余（messages 构建策略/阶段化 tools/多轮循环协议/回调消费）全部留给插件。

收编插件（autoloop/chinese-chess）曾各自维护的样板：
1. 自建 ConversationCore（隔离 SessionManager，不污染主窗口会话）
2. threading.Event 同步 Adapter（等待 worker 完成，跨线程安全）
3. stale worker 复位防御（上轮竞态残留 is_streaming=True 且 worker 已销毁）
4. 会话初始化（ChatSession 注册进 SessionManager）
5. 空响应兜底恢复（finished 传空时从消息流恢复 assistant 文本）

hook 规范化：经 ConversationConfig.hook_policy 由引擎声明参与级别
（默认 NONE —— 插件循环不再被动触发全局 hooks）。
"""

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from loguru import logger

from app.core.conversation.config import ConversationConfig, HookPolicy, PermissionStrategy
from app.core.conversation.core import ConversationCore
from app.core.conversation.executor import ConversationExecutor


@dataclass
class ChatResult:
    """turn() 结果（满足 ChatResultLike 契约）"""

    text: str = ""
    error: Optional[str] = None
    cancelled: bool = False
    timed_out: bool = False
    messages: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error is None and not self.cancelled and not self.timed_out


class _SyncAdapter:
    """线程同步适配器 — threading.Event 等待 ChatWorker 完成"""

    def __init__(self):
        self._done = threading.Event()
        self._response: str = ""
        self._error: Optional[str] = None

    def reset(self):
        self._response = ""
        self._error = None
        self._done.clear()

    def on_finished(self, response: str):
        self._response = response or ""
        self._done.set()

    def on_error(self, error: str):
        self._error = error
        self._done.set()

    def wait(self, timeout: float) -> bool:
        return self._done.wait(timeout=timeout)


# hook_policy / permission_strategy 字符串快速映射
_HOOK_POLICIES = {p.value: p for p in HookPolicy}
_PERM_STRATEGIES = {p.value: p for p in PermissionStrategy}


class EngineSessionImpl:
    """EngineSession 契约实现（同步阻塞驱动原语）

    插件应在其后台线程调用 turn()；UI 更新经 Qt 信号自行转发。
    """

    def __init__(
        self,
        engine_name: str,
        get_model_config: Callable[[], Dict[str, Any]],
        tool_executor: Any = None,
        agent_manager: Any = None,
        backend: Any = None,
        hook_policy: Any = HookPolicy.NONE,
        permission_strategy: Any = PermissionStrategy.AUTO_ALLOW,
        model_config_override: Optional[Dict[str, Any]] = None,
        hook_policy_id: Optional[str] = None,
        loop_policy_id: Optional[str] = None,
    ):
        self.engine_name = engine_name
        self._is_cancelled = False
        self._history: List[Dict[str, Any]] = []

        # 模型配置覆盖：插件可强制关思考/降温度等（如象棋插件关掉 reasoning 提速）。
        # 实时包裹 get_model_config，保留「模型切换后即时生效」语义，仅在顶层叠加。
        if model_config_override:
            base_get = get_model_config
            override = dict(model_config_override)
            get_model_config = lambda: {**base_get(), **override}
        self._get_model_config = get_model_config

        if isinstance(hook_policy, str):
            hook_policy = _HOOK_POLICIES.get(hook_policy, HookPolicy.NONE)
        if isinstance(permission_strategy, str):
            permission_strategy = _PERM_STRATEGIES.get(permission_strategy, PermissionStrategy.AUTO_ALLOW)

        # 隔离的 ConversationCore：独立 SessionManager，不污染主窗口会话
        self._core = ConversationCore.create(
            get_model_config=get_model_config,
            agent_manager=agent_manager,
            backend=backend,
        )
        self._executor = ConversationExecutor(
            core=self._core,
            config=ConversationConfig(
                permission_strategy=permission_strategy,
                hook_policy=hook_policy,
                hook_policy_id=hook_policy_id,
                loop_policy_id=loop_policy_id,
            ),
            tool_executor=tool_executor,
            agent_manager=agent_manager,
        )
        self._adapter = _SyncAdapter()
        self._round_messages: List[Dict[str, Any]] = []

        # 会话初始化（executor.execute 依赖 current session）
        sm = self._core.session_manager
        if not sm.get_current_session():
            from app.core.chat_session import ChatSession

            session = ChatSession(name=f"plugin:{engine_name}")
            sm.sessions.append(session)
            sm.current_index = 0
            sm._touch_session(session.session_id)

    # ========== 逃生舱（公开完整对话执行栈） ==========

    @property
    def core(self) -> ConversationCore:
        return self._core

    @property
    def executor(self) -> ConversationExecutor:
        return self._executor

    @property
    def history(self) -> List[Dict[str, Any]]:
        return self._history

    # ========== 契约方法 ==========

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
    ) -> ChatResult:
        """执行一轮对话（阻塞）。见 contracts/engine_session.py 契约文档。"""
        msgs = list(messages) if messages else []
        if not msgs:
            if not user:
                return ChatResult(error="turn() 需要 messages 或 user 参数")
            if system:
                msgs.append({"role": "system", "content": system})
            msgs.append({"role": "user", "content": user})
        if auto_history:
            msgs = self._history + msgs

        self._is_cancelled = False
        self._adapter.reset()
        self._round_messages = []

        self._reset_stale_streaming()

        # 回调合并：finished/error 由会话持有（同步等待必需），其余全量透传
        wrapped: Dict[str, Callable] = dict(callbacks or {})
        wrapped["finished"] = self._adapter.on_finished
        wrapped["error"] = self._adapter.on_error
        wrapped["messages_updated"] = self._on_messages_updated

        llm_config = self._get_model_config() if self._get_model_config else {}
        started = self._executor.execute(
            messages=msgs,
            llm_config=llm_config,
            tools=tools or [],
            callbacks=wrapped,
            direct_signals=True,  # 本方法运行在调用方线程（常为插件后台线程），必须直连才能收到完成信号
        )
        if not started:
            return ChatResult(error="worker 启动失败（可能上轮仍在运行）")

        done = self._adapter.wait(timeout)
        if not done:
            # 超时：非阻塞取消，让 turn() 尽快返回
            self.cancel()
            if auto_history:
                self._history = msgs + self._round_messages
            return ChatResult(
                timed_out=True,
                messages=self._round_messages,
                error=f"turn() 超时（{timeout}s），已取消",
            )

        if auto_history:
            self._history = msgs + self._round_messages

        if self._is_cancelled:
            return ChatResult(cancelled=True, messages=self._round_messages)

        err = self._adapter._error
        if err:
            return ChatResult(error=err, messages=self._round_messages)

        # 兜底：finished 传空但消息流有 assistant 内容（收编 autoloop 防御）
        text = self._adapter._response
        if not text:
            for msg in reversed(self._round_messages):
                if msg.get("role") == "assistant":
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        content = "".join(
                            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
                        )
                    if content:
                        text = content
                        break

        return ChatResult(text=text or "", messages=self._round_messages)

    def cancel(self) -> None:
        """非阻塞取消当前 turn()"""
        self._is_cancelled = True
        self._executor.cancel_worker()

    def cleanup(self) -> None:
        """释放执行器资源"""
        try:
            self._executor.cleanup()
        except Exception as e:
            logger.warning(f"[EngineSession:{self.engine_name}] cleanup: {e}")

    # ========== 内部防御 ==========

    def _on_messages_updated(self, messages: List[Dict[str, Any]]):
        """收集本轮完整消息流（含工具调用轮次）"""
        self._round_messages = list(messages or [])

    def _reset_stale_streaming(self):
        """复位残留的流式状态（上轮 worker 已销毁但 is_streaming 仍 True）

        收编 autoloop 死锁解锁：竞态残留时 execute() 被 "Already streaming"
        拒绝，每轮 turn() 都失败。
        """
        if not self._executor.is_streaming:
            return
        stale = self._executor.get_current_worker()
        if not self._alive_worker(stale):
            self._executor._is_streaming = False
            self._executor._current_worker = None
            logger.info(f"[EngineSession:{self.engine_name}] 复位残留流式状态（worker 已销毁）")

    @staticmethod
    def _alive_worker(w) -> bool:
        """wrapper C++ 对象是否仍存活（deleteLater 后访问会 RuntimeError）"""
        if w is None:
            return False
        try:
            w.isRunning()
            return True
        except RuntimeError:
            return False
