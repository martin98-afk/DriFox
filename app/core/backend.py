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
        # 毫秒级：hook 常常同秒连发多条，秒级时间戳排不出先后
        "ts_ms": int(time.time() * 1000),
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


def _gw_str_platform(platform: Any):
    """Gateway 平台入参归一：枚举内平台转 Platform；第三方 str 平台 id 原样直通（Phase E 契约）。"""
    from app.gateway.base import Platform as GatewayPlatform

    if isinstance(platform, GatewayPlatform):
        return platform
    try:
        return GatewayPlatform(platform)
    except ValueError:
        return platform


def _safe_agent_manager(backend: "ChatBackend") -> Any:
    """安全读取 _agent_manager：未 __init__ 时返回 None 而不触发 super().__init__ 异常

    ChatBackend.__new__(...) 路径（测试场景）下，self._agent_manager 是 descriptor，
    任何属性访问会触发 QObject.__init__() 链校验。bind_runtime 需 None 而非异常。
    """
    try:
        return object.__getattribute__(backend, "_agent_manager")
    except AttributeError, RuntimeError:
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
    # ⚠️ 签名与引擎侧一致（response: str），不再是旧的 dict —— 见 _TRACE_SIGNAL_ARITY
    stream_finished = pyqtSignal(str)
    reasoning_content = pyqtSignal(str)  # DeepSeek thinking mode

    # 工具相关
    tool_call_started = pyqtSignal(str, str, dict)  # tool_call_id, tool_name, arguments
    # ⚠️ 与引擎回调一致：(tool_call_id, name, arguments, result)；
    # 旧签名 (id, name, result, success) 与 emit 源对不上，从来没有数据。
    tool_result_received = pyqtSignal(str, str, dict, object)

    # 权限相关
    permission_requested = pyqtSignal(str, str, dict)  # tool_call_id, tool_name, arguments

    # 错误
    error_occurred = pyqtSignal(str)

    # 上下文
    context_updated = pyqtSignal(int, int)  # token_count, limit

    # Auto-compact 请求（由 tool_executor 在 PostToolUse hook 中检测阈值触发）
    auto_compact_requested = pyqtSignal(float)  # ratio

    # SubAgentManager 延迟创建完成信号（[审查 #8r Bug D] 窗口在 __init__ 时
    # sub_agent_manager 尚为 None 跳过信号连接，创建完成后据此补连）
    sub_agent_ready = pyqtSignal()

    # Hook 执行状态信号（event_name, status_message, is_start）
    # TODO: 当前没有 UI 订阅此信号。状态消息字段 (`statusMessage`) 已可解析但尚未展示。
    #       待 hook_setting_card 或状态栏/通知组件接入后即可移除此 TODO。
    hook_status_changed = pyqtSignal(str, str, bool)

    # 活跃 backend 实例集合：PluginHostService 触发 PluginChanged hook 时
    # 需要各 tab 的 hook_manager（hook 输出注入各自对话队列）。
    # __init__ 注册 / cleanup 移除；类级 set（遍历时 discard 安全）。
    _active_instances: set = set()

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
          SubAgentManager + MCP + git 预热）用 QTimer
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
            # PluginChanged → 主队列（环境事件，触发时通常无活跃对话，排队至下轮 loop 顶部
            #   消费，AI 下一轮对话可见工具/MCP/插件增减明细）
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
            elif is_prompt_hook or event_name in ("PostToolUse", "PluginChanged"):
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

        # 6. PluginManager 初始化已上移 PluginHostService（TabManagerWindow 启动时
        # 同步扫描，agent/命令数据经全局单例供本窗口复用）。

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

    # ========== 非首帧必需组件：QTimer 错峰创建 ==========

    def _defer_non_critical_components(self):
        """[PERF] 非首帧必需组件用 QTimer 错峰创建（0/200/400/600ms）

        首帧路径（OpenAIChatToolWindow.__init__）只保留 SessionManager /
        HookManager / create_session / AgentManager / HistoryManager，
        其余组件延迟到事件循环就绪后分批构建，缩短窗口显示前的主线程阻塞：

        - 0ms:   MemoryManagerCore（全局单例，ToolExecutor 依赖）
        - 200ms: ToolExecutor（app/tools 级联 import 8 模块 + LSP + codegraph，
                实测 import 重头，最值得延迟）
        - 400ms: ChatEngine（依赖 tool_executor）
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
        """400ms 批：ChatEngine（依赖 tool_executor）"""
        if self._tool_executor is None:
            logger.warning("[ChatBackend] ToolExecutor 未创建，跳过引擎延迟创建")
            return
        try:
            from app.core.engines.ui import ChatEngine
            from app.plugins.registries.engine_registry import create_engine_for_slot
            from app.plugins.loaders.runtime_component_loader import ensure_engine_watcher

            # 触发引擎 watcher（注册/热重载），无插件时 no-op
            ensure_engine_watcher()
            # 多窗口隔离：团队成员窗口使用 team_member 策略（跳过 Pre/PostAssistant
            # 等主对话语义 hook，避免污染成员的邮件驱动对话流边界）。
            hook_policy_id = None
            try:
                from app.core.team_manager import TeamManager

                if TeamManager.get_instance().is_team_member(self._window_id):
                    hook_policy_id = "team_member"
            except Exception:
                pass
            self._chat_engine = create_engine_for_slot(
                "ui",
                ChatEngine,
                session_manager=self._session_manager,
                get_model_config=self._get_model_config,
                tool_executor=self._tool_executor,
                agent_manager=self._agent_manager,
                get_chat_cards=getattr(self, "_build_chat_cards_context", None),
                backend=self,
                hook_policy_id=hook_policy_id,
            )
            logger.info("[ChatBackend] ChatEngine 延迟创建完成")
            # [审查 #8r Bug C] 窗口构造期暂存的 UI 回调（流式更新等）补注册
            self._flush_pending_engine_callbacks()
        except Exception as e:
            logger.error(f"[ChatBackend] ChatEngine 延迟创建失败: {e}")

        # GatewayEngine 已上移 GatewayService（应用级单例），窗口不再创建

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

        # MCP 自动发现/连接已上移 PluginHostService（全局一次，非每窗口）。

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

    # 引擎回调 → 本对象同名 Qt 信号的转发表：{回调名: 透传给信号的参数个数}
    #
    # 背景：上面这一组信号（tool_call_started / tool_result_received /
    # stream_started / stream_finished / context_updated）历史上**没有任何 emit
    # 点**——实时事件实际走的是「回调字典」链路（conversation/adapters/ui.py
    # emit → engines/ui/engine.py 转发 → backend.set_all_callbacks →
    # main_widget._setup_engine_callbacks），只有主程序那一份消费者。
    # 插件若再注册同名回调会把主程序的顶掉（工具卡片/流式渲染全废），所以这里
    # 在注册时统一包一层：先跑原回调，再把事件原样 emit 到 Qt 信号供插件订阅。
    _TRACE_SIGNAL_ARITY: Dict[str, int] = {
        "stream_started": 0,
        "stream_finished": 1,  # (response,)
        "tool_call_started": 3,  # (tool_call_id, tool_name, arguments)；引擎第 4 参 round_id 不透传
        "tool_result_received": 4,  # (tool_call_id, name, arguments, result)
        "context_updated": 2,  # (token_count, limit)；引擎第 3 参 from_api 不透传
    }

    def _wrap_trace_callback(self, name: str, callback: Callable) -> Callable:
        """把引擎回调包一层：原回调照跑，之后把事件 emit 到同名 Qt 信号。"""
        arity = self._TRACE_SIGNAL_ARITY.get(name)
        if arity is None:
            return callback
        sig = getattr(self, name, None)
        if sig is None or not hasattr(sig, "emit"):
            return callback

        def wrapped(*args, **kwargs):
            try:
                return callback(*args, **kwargs)
            finally:
                try:
                    sig.emit(*args[:arity])
                except Exception as e:  # 转发失败绝不能影响主流程
                    logger.debug(f"[ChatBackend] {name} 事件转发到信号失败: {e}")

        return wrapped

    def set_callback(self, name: str, callback: Callable):
        """设置回调（代理到 ChatEngine），并顺带把事件转发到同名 Qt 信号。"""
        callback = self._wrap_trace_callback(name, callback)
        if self._chat_engine:
            self._chat_engine.set_callback(name, callback)
        else:
            # [审查 #8r Bug C] ChatEngine 延迟创建（400ms 批）期间 main_widget 同步
            # 设置回调会静默丢弃 → 暂存，创建完成后补注册
            self._pending_engine_callbacks[name] = callback

    def set_all_callbacks(self, callbacks: Dict[str, Callable]):
        """批量设置回调（同样会包装出事件转发）"""
        wrapped = {name: self._wrap_trace_callback(name, cb) for name, cb in callbacks.items()}
        if self._chat_engine:
            for name, callback in wrapped.items():
                self._chat_engine.set_callback(name, callback)
        else:
            # [审查 #8r Bug C] 同上：先缓存，ChatEngine 创建后统一补注册
            self._pending_engine_callbacks.update(wrapped)

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

        # 0. 取消待执行的去抖重载（修 #2 timer：已改为 QTimer.singleShot，无实例可 stop，
        #    置标志位由 _do_debounced_reload 自行跳过，避免 cleanup 后回调访问已清理对象）。
        self._reload_pending = False

        # 1. 清理 ChatEngine（停止 worker + 清空回调）
        if self._chat_engine:
            try:
                self._chat_engine.clear_callbacks()
                self._chat_engine.cleanup_worker()
            except Exception as e:
                logger.warning(f"[ChatBackend] cleanup chat_engine: {e}")
            self._chat_engine = None

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

        # 插件 watcher 已上移 PluginHostService（应用级，随 TabManagerWindow.cleanup 停止）。

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