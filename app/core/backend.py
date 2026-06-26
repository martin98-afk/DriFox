# -*- coding: utf-8 -*-
"""
ChatBackend - 统一后端接口
后端自己创建和管理所有组件，前端只负责 UI 调用
"""
import asyncio
import os
import queue
import re
import time
from typing import Any, Callable, Dict, List, Optional

import orjson as json
from loguru import logger
from PyQt5.QtCore import QObject, QThreadPool, pyqtSignal

from app.core.agent import AgentManager
from app.core.chat_session import ChatSession, SessionManager
from app.core.engines.ui import ChatEngine
from app.core.hook_manager import HookManager
from app.core.memory_manager import MemoryManagerCore
from app.core.store import SessionStore
from app.core.tool_executor import ToolExecutor
from app.core.workers.subagent_worker import SubAgentManager
from app.utils.history_manager import HistoryManager

# 支持的图片扩展名
_IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp'}


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
    kebab = re.sub(r'([a-z0-9])([A-Z])', r'\1-\2', event_name)
    kebab = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1-\2', kebab)
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
    m = re.search(
        r'<([a-z0-9-]+-hook)>\s*(.*?)\s*</\1>',
        content, re.DOTALL
    )
    if m:
        return m.group(2).strip()

    # 旧分隔线格式
    if content.startswith('---') and '🔌 **Hook 内部通知**' in content:
        match = re.search(
            r'---\n.*?🔌\s*\*\*Hook 内部通知\*\*.*?\n\n(.+?)\n---',
            content, re.DOTALL
        )
        if match:
            return match.group(1).strip()
        return content

    # 最早旧格式
    match = re.search(r'<hook[^>]*>(.*?)</hook>', content, re.DOTALL)
    if match:
        return match.group(1).strip()
    return content


def _format_hook_output(event_name: str, output: str) -> str:
    """格式化 hook 输出为 Claude Code 兼容的双层 XML 标签格式

    外层: <system-reminder>...</system-reminder>
    内层: <{kebab-case-event}-hook>...</{kebab-case-event}-hook>

    与 Claude Code 实际格式对齐：
    - <system-reminder> 是 Claude Code 通用系统注入容器
    - <user-prompt-submit-hook> 等是 Claude Code 提示词中明说的 hook 反馈标签
    LLM 收到时按 system prompt 约定识别为 hook 注入内容。
    """
    tag = _event_to_tag(event_name)
    return (
        f"<system-reminder>\n"
        f"<{tag}-hook>\n"
        f"{output}\n"
        f"</{tag}-hook>\n"
        f"</system-reminder>"
    )


def _make_hook_message(event_name: str, output: str) -> Dict[str, Any]:
    """构建一条 hook 消息 dict（带 _hook_event 标记和 timestamp）"""
    import datetime as _dt
    return {
        "role": "assistant",
        "content": _format_hook_output(event_name, output),
        "_hook_event": event_name,
        "timestamp": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _inject_hook_to_session(session, event_name: str, output: str):
    """将 hook 输出追加到 session.messages（只追加不删除，保证历史稳定）"""
    if not session:
        return
    msg = _make_hook_message(event_name, output)
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
            if ext in _IMAGE_EXTENSIONS:
                image_paths.append(path)
                return ""  # 移除图片标记
        return match.group(0)  # 保留原样

    cleaned = re.sub(r'!\[.*?\]\((.+?)\)', _replace_img, content)
    return cleaned, image_paths


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

    # Gateway 状态
    gateway_status_changed = pyqtSignal(dict)  # status dict

    # Gateway 消息处理（跨线程）
    gateway_input_received = pyqtSignal(object)  # dict: {text, chat_id, user_id, platform, future}

    # 插件热更新信号（watchfiles 检测到变更时触发）
    plugin_changed = pyqtSignal(dict)  # {"agents": int, "commands": bool, "themes": bool}
    # 后台线程请求主线程执行插件重载（内部信号）
    _hot_reload_requested = pyqtSignal(str, str)  # (插件名, 组件), ""=全量/空组件=全部组件
    # _watch_loop 检测到新插件时，用此 sentinel 作为 plugin_name 标记走增量加载路径
    _NEW_PLUGIN_SENTINEL = "__NEW__"

    def __init__(self, parent=None):
        super().__init__(parent)

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
        return self._session_store

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
        
        Args:
            get_model_config: 获取模型配置的回调
            agent_manager: 已有的 AgentManager（可选）
            workdir: 工作目录
        """
        logger.info("[ChatBackend] 初始化中...")

        self._get_model_config = get_model_config

        # 1. 创建 SessionManager
        self._session_store = SessionStore.get_instance()
        self._session_manager = SessionManager()
        logger.info("[ChatBackend] SessionManager 创建完成")

        # 2. 创建 MemoryManager（全局单例，跨窗口共享）
        self._memory_manager = MemoryManagerCore.get_instance()
        logger.info("[ChatBackend] MemoryManager 创建完成")

        # 3. 创建 HookManager（必须在 create_session 之前）
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
        def on_hook_finished(event_name: str, output: str, success: bool):
            if not getattr(self, '_ui_valid', True):
                logger.debug("[HookManager] Hook callback skipped: UI already closed")
                return

            is_prompt_hook = event_name.startswith("__prompt__:")
            if is_prompt_hook:
                event_name = event_name[len("__prompt__:"):]

            logger.info(f"[HookManager] Hook callback: event={event_name}, success={success}")

            if not success:
                return

            # PreToolUse → 独立队列（worker 在 tool 执行后立即消费，插入 tool result 之前）
            # PostToolUse → 主队列（在 loop 顶部消费，出现在 tool result 之后）
            # prompt hooks → 主队列
            if event_name == "PreToolUse":
                hook_output = _format_hook_output(event_name, output)
                self._pre_tool_message_queue.put({
                    "role": "assistant",
                    "content": hook_output,
                    "_hook_event": event_name,
                })
                logger.debug(f"[HookManager] PreToolUse queued to pre-tool queue")
            elif is_prompt_hook or event_name == "PostToolUse":
                hook_output = _format_hook_output(event_name, output)
                self._hook_message_queue.put({
                    "role": "assistant",
                    "content": hook_output,
                    "_hook_event": event_name,
                })
                self._hook_messages_updated.emit()
                logger.debug(f"[HookManager] Hook queued for worker: {event_name}")

        self._hook_manager.set_on_finished_callback(on_hook_finished)

        # 4. 创建初始会话（不触发 SessionStart hook，避免重复初始化）
        self.create_session(trigger_hook=False)

        # 5. 使用全局共享的 AgentManager（只读数据，跨窗口复用）
        # agents_dir 传 None，智能体从已启用插件动态加载
        self._agent_manager = AgentManager.get_instance(None, self._hook_manager)
        logger.info(f"[ChatBackend] AgentManager 就绪，{len(self._agent_manager.list_agents())} 个 Agent")

        # 加载全局 hooks（从 PluginManager 获取路径）
        from app.core.plugin_manager import PluginManager
        pm = PluginManager.get_instance()
        if pm.is_initialized():
            global_hooks_file = pm.get_global_hooks_file()
            if global_hooks_file.exists() and "user-custom" not in self._hook_manager._skill_to_hooks:
                try:
                    with open(global_hooks_file, 'rb') as f:
                        config = json.loads(f.read())
                    skill_root = str(global_hooks_file.parent)
                    count = self._hook_manager.register_hooks_from_json(
                        "user-custom", skill_root, config, str(global_hooks_file)
                    )
                    if count > 0:
                        logger.info(f"[ChatBackend] Loaded {count} global hooks from {global_hooks_file}")
                except Exception as e:
                    logger.error(f"[ChatBackend] Failed to load global hooks from {global_hooks_file}: {e}")

        # 6. 创建 ToolExecutor（不传递 homepage，解耦 Qt）
        self._tool_executor = ToolExecutor(workdir=workdir, backend=self)
        self._tool_executor.set_memory_manager(self._memory_manager)
        self._tool_executor.set_llm_config_getter(get_model_config)
        self._tool_executor.set_agent_manager(self._agent_manager)
        # 设置 AgentManager 的 builtin_tools 引用（用于获取 MCP 工具 schema）
        self._agent_manager._builtin_tools = self._tool_executor._builtin_tools
        # 设置关键文档仓储
        if self._memory_manager and self._memory_manager.key_documents:
            self._tool_executor.set_key_documents_repo(
                self._memory_manager.key_documents,
                "默认项目"  # 初始值，main_widget 初始化后会通过 set_current_project 覆盖
            )
        logger.info("[ChatBackend] ToolExecutor 创建完成")

        # 7. 创建 ChatEngine（暂时不传 get_memory_context，后面通过 setter 设置）
        self._chat_engine = ChatEngine(
            session_manager=self._session_manager,
            get_model_config=get_model_config,
            tool_executor=self._tool_executor,
            agent_manager=self._agent_manager,
            get_chat_cards=getattr(self, '_build_chat_cards_context', None),
            backend=self,  # 暂时设为 None，后面通过 setter 设置
        )
        logger.info("[ChatBackend] ChatEngine 创建完成")

        # 连接 hook 消息更新信号 → UI 刷新（跨线程安全）
        self._hook_messages_updated.connect(self._on_hook_messages_changed)

        # 创建 GatewayEngine（全局单例，多个窗口共享）
        from app.core.engines.gateway import GatewayEngine
        self._gateway_engine = GatewayEngine.get_instance(
            get_model_config=get_model_config,
            tool_executor=self._tool_executor,
            agent_manager=self._agent_manager,
            session_store=self._session_store,
        )
        logger.info("[ChatBackend] GatewayEngine 创建完成")

        # 创建 SubAgentManager（管理子智能体任务）
        # 使用包装的 get_llm_config：优先使用子智能体默认模型，回退到主模型
        self._subagent_model_resolver: Optional[Callable] = None  # 由 main_widget 设置

        def _get_subagent_llm_config():
            from app.utils.config import Settings
            cfg = Settings.get_instance()
            saved = cfg.llm_subagent_default_model.value
            if saved and self._subagent_model_resolver:
                resolved = self._subagent_model_resolver(saved)
                if resolved:
                    return resolved
            return get_model_config()

        self._sub_agent_manager = SubAgentManager(
            agent_manager=self._agent_manager,
            tool_executor=self._tool_executor,
            get_llm_config=_get_subagent_llm_config,
        )
        self._sub_agent_manager.set_session_store(self._session_store)
        # 设置给 ToolExecutor，让工具能访问子智能体
        self._tool_executor.set_sub_agent_manager(self._sub_agent_manager)
        # 启动日志活力度 stall 检测（默认 180s 无日志输出视为卡死）
        self._sub_agent_manager.start_stall_detector()
        logger.info("[ChatBackend] SubAgentManager 创建完成（Stall 检测器已启动）")

        # 提供设置 history getter 的方法（由 main_widget 在初始化时调用）
        self._sub_agent_history_getter = None

        self._get_memory_context_getter = None

        self._history_manager = HistoryManager.get_instance()

        # 8. 初始化 PluginManager（系统 + 用户插件发现）
        self._init_plugin_system()

        # 9. 自动发现并合并其他来源的 MCP 服务器配置（仅首次）
        self._discover_mcp_servers()

        # 10. 初始化 MCP 连接
        self._init_mcp_connections()

        self._initialized = True
        logger.info("[ChatBackend] 初始化完成")

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

    def set_callback(self, name: str, callback: Callable):
        """设置回调（代理到 ChatEngine）"""
        if self._chat_engine:
            self._chat_engine.set_callback(name, callback)

    def set_all_callbacks(self, callbacks: Dict[str, Callable]):
        """批量设置回调"""
        if self._chat_engine:
            for name, callback in callbacks.items():
                self._chat_engine.set_callback(name, callback)

    def _on_hook_messages_changed(self):
        """槽：hook 消息已添加到 session，通知 UI 刷新消息列表

        通过 _hook_messages_updated 信号连接（跨线程安全），
        确保在 hook 后台线程执行完毕后，UI 能及时显示 hook 输出。

        Hook 消息注入 worker API 缓存的职责已由队列路径
        （_inject_pending_hook_messages，在 worker 线程中运行）承担，
        避免跨线程直接访问 worker 内部状态带来的竞态风险。
        """
        if not getattr(self, '_ui_valid', True):
            return

        session = self.get_current_session()
        if not session:
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

        首次初始化时全量加载智能体和主题；
        后续窗口创建时 PluginManager 已初始化，跳过重复刷新。
        首次初始化后自动启动插件文件变更监听（watchfiles 热更新）。
        """
        try:
            from app.core.plugin_manager import PluginManager
            from app.utils.utils import get_app_data_dir

            pm = PluginManager.get_instance()
            app_data_dir = get_app_data_dir()

            # 记录初始化前的状态，用于判断是否为首次初始化
            was_initialized = pm.is_initialized()
            pm.initialize(app_data_dir)

            # 首次初始化时才需要全量重载智能体和主题
            # 后续窗口复用已有的 PluginManager/AgentManager 单例数据
            if not was_initialized:
                # AgentManager 重新从已启用插件加载智能体
                if self._agent_manager:
                    self._agent_manager.reload_agents()

                # 插件系统初始化后，必须刷新主题
                self._reload_themes_from_plugins()

                # 启动插件文件变更监听（热更新，仅启动一次）
                self._start_plugin_watcher()

                # ── 初始化 LSP 管理器（仅首次，多窗口共享单例）──
                try:
                    from app.core.lsp.lsp_manager import LspManager
                    lsp_mgr = LspManager.get_instance()
                    lsp_configs = pm.get_lsp_configs()
                    # 使用项目根目录作为 LSP workspace root，而非 os.getcwd()
                    # 避免 pyright 扫描整个上级目录（如 d:\work 下所有项目）
                    # backend.py 位于 app/core/backend.py，项目根在其上 3 层
                    workdir = os.path.dirname(
                        os.path.dirname(
                            os.path.dirname(os.path.abspath(__file__))
                        )
                    )
                    lsp_mgr.initialize(workdir, lsp_configs)
                    logger.info(f"[ChatBackend] LspManager 初始化完成，"
                               f"已注册 {len(lsp_mgr._clients)} 个 LSP 服务器")
                    lsp_mgr.start_all_background()
                except Exception as e:
                    logger.error(f"[ChatBackend] LspManager 初始化失败: {e}")

            logger.info(f"[ChatBackend] PluginManager 初始化完成，"
                       f"已加载 {len(pm.list_plugins())} 个插件，"
                       f"智能体 {len(self._agent_manager.list_agents())} 个")

        except Exception as e:
            logger.error(f"[ChatBackend] PluginManager 初始化失败: {e}")

    def _reload_themes_from_plugins(self):
        """插件系统初始化后，重新加载插件主题"""
        try:
            from app.utils.theme_manager import theme_manager
            theme_manager.reload()
            # 同时更新 Settings 中的主题选项
            from app.utils.config import update_theme_options
            update_theme_options()
            logger.info(f"[ChatBackend] 插件主题刷新完成，"
                       f"共 {len(theme_manager.list_themes())} 个主题")
        except Exception as e:
            logger.error(f"[ChatBackend] 刷新插件主题失败: {e}")

    # ========== 插件热更新（watchfiles） ==========

    _plugin_watcher_started = False  # 类级别标志，确保全局只启动一次

    def _start_plugin_watcher(self):
        """启动 watchfiles 插件文件变更监听（仅启动一次）"""
        if ChatBackend._plugin_watcher_started:
            return
        ChatBackend._plugin_watcher_started = True

        try:
            from watchfiles import watch
        except ImportError:
            logger.warning("[ChatBackend] watchfiles 未安装，插件热更新不可用。pip install watchfiles")
            return

        # 收集需要监听的插件目录
        from app.core.plugin_manager import PluginManager
        pm = PluginManager.get_instance()

        watch_paths = []
        from pathlib import Path as _Path

        # 系统插件目录
        if hasattr(pm, '_SYSTEM_PLUGIN_DIR') and pm._SYSTEM_PLUGIN_DIR.exists():
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

        import threading

        # 去重缓存：(plugin_name, component) → 上次重载时间
        _dedup_cache: Dict[tuple, float] = {}
        _DEDUP_INTERVAL = 3.0  # 同一插件+组件 3 秒内重复请求只执行一次

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

        def _rebuild_prefixes():
            """重建插件路径索引（在 watch 线程中调用）"""
            _prefixes_ref[0] = self._build_plugin_path_index()

        def _try_identify_new_plugin(changes) -> Optional[str]:
            """直接扫描变更路径的父目录链，检测是否为新增插件的首次变更

            当 _identify_plugin_from_changes 返回 None 时调用此方法，
            作为 fallback 直接从文件系统查找插件清单。
            """
            import json as _json
            from pathlib import Path as _Path

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
                            return data.get("name", parent.name)
                        except Exception:
                            return parent.name
                    # 检查 .claude-plugin 格式
                    manifest = parent / ".claude-plugin" / "plugin.json"
                    if manifest.exists():
                        try:
                            data = _json.loads(manifest.read_text(encoding="utf-8"))
                            return data.get("name", parent.name)
                        except Exception:
                            return parent.name
            return None

        def _watch_loop():
            """后台线程: 监听插件目录文件变更，识别所属插件后请求主线程增量重载"""
            logger.debug("[ChatBackend] watchfiles 监听线程已启动")
            try:
                for changes in watch(
                    *watch_paths,
                    recursive=True,
                    debounce=2000,  # 2秒防抖
                    yield_on_timeout=False,
                ):
                    # changes: set of (Change, Path)
                    if not changes:
                        continue
                    # 过滤掉 .git/ __pycache__/ .pyc 等无关变更
                    relevant_changes = []
                    for change_type, change_path in changes:
                        p = change_path.lower()
                        if '.git' in p or '__pycache__' in p or p.endswith('.pyc'):
                            continue
                        relevant_changes.append((change_type, change_path))

                    if not relevant_changes:
                        continue

                    current_prefixes = _prefixes_ref[0]

                    # 识别变更所属插件
                    plugin_name = self._identify_plugin_from_changes(
                        relevant_changes, current_prefixes
                    )

                    if plugin_name == "__ALL__":
                        logger.info(
                            f"[ChatBackend] 跨插件文件变更 ({len(relevant_changes)} 处)，"
                            f"请求主线程全量重载..."
                        )
                        # 不提前 _rebuild_prefixes()，改在 _on_hot_reload_requested 重载完成后重建
                        self._hot_reload_requested.emit("", "")
                    elif plugin_name:
                        # 识别变更所属组件（agents/hooks/commands/themes/skills/mcp/空=根目录）
                        component = self._identify_component_from_changes(
                            relevant_changes, current_prefixes, plugin_name
                        )
                        if component:
                            detail = f" ({component})"
                        else:
                            detail = " (根目录)"
                        # 去重：同一插件+组件短时间内重复触发则跳过
                        if _is_duplicate(plugin_name, component):
                            logger.debug(
                                f"[ChatBackend] 插件 [{plugin_name}]{detail} "
                                f"文件变更 ({len(relevant_changes)} 处)，去重跳过..."
                            )
                            continue
                        logger.info(
                            f"[ChatBackend] 插件 [{plugin_name}]{detail} "
                            f"文件变更 ({len(relevant_changes)} 处)，请求主线程增量重载..."
                        )
                        self._hot_reload_requested.emit(plugin_name, component)
                    else:
                        # 无法通过路径索引识别：尝试直接从文件系统检测新插件
                        new_name = _try_identify_new_plugin(relevant_changes)
                        if new_name:
                            logger.info(
                                f"[ChatBackend] 检测到新插件「{new_name}」文件变更，"
                                f"请求增量重载..."
                            )
                            # 预填充 dedup cache，防止路径索引重建后同一批 watch 事件
                            # 的剩余部分以已知插件路径再次触发（ghost trigger）
                            _dedup_cache[(new_name, "")] = time.time() + _DEDUP_INTERVAL
                            # 发射新插件标记，走 _reload_new_plugin 增量路径
                            # 只扫描这一个插件目录，不触发全量 rescan
                            self._hot_reload_requested.emit(self._NEW_PLUGIN_SENTINEL, new_name)
                        else:
                            logger.info(
                                f"[ChatBackend] 文件变更无法识别所属插件，触发全量重扫: "
                                f"{relevant_changes[0][1]}"
                            )
                            # 注意：此处不调用 _rebuild_prefixes()，因为 pm.rescan() 还没执行
                            # 路径重建改在 _on_hot_reload_requested 重载完成后进行
                            self._hot_reload_requested.emit("", "")
            except Exception as e:
                logger.error(f"[ChatBackend] watchfiles 监听异常退出: {e}")

        t = threading.Thread(target=_watch_loop, daemon=True, name="plugin-watcher")
        t.start()

    def _build_plugin_path_index(self) -> Dict[str, str]:
        """构建插件路径前缀 → 插件名的映射表

        前缀不带尾部分隔符，匹配时同时支持目录本身和目录内文件。
        Returns:
            {小写路径: 插件名}
        """
        from app.core.plugin_manager import PluginManager
        pm = PluginManager.get_instance()
        prefixes = {}
        for plugin in pm.list_plugins():
            path = str(plugin.path.resolve()).lower().rstrip('\\/')
            prefixes[path] = plugin.name
        return prefixes

    def _identify_plugin_from_changes(
        self, changes: list, plugin_prefixes: Dict[str, str]
    ) -> Optional[str]:
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

    def _identify_component_from_changes(
        self, changes: list, plugin_prefixes: Dict[str, str], plugin_name: str
    ) -> str:
        """从变更文件路径识别所属组件子目录

        Args:
            changes: [(Change, path_str), ...]
            plugin_prefixes: 插件路径 → 插件名 映射
            plugin_name: 已识别出的插件名

        Returns:
            "agents" | "hooks" | "commands" | "themes" | "skills" | "mcp" | "" (根目录/无法确定)
        """
        # 找到该插件的路径前缀
        plugin_path = None
        for path, name in plugin_prefixes.items():
            if name == plugin_name:
                plugin_path = path
                break
        if not plugin_path:
            return ""

        KNOWN_COMPONENTS = {"agents", "hooks", "commands", "themes", "skills", "mcp", "lsp"}
        # 插件根目录的关键文件 → 映射到对应组件
        ROOT_FILE_COMPONENTS = {
            ".mcp.json": "mcp",
            ".lsp.json": "lsp",
        }

        for _, change_path in changes:
            cp = change_path.lower()
            if cp == plugin_path:
                continue  # 插件根目录本身变更，全量
            if cp.startswith(plugin_path + os.sep):
                rel = cp[len(plugin_path) + 1:]  # 去掉 "plugin_path\"
                first_seg = rel.split(os.sep)[0] if os.sep in rel else rel
                if first_seg in KNOWN_COMPONENTS:
                    return first_seg
                # 根目录的关键文件（如 .mcp.json）映射到对应组件
                if first_seg in ROOT_FILE_COMPONENTS:
                    return ROOT_FILE_COMPONENTS[first_seg]
        return ""

    def _on_hot_reload_requested(self, plugin_name: str, component: str):
        """主线程中执行的插件热更新

        Args:
            plugin_name: 插件名
                - "" (空) → 全量重载（走 reload_plugin_subsystems）
                - _NEW_PLUGIN_SENTINEL → 新增插件（component 参数存储插件名）
                - 其他 → 已知插件的增量重载（component 为具体组件名或 "" 表示根目录变更）
            component: 组件名（"" 表示该插件的全部组件，否则为 agents/hooks/commands/themes/skills/mcp/lsp）
        """
        try:
            if plugin_name == self._NEW_PLUGIN_SENTINEL:
                # 新增插件：只扫描这一个插件目录，增量加载其组件
                result = self._reload_new_plugin(component)
            elif plugin_name:
                result = self._reload_single_plugin(plugin_name, component)
            else:
                result = self.reload_plugin_subsystems()
            self.plugin_changed.emit(result)

            # 重载完成后重建 watchfiles 路径索引，确保新注册的插件路径可被后续变更识别
            # 注意：不能提前重建（在 _watch_loop 的 else 分支），因为那时 pm.rescan() 还没执行
            self._rebuild_watcher_prefixes()
        except Exception as e:
            logger.error(f"[ChatBackend] 插件热更新失败: {e}")

    def _rebuild_watcher_prefixes(self):
        """重建 watchfiles 线程的插件路径索引（主线程调用）"""
        prefixes_ref = getattr(self, '_watcher_prefixes_ref', None)
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
            {"agents": int, "commands": bool, "hooks": bool, "themes": bool,
             "skills": bool, "mcp": bool, "lsp": bool}
        """
        result: dict = {"agents": 0, "commands": False, "hooks": False, "themes": False,
                        "skills": False, "mcp": False, "lsp": False}

        try:
            from app.core.plugin_manager import PluginManager
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
                    from app.core.builtin_commands import reload_all_commands
                    reload_all_commands()
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
            result["skills"] = bool(comps.get("skills"))
            result["mcp"] = bool(comps.get("mcp"))

            # 6. LSP：增量注册，不重启已有服务器
            if comps.get("lsp"):
                try:
                    from app.core.lsp.lsp_manager import LspManager
                    lsp_mgr = LspManager.get_instance()
                    lsp_config = pm.get_plugin_lsp_config(plugin_name)
                    if lsp_config:
                        count = lsp_mgr.add_plugin_servers(plugin_name, lsp_config["config"])
                        result["lsp"] = count > 0
                    logger.info(
                        f"[ChatBackend] Plugin '{plugin_name}' LSP 增量加载完成"
                    )
                except Exception as e:
                    logger.error(f"[ChatBackend] Plugin '{plugin_name}' LSP 增量加载失败: {e}")

            logger.info(f"[ChatBackend] 新插件增量加载「{plugin_name}」完成: "
                       f"agents={result['agents']}, commands={result['commands']}, "
                       f"themes={result['themes']}, skills={result['skills']}, "
                       f"mcp={result['mcp']}, lsp={result['lsp']}")
        except Exception as e:
            logger.error(f"[ChatBackend] Failed to reload new plugin '{plugin_name}': {e}")

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
        - ""         → 跳过（根目录文件变更如 README/LICENSE，不影响运行时）

        Args:
            plugin_name: 插件名称
            component: 变更的组件名

        Returns:
            {"agents": int, "commands": bool, "themes": bool, "skills": bool, "mcp": bool}
        """
        result: dict = {"agents": 0, "commands": False, "hooks": False, "themes": False,
                        "skills": False, "mcp": False, "lsp": False}

        try:
            from app.core.plugin_manager import PluginManager
            pm = PluginManager.get_instance()
            if not pm.is_initialized():
                logger.warning("[ChatBackend] PluginManager not initialized, cannot reload")
                return result

            # 1. 只重新扫描该插件目录
            pm.rescan_plugin(plugin_name)

            plugin = pm.get_plugin(plugin_name)
            if not plugin:
                # 插件已被删除（目录或 manifest 已不存在）
                # 清理该插件在各子系统中的残留数据
                logger.info(f"[ChatBackend] Plugin '{plugin_name}' removed, cleaning up artifacts...")
                if self._agent_manager:
                    self._agent_manager.cleanup_plugin_artifacts(plugin_name)
                try:
                    from app.core.builtin_commands import reload_all_commands
                    reload_all_commands()
                    result["commands"] = True
                except (ImportError, Exception) as e:
                    logger.error(f"[ChatBackend] Failed to reload commands after plugin removal: {e}")
                try:
                    from app.utils.config import update_theme_options
                    from app.utils.theme_manager import theme_manager
                    theme_manager.reload()
                    update_theme_options()
                    result["themes"] = True
                except (ImportError, Exception) as e:
                    logger.error(f"[ChatBackend] Failed to reload themes after plugin removal: {e}")
                # 技能和 MCP：PluginManager 已移除该插件的目录，
                # UI 通过 get_local_skills() / get_mcp_servers() 懒加载，下次访问时自动排除
                result["hooks"] = True
                result["skills"] = True
                result["mcp"] = True

                # LSP：重新初始化 LspManager（停止旧服务 → 仅加载剩余插件的新配置 → 启动新服务）
                try:
                    from app.core.lsp.lsp_manager import LspManager
                    lsp_mgr = LspManager.get_instance()
                    lsp_configs = pm.get_lsp_configs()
                    workdir = os.getcwd()
                    if self._tool_executor and getattr(self._tool_executor, '_workdir', None):
                        workdir = str(self._tool_executor._workdir)
                    lsp_mgr.initialize(workdir, lsp_configs)
                    lsp_mgr.start_all_background()
                    result["lsp"] = True
                    logger.info(
                        f"[ChatBackend] Plugin '{plugin_name}' LSP 已清理，"
                        f"剩余 {len(lsp_mgr._clients)} 个服务器"
                    )
                except Exception as e:
                    logger.error(f"[ChatBackend] Plugin '{plugin_name}' LSP 清理失败: {e}")

                return result

            # 2. 智能体：仅当变更在 agents/ 目录（含 hooks 重载一并完成）
            if component == "agents" and self._agent_manager:
                result["agents"] = self._agent_manager.reload_plugin_agents(plugin_name)
                result["hooks"] = True  # agents 组件包含 hooks 重载
                # 智能体变更后同步重载命令注册（agent 文件同时也是 /agent_name 命令源）
                # 确保 CommandManager 中的 agent 命令同步更新，否则 /silent-failure-hunter 等命令无法识别
                try:
                    from app.core.builtin_commands import reload_all_commands
                    reload_all_commands()
                    result["commands"] = True
                    logger.debug(f"[ChatBackend] Commands reloaded after agent change for plugin: {plugin_name}")
                except (ImportError, Exception) as e:
                    logger.error(f"[ChatBackend] Failed to reload commands after agent change: {e}")

            # 3. Hooks：仅当变更在 hooks/ 目录（只重载 hooks，不碰 agents）
            if component == "hooks" and self._agent_manager:
                self._agent_manager.reload_plugin_hooks(plugin_name)
                result["hooks"] = True
                logger.debug(f"[ChatBackend] Hooks reloaded for plugin: {plugin_name}")

            # 4. 命令：仅变更在 commands 目录才触发
            if component == "commands" and plugin.has_component("commands"):
                try:
                    from app.core.builtin_commands import reload_all_commands
                    reload_all_commands()
                    result["commands"] = True
                except (ImportError, Exception) as e:
                    logger.error(f"[ChatBackend] Failed to reload commands: {e}")

            # 5. 主题：watchfiles 已通过路径识别为 themes 组件变更
            #    不依赖 plugin.has_component("themes")，因为：
            #    - .drifox-plugin 格式 rescan 不会自动检测新增的 themes 目录
            #    - 实际文件在 themes/ 下变更，theme_manager 必须 reload 才能反映新主题
            if component == "themes":
                try:
                    from app.utils.config import update_theme_options
                    from app.utils.theme_manager import theme_manager
                    theme_manager.reload()
                    update_theme_options()
                    result["themes"] = True
                    logger.debug(f"[ChatBackend] Themes reloaded for plugin: {plugin_name}")
                except (ImportError, Exception) as e:
                    logger.error(f"[ChatBackend] Failed to reload themes: {e}")

            # 6. 技能：PluginManager 已在 rescan_plugin 中更新
            #    UI 通过 get_local_skills() 懒加载，下次打开命令面板时自动生效
            if component == "skills":
                result["skills"] = True
                logger.debug(f"[ChatBackend] Plugin '{plugin_name}' skills reloaded (lazy)")

            # 7. MCP 配置：PluginManager 已在 rescan_plugin 中更新
            #    MCP 设置面板通过 pm.get_mcp_servers() 读取，下次刷新时自动生效
            if component == "mcp":
                result["mcp"] = True
                logger.debug(f"[ChatBackend] Plugin '{plugin_name}' MCP config reloaded (lazy)")

            # 8. LSP 配置：热重载 LSP 服务器
            #    使用增量 API：先移除该插件旧的所有 LSP 服务器，再注册新的
            #    不走 LspManager.initialize 全量重建路径
            if component == "lsp":
                try:
                    from app.core.lsp.lsp_manager import LspManager
                    lsp_mgr = LspManager.get_instance()
                    # 先移除旧服务器（如果 .lsp.json 被删除也需要清理）
                    lsp_mgr.remove_plugin_servers(plugin_name)
                    # 再注册并启动新服务器（如果 .lsp.json 还存在）
                    lsp_config = pm.get_plugin_lsp_config(plugin_name)
                    if lsp_config:
                        count = lsp_mgr.add_plugin_servers(plugin_name, lsp_config["config"])
                        result["lsp"] = count > 0
                    logger.info(
                        f"[ChatBackend] Plugin '{plugin_name}' LSP 增量重载完成"
                    )
                except Exception as e:
                    logger.error(f"[ChatBackend] Plugin '{plugin_name}' LSP 增量重载失败: {e}")

            logger.info(f"[ChatBackend] Plugin [{plugin_name}] reloaded: "
                       f"agents={result['agents']}, commands={result['commands']}, "
                       f"themes={result['themes']}, skills={result['skills']}, "
                       f"mcp={result['mcp']}, lsp={result.get('lsp', False)}")
        except Exception as e:
            logger.error(f"[ChatBackend] Failed to reload plugin '{plugin_name}': {e}")

        return result

    def reload_plugin_subsystems(self) -> dict:
        """运行时重载所有插件子系统

        当插件启用/禁用或新增/删除后调用，统一触发所有子系统的重载。
        由 main_widget 或设置面板中的"应用"操作触发。

        Returns:
            {"agents": int, "commands": bool, "hooks": bool, "themes": bool,
             "skills": bool, "mcp": bool}
            各子系统的重载结果
        """
        result: dict = {"agents": 0, "commands": False, "hooks": False, "themes": False,
                        "skills": False, "mcp": False, "lsp": False}

        try:
            from app.core.plugin_manager import PluginManager
            pm = PluginManager.get_instance()
            if not pm.is_initialized():
                logger.warning("[ChatBackend] PluginManager not initialized, cannot reload")
                return result

            # 1. 重新扫描插件目录，获取变更详情
            diff = pm.rescan()
            added = diff.get("added", [])
            removed = diff.get("removed", [])
            changed = diff.get("changed", [])

            # 2. 判断是否为"单一新增"——只有新增一个插件，无移除/变更
            #    watchfiles 检测到新插件目录时，路径索引尚未包含它，
            #    会触发全量重扫，但实际只需加载这一个新插件
            is_single_addition = len(added) == 1 and not removed and not changed

            if is_single_addition:
                # ── 增量重载：只加载新增插件的组件 ──
                plugin = added[0]
                name = plugin.name
                comps = plugin.components  # {"agents": True, "commands": True, ...}

                logger.info(f"[ChatBackend] 检测到新插件「{name}」，执行增量重载")

                # 智能体 + hooks（agents 组件同时处理 hooks）
                if comps.get("agents") and self._agent_manager:
                    result["agents"] = self._agent_manager.reload_plugin_agents(name)
                    result["hooks"] = True  # agents 组件包含 hooks 重载
                    # 智能体文件同时也是命令源（/agent_name）
                    try:
                        from app.core.builtin_commands import reload_all_commands
                        reload_all_commands()
                        result["commands"] = True
                    except (ImportError, Exception) as e:
                        logger.error(f"[ChatBackend] Failed to reload commands after agent change: {e}")

                # hooks-only（没有 agents 但有 hooks）
                if comps.get("hooks") and not comps.get("agents") and self._agent_manager:
                    self._agent_manager.reload_plugin_hooks(name)
                    result["hooks"] = True

                # 命令（非 agents 触发的独立命令目录）
                if comps.get("commands") and not result["commands"]:
                    try:
                        from app.core.builtin_commands import reload_all_commands
                        reload_all_commands()
                        result["commands"] = True
                    except (ImportError, Exception) as e:
                        logger.error(f"[ChatBackend] Failed to reload commands: {e}")

                # 主题
                if comps.get("themes"):
                    try:
                        from app.utils.config import update_theme_options
                        from app.utils.theme_manager import theme_manager
                        theme_manager.reload()
                        update_theme_options()
                        result["themes"] = True
                    except (ImportError, Exception) as e:
                        logger.error(f"[ChatBackend] Failed to reload themes: {e}")

                # 技能：PluginManager 已更新，UI 懒加载
                result["skills"] = bool(comps.get("skills"))

                # MCP：PluginManager 已更新，UI 懒加载
                result["mcp"] = bool(comps.get("mcp"))

                # LSP：新增插件带 lsp 组件时，使用增量 API 只注册该新增插件的服务器
                # 不重启已有服务器，不走 LspManager.initialize 全量重建路径
                if comps.get("lsp"):
                    try:
                        from app.core.lsp.lsp_manager import LspManager
                        lsp_mgr = LspManager.get_instance()
                        lsp_config = pm.get_plugin_lsp_config(name)
                        if lsp_config:
                            count = lsp_mgr.add_plugin_servers(name, lsp_config["config"])
                            result["lsp"] = count > 0
                        logger.info(
                            f"[ChatBackend] Plugin '{name}' LSP 增量加载完成"
                        )
                    except Exception as e:
                        logger.error(f"[ChatBackend] Plugin '{name}' LSP 增量加载失败: {e}")

                logger.info(f"[ChatBackend] 增量重载「{name}」完成: "
                           f"agents={result['agents']}, commands={result['commands']}, "
                           f"themes={result['themes']}, skills={result['skills']}, "
                           f"mcp={result['mcp']}, lsp={result['lsp']}")
                return result

            # ── 全量重载（多插件变更/移除/覆盖，或非 watchfiles 触发） ──
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
                from app.core.lsp.lsp_manager import LspManager
                lsp_mgr = LspManager.get_instance()
                lsp_configs = pm.get_lsp_configs()
                workdir = os.getcwd()
                if self._tool_executor and getattr(self._tool_executor, '_workdir', None):
                    workdir = str(self._tool_executor._workdir)
                lsp_mgr.initialize(workdir, lsp_configs)
                lsp_mgr.start_all_background()
                result["lsp"] = True
                logger.info(
                    f"[ChatBackend] LSP 全量重载完成，"
                    f"已注册 {len(lsp_mgr._clients)} 个服务器"
                )
            except Exception as e:
                logger.error(f"[ChatBackend] LSP 全量重载失败: {e}")

            logger.info(f"[ChatBackend] Plugin subsystems reloaded: agents={result['agents']}, "
                       f"commands={result['commands']}, themes={result['themes']}, "
                       f"skills={result['skills']}, mcp={result['mcp']}, lsp={result.get('lsp', False)}")
        except Exception as e:
            logger.error(f"[ChatBackend] Failed to reload plugin subsystems: {e}")

        return result

    # ========== MCP 自动发现 ==========

    def _discover_mcp_servers(self):
        """自动发现其他工具的 MCP 配置并保存到 user-custom 插件（仅首次运行生效）"""
        from app.core.plugin_manager import PluginManager
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
        from app.core.plugin_manager import PluginManager
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
                f"[ChatBackend] MCP 后台连接完成: {ok}/{total}"
                + (f", 失败: {failed}" if failed else "")
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

        # 4. 清除 SubAgentManager 回调引用
        if self._sub_agent_manager:
            self._sub_agent_manager = None

        # 5. 清除 SessionManager（窗口独有的会话）
        self._session_manager = None

        # 6. 清除 UI 有效性标志
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
        return getattr(self, '_last_cache_stats', None)

    def set_last_cache_stats(self, stats: Dict):
        """保存最后一次的缓存统计"""
        self._last_cache_stats = stats


    def get_context_usage_snapshot(self, session, llm_config) -> Dict:
        """获取上下文使用快照"""
        if self._chat_engine:
            return self._chat_engine.get_context_usage_snapshot(session, llm_config)
        return {}

    def switch_agent(self, agent_name: str):
        """切换 Agent"""
        if self._chat_engine:
            self._chat_engine.switch_agent(agent_name)

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
            return getattr(self._tool_executor, 'file_recorder', None)
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
            self._memory_manager.add_entry_memory(content, kwargs.get('source', 'assistant'))

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
            wd_path = self._tool_executor.get_workdir() if self._tool_executor else os.getcwd()
            docs = self._memory_manager.get_key_documents(self._current_project)[:50]
            if docs:
                doc_items = []
                for doc in docs:
                    file_path = doc.get("file_path", "")
                    file_name = doc.get("file_name", "")
                    is_url = file_path and (file_path.startswith('http://') or file_path.startswith('https://'))
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
                    doc_items.append({
                        "file_name": file_name,
                        "display": display,
                        "is_url": is_url,
                        "is_wd": is_wd,
                    })
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
            包含 worktree 信息和路径建议的 dict
        """
        project_root = self._tool_executor.get_workdir() if self._tool_executor else os.getcwd()

        ctx: Dict[str, Any] = {
            "project_root": project_root,
            "project_name": self._current_project or os.path.basename(project_root),
        }

        # Worktree / git 分支信息
        try:
            from app.utils.git_worktree import GitWorktreeDetector
            repo_info = GitWorktreeDetector.get_repo_info(project_root)
            if repo_info and repo_info.worktrees:
                ctx["worktree"] = {
                    "repo_name": os.path.basename(repo_info.root),
                    "current_branch": repo_info.current_branch,
                    "workdir": project_root,
                    "is_worktree": project_root != repo_info.root,
                    "other_branches": [
                        wt.branch for wt in repo_info.worktrees if not wt.is_current
                    ],
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
        project_root = self._tool_executor.get_workdir() if self._tool_executor else os.getcwd()
        ctx: Dict[str, Any] = {
            "project_root": project_root,
            "state": state,
            "project_name": os.path.basename(project_root),
        }

        # 项目笔记（AGENTS.md 内容）
        if self._memory_manager:
            try:
                note = self._memory_manager.get_project_note(
                    self._current_project,
                    workdir=project_root,
                )
                if note and note.get("content"):
                    ctx["project_notes_content"] = note["content"]
            except Exception:
                pass

        # Worktree / git 分支信息
        try:
            from app.utils.git_worktree import GitWorktreeDetector
            repo_info = GitWorktreeDetector.get_repo_info(project_root)
            if repo_info and repo_info.worktrees:
                ctx["worktree"] = {
                    "repo_name": os.path.basename(repo_info.root),
                    "current_branch": repo_info.current_branch,
                    "workdir": project_root,
                    "is_worktree": project_root != repo_info.root,
                    "other_branches": [
                        wt.branch for wt in repo_info.worktrees if not wt.is_current
                    ],
                }
        except Exception:
            pass

        return ctx

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
            results = self._hook_manager.trigger_event(
                "SessionStart",
                context=context,
                current_message="",
                trigger_async=False,  # 同步执行，收集输出直接注入
            )
            for r in results:
                if r.success and r.output:
                    _inject_hook_to_session(session, "SessionStart", r.output)
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
        results = self._hook_manager.trigger_event(
            "SessionStart", context=ctx, current_message="",
            trigger_async=False,  # 同步执行，收集输出直接注入
        )
        if session:
            for r in results:
                if r.success and r.output:
                    _inject_hook_to_session(session, "SessionStart", r.output)
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
        result = self._session_manager.delete_session(index)
        if result:
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
            logger.error(f"[Gateway] Processing error: {e}", exc_info=True)
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
                    session_id: str,
                    text: str,
                    platform: Any,
                    chat_id: str,
                    user_id: str,
                    **kwargs
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
        self,
        session_id: str,
        text: str,
        platform: Any,
        chat_id: str,
        user_id: str,
        **kwargs
    ) -> str:
        """
        处理 Gateway 消息 - 调用 AI

        先发送"思考中"提示，然后等待 AI 结果。
        """
        logger.info(f"[Gateway] Processing message from {platform.value}:{user_id}: {text[:50]}...")

        # 先发送"思考中"占位回复
        await self._gateway_send_message(
            platform, chat_id, "🤔 正在思考，请稍候..."
        )

        # 用 signal 发送到主线程
        import concurrent.futures
        future = concurrent.futures.Future()

        self.gateway_input_received.emit({
            "text": text,
            "chat_id": chat_id,
            "user_id": user_id,
            "platform": platform.value,
            "future": future,
        })

        try:
            # 异步等待 AI 结果（不超时，AI 回复多久等多久）
            response = await asyncio.wrap_future(future)

            # 注意：on_stream_finished 回调已通过 _push_to_platform 发送了最终回复
            # 所以这里返回空字符串，避免 message_handler 重复发送
            return ""
        except Exception as e:
            logger.error(f"[Gateway] AI processing error: {e}")
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
