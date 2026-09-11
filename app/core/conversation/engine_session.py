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

# HookPolicy 枚举 → HookPolicy 插件 id（供 worker 按 id 直接取策略对象）。
# ⚠️ 枚举值与插件 id 不同名：TOOL_EVENTS_ONLY="tool_events_only" 对应 id "tool_only"。
_HOOK_POLICY_IDS = {
    HookPolicy.ALL: "all",
    HookPolicy.TOOL_EVENTS_ONLY: "tool_only",
    HookPolicy.NONE: "none",
}


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
        # ★ 声明的枚举翻译成 hook_policy_id，否则形同虚设：
        #   ChatWorker._hook_policy_obj_resolve() 只在**显式 id** 存在时按 id 取策略，
        #   没给 id 时会忽略 ConversationConfig.hook_policy 枚举、直接回落
        #   get_active(SCOPE_MAIN) —— 于是本引擎默认声明的 NONE（契约承诺
        #   "插件循环不再被动触发全局 hooks"）实际等于 ALL。实测后果：Stop hook 的
        #   续命提醒被注入 assistant_hub 记忆整理的消息流（多花一轮 API + 污染产物）。
        if hook_policy_id is None and isinstance(hook_policy, HookPolicy):
            hook_policy_id = _HOOK_POLICY_IDS.get(hook_policy)
        # 存翻译后的显式 id（SessionStart 预对话事件判定复用）
        self._hook_policy_id = hook_policy_id
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

        # SessionStart 预对话事件（此前插件引擎无此触发点，UI 主对话专属）
        self._session_start_injections: List[str] = []
        self._trigger_session_start_hook()

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
        from app.plugins.contracts.hook_policy import (
            BuildSystemPromptEvent,
            PostUserMessageEvent,
            PreUserMessageEvent,
        )

        msgs = list(messages) if messages else []
        if not msgs:
            if not user:
                return ChatResult(error="turn() 需要 messages 或 user 参数")
            if system:
                msgs.append({"role": "system", "content": system})
            msgs.append({"role": "user", "content": user})
        if auto_history:
            msgs = self._history + msgs

        # 引擎 hook 事件统一拼接：SessionStart（会话初始化时触发）+
        # BuildSystemPrompt / PreUserMessage（本轮触发）→ 全部拼进 system。
        # cron 等插件对话不读 session.messages，注入必须进 prompt 才对 LLM 生效
        injections = list(self._session_start_injections)
        injections.extend(
            self._trigger_engine_hook(
                "BuildSystemPrompt",
                BuildSystemPromptEvent(),
            )
        )
        if user:
            injections.extend(
                self._trigger_engine_hook(
                    "PreUserMessage",
                    PreUserMessageEvent(message=user),
                    {"message": user},
                )
            )
        if injections:
            extra = "\n\n".join(injections)
            sys_idx = next((i for i, m in enumerate(msgs) if m.get("role") == "system"), None)
            if sys_idx is not None:
                msgs[sys_idx]["content"] = ((msgs[sys_idx].get("content") or "") + "\n\n" + extra)
            else:
                msgs.insert(0, {"role": "system", "content": extra})

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

        # PostUserMessage：任务 prompt 已消费完一轮（输出不注入——本轮请求已发出，
        # 触发仅为让 hook 感知事件；结束语义另有 Stop）
        if user:
            self._trigger_engine_hook(
                "PostUserMessage",
                PostUserMessageEvent(message=user),
                {"message": user},
            )

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

    def _resolve_hook_policy(self):
        """解析引擎声明的 hook policy 对象（hook_policy_id 显式 > 枚举回落）

        返回 None 表示无可用策略（registry 未加载/id 不存在）——调用方按 SKIP 处理。
        """
        try:
            from app.plugins.registries.hook_policy_registry import HookPolicyRegistry

            registry = HookPolicyRegistry.get_instance()
            registered = registry.policies()
            if self._hook_policy_id:
                return registered.get(self._hook_policy_id)
            enum_id = _HOOK_POLICY_IDS.get(self._hook_policy) if isinstance(self._hook_policy, HookPolicy) else None
            if enum_id:
                return registered.get(enum_id)
        except Exception as e:
            logger.debug(f"[EngineSession:{self.engine_name}] hook policy 解析失败: {e}")
        return None

    def _hook_context_base(self) -> Dict[str, Any]:
        """引擎 hook 事件 context 公共字段"""
        ctx: Dict[str, Any] = {
            "engine_name": self.engine_name,
            "project_root": "",
            "agent_name": "",
        }
        try:
            te = getattr(self._executor, "_tool_executor", None)
            get_workdir = getattr(te, "get_workdir", None)
            if callable(get_workdir):
                ctx["project_root"] = get_workdir() or ""
        except Exception:
            pass
        return ctx

    def _trigger_engine_hook(self, event_name: str, event, ctx_extra: Dict[str, Any] = None) -> List[str]:
        """按引擎 hook policy 判定并同步触发一个 hook 事件

        引擎链路（cron-tasks 等）的预对话/消息级事件统一通道：
        SessionStart / BuildSystemPrompt / PreUserMessage / PostUserMessage。
        worker 侧事件（工具级/Stop/消息级）由 ChatWorker 按同一策略过滤。

        - policy SKIP / registry / HookManager 不可用 → 返回空列表（不触发）
        - 同步执行：插件线程无 Qt 事件循环，异步回调无处回补
        - 输出注入方式：返回文本由调用方拼进 system prompt（插件对话不读
          session.messages，注入消息轨迹对 LLM 请求无效——与主对话的
          _inject_hook_to_session 路径不同）
        """
        try:
            policy = self._resolve_hook_policy()
            if policy is None:
                return []
            from app.plugins.contracts.hook_policy import HookDecision

            if policy.should_trigger(event) != HookDecision.TRIGGER:
                return []
            te = getattr(self._executor, "_tool_executor", None)
            backend = getattr(te, "_backend", None)
            hm = getattr(backend, "hook_manager", None)
            if hm is None:
                logger.debug(f"[EngineSession:{self.engine_name}] 无可用 HookManager，{event_name} 跳过")
                return []
            ctx = self._hook_context_base()
            if ctx_extra:
                ctx.update(ctx_extra)
            results = hm.trigger_event(
                event_name, context=ctx, current_message="", trigger_async=False
            )
            outs = [
                r.output
                for r in (results or [])
                if getattr(r, "success", False) and getattr(r, "output", "")
            ]
            if outs:
                logger.info(
                    f"[EngineSession:{self.engine_name}] {event_name} hook 注入 {len(outs)} 段上下文"
                )
            return outs
        except Exception as e:
            logger.warning(f"[EngineSession:{self.engine_name}] {event_name} 触发失败（忽略）: {e}")
            return []

    def _trigger_session_start_hook(self):
        """会话初始化后触发 SessionStart 预对话事件（按引擎 hook policy 判定）

        UI 主对话由 backend.create_session 触发；插件引擎此前无该触发点，
        SessionStart 类 hook 对插件循环永远不生效。此处补齐。
        """
        from app.plugins.contracts.hook_policy import SessionStartEvent

        outs = self._trigger_engine_hook(
            "SessionStart", SessionStartEvent(state="startup"), {"state": "startup"}
        )
        if outs:
            self._session_start_injections = outs

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
        """复位残留的流式状态（上轮 worker 线程已结束但 is_streaming 仍 True）

        收编 autoloop 死锁解锁：竞态残留时 execute() 被 "Already streaming"
        拒绝，之后每轮 turn() 都失败。判定依据是「worker 线程已结束」而不是
        「C++ wrapper 是否被 deleteLater」。
        """
        if not self._executor.is_streaming:
            return
        stale = self._executor.get_current_worker()
        if not self._alive_worker(stale):
            self._executor._is_streaming = False
            self._executor._current_worker = None
            # 线程已退出：顺带回收 worker（cleanup + deleteLater），否则每轮 turn()
            # 残留一个 QThread（daemon 线程无事件循环，deleteLater 不会被处理，
            # 只能靠 cleanup() 释放业务资源）。
            try:
                self._executor._finalize_worker_cleanup(stale)
            except Exception as e:
                logger.debug(f"[EngineSession:{self.engine_name}] 残留 worker 回收失败（忽略）: {e}")
            logger.info(f"[EngineSession:{self.engine_name}] 复位残留流式状态（worker 线程已结束）")

    @staticmethod
    def _alive_worker(w) -> bool:
        """worker 线程是否仍在运行（C++ wrapper 已销毁 → False）

        ⚠️ 必须返回 isRunning() 的**实际结果**，不能只看"访问是否抛异常"：
        插件后台线程（daemon / 无 Qt 事件循环）里 worker 的 finished 信号会丢
        （跨线程黑洞），线程早已退出但 C++ wrapper 还在 —— 此时 isRunning()
        返回 False 却不抛异常，旧实现会误判为"存活" → _reset_stale_streaming()
        永不复位 → 之后每次 execute() 都被
        "Already streaming (current_worker=..., is_alive=False)" 拒绝。
        """
        if w is None:
            return False
        try:
            return bool(w.isRunning())
        except RuntimeError:
            return False
