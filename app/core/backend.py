# -*- coding: utf-8 -*-
"""
ChatBackend - 统一后端接口
后端自己创建和管理所有组件，前端只负责 UI 调用
"""
import asyncio
import os
import time

import orjson as json
from pathlib import Path
from typing import Dict, List, Any, Optional, Callable

from PyQt5.QtCore import QObject, pyqtSignal, QThreadPool
from loguru import logger

from app.core.store import SessionStore
from app.core.gateway_engine import GatewayEngine
from app.core.agent import AgentManager
from app.core.chat_engine import ChatEngine
from app.core.chat_session import SessionManager, ChatSession
from app.core.memory_manager import MemoryManagerCore
from app.core.hook_manager import HookManager
from app.core.tool_executor import ToolExecutor
from app.utils.history_manager import HistoryManager
from app.utils.utils import get_app_data_dir


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
        
        # 2. 创建 MemoryManager
        self._memory_manager = MemoryManagerCore()
        logger.info("[ChatBackend] MemoryManager 创建完成")
        
        # 3. 创建 HookManager（必须在 create_session 之前）
        self._hook_manager = HookManager(self._thread_pool)
        # UI 有效性标志：当 UI 窗口关闭时应设为 False，防止 hook 回调访问已销毁的 UI
        self._ui_valid = True
        
        # Hook 完成后，把输出添加到上下文
        def on_hook_finished(event_name: str, output: str, success: bool):
            # 检查 UI 是否仍然有效，防止窗口关闭后 hook 回调访问已销毁的 UI
            if not getattr(self, '_ui_valid', True):
                logger.debug(f"[HookManager] Hook callback skipped: UI already closed")
                return
            
            logger.info(f"[HookManager] Hook callback: event={event_name}, success={success}，output={output[:100]}...")
            
            # 只有成功执行的 hook 才添加到消息列表
            if not success:
                return
            
            hook_output = f"<hook event=\"{event_name}\">\n{output}\n</hook>"
            
            # SessionStart 和 PreUserMessage 添加到消息列表
            add_to_messages = event_name in ("SessionStart", "PreUserMessage", "PostUserMessage")
            
            if add_to_messages:
                session = self.get_current_session()
                if session:
                    # 对于 PreUserMessage，先删除之前的同类 hook 消息，只保留最新一个
                    if event_name == "PreUserMessage":
                        session.messages = [
                            msg for msg in session.messages
                            if not (msg.get("role") == "assistant" and "<hook " in (msg.get("content") or "") and 'event="PreUserMessage"' in (msg.get("content") or ""))
                        ]
                    
                    # 添加新消息
                    session.add_assistant_message(hook_output)
                
                # 发送消息给前端显示（仅在 UI 有效时发送，防止窗口关闭后 emit 导致 segfault）
                if getattr(self, '_ui_valid', True):
                    self.message_received.emit({
                        "role": "assistant",
                        "content": hook_output
                    })
                logger.info(f"[HookManager] Hook added to messages: {event_name}")
        self._hook_manager.set_on_finished_callback(on_hook_finished)
        
        # 4. 创建初始会话（不触发 SessionStart hook，避免重复初始化）
        self.create_session(trigger_hook=False)
        
        # 5. 使用传入的 AgentManager 或创建新的
        self._agent_manager = AgentManager(str(Path(__file__).parent.parent / "agents"), self._hook_manager)
        logger.info(f"[ChatBackend] AgentManager 就绪，{len(self._agent_manager.list_agents())} 个 Agent")
        
        # 加载 .drifox 全局 hooks
        global_hooks_file = get_app_data_dir() / "hooks" / "hooks.json"
        if global_hooks_file.exists():
            try:
                with open(global_hooks_file, 'r', encoding='utf-8') as f:
                    config = json.loads(f.read())
                skill_root = str(global_hooks_file.parent)
                count = self._hook_manager.register_hooks_from_json("__global__", skill_root, config, str(global_hooks_file))
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

        # 创建 GatewayEngine（全局单例，多个窗口共享）
        self._gateway_engine = GatewayEngine.get_instance(
            get_model_config=get_model_config,
            tool_executor=self._tool_executor,
            agent_manager=self._agent_manager,
            session_store=self._session_store,
        )
        logger.info("[ChatBackend] GatewayEngine 创建完成")
        
        self._get_memory_context_getter = None

        self._history_manager = HistoryManager()
        
        # 7. 自动发现并合并其他来源的 MCP 服务器配置（仅首次）
        self._discover_mcp_servers()

        # 8. 初始化 MCP 连接
        self._init_mcp_connections()
        
        self._initialized = True
        logger.info("[ChatBackend] 初始化完成")
        
        # 连接 Gateway 信号（跨线程安全）
        self.gateway_input_received.connect(self._on_gateway_input)

        # 初始化 Gateway（后台进行，不阻塞）
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

    # ========== MCP 自动发现 ==========

    def _discover_mcp_servers(self):
        """自动发现其他工具的 MCP 配置并合并（仅首次运行生效）"""
        from app.utils.config import Settings

        cfg = Settings.get_instance()

        # 已处理过则跳过
        if cfg.mcp_discovered.value:
            return

        from app.tools.mcp_tools import discover_and_merge

        merged, new_ones = discover_and_merge()
        if new_ones:
            cfg.set(cfg.mcp_servers, merged, save=True)
            logger.info(f"[ChatBackend] MCP 自动发现完成，导入 {len(new_ones)} 个新服务器")

        # 标记已处理
        cfg.set(cfg.mcp_discovered, True, save=True)

    # ========== ChatEngine 代理方法 ==========

    def _init_mcp_connections(self):
        """初始化 MCP 服务器连接（后台异步，不阻塞 UI）"""
        from app.utils.config import Settings

        mcp_manager = self._tool_executor._builtin_tools._mcp_manager

        if mcp_manager.is_connected:
            logger.info("[ChatBackend] MCP 已连接，复用现有连接")
            return

        cfg = Settings.get_instance()
        if not cfg.mcp_enabled.value:
            logger.info("[ChatBackend] MCP 全局开关已关闭，跳过连接")
            return

        servers = cfg.mcp_servers.value
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
        """停止流式输出"""
        if self._chat_engine:
            return self._chat_engine.stop()
    
    def cleanup_worker(self):
        """清理 worker"""
        if self._chat_engine:
            self._chat_engine.cleanup_worker()
    
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
    
    def send_message_to_engine(self, text: str) -> bool:
        """发送消息到引擎"""
        if self._chat_engine:
            return self._chat_engine.send_message(text)
        return False
    
    # ========== ToolExecutor 代理方法 ==========
    
    def set_session_context(self, session_id: str):
        """设置会话上下文"""
        if self._tool_executor:
            self._tool_executor.set_session_context(session_id)
    
    def set_sub_agent_manager(self, manager):
        """设置子智能体管理器"""
        self._sub_agent_manager = manager
        if self._tool_executor:
            self._tool_executor.set_sub_agent_manager(manager)
    
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
    
    # ========== MemoryManager 代理方法 ==========
    def get_memory_context_string(self, limit: int = 8) -> str:
        """获取记忆上下文字符串
        
        Args:
            query: 搜索关键词
            limit: 条目记忆最大数量
            project: 当前项目名称
        """
        if self._memory_manager:
            return self._memory_manager.format_memories_for_prompt(
                project=self._current_project,
                entry_limit=limit,
                doc_limit=20
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
    
    def create_session(self, trigger_hook: bool = True) -> ChatSession:
        """创建新会话
        
        Args:
            trigger_hook: 是否触发 SessionStart hook。初始化时设为 False 避免重复触发。
        """
        session = self._session_manager.create_new_session()
        self.session_created.emit(session.session_id)
        
        # Trigger SessionStart hook
        if trigger_hook and self._hook_manager:
            context = {
                "project_root": os.getcwd(),
            }
            self._hook_manager.trigger_event(
                "SessionStart",
                context=context,
                current_message=""
            )
        
        return session
    
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
        """构建长期记忆上下文（供 ChatEngine 调用）"""
        if not self._memory_manager:
            return ""
        return self._memory_manager.format_memories_for_prompt(
            project=project,
            entry_limit=8,
            doc_limit=20
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
            from app.gateway.base import Platform as GatewayPlatform, MessageEvent, MessageType
            import asyncio

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
                logger.debug(f"[Gateway] _push_to_platform: content_len={len(content)}")
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
                """AI 完成 → 发送最终完整回复"""
                content = response or "".join(gateway_chunks)
                final = content or "抱歉，我没有生成有效回复，请重试。"

                logger.info(f"[Gateway] AI completed, response_len={len(response)}, final_len={len(final)}")

                # 发送最终回复（替换之前的流式预览，不重复）
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
        import asyncio
        from functools import partial

        def _do_init():
            try:
                # 延迟导入，避免主线程加载 adapter 模块（import 有阻塞风险）
                from app.gateway.config import get_gateway_config
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
                config = get_gateway_config()
                self._gateway_manager = create_platform_manager(process_message, send_message)

                self._gateway_initialized = True
                logger.info("[ChatBackend] Gateway 管理器创建完成")

                # 启动连接：纯异步调度，不等待结果（避免 WebSocket 连接慢时卡住后台线程）
                self._gateway_manager.start_all_async()
                    
            except Exception as e:
                logger.error(f"[ChatBackend] Gateway 初始化失败: {e}", exc_info=True)
        
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
