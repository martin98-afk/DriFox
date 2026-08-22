# -*- coding: utf-8 -*-
"""
ChatBackend - 统一后端接口
后端自己创建和管理所有组件，前端只负责 UI 调用
"""

from __future__ import annotations

import asyncio
import os
import queue
import re
import time
from typing import Any, Callable, Dict, List, Optional

import orjson as json
from loguru import logger
from PyQt5.QtCore import QObject, QThreadPool, pyqtSignal, QTimer, QCoreApplication

from app.constants import IMAGE_EXTENSIONS
from app.utils.utils import invalidate_skills_cache

# Auto-compact 防重复触发冷却（秒）
_AUTO_COMPACT_COOLDOWN = 30.0


def get_session_storage():
    """全局存储门面：返回 StorageRegistry 活跃引擎（非 UI 消费方统一入口）。

    冷启动防御（同 chat_worker._adapter_flags）：注册表为空（backend warmup
    尚未执行/测试环境）时幂等触发系统插件扫描再重试；仍失败（真实配置错误）
    让 RuntimeError 显式传播。registry 零硬编码兜底原则不变——兜底在门面侧。
    """
    from app.plugins.registries.storage_registry import StorageRegistry

    registry = StorageRegistry.get_instance()
    try:
        return registry.get_active()
    except RuntimeError:
        try:
            from app.plugins.loaders.runtime_component_loader import warmup_runtime_components

            warmup_runtime_components()
        except Exception:
            pass
        return registry.get_active()


def _callback_holds_backend(callback, backend) -> bool:
    """判断异步闭包是否捕获了指定 ChatBackend 实例（泄漏修复 6d 辅助）。

    _do_init 中定义的 process_message / send_message 是 async 函数，
    闭包通过自由变量捕获 self（backend）。检查闭包 cell 是否引用该实例，
    用于 cleanup 时确认 PlatformManager 单例持有的回调是否指向本 backend。
    """
    try:
        closure = getattr(callback, "__closure__", None) or ()
        for cell in closure:
            if cell.cell_contents is backend:
                return True
    except Exception:
        pass
    return False


def _event_to_tag(event_name: str) -> str:
    """将事件名转换为 Claude Code 兼容的 kebab-case 标签

    例:
        UserPromptSubmit → user-prompt-submit
        PreUserMessage   → pre-user-message
        PreToolUse       → pre-tool-use
        PostToolUse      → post-tool-use
        SessionStart     → session-start
        Stop             → stop
    """
    # PascalCase / camelCase → kebab-case
    # UserPromptSubmit → User-Prompt-Submit → user-prompt-submit
    kebab = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", event_name)
    kebab = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1-\2", kebab)
    return kebab.lower()


def _strip_hook_wrapper(content: str) -> str:
    """从 hook 消息格式中提取纯文本内容（兼容新旧格式）

    Claude Code 格式: <{kebab-case-event}-hook>\\n...\\n</{kebab-case-event}-hook>
    旧分隔线格式: ---\\n🔌 **Hook 内部通知** · 事件: `...`\\n\\n...\\n---
    最早旧格式: <hook event=\"...\">\\n...\\n</hook>
    """
    if not content:
        return content

    # Claude Code 格式：<xxx-hook>...</xxx-hook>
    # 用启发式：只要匹配 <xxx-hook>...</xxx-hook> 且标签以 -hook 结尾
    m = re.search(r"<([a-z0-9-]+-hook)>\s*(.*?)\s*</\1>", content, re.DOTALL)
    if m:
        return m.group(2).strip()

    # 旧分隔线格式
    if content.startswith("---") and "🔌 **Hook 内部通知**" in content:
        match = re.search(r"---\n.*?🔌\s*\*\*Hook 内部通知\*\*.*?\n\n(.+?)\n---", content, re.DOTALL)
        if match:
            return match.group(1).strip()
        return content

    # 最早旧格式
    match = re.search(r"<hook[^>]*>(.*?)</hook>", content, re.DOTALL)
    if match:
        return match.group(1).strip()
    return content


def _format_hook_output(
    event_name: str,
    output: str,
    status_message: str = "",
    wrap_system_reminder: bool = True,
) -> str:
    """格式化 hook 输出为 Claude Code 兼容的 XML 标签格式

    当 wrap_system_reminder=True（默认）：
        外层: <system-reminder>...</system-reminder>
        内层: <{kebab-case-event}-hook>...</{kebab-case-event}-hook>
    当 wrap_system_reminder=False：
        仅输出内层: <{kebab-case-event}-hook>...</{kebab-case-event}-hook>
        （用于团队邮件、子智能体消息等非系统 hook 注入，避免 LLM 误判为系统指令）
        注意：此时 status_message 会被静默丢弃（当前调用方均未传 status_message）

    当传入 status_message 时，在 <system-reminder> 和 <xxx-hook> 之间以纯文本形式插入状态描述。

    与 Claude Code 实际格式对齐：
    - <system-reminder> 是 Claude Code 通用系统注入容器
    - <user-prompt-submit-hook> 等是 Claude Code 提示词中明说的 hook 反馈标签
    LLM 收到时按 system prompt 约定识别为 hook 注入内容。

    🛡️ Stop 事件注入防幻觉：在消息末尾添加明确的「等待用户回复」指令，
    避免 LLM 将 hook 注入的消息误认为用户已确认/同意，导致跳过确认环节。
    """
    tag = _event_to_tag(event_name)
    parts: list[str] = []
    if wrap_system_reminder:
        parts.append("<system-reminder>")
        if status_message:
            parts.append(status_message)
    parts.append(f"<{tag}-hook>")
    parts.append(output)
    parts.append(f"</{tag}-hook>")
    if wrap_system_reminder:
        # 🛡️ Stop 事件：追加「等待用户回复」指令，防止 LLM 将 hook 注入消息
        # 误认为用户已确认。该标记在 <system-reminder> 内部，LLM 可见但明确
        # 告知其系统身份，不污染用户消息流。
        if event_name == "Stop":
            parts.append("以上是系统自动注入的辅助信息，不是用户的输入。")
        parts.append("</system-reminder>")
    return "\n".join(parts)


def _make_hook_message(event_name: str, output: str, status_message: str = "") -> Dict[str, Any]:
    """构建一条 hook 消息 dict（带 _hook_event 标记和 timestamp）"""
    import datetime as _dt

    return {
        "role": "user",
        "content": _format_hook_output(event_name, output, status_message),
        "_hook_event": event_name,
        "timestamp": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _inject_hook_to_session(session, event_name: str, output: str, status_message: str = ""):
    """将 hook 输出追加到 session.messages（只追加不删除，保证历史稳定）"""
    if not session:
        return
    if not output or not output.strip():
        return
    msg = _make_hook_message(event_name, output, status_message)
    session.messages.append(msg)
    session._update_timestamp()


def _extract_markdown_images(content: str) -> tuple[str, list[str]]:
    """
    从 Markdown 内容中提取本地图片文件路径。

    检测 ![alt](path) 语法，只提取本地存在的图片文件。
    对远程 URL、不存在的文件、非图片文件均不提取。

    Args:
        content: Markdown 文本内容

    Returns:
        (clean_content, image_paths) - 清理后的文本和本地图片路径列表
    """
    image_paths: list[str] = []

    def _replace_img(match: re.Match) -> str:
        path = match.group(1).strip()
        if os.path.isfile(path):
            ext = os.path.splitext(path)[1].lower()
            if ext in IMAGE_EXTENSIONS:
                image_paths.append(path)
                return ""  # 移除图片标记
        return match.group(0)  # 保留原样

    cleaned = re.sub(r"!\[.*?\]\((.+?)\)", _replace_img, content)
    return cleaned, image_paths


def _safe_agent_manager(backend: "ChatBackend") -> Any:
    """安全读取 _agent_manager：未 __init__ 时返回 None 而不触发 super().__init__ 异常

    ChatBackend.__new__(...) 路径（测试场景）下，self._agent_manager 是 descriptor，
    任何属性访问会触发 QObject.__init__() 链校验。bind_runtime 需 None 而非异常。
    """
    try:
        return object.__getattribute__(backend, "_agent_manager")
    except (AttributeError, RuntimeError):
        return None


class ChatBackend(QObject):
    """
    聊天后端 - 自己创建所有核心组件，暴露统一接口给前端

    职责：
    1. 创建并管理 ChatEngine, SessionManager, ToolExecutor 等
    2. 暴露统一的 API 给前端（UI 层）
    3. 发出状态变化信号供前端订阅
    """

    # ========== 信号定义 ==========
    # 会话相关
    session_created = pyqtSignal(str)  # session_id
    session_changed = pyqtSignal(str)  # session_id
    session_deleted = pyqtSignal(int)  # index

    # 消息相关
    message_received = pyqtSignal(dict)  # 新消息
    # 内部信号：hook 回调添加消息后触发 UI 刷新（跨线程安全）
    _hook_messages_updated = pyqtSignal()
    stream_started = pyqtSignal()
    stream_chunk = pyqtSignal(str)  # 流式内容片段
    stream_finished = pyqtSignal(dict)  # 完成时的消息
    reasoning_content = pyqtSignal(str)  # DeepSeek thinking mode

    # 工具相关
    tool_call_started = pyqtSignal(str, str, dict)  # tool_call_id, tool_name, arguments
    tool_result_received = pyqtSignal(str, str, dict, bool)  # tool_call_id, name, result, success

    # 权限相关
    permission_requested = pyqtSignal(str, str, dict)  # tool_call_id, tool_name, arguments

    # 错误
    error_occurred = pyqtSignal(str)

    # 上下文
    context_updated = pyqtSignal(int, int)  # token_count, limit

    # Auto-compact 请求（由 tool_executor 在 PostToolUse hook 中检测阈值触发）
    auto_compact_requested = pyqtSignal(float)  # ratio

    # Gateway 状态
    gateway_status_changed = pyqtSignal(dict)  # status dict

    # Gateway 消息处理（跨线程）
    gateway_input_received = pyqtSignal(object)  # dict: {text, chat_id, user_id, platform, future}

    # 插件热更新信号（watchfiles 检测到变更时触发）
    plugin_changed = pyqtSignal(dict)  # {"agents": int, "commands": bool, "themes": bool}

    # SubAgentManager 延迟创建完成信号（[审查 #8r Bug D] 窗口在 __init__ 时
    # sub_agent_manager 尚为 None 跳过信号连接，创建完成后据此补连）
    sub_agent_ready = pyqtSignal()

    # Hook 执行状态信号（event_name, status_message, is_start）
    # TODO: 当前没有 UI 订阅此信号。状态消息字段 (`statusMessage`) 已可解析但尚未展示。
    #       待 hook_setting_card 或状态栏/通知组件接入后即可移除此 TODO。
    hook_status_changed = pyqtSignal(str, str, bool)
    # 后台线程请求主线程执行插件重载（内部信号）
    _hot_reload_requested = pyqtSignal(str, str)  # (插件名, 组件), ""=全量/空组件=全部组件
    # _watch_loop 检测到新插件时，用此 sentinel 作为 plugin_name 标记走增量加载路径
    _NEW_PLUGIN_SENTINEL = "__NEW__"

    def __init__(self, parent=None, window_id: str = ""):
        super().__init__(parent)

        # 窗口标识（用于 per-window 隔离，如 hook 预设）
        self._window_id: str = window_id

        # 核心组件（后端自己创建）
        self._session_manager: Optional[SessionManager] = None
        self._chat_engine: Optional[ChatEngine] = None
        self._tool_executor: Optional[ToolExecutor] = None
        self._agent_manager: Optional[AgentManager] = None
        self._memory_manager: Optional[MemoryManagerCore] = None
        self._hook_manager: Optional[HookManager] = None
        self._sub_agent_manager = None
        self._session_store = None
        self._history_manager = None
        self._current_project = None
        # 子智能体默认模型解析回调（由 main_widget 设置；延迟创建 SubAgentManager 时不可重置）
        self._subagent_model_resolver: Optional[Callable] = None
        # 子智能体获取主会话历史的回调（由 main_widget 设置；SubAgentManager 创建后补传）
        self._sub_agent_history_getter: Optional[Callable] = None
        # ChatEngine 未创建前暂存的 UI 回调（延迟创建完成后补注册，见 set_all_callbacks）
        self._pending_engine_callbacks: Dict[str, Callable] = {}

        # 配置回调
        self._get_model_config: Optional[Callable] = None

        # 线程池
        self._thread_pool = QThreadPool()

        # 状态
        self._initialized = False

        # per-window 工具权限控制器（由 main_widget 在 initialize 之前注入）
        self._tool_permission_controller = None

        # Gateway 组件
        self._gateway_manager = None
        self._gateway_engine: Optional[GatewayEngine] = None
        self._gateway_initialized = False

        # Auto-compact 防重复触发时间戳
        self._last_auto_compact_time = 0.0

        # ★ T3 修复：注册为活跃实例（插件热更新 plugin_changed 广播目标）。
        # cleanup() 中移除，避免已关闭窗口的 backend 被广播（防泄漏）。
        ChatBackend._active_instances.add(self)

    # ========== 属性访问 ==========

    @property
    def current_project(self) -> str:
        return self._current_project

    @property
    def session_manager(self) -> SessionManager:
        return self._session_manager

    @property
    def tool_permission_controller(self):
        """per-window 工具权限控制器（主窗口注入,供 engine 读取）"""
        return self._tool_permission_controller

    def set_tool_permission_controller(self, controller):
        """注入 per-window 工具权限控制器(必须在 initialize 之前调用)"""
        self._tool_permission_controller = controller

    def request_auto_compact(self, ratio: float):
        """请求自动上下文压缩（带冷却防抖）

        tool_executor 的 PostToolUse hook 检测到上下文使用比例超过阈值时，
        调用此方法发射 auto_compact_requested 信号。
        主窗口收到信号后触发 /compact --clear。

        Args:
            ratio: 当前 token 使用比例 (0.0 ~ 1.0)
        """
        now = time.time()
        if now - self._last_auto_compact_time < _AUTO_COMPACT_COOLDOWN:
            logger.info(
                f"[ChatBackend] Auto-compact 触发被冷却抑制 "
                f"(ratio={ratio:.1%}, 距上次={now - self._last_auto_compact_time:.0f}s)"
            )
            return
        self._last_auto_compact_time = now

        logger.info(f"[ChatBackend] Auto-compact 触发 (ratio={ratio:.1%})")
        self.auto_compact_requested.emit(ratio)

    @property
    def chat_engine(self) -> ChatEngine:
        return self._chat_engine

    @property
    def tool_executor(self) -> ToolExecutor:
        return self._tool_executor

    @property
    def agent_manager(self) -> AgentManager:
        return self._agent_manager

    @property
    def memory_manager(self) -> MemoryManagerCore:
        return self._memory_manager

    @property
    def sub_agent_manager(self):
        return self._sub_agent_manager

    @property
    def hook_manager(self) -> Optional[HookManager]:
        return self._hook_manager

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    @property
    def session_store(self):
        """活跃存储引擎（StorageRegistry.get_active()，Phase C UI 收口）。

        冷启动防御：warmup 未执行时幂等加载系统插件（复用模块级门面）。
        多窗口共享同一引擎实例（registry 单例），db 连接与 SessionStore 单例一致。
        """
        try:
            return get_session_storage()
        except Exception as e:
            logger.warning(f"[ChatBackend] 存储引擎获取失败，回退 SessionStore: {e}")
            return self._session_store

    def get_session_storage(self):
        """实例门面：获取活跃存储引擎（委托模块级 get_session_storage）"""
        return get_session_storage()

    @property
    def history_manager(self):
        return self._history_manager

    # ========== 初始化 ==========

    def initialize(
        self,
        get_model_config: Callable[[], Dict[str, Any]],
        workdir: str = None,
    ):
        """
        后端初始化 - 自己创建所有组件（不依赖 Qt）

        [PERF] 组件创建分两段：
        - 同步段：首帧必需组件（SessionManager / HookManager / create_session /
          AgentManager 骨架 / HistoryManager），setup_ui 的 get_primary_agents
          等路径立即可用。
        - 延迟段：非首帧必需组件（MemoryManager / ToolExecutor / ChatEngine /
          GatewayEngine / SubAgentManager + MCP + git 预热）用 QTimer
          0/200/400/600ms 错峰创建，失败仅记日志不影响 UI 主流程。

        Args:
            get_model_config: 获取模型配置的回调
            agent_manager: 已有的 AgentManager（可选）
            workdir: 工作目录
        """
        import time as _time

        _t0 = _time.perf_counter()
        logger.info("[ChatBackend] 初始化中...")

        self._get_model_config = get_model_config
        self._initial_workdir = workdir

        # 1. 创建 SessionManager（延迟导入，减少 import 级联）
        from app.core.store import SessionStore
        from app.core.chat_session import SessionManager

        self._session_store = SessionStore.get_instance()
        self._session_manager = SessionManager()
        logger.info(f"[ChatBackend-Perf] SessionManager 创建完成 ({(_time.perf_counter() - _t0) * 1000:.0f}ms)")

        # 2. 创建 HookManager（必须在 create_session 之前）
        from app.core.hook_manager import HookManager

        self._hook_manager = HookManager(self._thread_pool)
        # UI 有效性标志：当 UI 窗口关闭时应设为 False，防止 hook 回调访问已销毁的 UI
        self._ui_valid = True

        # 线程安全队列：用于 tool_executor 中触发的 hook 输出传递
        # _hook_message_queue: PostToolUse（异步触发，在 loop 顶部消费）
        # _pre_tool_message_queue: PreToolUse（同步触发，在 tool 执行后立即消费）
        # 预对话 hook（SessionStart/PreUserMessage/PostUserMessage 等）由 engine.py 直接注入
        # chat_worker 内部 hook（PreAssistant/PostAssistant/Stop）由 worker 直接注入
        self._hook_message_queue: "queue.Queue[Dict]" = queue.Queue()
        self._pre_tool_message_queue: "queue.Queue[Dict]" = queue.Queue()

        # Hook 完成回调 — 仅处理需要通过队列传递给 worker 的事件
        # 预对话事件（SessionStart, PreUserMessage, PostUserMessage 等）不经过此回调，
        # 由 engine.py 收集 trigger_event 返回值后直接注入 session.messages
        # 🛡️ 这些预对话事件由调用方以 trigger_async=False 同步触发，结果通过返回值 +
        # _inject_hook_to_session 直接写入 session.messages。若在 _execute_hook 同步路径中
        # 再走本回调，会出现「signal 早于 _inject_hook_to_session」竞态：
        # _on_hook_messages_changed 看到 session.messages 还是空的，但此时若处在
        # _create_new_session 留下的 _session_switched 哨兵窗口里，_on_messages_updated 会
        # 误报「worker 过期回调」并在 main_widget 中产生 WARNING 日志。
        # 直接 return 让最终的 signal 由调用方（create_session / trigger_session_event /
        # engine.send_message）在所有 hook 输出注入完成后再 emit。
        _PRE_DIALOG_EVENTS = {"SessionStart", "PreUserMessage", "PostUserMessage"}

        def on_hook_finished(event_name: str, output: str, success: bool, status_message: str = ""):
            if not getattr(self, "_ui_valid", True):
                logger.debug("[HookManager] Hook callback skipped: UI already closed")
                return

            is_prompt_hook = event_name.startswith("__prompt__:")
            if is_prompt_hook:
                event_name = event_name[len("__prompt__:") :]

            # 🛡️ W1：异步 worker 完成回调标记（command/http 类型 hook 后台执行后
            # 回补注入用）。同步路径（trigger_async=False）无此前缀。
            # 🛡️ F1(W1-R2)：异步事件名可能携带触发时的 session_id（格式
            # `__async__:<event>:<sid>`），回补注入前校验当前会话一致。
            is_async_hook = event_name.startswith("__async__:")
            async_session_id = ""
            if is_async_hook:
                rest = event_name[len("__async__:") :]
                if ":" in rest:
                    event_name, _, async_session_id = rest.partition(":")
                else:
                    event_name = rest

            # BuildSystemPrompt hook 的输出已在 get_agent_system_prompt() 中直接注入 system prompt，
            # 不需要再通过队列注入到 assistant 消息中，跳过回调避免双重注入。
            if event_name == "BuildSystemPrompt":
                return

            # 🛡️ 预对话 hook 的输出由调用方负责直接注入 session.messages；
            # 这里再 emit signal 会和 _session_switched 哨兵冲突，同时还会污染
            # _hook_message_queue（chat_worker 不会消费 SessionStart 等预对话事件）。
            # 例外（W1）：异步路径（__async__ 前缀，UI 线程 SessionStart 的
            # command/http hook 后台执行）输出仅此一处回补注入，不注入则丢失。
            # 🛡️ F1(W1-R2)：回补注入前校验当前会话 == 触发时会话，不一致则丢弃
            # （用户已切换会话，注入会污染错误 session 的上下文）。
            if event_name in _PRE_DIALOG_EVENTS:
                if is_async_hook and success and output and output.strip():
                    session = self.get_current_session()
                    if session is not None and (not async_session_id or session.session_id == async_session_id):
                        _inject_hook_to_session(session, event_name, output, status_message)
                        self._hook_messages_updated.emit()
                        logger.info(f"[HookManager] Async hook output injected: {event_name}")
                    else:
                        logger.info(
                            f"[HookManager] Async hook output dropped (session switched): "
                            f"{event_name}, expect_sid={async_session_id!r}, "
                            f"current_sid={getattr(session, 'session_id', None)!r}"
                        )
                return

            logger.info(f"[HookManager] Hook callback: event={event_name}, success={success}")

            if not success:
                return

            # 跳过空输出（禁用/无匹配的 hook 不产生实际内容）
            if not output or not output.strip():
                return

            # PreToolUse → 独立队列（worker 在 tool 执行后立即消费，插入 tool result 之前）
            # PostToolUse → 主队列（在 loop 顶部消费，出现在 tool result 之后）
            # prompt hooks → 主队列
            if event_name == "PreToolUse":
                hook_output = _format_hook_output(event_name, output, status_message)
                self._pre_tool_message_queue.put(
                    {
                        "role": "user",
                        "content": hook_output,
                        "_hook_event": event_name,
                    }
                )
                logger.debug("[HookManager] PreToolUse queued to pre-tool queue")
            elif is_prompt_hook or event_name == "PostToolUse":
                hook_output = _format_hook_output(event_name, output, status_message)
                self._hook_message_queue.put(
                    {
                        "role": "user",
                        "content": hook_output,
                        "_hook_event": event_name,
                    }
                )
                self._hook_messages_updated.emit()
                logger.debug(f"[HookManager] Hook queued for worker: {event_name}")

        self._hook_manager.set_on_finished_callback(on_hook_finished)

        # Hook 执行状态回调 — 转发为 Qt 信号供 UI 使用
        def on_hook_status(event_name: str, status_message: str, is_start: bool):
            if not getattr(self, "_ui_valid", True):
                return
            self.hook_status_changed.emit(event_name, status_message, is_start)

        self._hook_manager.set_on_status_callback(on_hook_status)

        # 4. 创建初始会话（不触发 SessionStart hook，避免重复初始化）
        self.create_session(trigger_hook=False)

        # 5. 使用全局共享的 AgentManager（只读数据，跨窗口复用）
        # agents_dir 传 None，智能体从已启用插件动态加载
        from app.core.agent import AgentManager

        self._agent_manager = AgentManager.get_instance(None, self._hook_manager)
        logger.info(f"[ChatBackend] AgentManager 就绪，{len(self._agent_manager.list_agents())} 个 Agent")

        # 加载全局 hooks（从 PluginManager 获取路径）
        from app.plugins.managers.plugin_manager import PluginManager

        pm = PluginManager.get_instance()
        if pm.is_initialized():
            global_hooks_file = pm.get_global_hooks_file()
            if global_hooks_file.exists() and "user-custom" not in self._hook_manager._skill_to_hooks:
                try:
                    with open(global_hooks_file, "rb") as f:
                        config = json.loads(f.read())
                    skill_root = str(global_hooks_file.parent)
                    count = self._hook_manager.register_hooks_from_json(
                        "user-custom", skill_root, config, str(global_hooks_file)
                    )
                    if count > 0:
                        logger.info(f"[ChatBackend] Loaded {count} global hooks from {global_hooks_file}")
                except Exception as e:
                    logger.error(f"[ChatBackend] Failed to load global hooks from {global_hooks_file}: {e}")

        # 6. 初始化 PluginManager（系统 + 用户插件发现）+ AgentManager 重载
        # [PERF] 保持同步：插件加载的 agent/命令是 setup_ui 立即需要的（get_primary_agents），
        # 主题/LSP/热更新等非关键子步骤已在 _init_plugin_system 内部延迟（QTimer 2s）。
        self._init_plugin_system()

        # 旧覆盖层数据一次性迁移：非系统 hook 状态写回源文件、幽灵 id 清理
        # （必须等插件 hooks 全部注册完成后调用，否则会误删禁用插件的状态）
        try:
            self._hook_manager.migrate_legacy_hook_states()
        except Exception as e:
            logger.error(f"[ChatBackend] Hook state migration failed: {e}")

        # 7. HistoryManager（全局单例）
        # [PERF] 保持同步：main_widget 初始化时直接缓存 self.history_manager（92 处引用），
        # 延迟会导致历史面板引用 None 而静默失效。
        from app.utils.history_manager import HistoryManager

        self._history_manager = HistoryManager.get_instance()

        # 连接 hook 消息更新信号 → UI 刷新（跨线程安全）
        self._hook_messages_updated.connect(self._on_hook_messages_changed)

        # 8. 非首帧必需组件：QTimer 错峰创建（Memory/ToolExecutor/Engine/SubAgent/MCP/git）
        # [PERF] 延迟段组件失败仅记日志，不阻塞 UI 主流程；发送消息路径通过
        # ensure_deferred_components() 同步补建兜底。
        self._defer_non_critical_components()

        self._initialized = True
        logger.info("[ChatBackend] 初始化完成（同步段）")

        # 连接 Gateway 信号（跨线程安全，每个窗口实例连接自己的回调）
        self.gateway_input_received.connect(self._on_gateway_input)

        # 初始化 Gateway（后台进行，不阻塞）
        # 使用 get_platform_manager() 判断是否已存在管理器实例，避免重复初始化
        from app.gateway.manager import get_platform_manager

        existing_mgr = get_platform_manager()
        if existing_mgr is not None:
            self._gateway_manager = existing_mgr
            self._gateway_initialized = True
            logger.debug("[ChatBackend] Gateway 已存在，复用管理器")
        else:
            self._init_gateway_async()

    # ========== 非首帧必需组件：QTimer 错峰创建 ==========

    def _defer_non_critical_components(self):
        """[PERF] 非首帧必需组件用 QTimer 错峰创建（0/200/400/600ms）

        首帧路径（OpenAIChatToolWindow.__init__）只保留 SessionManager /
        HookManager / create_session / AgentManager / HistoryManager，
        其余组件延迟到事件循环就绪后分批构建，缩短窗口显示前的主线程阻塞：

        - 0ms:   MemoryManagerCore（全局单例，ToolExecutor 依赖）
        - 200ms: ToolExecutor（app/tools 级联 import 8 模块 + LSP + codegraph，
                实测 import 重头，最值得延迟）
        - 400ms: ChatEngine + GatewayEngine（依赖 tool_executor）
        - 600ms: SubAgentManager + MCP 连接 + git 缓存预热（依赖 tool_executor）

        失败处理：各批 try/except 只记日志，不抛到事件循环；
        UI 使用处均有 None 守卫（tool_executor/chat_engine 等访问都判空）。
        发送消息路径由 main_widget 调 ensure_deferred_components() 同步兜底。
        """
        from PyQt5.QtCore import QTimer

        QTimer.singleShot(0, self._deferred_create_memory_manager)
        QTimer.singleShot(200, self._deferred_create_tool_executor)
        QTimer.singleShot(400, self._deferred_create_engines)
        QTimer.singleShot(600, self._deferred_create_sub_agent_and_misc)

    def _deferred_create_memory_manager(self):
        """0ms 批：MemoryManagerCore（全局单例，跨窗口共享）"""
        try:
            from app.core.memory_manager import MemoryManagerCore

            self._memory_manager = MemoryManagerCore.get_instance()
            logger.debug("[ChatBackend] MemoryManager 延迟创建完成")
        except Exception as e:
            logger.error(f"[ChatBackend] MemoryManager 延迟创建失败: {e}")

    def _deferred_create_tool_executor(self):
        """200ms 批：ToolExecutor（含 app.tools 级联 import）"""
        if self._tool_executor is not None:
            return
        try:
            from app.core.tool_executor import ToolExecutor

            self._tool_executor = ToolExecutor(workdir=self._initial_workdir, backend=self)
            if self._memory_manager:
                self._tool_executor.set_memory_manager(self._memory_manager)
            self._tool_executor.set_llm_config_getter(self._get_model_config)
            self._tool_executor.set_agent_manager(self._agent_manager)
            # 初始化团队上下文（窗口 ID + 当前 agent，供团队工具使用）
            if self._tool_executor._builtin_tools:
                agent_name = self._agent_manager.list_agents(include_hidden=False)
                default_agent = agent_name[0].name if agent_name else "build"
                self._tool_executor._builtin_tools.set_team_context(self._window_id, default_agent)
            # 设置 AgentManager 的 builtin_tools 引用（用于获取 MCP 工具 schema）
            self._agent_manager._builtin_tools = self._tool_executor._builtin_tools
            # 设置关键文档仓储
            if self._memory_manager and self._memory_manager.key_documents:
                self._tool_executor.set_key_documents_repo(
                    self._memory_manager.key_documents,
                    "默认项目",  # 初始值，main_widget 初始化后会通过 set_current_project 覆盖
                )
            logger.info("[ChatBackend] ToolExecutor 延迟创建完成")
        except Exception as e:
            logger.error(f"[ChatBackend] ToolExecutor 延迟创建失败: {e}")

    def _deferred_create_engines(self):
        """400ms 批：ChatEngine + GatewayEngine（依赖 tool_executor）"""
        if self._tool_executor is None:
            logger.warning("[ChatBackend] ToolExecutor 未创建，跳过引擎延迟创建")
            return
        try:
            from app.core.engines.ui import ChatEngine
            from app.plugins.registries.engine_registry import create_engine_for_slot
            from app.plugins.loaders.runtime_component_loader import ensure_engine_watcher

            # 触发引擎 watcher（注册/热重载），无插件时 no-op
            ensure_engine_watcher()
            self._chat_engine = create_engine_for_slot(
                "ui",
                ChatEngine,
                session_manager=self._session_manager,
                get_model_config=self._get_model_config,
                tool_executor=self._tool_executor,
                agent_manager=self._agent_manager,
                get_chat_cards=getattr(self, "_build_chat_cards_context", None),
                backend=self,
            )
            logger.info("[ChatBackend] ChatEngine 延迟创建完成")
            # [审查 #8r Bug C] 窗口构造期暂存的 UI 回调（流式更新等）补注册
            self._flush_pending_engine_callbacks()
        except Exception as e:
            logger.error(f"[ChatBackend] ChatEngine 延迟创建失败: {e}")

        try:
            # 创建 GatewayEngine（全局单例，多个窗口共享）
            from app.core.engines.gateway import GatewayEngine

            self._gateway_engine = GatewayEngine.get_instance(
                get_model_config=self._get_model_config,
                tool_executor=self._tool_executor,
                agent_manager=self._agent_manager,
                session_store=self._session_store,
            )
            logger.info("[ChatBackend] GatewayEngine 延迟创建完成")
        except Exception as e:
            logger.error(f"[ChatBackend] GatewayEngine 延迟创建失败: {e}")

    def _deferred_create_sub_agent_and_misc(self):
        """600ms 批：SubAgentManager + MCP 连接 + git 缓存预热"""
        if self._tool_executor is None:
            logger.warning("[ChatBackend] ToolExecutor 未创建，跳过 SubAgentManager 延迟创建")
            return
        try:
            # SubAgentManager（依赖 tool_executor / agent_manager）
            # 注意：不得重置 _subagent_model_resolver / _sub_agent_history_getter——
            # main_widget 在窗口构造时（initialize 返回后）已设置，此处延迟创建
            # 若覆盖会导致子智能体默认模型/历史回调失效（审查 #8r Bug A/B）。

            def _get_subagent_llm_config():
                from app.utils.config import Settings

                cfg = Settings.get_instance()
                saved = cfg.llm_subagent_default_model.value
                if saved and self._subagent_model_resolver:
                    resolved = self._subagent_model_resolver(saved)
                    if resolved:
                        return resolved
                return self._get_model_config()

            from app.core.workers.subagent_worker import SubAgentManager

            self._sub_agent_manager = SubAgentManager(
                agent_manager=self._agent_manager,
                tool_executor=self._tool_executor,
                get_llm_config=_get_subagent_llm_config,
            )
            self._sub_agent_manager.set_session_store(self._session_store)
            # 补传主会话历史 getter（main_widget 设置时 SubAgentManager 尚未创建）
            if self._sub_agent_history_getter:
                self._sub_agent_manager.set_history_getter(self._sub_agent_history_getter)
            # 设置给 ToolExecutor，让工具能访问子智能体
            self._tool_executor.set_sub_agent_manager(self._sub_agent_manager)
            # 启动日志活力度 stall 检测（默认 180s 无日志输出视为卡死）
            self._sub_agent_manager.start_stall_detector()
            # [审查 #8r Bug D] 通知窗口补连子智能体信号（窗口构造时 manager 为 None）
            self.sub_agent_ready.emit()
            logger.info("[ChatBackend] SubAgentManager 延迟创建完成")
        except Exception as e:
            logger.error(f"[ChatBackend] SubAgentManager 延迟创建失败: {e}")

        # 自动发现并合并其他来源的 MCP 服务器配置（仅首次）
        try:
            self._discover_mcp_servers()
        except Exception as e:
            logger.error(f"[ChatBackend] MCP 自动发现失败: {e}")

        # 初始化 MCP 连接（后台异步，不阻塞 UI）
        try:
            self._init_mcp_connections()
        except Exception as e:
            logger.error(f"[ChatBackend] MCP 连接失败: {e}")

        # 后台预热 git 缓存，避免 create_session 时同步执行 git 子进程（~1.1s）
        try:
            project_root = self._tool_executor.get_workdir() if self._tool_executor else ""
            if project_root is None:
                project_root = ""
            self._warm_git_cache(project_root)
        except Exception as e:
            logger.error(f"[ChatBackend] git 缓存预热失败: {e}")

    def ensure_deferred_components(self):
        """发送消息等关键路径：同步补建延迟组件（QTimer 未触发时兜底）

        用户可能在 600ms 延迟窗口内发起首条消息，此时 chat_engine 尚未创建，
        发送会静默失败。此方法按依赖顺序同步创建缺失组件，保证发送路径可用。
        """
        if self._chat_engine is not None:
            return
        if self._memory_manager is None:
            self._deferred_create_memory_manager()
        if self._tool_executor is None:
            self._deferred_create_tool_executor()
        if self._chat_engine is None:
            self._deferred_create_engines()
        if self._sub_agent_manager is None:
            self._deferred_create_sub_agent_and_misc()

    def set_callback(self, name: str, callback: Callable):
        """设置回调（代理到 ChatEngine）"""
        if self._chat_engine:
            self._chat_engine.set_callback(name, callback)
        else:
            # [审查 #8r Bug C] ChatEngine 延迟创建（400ms 批）期间 main_widget 同步
            # 设置回调会静默丢弃 → 暂存，创建完成后补注册
            self._pending_engine_callbacks[name] = callback

    def set_all_callbacks(self, callbacks: Dict[str, Callable]):
        """批量设置回调"""
        if self._chat_engine:
            for name, callback in callbacks.items():
                self._chat_engine.set_callback(name, callback)
        else:
            # [审查 #8r Bug C] 同上：先缓存，ChatEngine 创建后统一补注册
            self._pending_engine_callbacks.update(callbacks)

    def _flush_pending_engine_callbacks(self):
        """ChatEngine 创建完成后补注册暂存的 UI 回调（审查 #8r Bug C）"""
        if self._chat_engine is None or not self._pending_engine_callbacks:
            return
        callbacks, self._pending_engine_callbacks = self._pending_engine_callbacks, {}
        for name, callback in callbacks.items():
            try:
                self._chat_engine.set_callback(name, callback)
            except Exception as e:
                logger.error(f"[ChatBackend] 补注册引擎回调失败 {name}: {e}")
        if callbacks:
            logger.debug(f"[ChatBackend] 补注册 {len(callbacks)} 个暂存引擎回调")

    def _on_hook_messages_changed(self):
        """槽：hook 消息已添加到 session，通知 UI 刷新消息列表

        通过 _hook_messages_updated 信号连接（跨线程安全），
        确保在 hook 后台线程执行完毕后，UI 能及时显示 hook 输出。

        Hook 消息注入 worker API 缓存的职责已由队列路径
        （_inject_pending_hook_messages，在 worker 线程中运行）承担，
        避免跨线程直接访问 worker 内部状态带来的竞态风险。
        """
        if not getattr(self, "_ui_valid", True):
            return

        session = self.get_current_session()
        if not session:
            return

        # 🛡️ 兜底：极少数遗留路径仍可能在 _inject_hook_to_session 之前 emit signal
        # （例如未来新增的预对话 hook 漏掉 on_hook_finished 跳过的情形）。
        # 空消息触发 messages_updated 会让 UI 进入「等不到消息」状态，且会触发
        # _session_switched 哨兵误报。直接跳过即可，调用方会在注入后再 emit 一次。
        if not session.messages:
            return

        # 通知 UI 刷新消息列表
        if self._chat_engine:
            self._chat_engine._emit("messages_updated", list(session.messages))

    def set_subagent_model_resolver(self, resolver: Callable[[str], Optional[Dict]]):
        """设置子智能体默认模型解析回调

        Args:
            resolver: 接收 model_value 字符串，返回完整模型配置 dict 或 None
                      由 main_widget._resolve_subagent_model_config 提供
        """
        self._subagent_model_resolver = resolver

    # ========== 插件系统初始化 ==========

    def _init_plugin_system(self):
        """初始化 PluginManager，加载所有插件

        [PERF] 拆分为关键路径和非关键路径：
        - 关键路径：PluginManager 扫描 + AgentManager 重载（必须同步，智能体/命令需要）
        - 非关键路径：主题刷新 + 热更新监听 + LSP 初始化 → 延迟到窗口就绪后执行
        """
        try:
            from app.plugins.managers.plugin_manager import PluginManager
            from app.utils.utils import get_app_data_dir

            pm = PluginManager.get_instance()
            app_data_dir = get_app_data_dir()

            # 记录初始化前的状态，用于判断是否为首次初始化
            was_initialized = pm.is_initialized()
            pm.initialize(app_data_dir)

            # 首次初始化时才需要全量重载智能体
            # 后续窗口复用已有的 PluginManager/AgentManager 单例数据
            if not was_initialized:
                # AgentManager 重新从已启用插件加载智能体（关键路径）
                if self._agent_manager:
                    self._agent_manager.reload_agents()

                # ── 非关键路径：延迟到窗口就绪后执行 ──
                # 主题刷新、插件热更新监听、LSP 初始化不需要阻塞首帧显示
                # 使用 QTimer 推迟执行（backend 本身不依赖 Qt，由调用方确保）
                self._defer_non_critical_plugin_init(pm)
            else:
                # ── 窗口重开场景：补启动插件热更新监听 ──
                # 应用托盘驻留（setQuitOnLastWindowClosed(False)），关闭全部窗口后进程
                # 仍存活，但 backend.cleanup() 已把 watcher 引用计数归零并停止监听线程
                # （_stop_plugin_watcher 会复位 _plugin_watcher_started=False）。
                # 而 PluginManager 单例仍保持已初始化状态，上方首次初始化分支被跳过，
                # 导致 watcher 永不重启 → 此后新增 agent/命令/技能文件均不触发热重载，
                # /命令列表看不到新增子智能体。
                # _start_plugin_watcher 内部有引用计数 + started 标志双重保护：
                # watcher 存活时调用只 +1 引用计数，不会重复创建线程。
                try:
                    self._start_plugin_watcher()
                except Exception as e:
                    logger.error(f"[ChatBackend] 窗口重开重启插件监听失败: {e}")

            logger.info(
                f"[ChatBackend] PluginManager 初始化完成，"
                f"已加载 {len(pm.list_plugins())} 个插件，"
                f"智能体 {len(self._agent_manager.list_agents())} 个"
            )

        except Exception as e:
            logger.error(f"[ChatBackend] PluginManager 初始化失败: {e}")

    def _defer_non_critical_plugin_init(self, pm):
        """非关键插件初始化：主题/LSP/热更新，延迟执行不阻塞 UI"""
        # 使用 QTimer 延迟执行（backend 提供 _deferred_timer 供调用方关联到 Qt 事件循环）
        from PyQt5.QtCore import QTimer

        def _do_deferred():
            # 内置组件 reloader 注册（幂等，进程一次 — chat_backend.py 顶层注册表
            # 可能在 ChatBackend 之前已被其他模块 import kernel 注册过，幂等保护）
            try:
                from app.plugins.builtin_reloaders import bind_runtime, register_builtin_reloaders
                from app.plugins.kernel import get_reloader_registry

                bind_runtime(_safe_agent_manager(self))
                register_builtin_reloaders(get_reloader_registry())
            except Exception as e:
                logger.error(f"[ChatBackend] 内置 reloader 注册失败: {e}")

            # 刷新主题
            try:
                self._reload_themes_from_plugins()
            except Exception as e:
                logger.error(f"[ChatBackend] 延迟主题刷新失败: {e}")

            # 启动插件文件变更监听（热更新，仅启动一次）
            try:
                self._start_plugin_watcher()
            except Exception as e:
                logger.error(f"[ChatBackend] 延迟启动插件监听失败: {e}")

            # 插件工具按启用状态对齐重扫：工具加载发生在 import 期（早于 pm.initialize），
            # 彼时新插件尚未被 _restore_enabled_from_settings 补齐到 enabled 列表 →
            # 新装插件工具被过滤；pm.initialize 已在此前完成，重扫后
            # 新安装插件工具注册、被禁用插件工具注销，两边同时正确。
            try:
                from app.plugins.loaders.plugin_tool_loader import ensure_plugin_tool_watcher

                watcher = ensure_plugin_tool_watcher()
                if watcher is not None:
                    watcher.scan_now()
            except Exception as e:
                logger.error(f"[ChatBackend] 插件工具启用状态对齐重扫失败: {e}")

            # 服务商插件（providers）：延迟初始化加载 + 热重载 watcher
            # （与工具插件并列；服务商核心数据在 UI 初始化前就绪）
            try:
                from app.plugins.loaders.provider_loader import ensure_provider_watcher
                from app.plugins.registries.provider_registry import ProviderRegistry

                ProviderRegistry.get_instance().ensure_loaded()
                pwatcher = ensure_provider_watcher()
                if pwatcher is not None:
                    pwatcher.scan_now()
            except Exception as e:
                logger.error(f"[ChatBackend] 服务商插件初始化失败: {e}")

            # 运行时组件（model_adapters / loop_policies / storages）：
            # 内置实现先注册，插件目录可覆盖内置
            try:
                from app.plugins.loaders.runtime_component_loader import warmup_runtime_components

                warmup_runtime_components()
            except Exception as e:
                logger.error(f"[ChatBackend] 运行时组件 warmup 失败: {e}")

            # 初始化 LSP 管理器（仅首次，多窗口共享单例）
            try:
                from app.core.lsp.lsp_manager import get_lsp_manager

                lsp_mgr = get_lsp_manager()
                lsp_configs = pm.get_lsp_configs()
                workdir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                lsp_mgr.initialize(workdir, lsp_configs)
                logger.info(f"[ChatBackend] LspManager 延迟初始化完成，已注册 {len(lsp_mgr._clients)} 个 LSP 服务器")
                lsp_mgr.start_all_background()
            except Exception as e:
                logger.error(f"[ChatBackend] LSP 延迟初始化失败: {e}")

        # 延迟 2 秒执行，让窗口首帧 + 用户交互先就绪
        QTimer.singleShot(2000, _do_deferred)

    def _reload_themes_from_plugins(self):
        """插件系统初始化后，重新加载插件主题"""
        try:
            from app.utils.theme_manager import theme_manager

            theme_manager.reload()
            # 同时更新 Settings 中的主题选项
            from app.utils.config import update_theme_options

            update_theme_options()

            # 安全网：插件主题全部加载后，确保配置中保存的主题被正确恢复。
            # 读取配置文件中的保存值，与当前值比对。若被重置（启动过程中因列表不含
            # 插件主题而被回退到系统主题），在此修正。即使当前值恰好是列表中的某个
            # 系统主题，只要与保存值不同就恢复，保证用户选择不受中间状态影响。
            from app.utils.config import Settings

            settings = Settings.get_instance()
            themes = theme_manager.list_themes()
            current = settings.ui_theme_style.value
            from app.utils.utils import get_app_data_dir
            import orjson as json

            config_file = get_app_data_dir() / "app.config"
            saved_theme = None
            if config_file.exists():
                try:
                    raw = config_file.read_text(encoding="utf-8")
                    data = json.loads(raw)
                    saved_theme = data.get("UI", {}).get("ThemeStyle")
                except Exception:
                    pass

            if saved_theme and saved_theme in themes:
                if current != saved_theme:
                    settings.ui_theme_style.value = saved_theme
                    logger.info(f"[ChatBackend] 从配置文件恢复保存的主题: {saved_theme} (当前: {current})")
            elif saved_theme and saved_theme not in themes:
                # 保存的主题不可用（如插件已卸载），当前值如果也不在列表中才回退
                if current not in themes and themes:
                    fallback = next(iter(themes))
                    settings.ui_theme_style.value = fallback
                    logger.info(
                        f"[ChatBackend] 保存的主题 {saved_theme} 不可用，当前值 {current} 也不可用，回退至: {fallback}"
                    )

            logger.info(
                f"[ChatBackend] 插件主题刷新完成，共 {len(themes)} 个主题, 当前主题: {settings.ui_theme_style.value}"
            )
        except Exception as e:
            logger.error(f"[ChatBackend] 刷新插件主题失败: {e}")

    # ========== 插件热更新（watchfiles） ==========

    _plugin_watcher_started = False  # 类级别标志，确保全局只启动一次
    # 🚀 P5e：gitee 同步抑制窗口——config_sync 下载解压 user-custom 期间抑制
    # 本 watcher 热重载链（backend 独立 watchfiles 线程），避免与 config_sync
    # 应用链（Settings 写回 + 主题刷新）两条主线程重活链同时爆发叠加阻塞。
    # _suppress_watcher_until: 抑制截止时间戳（0=不抑制）；
    # _watcher_pending_reload: 抑制窗口内被跳过的 user-custom 变更标志，
    # 由 config_sync 下载完成后兜底合并触发一次 reload_plugin_subsystems。
    _suppress_watcher_until = 0.0
    _watcher_pending_reload = False
    # ★ T3 修复：活跃 backend 实例集合（插件热更新广播目标）
    # 根因：watcher 线程是类级单例（_plugin_watcher_started），只有首个启动
    # watcher 的 backend 连接了 _hot_reload_requested → _on_hot_reload_requested
    # 只 emit 该 backend 的 plugin_changed。宿主窗口关闭断开信号后，watcher
    # 线程仍存活（其他窗口 refcount>0）、数据照常重载，但 emit 无接收者 →
    # 所有窗口 UI 静默不刷新（全关重开才恢复）。
    # 修复：_on_hot_reload_requested 广播到全部活跃 backend 的 plugin_changed。
    _active_instances: set = set()  # ChatBackend 实例集合（__init__ 注册 / cleanup 移除）
    # ★ 泄漏修复（P1）：watcher 闭包持有首个 backend 实例引用（self._hot_reload_requested /
    # self.plugin_changed / self._identify_* 全部走实例成员），窗口关闭不停止则实例永不可回收。
    # 用引用计数 + stop_event 实现"最后一个窗口关闭时停止 watcher"：
    #   - refcount 在 __init__（_start_plugin_watcher）递增、cleanup 递减
    #   - 归零时设置 stop_event → watch() 生成器退出 → 线程结束 → 闭包释放 → 实例可回收
    #   - 新窗口启动时 refcount 从 0 递增会重新启动 watcher（stop_event 复位），热更新不丢失
    _plugin_watcher_refcount = 0  # 活跃 backend 引用计数
    _plugin_watcher_stop = None  # threading.Event：设置后 watch() 生成器退出
    _plugin_watcher_thread = None  # 当前 watcher 线程（cleanup 归零时 join 确保退出）

    def _start_plugin_watcher(self):
        """启动 watchfiles 插件文件变更监听（引用计数 +1，首个 backend 启动）"""
        ChatBackend._plugin_watcher_refcount += 1
        if ChatBackend._plugin_watcher_started:
            return
        ChatBackend._plugin_watcher_started = True

        try:
            from watchfiles import watch
        except ImportError:
            logger.warning("[ChatBackend] watchfiles 未安装，插件热更新不可用。pip install watchfiles")
            return

        # 自定义监听过滤器：在 watchfiles 默认 DefaultFilter（已排除 .git/__pycache__/
        # .pyc/.swp 等）基础上，额外排除插件内的“易变 / vendored”目录与产物文件。
        # 这是“文件多”插件（如带 lark_oapi SDK deps 的 gateway-feishu，deps 含数千 .py）
        # 热重载卡顿的根因：clone / 导入时成千上万的文件变更事件被 watchfiles 与后续
        # 分类逻辑处理，拖垮 watcher 线程并诱发主线程重载风暴。排除后事件量降 ~95%，
        # 且不影响各组件目录（agents/hooks/commands/... 与 deps 平级，不被排除）的热更新。
        from watchfiles import DefaultFilter

        class _PluginWatchFilter(DefaultFilter):
            # 额外排除的目录段（任意层级命中即忽略其下全部变更）
            _EXTRA_SKIP_DIRS = {
                "deps",
                "node_modules",
                ".venv",
                "venv",
                "build",
                "dist",
                "install_tmp",
                "__pycache__",
                ".git",
                ".hg",
                ".svn",
                ".mypy_cache",
                ".pytest_cache",
                ".ruff_cache",
                "tmp",
                "temp",
            }
            # 额外排除的产物 / 缓存文件
            _EXTRA_SKIP_EXTS = (
                ".pyc",
                ".pyo",
                ".pyd",
                ".so",
                ".egg-info",
                ".log",
                ".tmp",
                ".bak",
                ".swp",
            )

            def __call__(self, change, path):
                if not super().__call__(change, path):
                    return False
                norm = str(path).replace("\\", "/").lower()
                if any(seg in self._EXTRA_SKIP_DIRS for seg in norm.split("/")):
                    return False
                if norm.endswith(self._EXTRA_SKIP_EXTS):
                    return False
                return True

        watch_filter = _PluginWatchFilter()

        # 收集需要监听的插件目录
        from app.plugins.managers.plugin_manager import PluginManager

        pm = PluginManager.get_instance()

        watch_paths = []
        from pathlib import Path as _Path

        # 系统插件目录
        if hasattr(pm, "_SYSTEM_PLUGIN_DIR") and pm._SYSTEM_PLUGIN_DIR.exists():
            watch_paths.append(str(pm._SYSTEM_PLUGIN_DIR.resolve()))
        # 用户插件目录（开发环境下可能是相对路径，统一 resolve 为绝对路径）
        if pm._app_data_dir:
            user_plugin_dir = pm._app_data_dir / pm._USER_PLUGIN_DIR_NAME
            # 确保目录存在，否则 watcher 无法监听（用户后创建目录时热更新不生效）
            user_plugin_dir.mkdir(parents=True, exist_ok=True)
            watch_paths.append(str(user_plugin_dir.resolve()))
        # Claude Code 插件目录（同时支持两种生态）
        claude_skills_dir = _Path.home() / ".claude" / "skills"
        claude_skills_dir.mkdir(parents=True, exist_ok=True)
        watch_paths.append(str(claude_skills_dir.resolve()))
        claude_cache_dir = _Path.home() / ".claude" / "plugins" / "cache"
        if claude_cache_dir.exists():
            watch_paths.append(str(claude_cache_dir.resolve()))

        if not watch_paths:
            logger.warning("[ChatBackend] 无插件目录可监听，跳过热更新")
            return

        logger.info(f"[ChatBackend] 启动插件文件变更监听: {watch_paths}")

        # 连接内部信号到主线程重载方法
        self._hot_reload_requested.connect(self._on_hot_reload_requested)

        # 预计算插件路径 → 插件名映射（用于快速定位变更文件所属插件）
        plugin_prefixes = self._build_plugin_path_index()

        import threading as _threading

        # 上次全部窗口关闭后 stop_event 可能处于 set 状态（watch() 已退出），
        # 此处重建新事件，支持 watcher 在下一个 backend 上重启（热更新不丢失）。
        ChatBackend._plugin_watcher_stop = _threading.Event()

        # 去重缓存：(plugin_name, component) → 上次重载时间
        _dedup_cache: Dict[tuple, float] = {}
        # watchfiles 每 ~8s 对每个组件 emit 一次重载（防抖 2s + 触发间隙），
        # 3s 窗口拦不住 → 放宽到 10s 覆盖组件重载间隔，同插件+组件 10s 内重复请求只执行一次
        _DEDUP_INTERVAL = 10.0

        def _is_duplicate(plugin_name: str, component: str) -> bool:
            now = time.time()
            key = (plugin_name, component)
            last = _dedup_cache.get(key, 0.0)
            if now - last < _DEDUP_INTERVAL:
                return True
            _dedup_cache[key] = now
            return False

        # 用可变容器包装 plugin_prefixes，闭包内可更新
        _prefixes_ref = [plugin_prefixes]
        # 保存引用给主线程的 _on_hot_reload_requested，在重载完成后重建索引
        self._watcher_prefixes_ref = _prefixes_ref
        # 保存 dedup 缓存引用给主线程：插件删除/清理路径需清空对应键，
        # 防止"删除 → 3s 内重装"被 _is_duplicate 误吞（review B3）
        self._watcher_dedup_cache = _dedup_cache

        def _rebuild_prefixes():
            """重建插件路径索引（在 watch 线程中调用）"""
            _prefixes_ref[0] = self._build_plugin_path_index()

        def _try_identify_new_plugins(changes) -> set:
            """直接扫描变更路径的父目录链，检测所有新增插件的首次变更

            当 _identify_plugin_from_changes 返回 None 时调用此方法，
            作为 fallback 直接从文件系统查找插件清单。

            修复：原 _try_identify_new_plugin 找到第一个插件就 return，
            导致一次性复制多个新插件时只检测到 1 个。现改为返回所有新插件名集合。
            """
            import json as _json
            from pathlib import Path as _Path

            found: set = set()
            for _, change_path in changes:
                p = _Path(change_path)
                # 遍历变更路径及其所有父目录
                for parent in [p] + list(p.parents):
                    if not parent.exists() or not parent.is_dir():
                        continue
                    # 检查 .drifox-plugin 格式
                    manifest = parent / ".drifox-plugin" / "plugin.json"
                    if manifest.exists():
                        try:
                            data = _json.loads(manifest.read_text(encoding="utf-8"))
                            found.add(data.get("name", parent.name))
                        except Exception:
                            found.add(parent.name)
                        break  # 跳出父目录链，处理下一个变更路径
                    # 检查 .claude-plugin 格式
                    manifest = parent / ".claude-plugin" / "plugin.json"
                    if manifest.exists():
                        try:
                            data = _json.loads(manifest.read_text(encoding="utf-8"))
                            found.add(data.get("name", parent.name))
                        except Exception:
                            found.add(parent.name)
                        break  # 跳出父目录链，处理下一个变更路径
            return found

        def _watch_loop():
            """后台线程: 监听插件目录文件变更，识别所属插件后请求主线程增量重载

            stop_event 被设置时 watch() 生成器正常退出（cleanup 归零引用后触发），
            线程随之结束，闭包对 self（ChatBackend 实例）的引用被释放。
            """
            logger.debug("[ChatBackend] watchfiles 监听线程已启动")
            # 跟踪抑制窗口状态，用于“退出抑制时消费 pending 触发一次兜底重载”
            _prev_suppressed = False
            try:
                for changes in watch(
                    *watch_paths,
                    recursive=True,
                    debounce=2000,  # 2秒防抖
                    yield_on_timeout=False,
                    stop_event=ChatBackend._plugin_watcher_stop,
                    watch_filter=watch_filter,  # 排除 deps/node_modules 等易变目录
                ):
                    # changes: set of (Change, Path)
                    if not changes:
                        continue
                    # 过滤掉 .git/ __pycache__/ .pyc 等无关变更
                    relevant_changes = []
                    for change_type, change_path in changes:
                        p = change_path.lower()
                        # 跳过 git/__pycache__/pyc 等无关文件
                        if ".git" in p or "__pycache__" in p or p.endswith(".pyc"):
                            continue
                        # 目录的 Change.modified 是子项变更的副作用（如 __pycache__ 创建/删除导致
                        # 父目录 ui/ 被标记为 modified），实际变更已被子项事件或 DefaultFilter 捕获，
                        # 过滤掉避免误触发跨插件重载。
                        if change_type == 2:  # Change.modified
                            # 以分隔符结尾 or 不含扩展名 → 疑似目录
                            if p.endswith(("\\", "/")) or "." not in p.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]:
                                continue
                        # 跳过用户自定义目录中的内部数据文件（避免自我触发）
                        # 这些是 hook 持久化的数据文件，不是插件源码，修改它们不需要触发插件热更新
                        if "user-custom" in p:
                            pname = change_path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
                            if pname in ("hooks_overrides.json", "hooks.json", "hook_states.json"):
                                continue
                        relevant_changes.append((change_type, change_path))

                    if not relevant_changes:
                        continue

                    # 🚀 P5e：外部批量变更期间（gitee 同步解压 user-custom、或插件市场
                    # installer 落盘 plugins/ 目录）抑制 watcher 热重载链。窗口内所有
                    # 变更先标记 pending 并跳过 emit，避免 watcher 链（rescan + agents
                    # 重载 + reload_all_commands ~2500ms + LSP 子进程 + plugin_changed
                    # 广播）与「clone/解压期间成百上千文件涌入」同时爆发叠加阻塞主线程；
                    # 也避免半安装插件被提前 import 报错。窗口结束后由调用方
                    # （config_sync 下载完成 / installer 安装完成）主动触发一次
                    # reload_plugin_subsystems 兜底加载，pending 事件不丢失。
                    if time.time() < ChatBackend._suppress_watcher_until:
                        ChatBackend._watcher_pending_reload = True
                        logger.info(
                            f"[ChatBackend] 抑制窗口内收到 {len(relevant_changes)} 处变更，标记 pending 待合并重载"
                        )
                        _prev_suppressed = True
                        continue

                    # 刚退出抑制窗口：若窗口内累积了 pending 变更，触发一次兜底重载
                    # （经 _on_hot_reload_requested 去抖合并，仅 reload 一次），
                    # 使被抑制的变更不丢失，且避免窗口结束后每批真实 emit 形成风暴。
                    if _prev_suppressed and ChatBackend._watcher_pending_reload:
                        ChatBackend._watcher_pending_reload = False
                        logger.info("[ChatBackend] 抑制窗口结束，合并触发一次兜底重载（消费 pending）")
                        self._hot_reload_requested.emit("", "")  # 空名 → reload_plugin_subsystems
                    _prev_suppressed = False

                    current_prefixes = _prefixes_ref[0]

                    # 识别变更所属插件
                    plugin_name = self._identify_plugin_from_changes(relevant_changes, current_prefixes)

                    if plugin_name == "__ALL__":
                        # 跨插件变更：逐一识别受影响的插件，各自走增量重载路径
                        affected_plugins = self._identify_all_affected_plugins(relevant_changes, current_prefixes)
                        logger.info(
                            f"[ChatBackend] 跨插件文件变更 ({len(relevant_changes)} 处，"
                            f"涉及 {len(affected_plugins)} 个插件: {', '.join(sorted(affected_plugins))})，"
                            f"逐一增量重载..."
                        )
                        for pname in affected_plugins:
                            all_components = self._identify_all_components_from_changes(
                                relevant_changes, current_prefixes, pname
                            )
                            if all_components:
                                ordered = sorted(
                                    all_components,
                                    key=lambda c: self._COMPONENT_ORDER.get(c, 99),
                                )
                                for component in ordered:
                                    if _is_duplicate(pname, component):
                                        continue
                                    logger.info(
                                        f"[ChatBackend] 插件 [{pname}] ({component}) "
                                        f"跨插件文件变更，请求主线程增量重载..."
                                    )
                                    self._hot_reload_requested.emit(pname, component)
                            else:
                                # 变更不在已知组件目录中（如 data/ 等非相关目录），跳过不触发重载
                                # 特殊 case：插件根目录被删除（整个插件被移出），此时 path 精确等于
                                # plugin_path，被 _identify_all_components_from_changes 跳过（continue），
                                # 导致 all_components 为空。需要在此处兜底检测并触发全组件卸载。
                                _root_deleted = any(
                                    ct == 3 and cp.lower() == path
                                    for path, name in current_prefixes.items()
                                    if name == pname
                                    for ct, cp in relevant_changes
                                )
                                if _root_deleted:
                                    logger.info(
                                        f"[ChatBackend] 插件 [{pname}] 目录已被删除，跨插件变更中触发全组件卸载..."
                                    )
                                    self._hot_reload_requested.emit(pname, "")
                                else:
                                    logger.debug(
                                        f"[ChatBackend] 插件 [{pname}] 跨插件文件变更不涉及已知组件，"
                                        f"跳过重载: {relevant_changes[0][1]}"
                                    )
                    elif plugin_name:
                        # 识别变更所属组件（agents/hooks/commands/themes/skills/mcp/lsp/ui）
                        # 多组件批处理：一次 watchfiles batch 中可能同时修改多个组件目录
                        # 原代码用 _identify_component_from_changes 只返回第一个组件，导致
                        # 多组件变更时只有第一个被处理，其他被静默忽略 → UI 不更新。
                        # 现改为识别所有涉及的组件，按优先级顺序逐个 emit。
                        all_components = self._identify_all_components_from_changes(
                            relevant_changes, current_prefixes, plugin_name
                        )
                        if all_components:
                            # 按优先级排序（agents 先于 commands 先于 skills 等）
                            ordered = sorted(all_components, key=lambda c: self._COMPONENT_ORDER.get(c, 99))
                            for component in ordered:
                                detail = f" ({component})"
                                # 去重：同一插件+组件短时间内重复触发则跳过
                                if _is_duplicate(plugin_name, component):
                                    logger.debug(f"[ChatBackend] 插件 [{plugin_name}]{detail} 文件变更，去重跳过...")
                                    continue
                                logger.info(
                                    f"[ChatBackend] 插件 [{plugin_name}]{detail} "
                                    f"文件变更 ({len(relevant_changes)} 处)，请求主线程增量重载..."
                                )
                                self._hot_reload_requested.emit(plugin_name, component)
                        else:
                            # 变更不在已知组件目录中（如 data/ 等非相关目录），跳过不触发重载
                            # 特殊 case：插件根目录被删除（整个插件被移出），此时 path 精确等于
                            # plugin_path，被 _identify_all_components_from_changes 跳过（continue），
                            # 导致 all_components 为空。需要在此处兜底检测并触发全组件卸载。
                            _root_deleted = any(
                                ct == 3 and cp.lower() == path
                                for path, name in current_prefixes.items()
                                if name == plugin_name
                                for ct, cp in relevant_changes
                            )
                            if _root_deleted:
                                logger.info(f"[ChatBackend] 插件 [{plugin_name}] 目录已被删除，触发全组件卸载...")
                                self._hot_reload_requested.emit(plugin_name, "")
                            else:
                                logger.debug(
                                    f"[ChatBackend] 插件 [{plugin_name}] 文件变更不涉及已知组件，"
                                    f"跳过重载: {relevant_changes[0][1]}"
                                )
                    else:
                        # 无法通过路径索引识别：尝试直接从文件系统检测新插件
                        new_names = _try_identify_new_plugins(relevant_changes)
                        if new_names:
                            logger.info(
                                f"[ChatBackend] 检测到 {len(new_names)} 个新插件文件变更"
                                f"「{', '.join(sorted(new_names))}」，逐一请求增量重载..."
                            )
                            from app.plugins.managers.plugin_manager import PluginManager as _PM

                            for new_name in sorted(new_names):
                                # 已注册插件（索引未及时重建导致路径识别失败）：
                                # 不再走 __NEW__ 全量路径，改按组件增量重载，
                                # 避免 bridge.json 等运行时文件变更反复触发全量加载
                                if _PM.get_instance().has_plugin(new_name):
                                    # 路径索引过期时，使用插件实际路径识别变更组件，
                                    # 避免空组件导致 _reload_single_plugin 跳过所有子系统重载
                                    _identified = self._identify_components_from_changes_fallback(
                                        new_name, relevant_changes
                                    )
                                    if _identified:
                                        for _comp in _identified:
                                            if _is_duplicate(new_name, _comp):
                                                continue
                                            logger.info(
                                                f"[ChatBackend] 插件 [{new_name}] ({_comp}) "
                                                f"文件变更（索引未更新），请求主线程增量重载..."
                                            )
                                            self._hot_reload_requested.emit(new_name, _comp)
                                        continue
                                    # fallback 未识别到组件变更（仅根目录文件变更）→ 空组件重载（跳过子系统）
                                    logger.debug(
                                        f"[ChatBackend] 插件 [{new_name}] fallback 未识别到组件变更 "
                                        f"({len(relevant_changes)} 处)，按已知插件增量重载（跳过子系统）..."
                                    )
                                    logger.info(
                                        f"[ChatBackend] 插件 [{new_name}] 已注册（索引未更新），改按已知插件增量重载..."
                                    )
                                    self._hot_reload_requested.emit(new_name, "")
                                    continue
                                # 未注册：先 rescan_plugin 复查——对目录存在 + plugin.json
                                # 有效但注册表缺失的插件（如 user-custom），rescan 会直接注册成功，
                                # 此时应走已知插件增量路径，避免误判为「新插件」触发 __NEW__ 全量加载
                                _PM.get_instance().rescan_plugin(new_name)
                                if _PM.get_instance().has_plugin(new_name):
                                    # 全新安装/重装判定：本批变更含"新增"(Changed.added==1)事件
                                    # 落在插件根目录或其下（根目录/组件目录被重建），说明插件此前
                                    # 未注册且组件从未加载 → 走 __NEW__ 全组件加载，避免空组件
                                    # 跳过导致 new_build 组件永不生效（卸载后重装必触发此分支）。
                                    # 仅运行时数据文件变更（如 bridge.json 等 Modified/已存在目录）
                                    # 不算新增，继续走已知插件增量/跳过，避免全量加载。
                                    _plugin = _PM.get_instance().get_plugin(new_name)
                                    _root_path = str(_plugin.path.resolve()).lower().rstrip("\\/")
                                    _is_fresh_install = any(
                                        ct == 1
                                        and (cp.lower() == _root_path or cp.lower().startswith(_root_path + os.sep))
                                        for ct, cp in relevant_changes
                                    )
                                    if _is_fresh_install:
                                        logger.info(
                                            f"[ChatBackend] 插件 [{new_name}] 检测到新增事件，"
                                            f"判定为全新安装，请求 __NEW__ 全组件加载..."
                                        )
                                        # 预填充 dedup cache，防止路径索引重建后同一批
                                        # watch 事件的剩余部分以已知插件路径再次触发
                                        _dedup_cache[(new_name, "")] = time.time() + _DEDUP_INTERVAL
                                        self._hot_reload_requested.emit(self._NEW_PLUGIN_SENTINEL, new_name)
                                        continue
                                    # 非全新安装：使用插件实际路径识别变更组件
                                    _identified = self._identify_components_from_changes_fallback(
                                        new_name, relevant_changes
                                    )
                                    if _identified:
                                        for _comp in _identified:
                                            if _is_duplicate(new_name, _comp):
                                                continue
                                            logger.info(
                                                f"[ChatBackend] 插件 [{new_name}] ({_comp}) "
                                                f"rescan 后文件变更，请求主线程增量重载..."
                                            )
                                            self._hot_reload_requested.emit(new_name, _comp)
                                        continue
                                    # fallback 未识别到组件变更 → 空组件重载（跳过子系统）
                                    logger.debug(
                                        f"[ChatBackend] 插件 [{new_name}] rescan 后 fallback 未识别到组件变更 "
                                        f"({len(relevant_changes)} 处)，按已知插件增量重载（跳过子系统）..."
                                    )
                                    logger.info(
                                        f"[ChatBackend] 插件 [{new_name}] rescan 后已注册，改按已知插件增量重载..."
                                    )
                                    self._hot_reload_requested.emit(new_name, "")
                                    continue
                                # 预填充 dedup cache，防止路径索引重建后同一批 watch 事件
                                # 的剩余部分以已知插件路径再次触发（ghost trigger）
                                _dedup_cache[(new_name, "")] = time.time() + _DEDUP_INTERVAL
                                # 发射新插件标记，走 _reload_new_plugin 增量路径
                                # 只扫描这一个插件目录，不触发全量 rescan
                                self._hot_reload_requested.emit(self._NEW_PLUGIN_SENTINEL, new_name)
                        else:
                            # 无法识别的新增文件变更（如编辑器临时文件、git 残留等）
                            # 跳过不处理，等下次事件重试。不触发全量重扫
                            logger.debug(f"[ChatBackend] 文件变更无法识别所属插件，跳过: {relevant_changes[0][1]}")
            except Exception as e:
                logger.error(f"[ChatBackend] watchfiles 监听异常退出: {e}")

        import threading as _threading

        t = _threading.Thread(target=_watch_loop, daemon=True, name="plugin-watcher")
        ChatBackend._plugin_watcher_thread = t
        t.start()

    def _stop_plugin_watcher(self):
        """backend 关闭时递减 watcher 引用计数；归零时停止 watchfiles 线程。

        泄漏修复（P1）：watcher 闭包持有启动它的第一个 backend 实例引用
        （self._hot_reload_requested / self.plugin_changed / self._identify_*），
        若窗口关闭而线程不退出，该实例（及其整棵窗口对象树）永远无法被 GC。

        - refcount > 0：仍有活跃窗口，维持 watcher（热更新继续工作）
        - refcount == 0：设置 stop_event → watch() 生成器退出 → join 等待线程结束
          → 闭包释放 → 首个 backend 实例可回收；同时复位标志，允许新窗口
          重新启动 watcher（stop_event 在 _start_plugin_watcher 中重建），热更新不丢失。
        """
        ChatBackend._plugin_watcher_refcount = max(0, ChatBackend._plugin_watcher_refcount - 1)
        if ChatBackend._plugin_watcher_refcount > 0:
            return
        stop = ChatBackend._plugin_watcher_stop
        if stop is not None:
            stop.set()
        t = ChatBackend._plugin_watcher_thread
        if t is not None and t.is_alive():
            try:
                t.join(timeout=2.0)
            except Exception:
                pass
        ChatBackend._plugin_watcher_thread = None
        ChatBackend._plugin_watcher_started = False

    def _build_plugin_path_index(self) -> Dict[str, str]:
        """构建插件路径前缀 → 插件名的映射表

        前缀不带尾部分隔符，匹配时同时支持目录本身和目录内文件。
        Returns:
            {小写路径: 插件名}
        """
        from app.plugins.managers.plugin_manager import PluginManager

        pm = PluginManager.get_instance()
        prefixes = {}
        for plugin in pm.list_plugins():
            path = str(plugin.path.resolve()).lower().rstrip("\\/")
            prefixes[path] = plugin.name
        return prefixes

    def _identify_plugin_from_changes(self, changes: list, plugin_prefixes: Dict[str, str]) -> Optional[str]:
        """从变更文件路径识别所属插件名称

        Args:
            changes: [(Change, path_str), ...]
            plugin_prefixes: 插件根路径 → 插件名 映射

        Returns:
            - 插件名: 单一插件变更，可增量重载
            - "__ALL__": 跨插件变更，需要全量重载
            - None: 无法识别（不属于任何已知插件），跳过
        """
        # 按路径长度降序排列（优先精确匹配）
        sorted_prefixes = sorted(plugin_prefixes.keys(), key=len, reverse=True)

        found = set()
        for _, change_path in changes:
            cp = change_path.lower()
            for prefix in sorted_prefixes:
                # 精确匹配目录本身 或 匹配目录内文件
                if cp == prefix or cp.startswith(prefix + os.sep):
                    found.add(plugin_prefixes[prefix])
                    break

        if not found:
            return None

        if len(found) == 1:
            return next(iter(found))

        # 跨插件变更，需要全量重载
        logger.debug(f"[ChatBackend] 跨插件变更: {found}，触发全量重载")
        return "__ALL__"

    def _identify_all_affected_plugins(self, changes: list, plugin_prefixes: Dict[str, str]) -> set:
        """从变更文件路径识别所有涉及的插件名集合

        与 _identify_plugin_from_changes 共享路径匹配逻辑，但返回完整集合
        而非在跨插件时返回 __ALL__。用于 watch_loop 在跨插件变更时
        逐个插件增量重载。

        Args:
            changes: [(Change, path_str), ...]
            plugin_prefixes: 插件路径 → 插件名 映射

        Returns:
            set[str]: 受影响的所有插件名集合
        """
        sorted_prefixes = sorted(plugin_prefixes.keys(), key=len, reverse=True)
        found: set = set()
        for _, change_path in changes:
            cp = change_path.lower()
            for prefix in sorted_prefixes:
                if cp == prefix or cp.startswith(prefix + os.sep):
                    found.add(plugin_prefixes[prefix])
                    break
        return found

    def _identify_component_from_changes(self, changes: list, plugin_prefixes: Dict[str, str], plugin_name: str) -> str:
        """从变更文件路径识别所属组件子目录

        Args:
            changes: [(Change, path_str), ...]
            plugin_prefixes: 插件路径 → 插件名 映射
            plugin_name: 已识别出的插件名

        Returns:
            "agents" | "hooks" | "commands" | "themes" | "skills" | "mcp" | "lsp" | "ui"
            | "" (根目录/无法确定)

        注意：watchfiles 的 2 秒防抖会把同一批变更聚合。一次 batch 中可能同时
        修改多个组件目录下的文件（如同时编辑 commands/ 和 skills/）。
        本方法只返回第一个匹配的组件，多组件场景请使用
        `_identify_all_components_from_changes()` 并配合 watch_loop 多次 emit。
        """
        components = self._identify_all_components_from_changes(changes, plugin_prefixes, plugin_name)
        if not components:
            return ""
        # 优先返回优先级最高的组件（与原行为兼容：先匹配的先返回）
        return sorted(components, key=lambda c: self._COMPONENT_ORDER.get(c, 99))[0]

    # 组件优先级（用于在多组件批处理中决定先后顺序）
    # agents 最先：它会影响 commands 和 hooks 同步
    _COMPONENT_ORDER = {
        "agents": 0,
        "hooks": 1,
        "commands": 2,
        "themes": 3,
        "skills": 4,
        "mcp": 5,
        "lsp": 6,
        "ui": 7,
    }

    def _identify_all_components_from_changes(
        self, changes: list, plugin_prefixes: Dict[str, str], plugin_name: str
    ) -> set:
        """从变更文件路径识别所有涉及的组件子目录（多组件批处理）

        一次 watchfiles batch 中可能同时修改多个组件目录下的文件。
        原 _identify_component_from_changes 只返回第一个组件，导致多组件
        同时变更时只有一个被处理，其他被静默忽略，UI 不刷新。
        本方法返回所有涉及的组件，让 watch_loop 拆分多次 emit。

        Args:
            changes: [(Change, path_str), ...]
            plugin_prefixes: 插件路径 → 插件名 映射
            plugin_name: 已识别出的插件名

        Returns:
            set[str]: 涉及的所有组件名；空 set 表示根目录/无法识别
        """
        # 找到该插件的路径前缀
        plugin_path = None
        for path, name in plugin_prefixes.items():
            if name == plugin_name:
                plugin_path = path
                break
        if not plugin_path:
            return set()

        from app.plugins.kernel import KNOWN_COMPONENTS, ROOT_FILE_COMPONENTS

        components: set = set()
        for _, change_path in changes:
            cp = change_path.lower()
            if cp == plugin_path:
                continue  # 插件根目录本身变更，留给后续逻辑判断
            if cp.startswith(plugin_path + os.sep):
                rel = cp[len(plugin_path) + 1 :]  # 去掉 "plugin_path\"
                first_seg = rel.split(os.sep)[0] if os.sep in rel else rel
                if first_seg in KNOWN_COMPONENTS:
                    components.add(first_seg)
                    continue
                # 根目录的关键文件（如 .mcp.json）映射到对应组件
                if first_seg in ROOT_FILE_COMPONENTS:
                    components.add(ROOT_FILE_COMPONENTS[first_seg])
        return components

    def _identify_components_from_changes_fallback(self, plugin_name: str, changes: list) -> list:
        """从变更文件路径识别涉及的组件（使用插件实际路径，不依赖路径索引）

        当路径索引过期（新安装/更新插件后索引尚未重建）时，
        _identify_all_components_from_changes 无法找到插件路径，
        导致返回空 set，最终空组件 emit 使 _reload_single_plugin 跳过所有重载。
        本方法直接从 pm.get_plugin() 获取插件实际路径，绕过过期索引。

        Args:
            plugin_name: 已识别出的插件名
            changes: [(Change, path_str), ...]

        Returns:
            list[str]: 按优先级排序的组件名列表；空列表表示无组件变更
        """
        from app.plugins.managers.plugin_manager import PluginManager

        pm = PluginManager.get_instance()
        plugin = pm.get_plugin(plugin_name)
        if not plugin:
            return []

        plugin_path = str(plugin.path.resolve()).lower().rstrip("\\/")
        from app.plugins.kernel import KNOWN_COMPONENTS, ROOT_FILE_COMPONENTS

        components: set = set()
        for _, change_path in changes:
            cp = change_path.lower()
            if cp == plugin_path:
                continue
            if cp.startswith(plugin_path + os.sep):
                rel = cp[len(plugin_path) + 1 :]
                first_seg = rel.split(os.sep)[0] if os.sep in rel else rel
                if first_seg in KNOWN_COMPONENTS:
                    components.add(first_seg)
                    continue
                if first_seg in ROOT_FILE_COMPONENTS:
                    components.add(ROOT_FILE_COMPONENTS[first_seg])

        if not components:
            return []
        return sorted(components, key=lambda c: self._COMPONENT_ORDER.get(c, 99))

    def _on_hot_reload_requested(self, plugin_name: str, component: str):
        """主线程中执行的插件热更新

        单次请求同步执行（保持原有增量语义与单元测试同步断言）；300ms 内的
        重复 / 风暴请求走去抖合并：首次立即执行，窗口内的后续请求不再逐个触发
        主线程重载（reload_agents / reload_all_commands ~2500ms），改为计划一次
        合并的 reload_plugin_subsystems，避免“文件多”的插件在批量写入 / clone 时
        每批都重载阻塞 UI。

        Args:
            plugin_name: 插件名
                - "" (空) → 全量重载（走 reload_plugin_subsystems）
                - _NEW_PLUGIN_SENTINEL → 新增插件（component 参数存储插件名）
                - 其他 → 已知插件的增量重载（component 为具体组件名或 "" 表示根目录变更）
            component: 组件名（"" 表示该插件的全部组件，否则为 agents/hooks/commands/themes/skills/mcp/lsp）
        """
        now = time.time()
        last = getattr(self, "_last_reload_at", 0.0)
        if now - last < 0.3:
            # 去抖窗口内：不立即重载，改为计划一次合并兜底（仅执行一次）
            self._schedule_debounced_reload()
            return
        self._last_reload_at = now
        try:
            result = self._do_single_reload(plugin_name, component)
            self.emit_plugin_changed(result, plugin_name)
        except Exception as e:
            logger.error(f"[ChatBackend] 插件热更新失败: {e}")
        finally:
            # 重载完成后重建 watchfiles 路径索引（finally 保证异常路径也更新，
            # 否则已注册插件会被反复识别为"新插件"，触发 bridge.json 自触发循环）
            self._rebuild_watcher_prefixes()

    def emit_plugin_changed(self, result: dict, plugin_name: str = "") -> None:
        """广播插件变更结果到全部活跃 backend 的 plugin_changed 信号

        附加事件标识（仅广播用，不污染业务 result 消费方）：
        - _event_seq：实例级递增序号——同一事件广播到多 backend/多窗口时
          指纹一致可去重；不同事件即使 result 相同（如 10s 内连续热重载
          两个插件均为 {ui: True}）也不会被窗口级指纹短窗误吞。
        - _plugin_name：本次变更的插件名（"" = 全量/合并路径），UI 据此
          精准重绘该插件已挂载的视图（消息内容块 / 浮动卡片 / 欢迎卡片）。
        """
        seq = getattr(self, "_hot_reload_seq", 0) + 1
        self._hot_reload_seq = seq
        annotated = dict(result)
        annotated["_event_seq"] = seq
        annotated["_plugin_name"] = plugin_name
        self.plugin_changed.emit(annotated)
        # ★ T3 修复：广播到所有活跃 backend 的 plugin_changed。
        # watcher 由首个 backend 驱动，_on_hot_reload_requested 只在该实例的
        # 槽上执行；若仅 emit 宿主实例的信号，宿主窗口关闭（信号断开）后其他
        # 窗口的 UI 收不到刷新通知（热加载数据成功但列表不刷新）。广播后
        # 每个活跃窗口的 backend 都通知自己的 UI 刷新。
        for _b in list(ChatBackend._active_instances):
            if _b is not self:
                # 窗口关闭竞态防护：backend 已 deleteLater 但未 cleanup 时
                # emit 可能触发 RuntimeError，跳过该实例不影响正常广播
                try:
                    _b.plugin_changed.emit(annotated)
                except RuntimeError:
                    pass

    def _schedule_debounced_reload(self):
        """风暴期合并重载：300ms 后执行一次全量 reload_plugin_subsystems（仅触发一次）"""
        if getattr(self, "_reload_timer", None) is None:
            self._reload_timer = QTimer()
            self._reload_timer.setSingleShot(True)
            self._reload_timer.timeout.connect(self._do_debounced_reload)
        if not self._reload_timer.isActive():
            self._reload_timer.start(300)

    def _do_debounced_reload(self):
        """去抖到期：合并执行一次全量重载（已内部对 added/changed/removed 增量处理）"""
        self._last_reload_at = time.time()
        try:
            result = self.reload_plugin_subsystems()
            self.emit_plugin_changed(result)
        except Exception as e:
            logger.error(f"[ChatBackend] 插件热更新失败: {e}")
        finally:
            self._rebuild_watcher_prefixes()

    def _do_single_reload(self, plugin_name: str, component: str) -> dict:
        """执行单条增量重载（不含 emit / 广播 / 索引重建，由 _flush_reload_intents 统一处理）"""
        if plugin_name == self._NEW_PLUGIN_SENTINEL:
            # 新增插件：只扫描这一个插件目录，增量加载其组件。
            # 不再"已注册则降级为空组件跳过"——watch 线程可能在 emit 前
            # 对全新安装的插件做过 rescan 注册（组件尚未加载），此时
            # 降级为空组件（""）会全 False 跳过，导致重装后的插件组件
            # 永不生效。_reload_new_plugin 对已注册插件幂等（组件错重载，
            # UI/LSP 先卸载后加载），可直接复用。
            return self._reload_new_plugin(component)
        elif plugin_name:
            return self._reload_single_plugin(plugin_name, component)
        else:
            return self.reload_plugin_subsystems()

    def _rebuild_watcher_prefixes(self):
        """重建 watchfiles 线程的插件路径索引（主线程调用）"""
        prefixes_ref = getattr(self, "_watcher_prefixes_ref", None)
        if prefixes_ref is not None:
            prefixes_ref[0] = self._build_plugin_path_index()

    def _reload_new_plugin(self, plugin_name: str) -> dict:
        """增量加载新增插件的所有组件，不重启已有子系统

        与 _reload_single_plugin 的区别：
        - 由 _watch_loop 检测到全新插件时调用（emit "__NEW__"）
        - 只扫描这一个插件目录（避免全量 rescan）
        - 只注册/启动该插件新增的 LSP 服务器（不碰已有的）
        - 不触发全量 rescan 也就不触发全量 reload_plugin_subsystems

        Args:
            plugin_name: 新增插件名

        Returns:
            dict: 各组件重载结果；key 集合基于 kernel.KNOWN_COMPONENTS 动态生成，
            agents → int（数量），其余 → bool。新增组件类型时无需改此处。
        """
        from app.plugins.kernel import KNOWN_COMPONENTS as _KC

        # result 基于 KNOWN_COMPONENTS 动态生成：新增组件类型零改动（Task 7）
        result: dict = {k: (0 if k == "agents" else False) for k in _KC}

        try:
            from app.plugins.managers.plugin_manager import PluginManager

            pm = PluginManager.get_instance()
            if not pm.is_initialized():
                logger.warning("[ChatBackend] PluginManager not initialized, cannot reload")
                return result

            # 1. 只重新扫描这一个插件目录（不走全量 rescan）
            pm.rescan_plugin(plugin_name)

            plugin = pm.get_plugin(plugin_name)
            if not plugin:
                logger.warning(f"[ChatBackend] New plugin '{plugin_name}' not found after scan")
                return result

            comps = plugin.components
            logger.info(f"[ChatBackend] 检测到新插件「{plugin_name}」，执行增量加载")

            # 2. 智能体 + hooks
            if comps.get("agents") and self._agent_manager:
                result["agents"] = self._agent_manager.reload_plugin_agents(plugin_name)
                result["hooks"] = True  # agents 组件包含 hooks 重载
                try:
                    from app.core.builtin_commands import reload_agent_commands

                    reload_agent_commands()
                    result["commands"] = True
                except (ImportError, Exception) as e:
                    logger.error(f"[ChatBackend] Failed to reload commands after agent change: {e}")

            if comps.get("hooks") and not comps.get("agents") and self._agent_manager:
                self._agent_manager.reload_plugin_hooks(plugin_name)
                result["hooks"] = True

            # 3. 命令
            if comps.get("commands") and not result["commands"]:
                try:
                    from app.core.builtin_commands import reload_all_commands

                    reload_all_commands()
                    result["commands"] = True
                except (ImportError, Exception) as e:
                    logger.error(f"[ChatBackend] Failed to reload commands: {e}")

            # 4. 主题
            if comps.get("themes"):
                try:
                    from app.utils.config import update_theme_options
                    from app.utils.theme_manager import theme_manager

                    theme_manager.reload()
                    update_theme_options()
                    result["themes"] = True
                except (ImportError, Exception) as e:
                    logger.error(f"[ChatBackend] Failed to reload themes: {e}")

            # 5. 技能 / MCP：懒加载，只需标记
            if comps.get("skills"):
                invalidate_skills_cache()
            result["skills"] = bool(comps.get("skills"))
            result["mcp"] = bool(comps.get("mcp"))

            # 6. LSP：先移除旧服务再注册新服务（幂等，避免对已注册插件重复加载）
            if comps.get("lsp"):
                try:
                    from app.core.lsp.lsp_manager import get_lsp_manager

                    lsp_mgr = get_lsp_manager()
                    # 先移除该插件已有的 LSP 服务器（若此前已加载），再注册新配置
                    lsp_mgr.remove_plugin_servers(plugin_name)
                    lsp_config = pm.get_plugin_lsp_config(plugin_name)
                    if lsp_config:
                        count = lsp_mgr.add_plugin_servers(plugin_name, lsp_config["config"])
                        result["lsp"] = count > 0
                    logger.info(f"[ChatBackend] Plugin '{plugin_name}' LSP 增量加载完成")
                except Exception as e:
                    logger.error(f"[ChatBackend] Plugin '{plugin_name}' LSP 增量加载失败: {e}")

            # 7. UI 组件：增量加载，不重复加载已存在的插件
            if comps.get("ui"):
                try:
                    from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

                    UIPluginRegistry.get_instance().load_plugin(plugin_name, plugin.path)
                    result["ui"] = True
                    logger.info(f"[ChatBackend] Plugin '{plugin_name}' UI 组件已加载")
                except Exception as e:
                    logger.error(f"[ChatBackend] Plugin '{plugin_name}' UI 加载失败: {e}")

            # 8. 其余组件：与 builtin_reloaders 同构的 kernel 分派
            # 走内核注册表而非硬编码 if，保持单源真理（新增组件类型零改动）。
            # 遍历 COMPONENT_ORDER（排除上方 1-7 已手工处理的组件），覆盖
            # tools/providers/team_templates/model_adapters/loop_policies/
            # storages/serializers/gateways 全部 registry 分派组件——
            # 历史 bug：此处曾硬编码 3 项漏掉 gateways，卸载重装（__NEW__ 路径）
            # 后 gateway 平台 def 不注册/adapter 不建/连接不启 → 机器人无响应。
            from app.plugins.builtin_reloaders import bind_runtime, register_builtin_reloaders
            from app.plugins.kernel import COMPONENT_ORDER, ReloadContext, get_reloader_registry

            bind_runtime(self._agent_manager)
            registry = get_reloader_registry()
            register_builtin_reloaders(registry)  # 幂等
            _MANUAL_STEPS = {"agents", "hooks", "commands", "themes", "skills", "mcp", "lsp", "ui"}
            for comp in COMPONENT_ORDER:
                if comp in _MANUAL_STEPS or not comps.get(comp):
                    continue
                reloaded = registry.reload(
                    ReloadContext(
                        plugin_name=plugin_name,
                        plugin=plugin,
                        component=comp,
                        is_new_plugin=True,
                    )
                )
                result[comp] = reloaded if reloaded is not None else False

            logger.info(
                f"[ChatBackend] 新插件增量加载「{plugin_name}」完成: "
                f"agents={result['agents']}, commands={result['commands']}, "
                f"themes={result['themes']}, skills={result['skills']}, "
                f"mcp={result['mcp']}, lsp={result['lsp']}, ui={result['ui']}, "
                f"tools={result['tools']}, providers={result['providers']}, "
                f"team_templates={result['team_templates']}"
            )
        except Exception as e:
            logger.error(f"[ChatBackend] Failed to reload new plugin '{plugin_name}': {e}")

        return result

    def _cleanup_removed_plugin_components(
        self,
        plugin_name: str,
        removed_components: dict,
        result: dict,
        result_keys: tuple,
    ) -> dict:
        """精准清理已移除插件的全部组件（删除段核心，供两处复用）

        - `_reload_single_plugin` 删除段（rescan 前捕获的 removed_components）
        - `reload_plugin_subsystems` diff 的 removed 分支（rescan 后插件已不在索引，
          components 取自 rescan 返回的 Plugin 对象，避免「索引已移除 → 捕获为空 → 卸载不干净」）

        只处理该插件实际含有的组件：agents→cleanup_plugin_artifacts、hooks-only→unregister、
        commands→reload_all、themes→reload、skills→invalidate、ui→unload、lsp→remove_only。
        """
        # 清空该插件的 watcher 去重缓存键（review B3）：
        # 防止"删除 → 3s 内重装"被 _is_duplicate 误判为重复而吞掉重装加载
        dedup_cache = getattr(self, "_watcher_dedup_cache", None)
        if dedup_cache is not None:
            stale_keys = [k for k in dedup_cache.keys() if k[0] == plugin_name]
            for k in stale_keys:
                dedup_cache.pop(k, None)
            if stale_keys:
                logger.debug(f"[ChatBackend] 插件 [{plugin_name}] 移除，清空 {len(stale_keys)} 个 watcher 去重键")

        # 表分派：遍历该插件原有组件 → registry.reload(plugin=None) 走清理语义
        # 遍历序按 COMPONENT_ORDER（tuple）— KNOWN_COMPONENTS 是 set 序不确定，
        # 漏遍历会跳过某组件的清理。agents 置首：先于 hooks/commands 处理以保留其联动标记语义。
        from app.plugins.kernel import COMPONENT_ORDER, ReloadContext, get_reloader_registry

        registry = get_reloader_registry()
        for comp in COMPONENT_ORDER:
            if not removed_components.get(comp):
                continue
            reloaded = registry.reload(
                ReloadContext(
                    plugin_name=plugin_name,
                    plugin=None,
                    component=comp,
                    is_new_plugin=False,
                )
            )
            if comp in result_keys:
                # agents 返回 int(数量)，其余 True/False — agents 删除归零
                result[comp] = reloaded if reloaded is not None else False
            # agents 联动标记：与旧 backend elif 语义一致 — agents 命中后置 commands=True 走 plugin_changed 广播
            # 重建快捷键（agents-only 插件无 commands 组件时也置，触发 _on_plugin_hot_reload 双保险）。
            # hooks 是否置位由下方 _reload_hooks 遍历按 removed_components.get("hooks") 自然决定，
            # 对齐旧代码 `result["hooks"] = removed_components.get("hooks", False)`。
            # 此处显式置 True 是保守保留旧行为：删除段可能由 watchfiles 单点事件触发而非完整
            # 遍历原 components，强制 hooks=True 保证窗口侧 UI 刷新链不漏。
            if comp == "agents":
                result["hooks"] = True
                result["commands"] = True

        logger.info(
            f"[ChatBackend] Plugin '{plugin_name}' cleanup done via kernel: { {k: result[k] for k in result_keys} }"
        )
        return result

    def _reload_single_plugin(self, plugin_name: str, component: str = "") -> dict:
        """增量重载单个插件（不清除其他插件的数据）

        根据变更的组件名精确重载，不触发无关子系统：

        - "agents"   → 重载智能体 + hooks
        - "hooks"    → 仅重载 hooks（不碰智能体）
        - "commands" → 重载命令
        - "themes"   → 重载主题
        - "skills"   → 重载技能（PluginManager 已更新，UI 下次调用 get_local_skills() 自动生效）
        - "mcp"      → 重载 MCP 配置（PluginManager 已更新，UI 下次调用 get_mcp_servers() 自动生效）
        - "lsp"      → 热重载 LSP 配置（使用增量 API：先移除旧服务 → 再注册新服务）
        - "ui"       → 热重载 UI 组件（先卸载后加载，reload_plugin）
        - ""         → 跳过（根目录文件变更如 README/LICENSE，不影响运行时）

        Args:
            plugin_name: 插件名称
            component: 变更的组件名

        Returns:
            dict: 各组件重载结果；key 集合基于 kernel.KNOWN_COMPONENTS 动态生成，
            agents → int（数量），其余 → bool。新增组件类型时无需改此处。
        """
        from app.plugins.kernel import KNOWN_COMPONENTS as _KC

        # 表分派：原 8 分支 if 已在 builtin_reloaders（commit 0e141cd9）— 此处仅查注册表
        # result / result_keys 基于 KNOWN_COMPONENTS 动态生成：新增组件类型零改动（Task 7）
        result: dict = {k: (0 if k == "agents" else False) for k in _KC}
        result_keys = tuple(_KC)

        try:
            from app.plugins.managers.plugin_manager import PluginManager

            pm = PluginManager.get_instance()
            if not pm.is_initialized():
                logger.warning("[ChatBackend] PluginManager not initialized, cannot reload")
                return result

            # 1. 捕获移除前的插件组件信息，用于精确清理
            plugin_before = pm.get_plugin(plugin_name)
            removed_components = dict(plugin_before.components) if plugin_before else {}

            pm.rescan_plugin(plugin_name)

            plugin = pm.get_plugin(plugin_name)
            if not plugin:
                # 插件已被删除（目录或 manifest 已不存在）—— 删除清理段并入 reloader 分派
                # 仅清理该插件实际含有的组件（按 kernel.KNOWN_COMPONENTS 优先级遍历）
                logger.info(
                    f"[ChatBackend] Plugin '{plugin_name}' removed, "
                    f"components={ {k for k, v in removed_components.items() if v} or {'(unknown)'} }, "
                    f"cleaning up artifacts..."
                )
                return self._cleanup_removed_plugin_components(plugin_name, removed_components, result, result_keys)

            # 2-N. 组件分派：查 kernel reloader 注册表（原 8 分支 if 已迁 builtin_reloaders）
            # 注册 / 注入 runtime 句柄由 _do_deferred + reload_plugin_subsystems 集中完成
            # （builtin_reloaders._BUILTIN_REGISTERED 幂等保护，此处不重复）
            from app.plugins.kernel import ReloadContext, get_reloader_registry

            registry = get_reloader_registry()

            if component == "__manifest__":
                # manifest 变更 = 组件清单可能增删，必须全组件重载以重新探测差异
                # rescan 已在函数前置（pm.rescan_plugin(plugin_name)）保证 plugin.components 是最新
                # 遍历按 COMPONENT_ORDER：agents 先 → tools/providers/team_templates 后（保 hooks/commands 联动标记自然置位）
                from app.plugins.kernel import COMPONENT_ORDER

                for comp in COMPONENT_ORDER:
                    if not plugin.has_component(comp):
                        continue
                    reloaded = registry.reload(
                        ReloadContext(
                            plugin_name=plugin_name,
                            plugin=plugin,
                            component=comp,
                            is_new_plugin=False,
                        )
                    )
                    if comp in result_keys:
                        result[comp] = reloaded if reloaded is not None else False
                    # agents 联动标记保持旧行为
                    if comp == "agents":
                        result["hooks"] = True
                        result["commands"] = True
                logger.info(
                    f"[ChatBackend] Plugin '{plugin_name}' manifest changed, reloaded all components: "
                    f"{ {k: result[k] for k in result_keys} }"
                )
            elif component:
                # 统一守卫层：plugin 缺失或无该组件时跳过（对齐原 commands/ui 分支的 has_component 前置）
                if plugin is not None and not plugin.has_component(component):
                    logger.debug(f"[ChatBackend] Plugin '{plugin_name}' has no '{component}' component, skip")
                else:
                    reloaded = registry.reload(
                        ReloadContext(
                            plugin_name=plugin_name,
                            plugin=plugin,
                            component=component,
                            is_new_plugin=False,
                        )
                    )
                    if component in result_keys:
                        result[component] = reloaded if reloaded is not None else False
                    # agents 联动标记保持旧行为：agents 变更 → hooks/commands 视为已处理
                    if component == "agents":
                        result["hooks"] = True
                        result["commands"] = True
                    logger.info(
                        f"[ChatBackend] Plugin [{plugin_name}] reloaded via kernel: "
                        f"component={component}, outcome={reloaded} → {result}"
                    )
            else:
                logger.debug(f"[ChatBackend] Plugin [{plugin_name}] root change, skip component reload")

            logger.info(
                f"[ChatBackend] Plugin [{plugin_name}] reloaded: "
                f"agents={result['agents']}, commands={result['commands']}, "
                f"themes={result['themes']}, skills={result['skills']}, "
                f"mcp={result['mcp']}, lsp={result.get('lsp', False)}, "
                f"ui={result.get('ui', False)}, tools={result.get('tools', False)}, "
                f"providers={result.get('providers', False)}, "
                f"team_templates={result.get('team_templates', False)}"
            )
        except Exception as e:
            logger.error(f"[ChatBackend] Failed to reload plugin '{plugin_name}': {e}")

        return result

    def reload_plugin_targeted(self, plugin_name: str) -> dict:
        """精准重载单个插件（UI 安装/更新/启用/禁用专用），不触发全量子系统重载

        与 reload_plugin_subsystems（全量：rescan + 所有 agents/hooks/commands/
        themes/skills/mcp/lsp/ui 重载）的区别：只处理目标插件，避免
        「卸载一个插件却把全部插件的 hooks 注销重注册、全部 agents 重载」。

        实现：复用 _reload_single_plugin 的 __manifest__ 分派——
        - 插件已不存在（禁用/卸载，目录已移走）→ rescan 后走删除段，仅清理该插件
          原有组件（agents/hooks/commands/lsp/ui/tools 按需精准清理）
        - 插件存在（安装/启用/更新）→ 遍历该插件全部组件精准加载
          （agents/hooks 按插件名精准，仅 commands 因全局注册表需全量重建）

        ★ 重载完成后主动 emit_plugin_changed：本方法由插件市场 Installer 调用
        （安装/更新/启停），不走 watcher 的 _on_hot_reload_requested（那条链路
        自己 emit）——若此处不广播，窗口收不到 ui=True，已打开标签页的输入区
        插件按钮/消息内容块全部不刷新（watcher 抑制解除后的 fallback 事件组件
        归类常为 root → ui=False，顶替不了本事件的刷新语义）。
        """
        if not plugin_name:
            result = self.reload_plugin_subsystems()
        else:
            result = self._reload_single_plugin(plugin_name, "__manifest__")
        try:
            self.emit_plugin_changed(result, plugin_name)
        except Exception as e:
            logger.warning(f"[ChatBackend] reload_plugin_targeted 广播失败: {e}")
        return result

    def reload_plugin_subsystems(self, force_full: bool = False) -> dict:
        """重载插件子系统（默认 diff 精准；force_full=True 走全量）

        默认行为（新增/移除/变更场景，全部调用方自动受益）：
        rescan 对比出 added/removed/changed 插件后**逐个精准处理**——
        - removed → 精准清理该插件实际含有的组件（agents/hooks/commands/lsp/ui/tools）
        - added/changed → 精准重载该插件全部组件（_reload_single_plugin "__manifest__"）
        不触碰无关插件的 agents/hooks/commands，避免「卸载一个插件却把全部插件的
        hooks 注销重注册、全部 agents 重载」的性能浪费；无变更时不重载任何子系统。

        force_full=True：设置面板「重载插件」按钮的显式语义——无论是否有变更，
        全量重载所有子系统（agents/hooks/commands/themes/skills/mcp/lsp/ui）。

        Returns:
            dict: 各子系统的重载结果；key 集合基于 kernel.KNOWN_COMPONENTS 动态生成，
            agents → int（数量），其余 → bool。新增组件类型时无需改此处。
        """
        from app.plugins.kernel import KNOWN_COMPONENTS as _KC

        # result / result_keys 基于 KNOWN_COMPONENTS 动态生成：新增组件类型零改动（Task 7）
        result: dict = {k: (0 if k == "agents" else False) for k in _KC}
        result_keys = tuple(_KC)

        # 表分派：内置 reloader 注册（幂等）+ runtime 句柄注入
        try:
            from app.plugins.builtin_reloaders import bind_runtime, register_builtin_reloaders
            from app.plugins.kernel import get_reloader_registry

            bind_runtime(_safe_agent_manager(self))
            register_builtin_reloaders(get_reloader_registry())
        except Exception as e:
            logger.error(f"[ChatBackend] 内置 reloader 注册失败: {e}")

        try:
            from app.plugins.managers.plugin_manager import PluginManager

            pm = PluginManager.get_instance()
            if not pm.is_initialized():
                logger.warning("[ChatBackend] PluginManager not initialized, cannot reload")
                return result

            # 1. 重新扫描插件目录，获取变更详情
            diff = pm.rescan()
            added = diff.get("added", [])
            removed = diff.get("removed", [])
            changed = diff.get("changed", [])

            if force_full:
                # ── 全量路径：设置面板「重载插件」显式语义 ──
                return self._reload_all_subsystems(pm, result, result_keys)

            # ── 精准路径：无变更不重载任何子系统 ──
            if not added and not removed and not changed:
                logger.debug("[ChatBackend] 无插件变更，跳过子系统重载")
                return result

            logger.info(
                f"[ChatBackend] 插件变更 diff: added={[p.name for p in added]}, "
                f"removed={[p.name for p in removed]}, changed={[p.name for p in changed]}"
            )

            # 2. removed：精准清理。注意 rescan 已把插件移出索引，
            #    组件信息必须取自 rescan 返回的 Plugin 对象（get_plugin 已反查不到，
            #    否则 removed_components 为空 → 卸载不干净）。
            for plugin in removed:
                self._cleanup_removed_plugin_components(plugin.name, dict(plugin.components), result, result_keys)

            # 3. added/changed：逐插件精准加载全部组件（__manifest__ 语义）
            for plugin in list(added) + list(changed):
                sub = self._reload_single_plugin(plugin.name, "__manifest__")
                self._merge_reload_result(result, sub, result_keys)

            logger.info(f"[ChatBackend] 插件精准重载完成: { {k: result[k] for k in result_keys} }")
        except Exception as e:
            logger.error(f"[ChatBackend] Failed to reload plugin subsystems: {e}")

        return result

    @staticmethod
    def _merge_reload_result(result: dict, sub: dict, result_keys: tuple) -> None:
        """合并单插件重载结果到汇总 result（agents 计数累加，其余置 True）"""
        for k in result_keys:
            v = sub.get(k)
            if not v:
                continue
            if k == "agents":
                result[k] = result.get(k, 0) + v
            else:
                result[k] = True

    def _reload_all_subsystems(self, pm, result: dict, result_keys: tuple) -> dict:
        """全量重载所有子系统（设置面板「重载插件」显式语义，force_full=True）"""
        # 2. 重载 AgentManager（智能体 + hooks）
        if self._agent_manager:
            self._agent_manager.reload_agents()
            result["agents"] = len(self._agent_manager.list_agents(include_hidden=True))
            result["hooks"] = True

        # 3. 重载命令
        try:
            from app.core.builtin_commands import reload_all_commands

            reload_all_commands()
            result["commands"] = True
        except (ImportError, Exception) as e:
            logger.error(f"[ChatBackend] Failed to reload commands: {e}")

        # 4. 重载主题
        try:
            from app.utils.config import update_theme_options
            from app.utils.theme_manager import theme_manager

            theme_manager.reload()
            update_theme_options()
            result["themes"] = True
        except (ImportError, Exception) as e:
            logger.error(f"[ChatBackend] Failed to reload themes: {e}")

        # 5. 技能：PluginManager 已更新，UI 通过 get_local_skills() 懒加载
        result["skills"] = True

        # 6. MCP 配置：PluginManager 已更新，UI 通过 get_mcp_servers() 懒加载
        result["mcp"] = True

        # 7. LSP 配置：重新初始化 LspManager（停止旧服务 → 加载新配置 → 启动新服务）
        try:
            from app.core.lsp.lsp_manager import get_lsp_manager

            lsp_mgr = get_lsp_manager()
            lsp_configs = pm.get_lsp_configs()
            workdir = os.getcwd()
            if self._tool_executor and getattr(self._tool_executor, "_workdir", None):
                workdir = str(self._tool_executor._workdir)
            lsp_mgr.initialize(workdir, lsp_configs)
            lsp_mgr.start_all_background()
            result["lsp"] = True
            logger.info(f"[ChatBackend] LSP 全量重载完成，已注册 {len(lsp_mgr._clients)} 个服务器")
        except Exception as e:
            logger.error(f"[ChatBackend] LSP 全量重载失败: {e}")

        # 8. UI 组件：全量 rescan 已在 _load_plugin_ui/_unload_plugin_ui 中处理，
        #    此处标记为 True 以通知 UI 刷新
        result["ui"] = True

        logger.info(
            f"[ChatBackend] Plugin subsystems reloaded: agents={result['agents']}, "
            f"commands={result['commands']}, themes={result['themes']}, "
            f"skills={result['skills']}, mcp={result['mcp']}, lsp={result.get('lsp', False)}, "
            f"ui={result['ui']}, tools={result.get('tools', False)}, "
            f"providers={result.get('providers', False)}, "
            f"team_templates={result.get('team_templates', False)}"
        )
        return result

    # ========== MCP 自动发现 ==========

    def _discover_mcp_servers(self):
        """自动发现其他工具的 MCP 配置并保存到 user-custom 插件（仅首次运行生效）"""
        from app.plugins.managers.plugin_manager import PluginManager
        from app.utils.config import Settings

        cfg = Settings.get_instance()

        # 已处理过则跳过
        if cfg.mcp_discovered.value:
            return

        from app.tools.mcp_tools import discover_and_merge

        merged, new_ones = discover_and_merge()
        if new_ones:
            # 将发现的服务器写入 user-custom 插件
            pm = PluginManager.get_instance()
            if pm.is_initialized():
                for server_data in new_ones:
                    name = server_data.get("name", "")
                    if name:
                        pm.add_mcp_server(name, server_data)
                logger.info(f"[ChatBackend] MCP 自动发现完成，导入 {len(new_ones)} 个新服务器")

        # 标记已处理
        cfg.set(cfg.mcp_discovered, True, save=True)

    # ========== ChatEngine 代理方法 ==========

    def _init_mcp_connections(self):
        """初始化 MCP 服务器连接（后台异步，不阻塞 UI）

        MCP 配置完全由插件驱动，从 PluginManager 获取。
        """
        from app.plugins.managers.plugin_manager import PluginManager
        from app.utils.config import Settings

        mcp_manager = self._tool_executor._builtin_tools._mcp_manager

        if mcp_manager.is_connected:
            logger.info("[ChatBackend] MCP 已连接，复用现有连接")
            return

        cfg = Settings.get_instance()
        if not cfg.mcp_enabled.value:
            logger.info("[ChatBackend] MCP 全局开关已关闭，跳过连接")
            return

        # 从 PluginManager 获取 MCP 服务器列表
        pm = PluginManager.get_instance()
        servers = pm.get_mcp_servers()
        if not servers:
            logger.info("[ChatBackend] 无 MCP 服务器配置，跳过连接")
            return

        mcp_manager.connect_all_background(
            servers,
            on_done=lambda ok, total, failed: logger.info(
                f"[ChatBackend] MCP 后台连接完成: {ok}/{total}" + (f", 失败: {failed}" if failed else "")
            ),
        )

    def stop_streaming(self):
        """停止流式输出（同步方式，可能阻塞 UI 线程）

        Stop hook 已移至 chat_worker 退出循环前触发，此处不再重复触发。
        执行流程：cancel_worker() → finalize_stop()（阻塞等待 worker 线程结束，
        worker return 前会同步触发 Stop hook）。
        """
        if self._chat_engine:
            return self._chat_engine.stop()

    def cancel_streaming(self):
        """非阻塞取消流式输出

        仅设置取消标志并断开信号，不等待 worker 线程结束。
        调用后可以立即更新 UI，然后在适当时机调用 finalize_stop()。
        """
        if self._chat_engine:
            self._chat_engine.cancel_streaming()

    def finalize_stop(self) -> List[Dict]:
        """完成停止流程（阻塞操作，获取中断消息并清理）

        在 cancel_streaming() 调用后执行。
        此方法是阻塞的，应在 UI 更新后（或在后台线程中）调用。

        Returns:
            被中断的消息列表
        """
        if self._chat_engine:
            return self._chat_engine.finalize_stop()
        return []

    def cleanup_worker(self):
        """清理 worker"""
        if self._chat_engine:
            return self._chat_engine.cleanup_worker()

    def cleanup(self):
        """
        清理窗口独有资源，不影响其他窗口。

        安全规则：
        - 不清除任何单例/共享组件（AgentManager/MemoryManagerCore/HistoryManager/BuiltinTools）
        - 仅释放本窗口创建的实例和引用
        """
        self._initialized = False

        # 0. 停止去抖重载定时器（M8）：惰性创建、无 parent 的 QTimer，
        #    不先 stop 会在 cleanup 后 300ms 触发 _do_debounced_reload 访问已清理对象。
        if getattr(self, "_reload_timer", None) is not None and self._reload_timer.isActive():
            self._reload_timer.stop()

        # 1. 清理 ChatEngine（停止 worker + 清空回调）
        if self._chat_engine:
            try:
                self._chat_engine.clear_callbacks()
                self._chat_engine.cleanup_worker()
            except Exception as e:
                logger.warning(f"[ChatBackend] cleanup chat_engine: {e}")
            self._chat_engine = None

        # 1.5 泄漏修复（6b/6d）：解除两个全局单例对首个 backend 的持有
        # - GatewayEngine：get_model_config 是本窗口 backend 的 bound method，
        #   tool_executor/agent_manager/session_store 是窗口独有组件
        # - PlatformManager：_do_init 的 process_message/send_message 异步闭包
        #   捕获 self（backend），create_platform_manager 单例永久持有
        try:
            ge = getattr(self, "_gateway_engine", None)
            if ge is not None and hasattr(ge, "cleanup"):
                ge.cleanup()
        except Exception as e:
            logger.warning(f"[ChatBackend] cleanup gateway_engine: {e}")
        try:
            from app.gateway.manager import get_platform_manager

            pm = get_platform_manager()
            if pm is not None:
                mh = getattr(pm, "_message_handler", None)
                if mh is not None:
                    _pc = getattr(mh, "_process_message", None)
                    _sc = getattr(mh, "_send_message", None)
                    if (_pc is not None and _callback_holds_backend(_pc, self)) or (
                        _sc is not None and _callback_holds_backend(_sc, self)
                    ):
                        mh._process_message = None
                        mh._send_message = None
                        logger.debug("[ChatBackend] PlatformManager 回调已解除（backend 关闭）")
        except Exception as e:
            logger.warning(f"[ChatBackend] cleanup platform_manager: {e}")

        # 2. 清理 ToolExecutor 窗口独有状态（共享 BuiltinTools 不碰）
        if self._tool_executor:
            try:
                # 工具插件化：AutomationTools（紧急停止）/ TerminalTools（bash/bg）已迁
                # 系统插件（模块级单例，无窗口引用），以下 getattr 容错保留为防御性清理。
                _bt = getattr(self._tool_executor, "_builtin_tools", None)
                if _bt is not None:
                    _automation = getattr(_bt, "_automation_tools", None)
                    if _automation is not None and hasattr(_automation, "cleanup"):
                        _automation.cleanup()
                    _terminal = getattr(_bt, "_terminal_tools", None)
                    if _terminal is not None and hasattr(_terminal, "cleanup"):
                        _terminal.cleanup()
            except Exception as e:
                logger.warning(f"[ChatBackend] cleanup automation_tools: {e}")
            try:
                # 泄漏修复（P1）：backend 初始化时把本窗口 ToolExecutor 的
                # BuiltinTools 赋给全局单例 AgentManager._builtin_tools（后创建
                # 窗口覆盖先创建者，即"最后活跃窗口"持有）。窗口关闭前必须解除，
                # 否则单例强引用窗口的 BuiltinTools → 整棵窗口对象树无法回收。
                am = getattr(self, "_agent_manager", None)
                if am is not None and getattr(am, "_builtin_tools", None) is self._tool_executor._builtin_tools:
                    am._builtin_tools = None
                self._tool_executor.cleanup()
            except Exception as e:
                logger.warning(f"[ChatBackend] cleanup tool_executor: {e}")
            self._tool_executor = None

        # 3. 清除 HookManager 回调（闭包引用了本窗口的 ChatBackend）
        if self._hook_manager:
            try:
                self._hook_manager.set_on_finished_callback(None)
                self._hook_manager.set_on_decision_callback(None)
            except Exception as e:
                logger.warning(f"[ChatBackend] cleanup hook_manager: {e}")

        # 4. 清除 SubAgentManager：先取消所有运行中的子智能体任务 + 停止 Stall 检测器
        if self._sub_agent_manager:
            try:
                self._sub_agent_manager.cancel_all()
            except Exception as e:
                logger.warning(f"[ChatBackend] cleanup sub_agent_manager.cancel_all: {e}")
            self._sub_agent_manager = None

        # 5. 清除 SessionManager（窗口独有的会话）
        self._session_manager = None

        # 6. 停止插件 watcher（引用计数归零时停止线程，释放闭包对首个 backend 的引用）
        try:
            self._stop_plugin_watcher()
        except Exception as e:
            logger.warning(f"[ChatBackend] cleanup plugin_watcher: {e}")

        # ★ T3 修复：从活跃实例集合移除，已关闭窗口不再接收 plugin_changed 广播
        # （类级集合持有多余引用也是泄漏源；broadcast 循环遍历时 discard 安全）。
        ChatBackend._active_instances.discard(self)

        # 7. 清除 UI 有效性标志
        self._ui_valid = False

        logger.info("[ChatBackend] 窗口资源清理完成")

    def set_ui_valid(self, valid: bool):
        """设置 UI 有效性标志（由 MainWidget.closeEvent 调用）"""
        self._ui_valid = valid
        logger.debug(f"[ChatBackend] UI valid set to: {valid}")

    def get_current_worker(self):
        """获取当前 Worker 实例"""
        if self._chat_engine:
            return self._chat_engine.get_current_worker()
        return None

    def get_last_cache_stats(self) -> Optional[Dict]:
        """获取最后一次的缓存统计（Worker 被清理后仍可访问）"""
        return getattr(self, "_last_cache_stats", None)

    def set_last_cache_stats(self, stats: Dict):
        """保存最后一次的缓存统计"""
        self._last_cache_stats = stats

    def get_context_usage_snapshot(
        self,
        session,
        llm_config,
        api_prompt_tokens: int = 0,
        api_message_count: int = 0,
        from_api: bool = False,
    ) -> Dict:
        """获取上下文使用快照"""
        if self._chat_engine:
            return self._chat_engine.get_context_usage_snapshot(
                session,
                llm_config,
                api_prompt_tokens=api_prompt_tokens,
                api_message_count=api_message_count,
                from_api=from_api,
            )
        return {}

    def switch_agent(self, agent_name: str):
        """切换 Agent"""
        if self._chat_engine:
            self._chat_engine.switch_agent(agent_name)
        # 同步更新团队工具上下文（使 team_send_message 的 from_agent 正确）
        if self._tool_executor and self._tool_executor._builtin_tools:
            self._tool_executor._builtin_tools.set_team_context(self._window_id, agent_name)

    def approve_tool_permission(self, tool_call_id: str, auto_allow: bool = False, session_allow: bool = False):
        """批准工具调用权限"""
        if self._chat_engine:
            self._chat_engine.approve_tool_permission(tool_call_id, auto_allow, session_allow)

    def deny_tool_permission(self, tool_call_id: str):
        """拒绝工具调用权限"""
        if self._chat_engine:
            self._chat_engine.deny_tool_permission(tool_call_id)

    def provide_question_answer(self, answer: str):
        """提供问题答案"""
        if self._chat_engine:
            self._chat_engine.provide_question_answer(answer)

    def send_message_to_engine(self, text: str, **kwargs) -> bool:
        """发送消息到引擎，支持 _user_content（multimodal list）"""
        # [PERF] 延迟组件兜底：若用户赶在 QTimer 错峰窗口内发送，
        # 同步补建 ToolExecutor/ChatEngine，避免首条消息静默失败。
        self.ensure_deferred_components()
        if self._chat_engine:
            return self._chat_engine.send_message(text, **kwargs)
        return False

    # ========== ToolExecutor 代理方法 ==========

    def set_session_context(self, session_id: str):
        """设置会话上下文"""
        if self._tool_executor:
            self._tool_executor.set_session_context(session_id)
        # 同步会话 ID 到子智能体管理器，确保子智能体任务按会话隔离
        if self._sub_agent_manager:
            self._sub_agent_manager.set_current_session_id(session_id)

    def reset_session_state(self):
        """重置会话状态"""
        if self._tool_executor:
            self._tool_executor.reset_session_state()

    def clear_todo_list(self):
        """清空待办列表"""
        if self._tool_executor:
            self._tool_executor.clear_todo_list()

    def get_todos(self):
        """获取待办列表（返回副本）"""
        if self._tool_executor:
            return self._tool_executor.get_todos()
        return []

    @property
    def file_recorder(self):
        """获取文件操作记录器"""
        if self._tool_executor:
            return getattr(self._tool_executor, "file_recorder", None)
        return None

    def execute_skill(self, method: str, params: Dict):
        """执行技能"""
        if self._tool_executor:
            return self._tool_executor.execute_skill(method, params)
        return None

    def set_sub_agent_history_getter(self, getter: Callable[[], List[Dict]]):
        """设置子智能体获取历史消息的回调"""
        self._sub_agent_history_getter = getter
        if self._sub_agent_manager:
            self._sub_agent_manager.set_history_getter(getter)

    # ========== MemoryManager 代理方法 ==========
    def get_memory_context_string(self, limit: int = 100) -> str:
        """获取记忆上下文字符串

        多窗口隔离：优先使用 tool_executor 中的实例级 workdir，
        避免 DB 中其他窗口写入的工作目录值。
        """
        if self._memory_manager:
            # 多窗口隔离：从 tool_executor 获取实例级 workdir（而非 DB）
            workdir = None
            if self._tool_executor:
                workdir = self._tool_executor.get_workdir()
            # include_project_context=False：项目笔记/路径建议/Worktree 信息
            # 已由 SessionStart hook 注入，无需在每个用户消息中重复写入
            return self._memory_manager.format_memories_for_prompt(
                project=self._current_project,
                entry_limit=limit,
                doc_limit=50,
                workdir_override=workdir,
                include_project_context=False,
            )
        return ""

    def get_user_memories(self, memory_data: Dict = None) -> List[Dict]:
        """获取用户记忆列表（兼容旧接口）"""
        if self._memory_manager:
            return self._memory_manager.get_entry_memories()
        return []

    def load_memory_data(self) -> Dict:
        """加载记忆数据"""
        if self._memory_manager:
            return self._memory_manager.load_memory()
        return {"version": "3.0", "user_memories": []}

    def add_user_memory(self, content: str, **kwargs):
        """添加用户记忆"""
        if self._memory_manager:
            self._memory_manager.add_entry_memory(content, kwargs.get("source", "assistant"))

    def update_user_memories(self, memories: List[Dict]) -> bool:
        """更新用户记忆"""
        if self._memory_manager:
            return self._memory_manager.save_entry_memories(memories)
        return False

    # ========== AgentManager 代理方法 ==========

    def get_primary_agents(self) -> List:
        """获取主 Agent 列表"""
        if self._agent_manager:
            return self._agent_manager.list_primary_agents()
        return []

    def get_agent(self, name: str):
        """获取指定 Agent"""
        if self._agent_manager:
            return self._agent_manager.get_agent(name)
        return None

    # ========== 会话管理 ==========

    def build_memory_context_dict(self) -> Dict[str, Any]:
        """构建 PreUserMessage hook 记忆上下文 — 预取条目记忆 + 关键文档

        Returns:
            包含条目记忆和关键文档的 dict
        """
        from pathlib import Path

        ctx: Dict[str, Any] = {}
        if not self._memory_manager:
            return ctx

        # 条目记忆
        try:
            entries = self._memory_manager.get_entry_memories(limit=100)
            if entries:
                ctx["entry_memories"] = [e.get("content", "") for e in entries]
        except Exception:
            pass

        # 关键文档（含路径显示）
        try:
            wd_path = self._tool_executor.get_workdir() if self._tool_executor else ""
            docs = self._memory_manager.get_key_documents(self._current_project)[:50]
            if docs:
                doc_items = []
                for doc in docs:
                    file_path = doc.get("file_path", "")
                    file_name = doc.get("file_name", "")
                    is_url = file_path and (file_path.startswith("http://") or file_path.startswith("https://"))
                    is_wd = file_path == wd_path
                    if not is_url and not is_wd and file_path and wd_path:
                        try:
                            display = str(Path(file_path).relative_to(Path(wd_path)))
                        except ValueError:
                            display = file_path
                    elif is_url:
                        display = file_path
                    else:
                        display = file_path
                    doc_items.append(
                        {
                            "file_name": file_name,
                            "display": display,
                            "is_url": is_url,
                            "is_wd": is_wd,
                        }
                    )
                if doc_items:
                    ctx["key_documents"] = doc_items
        except Exception:
            pass

        return ctx

    def _build_worktree_context_dict(self) -> Dict[str, Any]:
        """构建 worktree + 路径使用建议上下文（PreUserMessage 每次触发时更新）

        将原本在 SessionStart 中的动态内容（可能随分支切换变化）
        移到 PreUserMessage，确保每次消息前都注入最新状态。

        Returns:
            包含 worktree 信息和路径建议的 dict（无项目时 project_root 为空字符串，
            下游 hook 可据此跳过"项目根目录"显示而非把 os.getcwd() 误当项目根）
        """
        # 【修复】未设置项目工作目录时直接留空，不要回退到 os.getcwd()。
        # 之前用 os.getcwd() 兜底，会让 hook（如 format_memory_context）把
        # 当前进程工作目录误当成"项目根目录"显示出来，与"未配置就不显示"的设计不符。
        # get_workdir() 在用户显式设置前返回 None（初始化默认兜底不算用户设置），
        # 这里统一转为空串，避免把软件启动路径注入 project_root。
        project_root = self._tool_executor.get_workdir() if self._tool_executor else ""
        if project_root is None:
            project_root = ""

        ctx: Dict[str, Any] = {
            "project_root": project_root,
            "project_name": self._current_project or (os.path.basename(project_root) if project_root else ""),
        }

        # Worktree / git 分支信息（project_root 为空时 GitWorktreeDetector.get_repo_info 会返回 None）
        try:
            from app.utils.git_worktree import GitWorktreeDetector

            if project_root:
                repo_info = GitWorktreeDetector.get_repo_info(project_root)
                if repo_info and repo_info.worktrees:
                    ctx["worktree"] = {
                        "repo_name": os.path.basename(repo_info.root),
                        "current_branch": repo_info.current_branch,
                        "workdir": project_root,
                        "is_worktree": project_root != repo_info.root,
                        "other_branches": [wt.branch for wt in repo_info.worktrees if not wt.is_current],
                    }
        except Exception:
            pass

        return ctx

    def _build_session_context(self, state: str) -> Dict[str, Any]:
        """构建 SessionStart hook 上下文 — 预取所有项目数据，hook 只做格式化

        Args:
            state: 会话状态（startup/resume/clear/compact）

        Returns:
            包含所有项目上下文数据的 dict
        """
        # 多窗口隔离：使用当前窗口的工作目录，不依赖进程级 os.getcwd()
        # get_workdir() 返回 None 表示未设置根目录，统一用 "" 表示"无"
        project_root = self._tool_executor.get_workdir() if self._tool_executor else ""
        if project_root is None:
            project_root = ""
        ctx: Dict[str, Any] = {
            "project_root": project_root,
            "state": state,
            "project_name": self._current_project or (os.path.basename(project_root) if project_root else ""),
        }

        # 团队模式：让 SessionStart hook 也能按 #team_member matcher 精确触发
        # （与 chat_worker._trigger_worker_hook 注入的 is_team_member 字段对齐）
        try:
            from app.core.workers.chat_worker import _check_team_member

            ctx["is_team_member"] = _check_team_member(self)
        except Exception:
            ctx["is_team_member"] = False

        # 当前窗口 ID：供团队上下文 hook 按成员定位角色（模板 agents 条目 → 角色描述）
        ctx["window_id"] = getattr(self, "_window_id", "") or ""

        # 项目笔记由 read_project_notes hook（BuildSystemPrompt）从本地 AGENTS.md 直接读取，
        # SessionStart 不再预取 notes 内容

        # Worktree / git 分支信息（仅从缓存读取，由 _warm_git_cache 后台线程预热）
        try:
            from app.utils.git_worktree import GitWorktreeDetector

            repo_info = GitWorktreeDetector._cache_get(GitWorktreeDetector._info_cache, project_root)
            if repo_info and repo_info.worktrees:
                ctx["worktree"] = {
                    "repo_name": os.path.basename(repo_info.root),
                    "current_branch": repo_info.current_branch,
                    "workdir": project_root,
                    "is_worktree": project_root != repo_info.root,
                    "other_branches": [wt.branch for wt in repo_info.worktrees if not wt.is_current],
                }
        except Exception:
            pass

        return ctx

    def _warm_git_cache(self, project_root: str):
        """后台线程预热 git 缓存，避免 create_session 时同步执行 git 子进程（~1.1s）"""
        if not project_root:
            return
        import threading

        def _warm():
            try:
                from app.utils.git_worktree import GitWorktreeDetector

                GitWorktreeDetector.get_repo_info(project_root)
            except Exception:
                pass

        threading.Thread(target=_warm, daemon=True).start()

    def create_session(self, trigger_hook: bool = True) -> ChatSession:
        """创建新会话

        Args:
            trigger_hook: 是否触发 SessionStart hook。初始化时设为 False 避免重复触发。
        """
        session = self._session_manager.create_new_session()
        self.session_created.emit(session.session_id)

        # Trigger SessionStart hook — 同步执行，直接注入 session.messages
        if trigger_hook and self._hook_manager:
            context = self._build_session_context("startup")
            context["session_id"] = session.session_id  # Claude Code 兼容字段
            # 🛡️ W1：UI 线程（新建/分支会话等 GUI 场景）→ trigger_async=True，
            # command/http 类型 hook 后台执行 + finished 回调回补注入，主线程不被
            # 外部进程/网络阻塞（PROMPT 类型仍同步顺序注入，语义不变）。
            # 非 UI 线程（CLI 等无 Qt 事件循环，异步回调无处回补）→ 保持同步。
            from app.core.hook_manager import _is_ui_thread

            trigger_async = _is_ui_thread()
            results = self._hook_manager.trigger_event(
                "SessionStart",
                context=context,
                current_message="",
                trigger_async=trigger_async,
            )
            for r in results:
                if r.success and r.output:
                    _inject_hook_to_session(session, "SessionStart", r.output, r.status_message)
            self._hook_messages_updated.emit()

        return session

    def trigger_session_event(self, state: str, extra_context: dict = None):
        """触发 SessionStart hook，带会话状态

        Args:
            state: 会话状态，可选 startup/resume/clear/compact
            extra_context: 额外上下文信息
        """
        if not self._hook_manager:
            return
        session = self.get_current_session()
        ctx = self._build_session_context(state)
        if session:
            ctx["session_id"] = session.session_id  # Claude Code 兼容字段
        if extra_context:
            ctx.update(extra_context)
        # 🛡️ W1：UI 线程（clear/compact 等 GUI 场景）→ trigger_async=True 走后台
        # 异步 + 回调回补；非 UI 线程保持同步（与 create_session 同策略）。
        from app.core.hook_manager import _is_ui_thread

        results = self._hook_manager.trigger_event(
            "SessionStart",
            context=ctx,
            current_message="",
            trigger_async=_is_ui_thread(),
        )
        if session:
            for r in results:
                if r.success and r.output:
                    _inject_hook_to_session(session, "SessionStart", r.output, r.status_message)
            self._hook_messages_updated.emit()

    def get_current_session(self) -> Optional[ChatSession]:
        """获取当前会话"""
        return self._session_manager.get_current_session()

    def switch_session(self, index: int):
        """切换会话"""
        self._session_manager.switch_to_session(index)
        session = self.get_current_session()
        if session:
            self.session_changed.emit(session.session_id)

    def set_current_session(self, session: ChatSession):
        """设置当前会话"""
        self._session_manager.set_current_session(session)
        if session:
            self.session_changed.emit(session.session_id)

    def delete_session(self, index: int) -> bool:
        """删除会话"""
        # 内存泄漏修复（P1）：删除前先取 session_id，删除成功后联动
        # HistoryManager 释放内存缓存中的消息驻留（_history_sessions
        # 全量消息不再随会话删除而泄漏）。
        session_id = None
        try:
            sessions = self._session_manager.get_all_sessions() if self._session_manager else []
            if 0 <= index < len(sessions):
                session_id = sessions[index].session_id
        except Exception:
            session_id = None

        result = self._session_manager.delete_session(index)
        if result:
            if session_id and self._history_manager:
                try:
                    self._history_manager.remove_session(session_id, release_messages_only=True)
                except Exception:
                    pass
            self.session_deleted.emit(index)
        return result

    def get_all_sessions(self) -> List[ChatSession]:
        """获取所有会话"""
        return self._session_manager.get_all_sessions()

    # ========== 对话操作 ==========

    def send_message(self, text: str, agent_name: str = None, **kwargs):
        """发送消息"""
        session = self.get_current_session()
        if not session:
            session = self.create_session()

        session.add_user_message(text, params=kwargs)

        self._chat_engine.send_message(
            text,
            session=session,
            agent_name=agent_name,
        )

    # ========== 状态查询 ==========

    def get_current_agent(self) -> str:
        """获取当前 Agent"""
        if self._chat_engine:
            return self._chat_engine.current_agent
        return "plan"

    def set_current_agent(self, agent_name: str):
        """设置当前 Agent"""
        if self._chat_engine:
            self._chat_engine.set_current_agent(agent_name)
        # 同步更新团队工具上下文
        if self._tool_executor and self._tool_executor._builtin_tools:
            self._tool_executor._builtin_tools.set_team_context(self._window_id, agent_name)

    def set_streaming_state(self, is_streaming: bool):
        """设置流式状态"""
        if self._chat_engine:
            self._chat_engine.set_streaming(is_streaming)

    def get_context_usage(self) -> tuple:
        """获取上下文使用情况"""
        if self._chat_engine:
            return self._chat_engine.get_context_usage()
        return (0, 0)

    # ========== 上下文构建方法 ==========

    def _build_memory_context(self, query: str = "", project: str = "默认项目") -> str:
        """构建长期记忆上下文（供 ChatEngine 调用）

        多窗口隔离：优先使用 tool_executor 中的实例级 workdir。
        """
        if not self._memory_manager:
            return ""
        workdir = None
        if self._tool_executor:
            workdir = self._tool_executor.get_workdir()
        return self._memory_manager.format_memories_for_prompt(
            project=project,
            entry_limit=100,
            doc_limit=50,
            workdir_override=workdir,
        )

    def _build_chat_cards_context(self) -> str:
        """构建卡片上下文"""
        # 如果有 get_chat_cards 回调，调用它
        if self._get_chat_cards:
            cards = self._get_chat_cards()
            if cards:
                return "\n\n# 已启用的卡片\n" + "\n".join(cards)
        return ""

    # ========== Gateway 方法 ==========

    def _on_gateway_input(self, data: dict):
        """
        处理 Gateway 发来的消息（在主线程运行）

        委托给 GatewayEngine 处理，不碰 UI 的 SessionManager/current_index。

        Args:
            data: {text, chat_id, user_id, platform, future}
        """
        text = data["text"]
        chat_id = data["chat_id"]
        user_id = data["user_id"]
        platform = data["platform"]
        future = data["future"]

        logger.info(f"[Gateway] Main thread processing: {text[:50]}...")

        try:
            import asyncio

            from app.gateway.base import MessageEvent, MessageType
            from app.gateway.base import Platform as GatewayPlatform

            gw_platform = GatewayPlatform(platform)

            # 1. 获取或创建 GatewaySession（WeCom/钉钉用户映射）
            gw_session = self._gateway_manager.session_manager.get_or_create_session(
                MessageEvent(
                    text=text,
                    message_type=MessageType.TEXT,
                    message_id=user_id,
                    chat_id=chat_id,
                    user_id=user_id,
                    platform=gw_platform,
                )
            )

            # 2. 查找或创建 Gateway 自己的 ChatSession（完全独立于 UI）
            stored_chat_id = gw_session.metadata.get("chat_session_id")
            chat_session = None
            if stored_chat_id:
                chat_session = self._gateway_engine.find_session(stored_chat_id)

            if not chat_session:
                from app.core.chat_session import ChatSession

                user_name = gw_session.user_name or user_id[:8]
                chat_session = ChatSession(
                    name=f"{platform}对话"  # UI 显示用（后续会被 topic_summary 覆盖）
                )
                # 单独设置 topic_summary，确保 DB 标题字段为有意义的内容
                # 注意：__init__ 中 topic_summary = name，所以需要覆盖
                chat_session.set_topic_summary(f"[{platform}] {user_name}")
                self._gateway_engine.add_session(chat_session)
                gw_session.metadata["chat_session_id"] = chat_session.session_id
                logger.debug(f"[Gateway] Created ChatSession: {chat_session.session_id} for {platform}:{user_id}")

            # 3. 流式发送辅助函数（在主线程调用，调度到事件循环执行）
            _ev_loop = getattr(self._gateway_manager, "_loop", None)

            def _push_to_platform(content: str) -> None:
                """发送中间更新到平台"""
                if not _ev_loop or not content.strip():
                    return
                try:
                    asyncio.run_coroutine_threadsafe(
                        self._gateway_send_message(gw_platform, chat_id, content),
                        _ev_loop,
                    )
                except Exception as e:
                    logger.error(f"[Gateway] Stream push error: {e}")

            # 4. 流式回调
            # 注意：钉钉/企微不支持编辑已发送消息，流式中间推送会与最终回复内容重叠。
            # 因此只保留工具进度推送，流式内容只在 on_stream_finished 一次性发送。
            gateway_chunks = []

            def on_content_received(chunk):
                """AI 流式输出到达——不推送中间内容，避免与最终回复重复"""
                gateway_chunks.append(chunk)

            def on_tool_call(tool_data: dict):
                """工具调用时发送进度"""
                name = tool_data.get("tool_name", tool_data.get("name", "未知工具"))
                args = tool_data.get("arguments", tool_data.get("args", ""))
                if isinstance(args, dict):
                    args_str = str(list(args.keys())) if args else ""
                else:
                    args_str = str(args)[:80]
                _push_to_platform(f"🔧 正在使用 **{name}**...\n参数: {args_str}")

            def on_tool_result(tool_data: dict):
                """工具返回结果时发送摘要"""
                name = tool_data.get("tool_name", tool_data.get("name", "未知工具"))
                result = tool_data.get("result", "")
                summary = str(result)[:200] if result else ""
                _push_to_platform(f"✅ **{name}** 完成\n{summary}")

            def on_stream_finished(response):
                """AI 完成 → 发送最终完整回复，自动检测并发送本地图片"""
                content = response or "".join(gateway_chunks)
                final = content or "抱歉，我没有生成有效回复，请重试。"

                logger.info(f"[Gateway] AI completed, response_len={len(response)}, final_len={len(final)}")

                # 提取并发送本地图片
                clean_content, image_paths = _extract_markdown_images(final)
                for img_path in image_paths:
                    try:
                        asyncio.run_coroutine_threadsafe(
                            self._gateway_send_image(gw_platform, chat_id, img_path),
                            _ev_loop,
                        )
                    except Exception as e:
                        logger.error(f"[Gateway] Image send error: {e}")

                # 发送清理后的文本
                text_to_send = clean_content.strip()
                if text_to_send:
                    _push_to_platform(f"💬 **DriFox 助手**\n\n{text_to_send}")
                elif not image_paths:
                    # 既无图片也无文字，兜底发送原文
                    _push_to_platform(f"💬 **DriFox 助手**\n\n{final}")

                # 通知异步等待的 future
                try:
                    if not future.done():
                        future.set_result(final)
                except Exception:
                    pass

            def on_error(error):
                logger.error(f"[Gateway] AI error: {error}")
                _push_to_platform(f"❌ 处理出错: {error}")
                try:
                    if not future.done():
                        future.set_result(f"处理消息时出错: {error}")
                except Exception:
                    pass

            callbacks = {
                "content_received": on_content_received,
                "tool_call_started": on_tool_call,
                "tool_result_received": on_tool_result,
                "stream_finished": on_stream_finished,
                "error": on_error,
            }

            self._gateway_engine.process(
                session=chat_session,
                text=text,
                callbacks=callbacks,
            )

        except Exception as e:
            import traceback

            logger.error(f"[Gateway] Processing error: {e}\n{traceback.format_exc()}")
            try:
                if not future.done():
                    future.set_exception(e)
            except Exception:
                pass

    def _init_gateway_async(self):
        """异步初始化 Gateway（后台进行）"""

        def _do_init():
            try:
                # 延迟导入，避免主线程加载 adapter 模块（import 有阻塞风险）
                from app.gateway.manager import create_platform_manager

                # 创建消息处理回调
                async def process_message(
                    session_id: str, text: str, platform: Any, chat_id: str, user_id: str, **kwargs
                ) -> str:
                    """处理 Gateway 消息"""
                    # 这里调用 AI 处理
                    # 由于 ChatBackend 在主线程，需要使用 Qt 信号或线程安全的方式
                    # 简化实现：直接返回处理结果
                    return await self._gateway_process_message(session_id, text, platform, chat_id, user_id, **kwargs)

                async def send_message(platform: Any, chat_id: str, content: str, **kwargs) -> Any:
                    """发送消息到平台"""
                    return await self._gateway_send_message(platform, chat_id, content, **kwargs)

                # 创建管理器（PlatformManager 是单例，连接逻辑在其后台事件循环）
                self._gateway_manager = create_platform_manager(process_message, send_message)

                self._gateway_initialized = True
                logger.info("[ChatBackend] Gateway 管理器创建完成")

                # 启动连接：纯异步调度，不等待结果（避免 WebSocket 连接慢时卡住后台线程）
                self._gateway_manager.start_all_async()

            except Exception as e:
                logger.exception(f"[ChatBackend] Gateway 初始化失败: {e}", exc_info=True)

        # 在后台线程运行
        import threading

        t = threading.Thread(target=_do_init, daemon=True)
        t.start()

    async def _gateway_process_message(
        self, session_id: str, text: str, platform: Any, chat_id: str, user_id: str, **kwargs
    ) -> str:
        """
        处理 Gateway 消息 - 调用 AI

        先发送"思考中"提示，然后等待 AI 结果。
        """
        logger.info(f"[Gateway] Processing message from {platform.value}:{user_id}: {text[:50]}...")

        # 先发送"思考中"占位回复
        await self._gateway_send_message(platform, chat_id, "🤔 正在思考，请稍候...")

        # 用 signal 发送到主线程
        import concurrent.futures

        future = concurrent.futures.Future()

        self.gateway_input_received.emit(
            {
                "text": text,
                "chat_id": chat_id,
                "user_id": user_id,
                "platform": platform.value,
                "future": future,
            }
        )

        try:
            # 异步等待 AI 结果（不超时，AI 回复多久等多久）
            response = await asyncio.wrap_future(future)

            # 注意：on_stream_finished 回调已通过 _push_to_platform 发送了最终回复
            # 所以这里返回空字符串，避免 message_handler 重复发送
            return ""
        except Exception as e:
            import traceback

            logger.error(f"[Gateway] AI processing error: {e}\n{traceback.format_exc()}")
            return ""

    async def _gateway_send_message(self, platform: Any, chat_id: str, content: str, **kwargs) -> Any:
        """发送消息到平台"""
        from app.gateway.base import SendResult

        adapter = self._gateway_manager.get_adapter(platform)
        if adapter:
            try:
                result = await adapter.send(chat_id, content)
                return result
            except Exception as e:
                logger.error(f"[Gateway] Send failed: {e}")
                return SendResult(success=False, error=str(e))

        logger.warning(f"[Gateway] No adapter for platform {platform}")
        return SendResult(success=False, error="No adapter")

    async def _gateway_send_image(self, platform: Any, chat_id: str, image_path: str, **kwargs) -> Any:
        """发送图片到平台"""
        from app.gateway.base import SendResult

        adapter = self._gateway_manager.get_adapter(platform)
        if adapter:
            try:
                result = await adapter.send_image(chat_id, image_path)
                return result
            except Exception as e:
                logger.error(f"[Gateway] Send image failed: {e}")
                return SendResult(success=False, error=str(e))

        logger.warning(f"[Gateway] No adapter for platform {platform}")
        return SendResult(success=False, error="No adapter")

    def _start_gateway_async(self):
        """异步启动 Gateway"""
        import threading

        def _do_start():
            try:
                if self._gateway_manager and self._gateway_initialized:
                    self._gateway_manager.start_all_async()
                    logger.info("[ChatBackend] Gateway 已启动（后台连接中）")
            except Exception as e:
                logger.error(f"[ChatBackend] Gateway 启动失败: {e}", exc_info=True)

        t = threading.Thread(target=_do_start, daemon=True)
        t.start()

    def _stop_gateway_async(self):
        """异步停止 Gateway"""
        import threading

        def _do_stop():
            try:
                if self._gateway_manager:
                    self._gateway_manager.stop_all()
                    logger.info("[ChatBackend] Gateway 已停止")
            except Exception as e:
                logger.error(f"[ChatBackend] Gateway 停止失败: {e}", exc_info=True)

        t = threading.Thread(target=_do_stop, daemon=True)
        t.start()

    def _on_gateway_status_changed(self, status: dict):
        """Gateway 状态变化回调"""
        self.gateway_status_changed.emit(status)

    @property
    def gateway_manager(self):
        """获取 Gateway 管理器"""
        return self._gateway_manager

    @property
    def gateway_engine(self) -> Optional[GatewayEngine]:
        """获取 Gateway 引擎（与 ChatEngine 完全独立）"""
        return self._gateway_engine

    @property
    def gateway_initialized(self) -> bool:
        """Gateway 是否已初始化"""
        return self._gateway_initialized

    def get_gateway_status(self) -> dict:
        """获取 Gateway 状态"""
        if self._gateway_manager:
            return self._gateway_manager.get_status()
        return {"running": False, "platforms": {}}

    def start_gateway(self):
        """启动 Gateway"""
        self._start_gateway_async()

    def stop_gateway(self):
        """停止 Gateway"""
        self._stop_gateway_async()
