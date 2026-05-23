# -*- coding: utf-8 -*-
from __future__ import annotations

import ctypes
import gc
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional

import orjson as json
import sip
from PyQt5.QtCore import (
    QTimer,
    pyqtSignal,
    QThreadPool,
    Qt,
)
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import (
    QVBoxLayout,
    QHBoxLayout,
    QApplication,
    QWidget,
    QFileDialog, QGraphicsOpacityEffect,
    QLabel,
    QPushButton,
    QButtonGroup, QFrame, QScrollArea, QSizePolicy,
)
from loguru import logger
from qfluentwidgets import (
    setFont,
    FluentIcon,
    SingleDirectionScrollArea,
    TransparentToolButton, InfoBar, InfoBarPosition, )

from app.constants import (
    FREE_PROVIDERS,
    PROVIDER_ICONS,
    PROVIDER_MODELS,
)
from app.core import (
    ChatBackend,
    ChatSession,
    consolidate_messages,
    content_to_text,
    group_messages_for_display,
    get_user_round_ranges,
    TopicSummaryTask,
)
from app.tool_popup import ToolWindow
from app.utils.config import Settings, update_theme_options
from app.utils.design_tokens import (
    Colors,
    font_size_css,
    scale_font_size,
    apply_font_size_to_widget,
)
from app.utils.theme_manager import theme_manager
from app.utils.utils import get_icon, get_font_family_css
from app.widgets.balance_display import BalanceDisplay
from app.widgets.cards.settings.base_settings_card import (
    BaseSettingsCard,
)
from app.widgets.bottom_input_area import (
    SendableTextEdit,
)
from app.widgets.context_usage_ring import (
    ContextUsageRing,
)
from app.widgets.conversation_node_preview import (
    ConversationNodePreview,
)
from app.widgets.file_undo_dialog import (
    FileUndoPreviewDialog,
)
from app.widgets.cards.settings.history_card import (
    HistoryCard,
    get_message_preview,
)
from app.widgets.cards.settings.hook_setting_card import HookEditCard
from app.widgets.cards.settings.llm_settings_card import (
    LLMSettingsCard,
)
from app.widgets.cards.settings.mcp_setting_card import (
    MCPEditCard,
)
from app.widgets.cards.settings.memory_card import (
    MemoryCardContent, TAB_PROJECT_NOTES,
)
from app.widgets.message_card import (
    MessageCard,
    create_welcome_card,
)
from app.widgets.cards.settings.model_config_card import (
    ModelConfigCard,
)
from app.widgets.project_selector_popup import ProjectSelectorPopup
from app.widgets.cards.floating.question_floating_widget import (
    QuestionFloatingWidget,
)
from app.widgets.cards.floating.sub_agent_floating_widget import (
    SubAgentFloatingWidget,
)
from app.widgets.cards.settings.system_card_frame import SystemCardFrame
from app.widgets.cards import CardManager, ContainerType, TopCardContainer, BottomCardContainer
from app.widgets.cards.floating.todo_floating_widget import (
    TodoFloatingWidget,
)
from app.widgets.cards.floating.tool_floating_widget import (
    ToolFloatingWidget,
)
from app.widgets.ui_helpers import *
from app.widgets.ui_helpers import add_message_to_layout, refresh_history_card_if_visible, \
    init_new_session_after_archive, clear_and_show_welcome, refresh_session_view, save_or_archive_session, \
    invalidate_session_card_cache, delete_widgets_from_layout, init_after_loading_session, setup_user_card_signals, \
    post_append_user_message, create_assistant_card_widget, scroll_to_bottom_if_streaming, \
    build_node_preview_from_session, truncate_and_remove_round, \
    log_deletion_stats, restore_input_from_card, find_last_tool_call_id_after_round, get_first_file_operation, \
    show_diff_viewer, render_batch_to_assistant_card, find_user_round_index


class OpenAIChatToolWindow(ToolWindow):
    name = "飘狐 DriFox"
    icon = get_icon("drifox")
    session_manager = None
    _valid_configs: Dict[str, Dict[str, Any]] = {}
    history_manager = None
    _current_agent: str = "build"
    _current_session_id: Optional[str] = None
    _settings_popup = None
    _is_welcome = False
    _is_searching: bool = False
    _search_results: List[int] = []
    _current_search_index: int = -1
    _is_continuing: bool = False
    _processed_tool_ids: set = set()
    _current_assistant_card = None
    _tool_call_depth: int = 0
    _pending_tool_calls: int = 0
    _first_tool_result: bool = True
    _tool_cancelled_by_user: bool = False
    _cancelled_tool_call_id: Optional[str] = None
    _todo_floating_widget = None
    _question_floating_widget = None
    _question_tool_call_id = None
    _todo_was_visible_before_system: bool = False  # 打开系统卡片前todo的可见状态
    _is_system_card_visible: bool = False  # 当前是否有系统卡片显示
    _window_active: bool = True
    # AutoLoop 状态
    _is_auto_loop_running: bool = False
    _auto_loop_config_card: Optional[AutoLoopConfigCard] = None
    _auto_loop_running_card: Optional[AutoLoopRunningCard] = None
    _auto_loop_worker: Optional[AutoLoopWorker] = None
    _history_preview_messages: Optional[List[dict]] = None
    _history_preview_title: str = ""
    insertResponse = pyqtSignal(str)
    createResponse = pyqtSignal(str)
    contextActionRequested = pyqtSignal(str, str)
    skillExecutionRequested = pyqtSignal(str, dict)
    userInterventionRequested = pyqtSignal(dict)
    executionResultProduced = pyqtSignal(str)
    toolStartUiSyncRequested = pyqtSignal(str, str, object, str)

    def __init__(self, homepage, button):
        # 调用父类（会触发 setup_ui -> _create_agent_switch_buttons）
        super().__init__(homepage)
        # 需要在 super().__init__() 之前初始化所有依赖项
        self.homepage = homepage  # 必须在 super() 之前设置，供 backend.initialize 使用
        self.cfg = Settings.get_instance()
        # 初始化当前项目（在 backend.initialize 之前）
        self._current_project = self.cfg.current_project.value or "默认项目"  # 当前项目
        # 多窗口隔离：实例级工作目录缓存（{project: workdir_path}）
        # 优先级：实例缓存 > DB；DB 写入仅作为新窗口的默认恢复值
        self._current_workdir: Dict[str, str] = {}
        # 标记窗口是否已销毁，防止异步回调访问已销毁的 widget
        # 多窗口隔离：实例级模型配置缓存（必须覆盖类变量，防止多窗口共享）
        self._valid_configs: Dict[str, Dict[str, Any]] = {}
        self._is_destroyed = False
        # 创建后端（后端自己创建所有组件）- 需要在 super() 之前创建并初始化
        # 因为 setup_ui() 中会用到 self.backend.get_primary_agents()
        self.backend = ChatBackend()
        self.backend.initialize(
            get_model_config=self._get_current_model_config,
            workdir=str(Path(__file__).parent.parent.parent),
        )
        self.backend._current_project = self._current_project
        # 同步项目到 tool_executor，确保 BuiltinTools.edit_project_note 等工具使用正确项目名
        if self.backend.tool_executor:
            self.backend.tool_executor.set_current_project(self._current_project)
        # 从后端获取组件（前端只负责 UI 逻辑）
        self.history_manager = self.backend.history_manager
        self.session_store = self.backend.session_store
        self.session_manager = self.backend.session_manager
        self._session_card_cache: Dict[str, Dict[str, Any]] = {}
        self._current_history_project: Optional[str] = None  # 当前历史面板项目过滤
        self._welcome_card_cache: Dict[str, MessageCard] = {}
        self._displayed_session_id: Optional[str] = None
        self._initial_visible_batch_count = 12
        self._incremental_visible_batch_count = 8
        self._history_load_threshold = 48
        # 虚拟滚动：可见范围外前后保留多少个增量批次（缓冲区）
        self._virtual_scroll_buffer = 2
        self._message_batch: List[List[Dict[str, Any]]] = []
        # 存储每个batch对应的UI卡片：None表示已回收（只存数据不存UI）
        self._batch_cards: List[Optional[List[MessageCard]]] = []
        # 前缀和缓存：_user_prefix[i] = 前 i 个 batch 中有多少个 user（用于 O(1) 的 round_index 计算）
        self._user_prefix_cache: List[int] = []
        self._visible_batch_start = 0
        self._visible_batch_end = 0
        self._is_loading_history_batches = False
        self._is_virtual_recycling = False
        # 滚动停止后延迟回收
        self._virtual_scroll_timer = QTimer(self)
        self._virtual_scroll_timer.setSingleShot(True)
        self._virtual_scroll_timer.setInterval(300)
        self._virtual_scroll_timer.timeout.connect(self._recycle_out_of_view_batches)
        self._suspend_auto_scroll = False
        self._gen_thread_pool = QThreadPool()
        self._gen_thread_pool.setMaxThreadCount(2)
        self._pending_scroll_to_bottom = False
        self._bottom_anchor_deadline = 0.0
        self._last_visible_user_pair_index = -1
        self._scroll_bottom_timer = QTimer(self)
        self._scroll_bottom_timer.setSingleShot(True)
        self._scroll_bottom_timer.setInterval(24)
        self._scroll_bottom_timer.timeout.connect(self._do_scroll_to_bottom)
        self._bottom_anchor_timer = QTimer(self)
        self._bottom_anchor_timer.setSingleShot(True)
        self._bottom_anchor_timer.setInterval(80)
        self._bottom_anchor_timer.timeout.connect(self._maintain_bottom_anchor)
        self._suppress_scroll_sync_count = 0  # 加载历史时抑制滚动同步的计数器
        self._loading_session = False  # 加载会话标志，用于懒渲染期间保持滚动位置
        self._initial_scroll_to_bottom = False  # 首次滚底标记：只在首次强制滚底，后续只有用户在底部才继续
        self._user_intentionally_away_from_bottom = False  # 用户主动滚上去标记
        self._pending_lazy_cards: List[MessageCard] = []  # 待处理的懒渲染卡片队列
        self._lazy_card_timer_active = False  # 懒渲染定时器是否已激活，防止重复调度
        # resize 防抖定时器 - 性能优化：增加防抖时间减少卡顿
        self._resize_debounce_timer = QTimer(self)
        self._resize_debounce_timer.setSingleShot(True)
        self._resize_debounce_timer.setInterval(30)  # 30ms 防抖，及时响应 resize
        self._resize_debounce_timer.timeout.connect(self._do_debounced_resize)
        # resize 完成后更新所有卡片的定时器（延迟更新非可见区域卡片）
        self._resize_complete_timer = QTimer(self)
        self._resize_complete_timer.setSingleShot(True)
        self._resize_complete_timer.setInterval(100)  # resize 结束后尽快恢复真实内容
        self._resize_complete_timer.timeout.connect(self._sync_all_cards_width)
        self._pending_resize_sync = False
        self._resize_preview_active = False
        self._last_chat_viewport_width = 0
        self._scroll_sync_timer = QTimer(self)
        self._scroll_sync_timer.setSingleShot(True)
        self._scroll_sync_timer.setInterval(80)
        self._scroll_sync_timer.timeout.connect(self._sync_visible_cards_on_scroll)
        self.toolStartUiSyncRequested.connect(
            self._handle_tool_start_ui_sync, type=Qt.BlockingQueuedConnection
        )
        self._is_streaming = False
        self._topic_summary_cancelled = False  # 🛡️ 标题生成取消标记
        self._response_start_time = None
        # 使用 try-except 保护 homepage 操作，防止 C++ 对象已删除错误
        try:
            from PyQt5 import sip
            if not sip.isdeleted(homepage):
                homepage.installEventFilter(self)
                self._window_active = homepage.isActiveWindow()
            else:
                self._window_active = False
        except Exception:
            self._window_active = False
        # 问题修复：初始化未定义的属性
        self._pending_permission_tool_call_id: Optional[str] = None
        self._question_tool_call_id: Optional[str] = None
        self._current_assistant_round_index: Optional[int] = None  # 跟踪当前应分配给 assistant 的 round_index
        self._pending_scroll_to_index: Optional[int] = None  # 时间线节点滚动目标索引
        self._pending_scroll_to_batch: Optional[int] = None  # 时间线节点滚动目标 batch 索引
        self._pending_scroll_to_update: Optional[int] = None  # 待更新的节点索引（用于同步高亮和进度）
        # 撤销删除功能：删除消息的缓存栈
        self._undo_delete_stack: List[Dict[str, Any]] = []  # 每个元素包含 deleted_messages, round_index, widgets


        self._current_session_id = self.session_manager.get_current_session().session_id

        # 初始化卡片管理器
        self._card_manager = CardManager.get_instance()

        # 初始化 UI
        self.setup_ui()

        # 初始化 UI 相关的回调
        self._setup_engine_callbacks()

        # 初始化子智能体管理器
        self._init_sub_agent_manager()

        # 初始化历史管理器
        self._project_label.setText(self._current_project)

        # 应用退出时自动保存
        app = QApplication.instance()
        if app is not None:
            try:
                app.aboutToQuit.connect(self._auto_save_current_session)
            except Exception:
                pass

        # 设置文件操作记录的会话上下文
        if self.backend.tool_executor:
            self.backend.set_session_context(self._current_session_id)

        # 自动检查更新（启动时静默检查）
        self._init_auto_update_check()

    def _init_auto_update_check(self):
        """启动时静默检查更新（使用延迟确保窗口完全就绪）"""

        # 检查是否启用自动更新
        if not self.cfg.auto_check_update.value:
            return

        # 延迟 500ms 确保 ToolPopupDialog 完全显示后再检查更新
        # 这样 InfoBar 能正确显示在已就绪的窗口上
        QTimer.singleShot(500, lambda: self._do_auto_update_check())

    def _do_auto_update_check(self):
        """执行自动更新检查"""
        from app.update_checker import UpdateChecker

        # 检查窗口是否已销毁（防止多窗口切换时的问题）
        if getattr(self, '_is_destroyed', False):
            return

        # 更新单例的 parent，确保 InfoBar 显示在正确的父窗口上
        checker = UpdateChecker.get_instance(self)
        checker.check_update()

    def _setup_engine_callbacks(self):
        """设置 ChatEngine 的回调"""
        callbacks = {
            "content_received": self._on_content_received,
            "reasoning_content_received": self._on_reasoning_content_received,
            "thinking_started": self._on_thinking_started,
            "tool_call_started": self._on_tool_call_started,
            "tool_args_updated": self._on_tool_args_updated,
            "tool_call_sync_requested": self._request_tool_start_ui_sync,
            "tool_result_received": self._on_tool_result_received,
            "stream_started": self._on_stream_started,
            "stream_finished": self._on_stream_finished,
            "messages_updated": self._on_messages_updated,
            "error": self._on_engine_error,
            "user_message_added": self._on_user_message_added,
            "skill_requested": self._on_skill_requested,
            "question_asked": self._on_question_asked,
            "agent_switched": self._on_agent_switched,
            "retry_status": self._on_retry_status,
            "retry_resolved": self._on_retry_resolved,
            "permission_approval_requested": self._on_permission_approval_requested,
            "compaction_updated": self._on_compaction_updated,  # 子智能体压缩完成回调
        }
        self.backend.set_all_callbacks(callbacks)

    def _init_sub_agent_manager(self):
        """初始化子智能体管理器"""
        from app.core import SubAgentManager

        self._sub_agent_manager = SubAgentManager(
            agent_manager=self.backend.agent_manager,
            tool_executor=self.backend.tool_executor,
            get_llm_config=self._get_current_model_config,
        )
        self._sub_agent_manager.task_started.connect(self._on_sub_agent_task_started)
        self._sub_agent_manager.task_finished.connect(self._on_sub_agent_task_finished)
        self._sub_agent_manager.set_history_getter(self._get_current_session_messages_for_tools)

        # 将子智能体管理器设置到 backend，让 ToolExecutor 能访问
        self.backend.set_sub_agent_manager(self._sub_agent_manager)

        # 初始化子智能体日志存储（在 ChatEngine 之后）
        self._init_sub_agent_log_store()

    def _init_sub_agent_log_store(self):
        """初始化子智能体日志存储（委托给 Backend.session_store）"""
        try:
            # 直接使用 backend 的 session_store（共享单一数据库连接）
            session_store = self.backend.session_store
            
            # 直接使用 SessionStore 的方法，无需中间的 SubAgentLogStore
            self._sub_agent_manager.set_session_store(session_store)
            logger.info(f"[LLMChatter] 子智能体日志存储初始化完成（通过 SessionStore）")
        except Exception as e:
            logger.error(f"[LLMChatter] 子智能体日志存储初始化失败: {e}")

    def _init_llm_api_service(self):
        """初始化 LLM API 服务"""
        from app.api import (
            LLMAPIService,
            APISessionHandler,
            is_service_running,
        )

        # 注册服务商列表获取回调
        def get_providers_list():
            return [{"name": name} for name in self._valid_configs.keys()]

        # 创建并注册 API 会话处理器（复用 UI 的 ChatEngine 和 SessionManager）
        self._api_session_handler = APISessionHandler(self)
        LLMAPIService.set_session_handler(self._api_session_handler)

        # 根据配置决定是否启动服务
        if self.cfg.llm_api_enabled.value:
            if not is_service_running():
                service = LLMAPIService()
                service.port = self.cfg.llm_api_port.value
                service.start(background=True)
        else:
            # 确保服务未启动
            if is_service_running():
                from app.api import (
                    stop_llm_api_service,
                )

                stop_llm_api_service()

    def _register_cards_to_manager(self):
        """注册所有卡片到 CardManager 并添加到容器
        
        容器分配：
        - TopCardContainer (chatscroll 上方): Todo、MCP Edit、Hook Edit、Settings 等系统配置
        - BottomCardContainer (chatscroll 下方): 长期记忆、历史会话、模型参数、AutoLoop、Tool、Question、SubAgent
        
        规则：
        - 同位置互斥：Top/Bottom 各自只能显示一个卡片
        - Question 强制覆盖：Question 显示时同时关闭其他所有卡片
        - 不同位置可共存：Top 的卡片和 Bottom 的卡片可以同时显示
        """
        mgr = self._card_manager
        
        # 绑定容器到 CardManager
        self._top_card_container.bind_card_manager(mgr)
        self._bottom_card_container.bind_card_manager(mgr)
        
        # ===== TopCardContainer (chatscroll 上方) =====
        # 系统配置卡片，互斥显示
        mgr.register_card(ContainerType.TOP, "todo", self._todo_floating_widget)
        self._top_card_container.add_card("todo", self._todo_floating_widget)
        
        mgr.register_card(ContainerType.TOP, "mcp_edit", self._mcp_edit_card)
        self._top_card_container.add_card("mcp_edit", self._mcp_edit_card)
        
        mgr.register_card(ContainerType.TOP, "settings", self._settings_popup)
        self._top_card_container.add_card("settings", self._settings_popup)
        
        mgr.register_card(ContainerType.TOP, "provider_edit", self._provider_edit_card)
        self._top_card_container.add_card("provider_edit", self._provider_edit_card)
        
        mgr.register_card(ContainerType.TOP, "hook_edit", self._hook_edit_card)
        self._top_card_container.add_card("hook_edit", self._hook_edit_card)
        
        # ===== BottomCardContainer (chatscroll 下方) =====
        # Question: 强制覆盖所有其他卡片
        # Tool/SubAgent: 实时卡片
        # History/Memory/ModelConfig/AutoLoop: 系统卡片
        mgr.register_card(ContainerType.BOTTOM, "question", self._question_floating_widget)
        self._bottom_card_container.add_card("question", self._question_floating_widget)
        
        mgr.register_card(ContainerType.BOTTOM, "tool", self._tool_floating_widget)
        self._bottom_card_container.add_card("tool", self._tool_floating_widget)
        
        mgr.register_card(ContainerType.BOTTOM, "sub_agent", self._sub_agent_floating_widget)
        self._bottom_card_container.add_card("sub_agent", self._sub_agent_floating_widget)
        
        mgr.register_card(ContainerType.BOTTOM, "history", self._history_card)
        self._bottom_card_container.add_card("history", self._history_card)
        
        mgr.register_card(ContainerType.BOTTOM, "memory", self._memory_card)
        self._bottom_card_container.add_card("memory", self._memory_card)
        
        mgr.register_card(ContainerType.BOTTOM, "model_config", self._model_config_card)
        self._bottom_card_container.add_card("model_config", self._model_config_card)
        
        mgr.register_card(ContainerType.BOTTOM, "auto_loop_config", self._auto_loop_config_card)
        self._bottom_card_container.add_card("auto_loop_config", self._auto_loop_config_card)
        
        mgr.register_card(ContainerType.BOTTOM, "auto_loop_running", self._auto_loop_running_card)
        self._bottom_card_container.add_card("auto_loop_running", self._auto_loop_running_card)
        
        logger.info("[CardManager] 所有卡片注册并添加到容器完成")

    def _setup_title_bar(self):
        """设置标题栏按钮"""
        title_bar = self.get_title_bar()
        # 显示内存标签
        title_bar.show_memory_label()
        # 创建复制窗口按钮
        self._copy_btn = TransparentToolButton(get_icon("新建窗口"), self)
        self._copy_btn.setToolTip("新建窗口")
        self._copy_btn.clicked.connect(lambda: self._duplicate_window(branch=False))
        title_bar.insert_button(0, self._copy_btn)

        # 创建分支按钮
        self._branch_btn = TransparentToolButton(get_icon("分支"), self)
        self._branch_btn.setToolTip("分支当前对话")
        self._branch_btn.clicked.connect(lambda: self._duplicate_window(branch=True))
        title_bar.insert_button(1, self._branch_btn)
        # 创建设置按钮
        self._settings_btn = TransparentToolButton(FluentIcon.SETTING, self)
        self._settings_btn.setFixedSize(28, 28)
        self._settings_btn.setToolTip("设置")
        self._settings_btn.clicked.connect(self._toggle_settings_card)
        title_bar.insert_button(2, self._settings_btn)

    def _toggle_settings_card(self):
        """切换设置卡片的显示"""
        self._card_manager.toggle_card("settings")

    def _open_api_docs(self):
        """打开 API 文档页面"""
        from app.api import open_docs
        open_docs()

    def _duplicate_window(self, branch: bool = False):
        """复制当前窗口并以弹窗方式显示，或从当前会话分支创建新会话

        Args:
            branch: 如果为 True，则复制当前会话的消息到新窗口
        """
        try:
            # 清理已关闭的弹窗引用，防止内存泄漏
            # 使用 sip.isdeleted 检查对象是否仍然有效
            from PyQt5 import sip
            if hasattr(self, '_popup_refs'):
                valid_refs = []
                for ref in self._popup_refs:
                    try:
                        if ref is not None and not sip.isdeleted(ref) and ref.isVisible():
                            valid_refs.append(ref)
                    except Exception:
                        pass  # 忽略检查失败的引用
                self._popup_refs = valid_refs

            # 验证 self 和 homepage 是否有效
            try:
                if sip.isdeleted(self) or sip.isdeleted(self.homepage):
                    InfoBar.error("窗口错误", "主窗口已关闭，无法创建新窗口", parent=self, position=InfoBarPosition.BOTTOM)
                    return
            except Exception:
                pass  # 忽略检查失败

            # 确保 homepage 有效后再使用
            valid_homepage = self.homepage
            if valid_homepage is None:
                return

            # 创建新的窗口实例
            new_instance = OpenAIChatToolWindow(valid_homepage, None)

            # 如果是分支模式，传递当前会话的消息
            if branch:
                current_session = self.session_manager.get_current_session()
                if current_session:
                    branch_messages = list(current_session.messages)
                    branch_name = current_session.name + " [分支]"
                    # 设置分支会话数据，新窗口会使用这些消息创建会话
                    new_instance._branch_session_data = {
                        "messages": branch_messages,
                        "name": branch_name,
                    }
                # 分支模式不跳过历史恢复，而是使用传入的分支数据
                new_instance._skip_restore_history = True  # 跳过 _restore_latest_session
            else:
                new_instance._skip_restore_history = True  # 跳过历史会话恢复，创建新会话

            # 复制模型选择（确保两个实例都已初始化 UI）
            try:
                if (
                        hasattr(self, "_current_provider_name")
                        and hasattr(new_instance, "_current_provider_name")
                        and self._current_provider_name
                ):
                    new_instance._current_provider_name = self._current_provider_name
                    new_instance._current_model_name = self._current_model_name
                    new_instance._update_model_selector_btn()
            except Exception:
                pass  # 忽略模型复制失败

            # 设置 session 初始化的标志，避免重复创建新 session
            # 并标记为新会话模式，跳过历史会话恢复
            # 注意：不要设置 _session_initialized，让 showEvent 正常执行初始化
            new_instance._skip_restore_history = True  # 跳过历史会话恢复

            # 以弹窗方式显示
            from app.tool_popup import ToolPopupDialog

            popup = ToolPopupDialog(new_instance, None)
            if branch:
                popup.setWindowTitle(f"{self.name} - 分支")
            else:
                popup.setWindowTitle(f"{self.name} - 副本")
            popup.resize(600, 900)

            # 保存引用防止被垃圾回收
            if not hasattr(self, '_popup_refs'):
                self._popup_refs = []

            # 使用更健壮的方式清理已关闭的弹窗引用
            valid_refs = []
            for ref in self._popup_refs:
                try:
                    if ref is not None and not sip.isdeleted(ref) and ref.isVisible():
                        valid_refs.append(ref)
                except Exception:
                    pass
            self._popup_refs = valid_refs

            # 限制最大引用数量，防止无限增长
            if len(self._popup_refs) >= 10:
                self._popup_refs = self._popup_refs[-10:]

            self._popup_refs.append(popup)

            # 在 show() 之前再次检查 popup 是否仍然有效
            if sip.isdeleted(popup):
                InfoBar.error("复制失败", "窗口创建失败，请重试", parent=self, position=InfoBarPosition.BOTTOM)
                return

            popup.show()
        except Exception as e:

            InfoBar.error("复制失败", str(e), parent=self, position=InfoBarPosition.BOTTOM)

    def _request_tool_start_ui_sync(
            self, tool_call_id: str, tool_name: str, arguments: dict, round_id: str = None
    ):
        self.toolStartUiSyncRequested.emit(
            tool_call_id, tool_name, arguments or {}, round_id or ""
        )

    def _handle_tool_start_ui_sync(
            self, tool_call_id: str, tool_name: str, arguments: object, round_id: str
    ):
        self._on_tool_call_started(tool_call_id, tool_name, arguments or {}, round_id)
        QApplication.sendPostedEvents()
        if self._tool_floating_widget:
            self._tool_floating_widget.repaint()
        self.repaint()
        QApplication.processEvents()

    def _get_chat_cards_for_engine(self):
        cards = []
        for i in range(self.chat_layout.count()):
            item = self.chat_layout.itemAt(i)
            if item and item.widget():
                widget = item.widget()
                if isinstance(widget, MessageCard):
                    cards.append(widget)
        return cards

    def _get_current_model_config(self) -> Dict[str, Any]:
        """获取当前选中的模型配置，实时从系统配置读取
        
        多窗口隔离：使用 _current_model_name 覆盖全局配置中的模型名称，
        确保每个窗口使用自己选中的模型，而非其他窗口最后选择的模型。
        """
        selected_name = self._current_provider_name if self._current_provider_name else (
            list(self._valid_configs.keys())[0] if self._valid_configs else "")

        # 优先从 _valid_configs 获取（已合并默认配置）
        if selected_name in self._valid_configs:
            config = self._valid_configs[selected_name].copy()
            # 确保使用当前窗口选中的模型名称，而非全局配置中的模型名称（多窗口隔离）
            if self._current_model_name:
                config["模型名称"] = self._current_model_name
            return config

        return {}

    def _get_current_session_messages_for_tools(self) -> List[Dict[str, Any]]:
        session = self.session_manager.get_current_session()
        if not session:
            return []
        return list(session.messages or [])

    def showEvent(self, event):
        if getattr(self, "_session_initialized", False):
            super().showEvent(event)
            self._connect_opacity_signal()
            return
        self._session_initialized = True
        # 标记正在初始化，防止窗口在初始化完成前被关闭导致竞态条件
        self._initialization_in_progress = True
        
        # 使用 _safe_timer_call 包装所有异步回调，在 widget 销毁后自动跳过
        QTimer.singleShot(0, lambda: self._safe_timer_call(self._load_agent_list))
        # 如果有分支数据，延迟调用分支会话处理，避免与 _restore_latest_or_create_session 冲突
        if getattr(self, "_branch_session_data", None):
            QTimer.singleShot(50, lambda: self._safe_timer_call(self._apply_branch_or_create_session))
        else:
            QTimer.singleShot(0, lambda: self._safe_timer_call(self._create_new_session))

        QTimer.singleShot(100, lambda: self._safe_timer_call(self._load_model_configs))
        # 初始化当前项目的工作目录
        QTimer.singleShot(200, lambda: self._safe_timer_call(self._sync_working_directory))
        # 初始化完成后解除保护
        QTimer.singleShot(300, lambda: self._safe_timer_call(self._on_initialization_complete))
        self._connect_opacity_signal()
        super().showEvent(event)

    def _on_initialization_complete(self):
        """初始化完成后调用，解除保护标志"""
        self._initialization_in_progress = False
        logger.debug("[OpenAIChatToolWindow] Initialization complete")

    def _safe_timer_call(self, func):
        """安全执行 QTimer.singleShot 回调，在 widget 已销毁时自动跳过
        
        防止 QTimer 回调在窗口关闭(deleteLater)后执行导致 segfault。
        必须在所有通过 QTimer.singleShot 调度的回调中使用。
        """
        if getattr(self, '_is_destroyed', False):
            logger.debug(f"[OpenAIChatToolWindow] Skipping timer callback {func.__name__}: widget destroyed")
            return
        try:
            from PyQt5 import sip
            if sip.isdeleted(self):
                logger.debug(f"[OpenAIChatToolWindow] Skipping timer callback {func.__name__}: C++ object deleted")
                return
        except Exception:
            pass
        try:
            func()
        except RuntimeError as e:
            # PyQt5 在访问已删除 C++ 对象时抛出 RuntimeError
            if 'C++' in str(e) or 'wrapped C/C++' in str(e):
                logger.debug(f"[OpenAIChatToolWindow] Skipping timer callback {func.__name__}: {e}")
                return
            raise

    def eventFilter(self, obj, event):
        """处理 viewport 大小变化，调整背景图片"""
        if hasattr(self, "chat_scroll_area") and obj == self.chat_scroll_area.viewport() and event.type() == event.Type.Resize:
            if hasattr(self, "_bg_label"):
                self._bg_label.resize(self.chat_scroll_area.viewport().size())
        return super().eventFilter(obj, event)

    def _connect_opacity_signal(self):
        """连接父窗口的透明度变化信号"""
        if getattr(self, "_opacity_signal_connected", False):
            return
        parent = self.parent()
        if parent and hasattr(parent, "globalOpacityChanged"):
            parent.globalOpacityChanged.connect(self._on_global_opacity_changed)
            self._opacity_signal_connected = True

    def _on_global_opacity_changed(self, opacity: float):
        """响应全局透明度变化，更新所有子组件的透明度"""
        self._update_widgets_opacity(opacity)

    def _update_widgets_opacity(self, opacity: float):
        """更新所有需要响应透明度变化的组件"""
        # 更新待办事项悬浮框
        if self._todo_floating_widget:
            self._todo_floating_widget.set_opacity(opacity)
        # 更新模型配置卡片
        if self._model_config_card:
            self._model_config_card.set_opacity(opacity)
        # 更新历史会话卡片
        if self._history_card:
            self._history_card.set_opacity(opacity)
        # 更新记忆管理卡片
        if self._memory_card:
            self._memory_card.set_opacity(opacity)
        # 更新设置卡片
        if self._settings_popup:
            self._settings_popup.set_opacity(opacity)
        # 更新子智能体悬浮框
        if hasattr(self, "_sub_agent_floating_widget") and self._sub_agent_floating_widget:
            self._sub_agent_floating_widget.set_opacity(opacity)
        # 更新工具悬浮框
        if hasattr(self, "_tool_floating_widget") and self._tool_floating_widget:
            self._tool_floating_widget.set_opacity(opacity)
        # 更新问题悬浮框
        if self._question_floating_widget:
            self._question_floating_widget.set_opacity(opacity)
        # 更新服务商编辑卡片
        if self._provider_edit_card:
            self._provider_edit_card.set_opacity(opacity)
        # 更新主窗口背景透明度
        self._update_window_bg_opacity(opacity)

    def _update_window_bg_opacity(self, opacity: float):
        """更新窗口背景透明度"""
        if not hasattr(self, '_window_bg_color'):
            return
        import re
        m = re.match(r'rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?\)', self._window_bg_color)
        if m:
            r, g, b = int(m.group(1)), int(m.group(2)), int(m.group(3))
            # 原始 alpha * 窗口透明度
            base_alpha = int(float(m.group(4)) * 255) if m.group(4) else 10
            final_alpha = int(base_alpha * opacity)
            from PyQt5.QtGui import QColor, QPalette
            color = QColor(r, g, b, max(0, final_alpha))
            p = QPalette()
            p.setColor(QPalette.Window, color)
            self.setPalette(p)
    
    def _apply_branch_or_create_session(self):
        # 检查窗口是否仍然有效，防止在初始化期间窗口被关闭后继续执行
        if getattr(self, '_is_destroyed', False):
            logger.debug("[OpenAIChatToolWindow] Window destroyed before branch session creation, skipping")
            return
        try:
            from PyQt5 import sip
            if sip.isdeleted(self):
                logger.debug("[OpenAIChatToolWindow] C++ object deleted before branch session creation, skipping")
                return
        except Exception:
            pass
        
        branch_data = getattr(self, "_branch_session_data", None)
        if branch_data:
            # 使用分支数据创建会话
            self._create_branched_session(
                branch_data.get("messages", []),
                branch_data.get("name", "分支对话"),
            )
        else:
            # 没有分支数据，创建新会话
            self._create_new_session()

    def _create_branched_session(self, messages: List[Dict], name: str):
        """创建分支会话并渲染消息"""
        # 检查窗口是否仍然有效，防止在初始化期间窗口被关闭后继续执行
        if getattr(self, '_is_destroyed', False):
            logger.debug("[OpenAIChatToolWindow] Window destroyed before branched session creation, skipping")
            return
        try:
            from PyQt5 import sip
            if sip.isdeleted(self):
                logger.debug("[OpenAIChatToolWindow] C++ object deleted before branched session creation, skipping")
                return
        except Exception:
            pass
        
        logger.info("[Branch] 开始创建分支会话")

        # 停止当前对话并清理
        if self._is_streaming and self.backend.chat_engine:
            self.backend.stop_streaming()
            self._is_streaming = False
            self._topic_summary_cancelled = True  # 🛡️ 取消标题生成重试
            self._toggle_send_stop(False)
        elif self.backend.chat_engine:
            # 即使不在流式输出，也要清理 worker
            self.backend.cleanup_worker()

        # 切换会话前彻底清理卡片
        self._cache_current_session_cards()
        # 只重置会话状态，保留 tool_executor（分支后还需要执行工具）
        if self.backend.tool_executor:
            self.backend.reset_session_state()
        session = self.backend.create_session()
        session.messages = messages
        session.name = name
        session.topic_summary = name  # 同步 topic_summary，避免 _display_current_session 覆盖 title_edit
        self._current_session_id = session.session_id

        # 清空聊天区域
        self._clear_chat_area()
        self.title_edit.setText(name)
        self.node_preview.clear_nodes()

        # 重置输入框高度
        if hasattr(self, 'input_area'):
            self.input_area.setFixedHeight(72)
            self.input_area._initializing = False

        # 复用现有的会话显示逻辑
        self._display_current_session()

    def _refresh_cache_stats(self):
        """刷新缓存统计显示（对话完成后调用）"""
        ring = getattr(self, "context_usage_ring", None)
        if not ring:
            return

        stats_dict = self.backend.get_last_cache_stats()

        if not stats_dict:
            worker = self.backend.get_current_worker()
            if worker:
                try:
                    raw = worker.get_cache_stats()
                    if hasattr(raw, 'to_dict'):
                        stats_dict = raw.to_dict()
                    elif isinstance(raw, dict):
                        stats_dict = raw
                except Exception:
                    pass

        if not stats_dict:
            return

        try:
            cost_savings = 0.0
            cost_with = stats_dict.get('cost_usd', 0.0)
            cost_without = stats_dict.get('cost_without_cache_usd', 0.0)
            if cost_without > 0:
                cost_savings = cost_without - cost_with

            hit_rate = stats_dict.get('hit_rate', 0.0)
            per_request_hit_rate = stats_dict.get('per_request_hit_rate', 0.0)
            total_input_hit_rate = stats_dict.get('total_input_hit_rate', 0.0)
            read_tokens = stats_dict.get('cache_read_tokens', 0)
            write_tokens = stats_dict.get('cache_creation_5m_tokens', 0) + stats_dict.get('cache_creation_1h_tokens', 0)
            cache_hits = stats_dict.get('cache_hits', 0)
            cache_misses = stats_dict.get('cache_misses', 0)

            logger.info(
                f"[CacheStats] hit_rate={hit_rate:.1%}"
                f" per_req={per_request_hit_rate:.1%}"
                f" total_input={total_input_hit_rate:.1%}"
                f" read={read_tokens} write={write_tokens}"
                f" hits={cache_hits} misses={cache_misses}"
                f" saved=${cost_savings:.4f}"
            )

            ring.set_cache_stats(
                hit_rate=hit_rate,
                read_tokens=read_tokens,
                write_tokens=write_tokens,
                cost_savings=cost_savings,
                per_request_hit_rate=per_request_hit_rate,
                total_input_hit_rate=total_input_hit_rate,
                cache_hits=cache_hits,
                cache_misses=cache_misses,
                requests=stats_dict.get('requests', 0),
            )
        except Exception as e:
            logger.debug(f"[CacheStats] Failed to refresh: {e}")

    def setup_ui(self):
        Colors.refresh()
        # 动态更新主题选项
        update_theme_options()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(1, 1, 4, 1)
        layout.setSpacing(1)
        
        # 创建卡片容器
        self._top_card_container = TopCardContainer()
        self._bottom_card_container = BottomCardContainer()

        # 设置窗口背景色（非常淡的主题色）
        colors = theme_manager.get_current_colors()
        window_bg = colors.get('window_bg', 'rgba(102, 198, 255, 0.04)')
        
        # 保存原始颜色供透明度变化时使用
        self._window_bg_color = window_bg
        
        # 解析颜色（包含 alpha）
        m = re.match(r'rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?\)', window_bg)
        if m:
            r, g, b = int(m.group(1)), int(m.group(2)), int(m.group(3))
            alpha = int(float(m.group(4)) * 255) if m.group(4) else 10  # 保留原始 alpha
            from PyQt5.QtGui import QColor, QPalette
            color = QColor(r, g, b, alpha)
            p = QPalette()
            p.setColor(QPalette.Window, color)
            self.setPalette(p)
            self.setAutoFillBackground(True)
        
        # 字体样式
        self.setStyleSheet("")

        session_bar_layout = QHBoxLayout()

        # 项目选择标签（跟随主题色）
        self._project_label = QLabel(self._current_project, self)
        self._project_label.setStyleSheet(f"""
            QLabel {{
                color: {Colors.TEXT_ACCENT};
                {get_font_family_css()}
                {font_size_css(13)}
                font-weight: bold;
                padding: 2px 6px;
                border-radius: 4px;
                background: {Colors.HOVER_BG};
            }}
            QLabel:hover {{
                background: {Colors.SELECTED_BG};
            }}
        """)
        self._project_label.setCursor(Qt.PointingHandCursor)
        self._project_label.mousePressEvent = self._on_project_label_clicked
        self._project_label.setContextMenuPolicy(Qt.CustomContextMenu)
        self._project_label.customContextMenuRequested.connect(self._show_context_menu)
        self._project_label.setToolTip("点击切换项目 · 右键更多操作")

        # Git 分支标签（从工作目录检测，左键点击打开关键文档卡片）
        self._branch_widget = QWidget(self)
        self._branch_widget.setCursor(Qt.PointingHandCursor)
        self._branch_widget.setObjectName("_branchWidget")
        self._branch_widget.setAttribute(Qt.WA_StyledBackground)
        self._branch_widget.mousePressEvent = self._on_branch_label_clicked
        self._branch_widget.setToolTip("当前 Git 分支 — 点击打开关键文档")
        self._branch_widget.setVisible(False)
        self._branch_layout = QHBoxLayout(self._branch_widget)
        self._branch_layout.setContentsMargins(3, 0, 3, 0)
        self._branch_layout.setSpacing(2)

        self._branch_icon = QLabel(self._branch_widget)
        _pix = QPixmap(":/icons/分支.svg")
        self._branch_icon.setPixmap(_pix.scaled(12, 12, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        self._branch_layout.addWidget(self._branch_icon)

        self._branch_label = QLabel("", self._branch_widget)
        self._branch_layout.addWidget(self._branch_label)

        self._update_branch()
        # 标题
        self.title_edit = QLabel("新对话", self)
        font_css = get_font_family_css()
        title_style = TITLE_STYLE.replace("    QLabel {", f"    QLabel {{\n        {font_css}")
        title_style = title_style.replace("font-size: 15px;", font_size_css(15))
        title_style = title_style.replace("#f3f6fc", Colors.TEXT_PRIMARY)  # 跟随主题色
        self.title_edit.setStyleSheet(title_style)
        self.title_edit.setCursor(Qt.PointingHandCursor)
        self.title_edit.mouseDoubleClickEvent = self._on_title_double_click

        session_bar_layout.addWidget(self._project_label)
        session_bar_layout.addWidget(self._branch_widget)
        session_bar_layout.addWidget(self.title_edit)

        # right_layout 保持简化，显示余额和 context_usage_ring
        right_layout = QHBoxLayout()
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(6)

        # 余额显示
        self.balance_display = BalanceDisplay(self)
        right_layout.addWidget(self.balance_display)

        # 上下文占用圆环
        self.context_usage_ring = ContextUsageRing(self)
        right_layout.addWidget(self.context_usage_ring)
        right_layout.addSpacing(10)

        session_bar_layout.addStretch()
        session_bar_layout.addLayout(right_layout)
        layout.addLayout(session_bar_layout)

        # 时间线节点
        self.node_preview = ConversationNodePreview(self)
        self.node_preview.nodeClicked.connect(self._on_node_preview_clicked)
        layout.addWidget(self.node_preview)

        # ===== 创建卡片容器 =====
        self._top_card_container = TopCardContainer()
        self._bottom_card_container = BottomCardContainer()

        # ===== 创建卡片容器 =====
        self._top_card_container = TopCardContainer()
        self._bottom_card_container = BottomCardContainer()

        self._settings_popup = LLMSettingsCard(self)
        self._settings_popup.setVisible(False)
        self._settings_popup.configChanged.connect(self._on_settings_config_changed)
        
        # 将 settings_popup 添加到 top_card_container
        self._top_card_container.add_card("settings", self._settings_popup)

        # 连接服务商添加/编辑信号
        self._settings_popup.llmProviderCard.showAddProviderCard.connect(self._show_provider_add_card)
        self._settings_popup.llmProviderCard.showEditProviderCard.connect(self._show_provider_edit_card)

        # 连接 Hook 添加/编辑信号
        self._settings_popup.hookListCard.showAddHookCard.connect(self._show_hook_add_card)
        self._settings_popup.hookListCard.showEditHookCard.connect(self._show_hook_edit_card)

        # 连接 MCP 添加/编辑信号
        self._settings_popup.mcpListCard.showAddCard.connect(self._show_mcp_add_card)
        self._settings_popup.mcpListCard.showEditCard.connect(self._show_mcp_edit_card)

        # Hook 编辑卡片
        self._hook_edit_card = BaseSettingsCard("Hook 配置", "⚙️", parent=self)
        self._hook_edit_card.setFixedHeight(300)
        self._hook_edit_popup = HookEditCard(parent=self)
        self._hook_edit_popup.saved.connect(self._on_hook_edit_saved)
        self._hook_edit_popup.closed.connect(self._on_hook_edit_closed)
        self._hook_edit_card.content_layout.addWidget(self._hook_edit_popup)
        self._hook_edit_card.set_save_button_handler(self._hook_edit_popup._on_save)
        self._hook_edit_card.setVisible(False)
        self._hook_edit_card.closed.connect(self._on_hook_edit_card_closed)
        # Hook Edit 卡片已添加到容器，不再需要直接 layout.addWidget

        # 服务商编辑卡片
        self._provider_edit_card = BaseSettingsCard("服务商配置", "⚙️", parent=self)
        self._provider_edit_card.setFixedHeight(380)
        self._provider_edit_popup = ProviderEditCard(parent=self)
        self._provider_edit_popup.saved.connect(self._on_provider_edit_saved)
        self._provider_edit_popup.closed.connect(self._on_provider_edit_closed)
        self._provider_edit_card.content_layout.addWidget(self._provider_edit_popup)
        # 照抄历史卡片的导入按钮模式：在标题栏加保存按钮，信号连到内容组件
        # 获取 ProviderEditCard 实例的保存方法
        save_handler = self._provider_edit_popup._on_save
        self._provider_edit_card.set_save_button_handler(save_handler)
        self._provider_edit_card.setVisible(False)
        self._provider_edit_card.closed.connect(self._on_provider_edit_card_closed)
        # Provider Edit 卡片已添加到容器，不再需要直接 layout.addWidget

        # MCP 编辑卡片
        self._mcp_edit_card = BaseSettingsCard("MCP 服务器", "🔌", parent=self)
        self._mcp_edit_card.setFixedHeight(350)
        self._mcp_edit_popup = None
        self._mcp_edit_card.setVisible(False)
        self._mcp_edit_card.closed.connect(self._on_mcp_edit_card_closed)
        # MCP Edit 卡片已添加到容器，不再需要直接 layout.addWidget

        self._todo_floating_widget = TodoFloatingWidget(self)
        self._todo_floating_widget.setVisible(False)

        self._sub_agent_floating_widget = SubAgentFloatingWidget(self)
        self._sub_agent_floating_widget.setVisible(False)

        self._tool_floating_widget = ToolFloatingWidget(self)
        self._tool_floating_widget.setVisible(False)
        self._tool_floating_widget.cancelled.connect(self._on_tool_cancelled)

        # 下方卡片容器 - 添加 Tool 和 SubAgent
        self._bottom_card_container.add_card("tool", self._tool_floating_widget)
        self._bottom_card_container.add_card("sub_agent", self._sub_agent_floating_widget)

        # 上方卡片容器 - 添加 Todo
        self._top_card_container.add_card("todo", self._todo_floating_widget)

        # 上方卡片容器 - 添加 MCP Edit
        self._top_card_container.add_card("mcp_edit", self._mcp_edit_card)

        # 上方卡片容器 - 添加 Hook Edit
        self._top_card_container.add_card("hook_edit", self._hook_edit_card)

        # 上方卡片容器 - 添加 Provider Edit
        self._top_card_container.add_card("provider_edit", self._provider_edit_card)

        layout.addWidget(self._top_card_container)

        self.chat_scroll_area = SingleDirectionScrollArea(self)
        self.chat_scroll_area.setMinimumHeight(1)
        self.chat_scroll_area.setMinimumWidth(400)
        self.chat_scroll_area.setStyleSheet(CHAT_SCROLL_STYLE)
        self.chat_scroll_area.setWidgetResizable(True)
        self.chat_scroll_area.setViewportMargins(2, 2, 10, 2)
        self.chat_scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        # 背景图片层 - 从主题配置加载
        self._apply_bg_from_theme()

        self.chat_container = QWidget()
        self.chat_container.setStyleSheet("background: transparent;")
        self.chat_layout = QVBoxLayout(self.chat_container)
        self.chat_layout.setContentsMargins(6, 6, 6, 6)
        self.chat_layout.setSpacing(8)
        self.chat_layout.setAlignment(Qt.AlignBottom)
        self.chat_scroll_area.setWidget(self.chat_container)

        # 连接滚动事件，触发虚拟滚动回收
        scroll_bar = self.chat_scroll_area.verticalScrollBar()
        scroll_bar.valueChanged.connect(self._on_chat_scrolled)

        layout.addWidget(self.chat_scroll_area, 1)

        # 下方卡片容器
        layout.addWidget(self._bottom_card_container)

        self._history_popup_card = HistoryCard()
        self._history_popup_card.sessionSelected.connect(self._on_history_session_selected)
        self._history_popup_card.sessionArchived.connect(self._archive_history_session)
        self._history_popup_card.sessionRenamed.connect(self._rename_history_session)
        self._history_popup_card.refreshRequested.connect(self._refresh_history_toggle_panel)
        self._history_popup_card.sessionImported.connect(self._on_session_imported)
        # 归档会话相关信号
        self._history_popup_card.sessionRestored.connect(self._on_archived_session_restored)
        self._history_popup_card.sessionPermanentlyDeleted.connect(self._on_archived_session_deleted)
        self._history_popup_card.archivedSessionRenamed.connect(self._on_archived_session_renamed)
        # 设置导入按钮的处理器
        self._history_card.set_extra_button_handler(
            self._history_popup_card.get_import_button_handler()
        )

        # 历史会话卡片
        self._history_card.content_layout.addWidget(self._history_popup_card)
        self._history_card.setVisible(False)
        # 搜索框（历史会话和归档标签都显示）
        self._history_card.set_search_handler(
            "🔍 搜索会话...",
            lambda text: self._history_popup_card.set_search_filter(text)
        )
        self._bottom_card_container.add_card("history", self._history_card)

        # 模型配置卡片
        self._model_config_card = BaseSettingsCard("模型配置", "🔧", self)
        self._model_config_popup = ModelConfigCard()
        self._model_config_popup.configApplied.connect(self._on_config_applied)
        self._model_config_card.content_layout.addWidget(self._model_config_popup)
        self._model_config_card.setVisible(False)
        self._bottom_card_container.add_card("model_config", self._model_config_card)

        # AutoLoop 配置卡片
        from app.widgets.cards.settings.auto_loop_card import AutoLoopConfigCard, AutoLoopRunningCard
        self._auto_loop_config_card = AutoLoopConfigCard()
        self._auto_loop_config_card.startRequested.connect(self._on_auto_loop_start)
        self._auto_loop_config_card.setVisible(False)
        self._bottom_card_container.add_card("auto_loop_config", self._auto_loop_config_card)

        # AutoLoop 运行卡片
        self._auto_loop_running_card = AutoLoopRunningCard()
        self._auto_loop_running_card.stopRequested.connect(self._on_auto_loop_stop)
        self._auto_loop_running_card.setVisible(False)
        self._bottom_card_container.add_card("auto_loop_running", self._auto_loop_running_card)

        self._question_floating_widget = QuestionFloatingWidget(self)
        self._question_floating_widget.setVisible(False)
        self._question_floating_widget.answered.connect(self._on_question_answered)
        self._question_floating_widget.cancelled.connect(self._on_question_cancelled)
        self._bottom_card_container.add_card("question", self._question_floating_widget)

        # 注册卡片到 CardManager（优先级：数值越小权限越高）
        self._register_cards_to_manager()

        self.chat_scroll_area.verticalScrollBar().valueChanged.connect(
            self._on_scroll_changed
        )

        # ===== 底部输入区域（一体化圆弧卡片设计）=====
        self._bottom_input_container = QWidget(self)
        self._bottom_input_container.setStyleSheet("QWidget#bottomContainer { background: transparent; }")
        self._bottom_input_container.setObjectName("bottomContainer")
        bottom_layout = QVBoxLayout(self._bottom_input_container)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.setSpacing(0)

        # ===== 一体化输入卡片（圆角大弧线包裹输入框+工具栏）=====
        self._input_card = QWidget(self._bottom_input_container)
        Colors.refresh()
        self._input_card.setStyleSheet(f"""
            QWidget {{
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 {Colors.INPUT_BG_START},
                    stop:1 {Colors.INPUT_BG_END});
                border: 1px solid {Colors.INPUT_BORDER};
                border-radius: 16px;
            }}
        """)
        card_layout = QVBoxLayout(self._input_card)
        card_layout.setContentsMargins(2, 2, 2, 2)
        card_layout.setSpacing(0)

        # 输入框（融入卡片，无边框）
        self.input_area = SendableTextEdit(self._input_card)
        self.input_area._agent_combo.hide()
        self.input_area._initializing = False
        self.input_area.setFixedHeight(52)
        self.input_area.setPlaceholderText("给 DriFox 发送消息...")
        setFont(self.input_area, scale_font_size(15))
        self.input_area.sendMessageRequested.connect(self._on_send_clicked)
        self.input_area.stopMessageRequested.connect(self._on_stop_clicked)
        self.input_area.clearRequested.connect(self._on_clear_shortcut)
        self.input_area.newSessionRequested.connect(self._create_new_session)
        self.input_area.agentChanged.connect(self._on_agent_changed)
        self.input_area.textChanged.connect(self._on_input_area_height_changed)
        card_layout.addWidget(self.input_area)

        # 分隔线
        separator = QFrame(self._input_card)
        separator.setFrameShape(QFrame.HLine)
        separator.setFixedHeight(1)
        separator.setStyleSheet(f"background: rgba(255,255,255,0.06); border: none;")
        card_layout.addWidget(separator)

        # ===== 工具栏（卡片内部，分隔线下方）=====
        toolbar_widget = QWidget(self._input_card)
        toolbar_widget.setFixedHeight(34)
        toolbar_widget.setStyleSheet("background: transparent; border: none;")
        toolbar_layout = QHBoxLayout(toolbar_widget)
        toolbar_layout.setContentsMargins(8, 2, 8, 2)
        toolbar_layout.setSpacing(8)

        # 模型选择（无边框，只保留背景）
        self._model_btn_container = QWidget(toolbar_widget)
        self._model_btn_container.setFixedHeight(26)
        self._model_btn_container.setStyleSheet(f"""
            background: rgba(255,255,255,0.05);
            border: none;
            border-radius: 8px;
        """)
        model_layout = QHBoxLayout(self._model_btn_container)
        model_layout.setContentsMargins(8, 0, 4, 0)
        model_layout.setSpacing(0)
        self.current_model_btn = QWidget(self._model_btn_container)
        self.current_model_btn.setCursor(Qt.PointingHandCursor)
        self.current_model_btn.setStyleSheet(MODEL_BTN_STYLE)
        self.current_model_btn.mousePressEvent = lambda e: self._show_model_selector_popup()
        btn_layout = QHBoxLayout(self.current_model_btn)
        btn_layout.setContentsMargins(2, 2, 0, 2)
        btn_layout.setSpacing(4)
        self._model_btn_icon = QLabel(self.current_model_btn)
        self._model_btn_icon.setStyleSheet("background: transparent; border: none;")
        self._model_btn_icon.setFixedSize(15, 15)
        btn_layout.addWidget(self._model_btn_icon)
        self._model_btn_text = QLabel("正在加载...", self.current_model_btn)
        self._model_btn_text.setStyleSheet(MODEL_BTN_TEXT_STYLE.replace("font-size: 13px;", font_size_css(11)))
        btn_layout.addWidget(self._model_btn_text)
        model_layout.addWidget(self.current_model_btn, 1)
        self.settings_btn = TransparentToolButton(get_icon("模型选择"), self._model_btn_container)
        self.settings_btn.setFixedSize(22, 22)
        self.settings_btn.setToolTip("模型参数配置")
        self.settings_btn.clicked.connect(self._toggle_model_config_card)
        model_layout.addWidget(self.settings_btn)
        toolbar_layout.addWidget(self._model_btn_container)

        self._current_provider_name = ""
        self._current_model_name = ""

        # 智能体切换（无边框）
        self._agent_switch_widget = self._create_agent_switch_buttons()
        self._agent_switch_widget.setFixedHeight(26)
        toolbar_layout.addWidget(self._agent_switch_widget)

        toolbar_layout.addStretch(1)

        # 右侧功能按钮组（无边框，间距加宽）
        self._toolbar_capsule = QWidget(toolbar_widget)
        self._toolbar_capsule.setFixedHeight(26)
        self._toolbar_capsule.setStyleSheet(f"""
            background: rgba(255,255,255,0.05);
            border: none;
            border-radius: 8px;
        """)
        capsule_layout = QHBoxLayout(self._toolbar_capsule)
        capsule_layout.setContentsMargins(6, 2, 6, 2)
        capsule_layout.setSpacing(6)

        btn_capsule_style = """
            TransparentToolButton { background: transparent; border: none; }
            TransparentToolButton:hover { background: rgba(255,255,255,0.1); border-radius: 4px; }
        """

        self.auto_loop_btn = TransparentToolButton(get_icon("无限"), self._toolbar_capsule)
        self.auto_loop_btn.setFixedSize(22, 22)
        self.auto_loop_btn.setToolTip("AutoLoop")
        self.auto_loop_btn.setStyleSheet(btn_capsule_style)
        self.auto_loop_btn.clicked.connect(self._show_auto_loop_config)
        capsule_layout.addWidget(self.auto_loop_btn)

        self.diff_btn = TransparentToolButton(get_icon("差异对比"), self._toolbar_capsule)
        self.diff_btn.setFixedSize(22, 22)
        self.diff_btn.setStyleSheet(btn_capsule_style)
        self.diff_btn.setToolTip("差异对比")
        self.diff_btn.clicked.connect(self._open_diff_viewer)
        capsule_layout.addWidget(self.diff_btn)

        self.memory_btn = TransparentToolButton(get_icon("长期记忆"), self._toolbar_capsule)
        self.memory_btn.setFixedSize(22, 22)
        self.memory_btn.setStyleSheet(btn_capsule_style)
        self.memory_btn.setToolTip("长期记忆")
        self.memory_btn.clicked.connect(self._show_soul_memory)
        capsule_layout.addWidget(self.memory_btn)

        self.history_btn = TransparentToolButton(FluentIcon.HISTORY, self._toolbar_capsule)
        self.history_btn.setFixedSize(22, 22)
        self.history_btn.setStyleSheet(btn_capsule_style)
        self.history_btn.setToolTip("历史会话")
        self.history_btn.clicked.connect(self._toggle_history_card)
        capsule_layout.addWidget(self.history_btn)

        self.new_session_btn = TransparentToolButton(get_icon("新会话"), self._toolbar_capsule)
        self.new_session_btn.setFixedSize(22, 22)
        self.new_session_btn.setStyleSheet(btn_capsule_style)
        self.new_session_btn.setToolTip("新建对话")
        self.new_session_btn.clicked.connect(self._create_new_session)
        capsule_layout.addWidget(self.new_session_btn)

        toolbar_layout.addWidget(self._toolbar_capsule)

        card_layout.addWidget(toolbar_widget)

        bottom_layout.addWidget(self._input_card)

        layout.addWidget(self._bottom_input_container)

    def _show_model_selector_popup(self):
        """显示扁平式模型选择上拉框"""
        provider_models_data = []
        for provider_name, config in self._valid_configs.items():
            model_list = []
            if "模型列表" in config:
                saved_models = config["模型列表"]
                if isinstance(saved_models, str):
                    try:
                        import ast;
                        saved_models = ast.literal_eval(saved_models)
                    except Exception:
                        saved_models = []
                if isinstance(saved_models, list):
                    model_list = list(saved_models)
            elif provider_name in PROVIDER_MODELS:
                model_list = list(PROVIDER_MODELS[provider_name])
            cur_model = config.get("模型名称", "")
            if cur_model and cur_model not in model_list:
                model_list.insert(0, cur_model)
            if not model_list and cur_model:
                model_list = [cur_model]
            is_current = provider_name == self._current_provider_name
            provider_models_data.append((provider_name, model_list, is_current))

        if not hasattr(self, "_model_selector_popup") or not self._model_selector_popup:
            from app.widgets.model_selector_popup import (
                ModelSelectorPopup, )
            self._model_selector_popup = ModelSelectorPopup(self)
            self._model_selector_popup.modelSelected.connect(self._on_model_selected_from_popup)
            self._model_selector_popup.addProviderClicked.connect(
                lambda: self._on_add_provider_from_popup()
            )
            self._model_selector_popup.configureProviderClicked.connect(
                lambda: self._on_configure_providers_from_popup()
            )

        self._model_selector_popup.set_providers_data(
            provider_models_data, self._current_provider_name or "", self._current_model_name or "",
        )
        self._model_selector_popup.show_at(self.current_model_btn)

    def _on_add_provider_from_popup(self):
        """从模型选择弹窗点击「添加」按钮 - 显示添加服务商卡片"""
        self._model_selector_popup.close()
        self._show_provider_add_card()

    def _on_configure_providers_from_popup(self):
        """从模型选择弹窗点击「配置」按钮 - 显示设置卡片并展开服务商下拉"""
        self._model_selector_popup.close()
        # 显示设置卡片
        self._settings_popup.show()
        self._settings_popup.raise_()
        # 滚动设置卡片内容到顶部
        QTimer.singleShot(100, self._scroll_settings_to_top)
        # 展开服务商下拉
        QTimer.singleShot(200, lambda: self._expand_provider_list_card())

    def _scroll_settings_to_top(self):
        """滚动设置卡片内容到顶部"""
        try:
            # 找到 LLMSettingsCard 内部的 QScrollArea 并滚到顶
            scroll_areas = self._settings_popup.findChildren(QScrollArea)
            if scroll_areas:
                scroll_areas[0].verticalScrollBar().setValue(0)
        except Exception:
            pass

    def _expand_provider_list_card(self):
        """展开服务商列表卡片"""
        try:
            if hasattr(self._settings_popup, 'llmProviderCard'):
                self._settings_popup.llmProviderCard.toggleExpand()
        except Exception:
            pass

    def _on_model_selected_from_popup(self, provider_name: str, model_name: str):
        """从弹窗选中模型后切换
        
        多窗口隔离：全局配置保存最后使用的服务高（作为新窗口默认值），
        但窗口实例的 _current_provider_name/_current_model_name 不受其他窗口影响。
        """
        self._current_provider_name = provider_name
        self._current_model_name = model_name
        # 保存到全局配置（作为新窗口的默认值，不影响当前窗口实例）
        self.cfg.set(self.cfg.llm_selected_model, provider_name, save=True)

        # 更新 saved_providers 中的模型名称
        saved_providers = self.cfg.llm_saved_providers.value or {}
        if provider_name in saved_providers:
            saved_providers[provider_name]["模型名称"] = model_name
            self.cfg.set(self.cfg.llm_saved_providers, saved_providers, save=True)

        # 更新 _valid_configs 确保 ChatEngine 能读到最新配置
        self._valid_configs[provider_name] = saved_providers.get(provider_name, {}).copy()
        self._valid_configs[provider_name]["模型名称"] = model_name

        # 重新加载模型配置（_load_model_configs 已修复：保持窗口自身选择优先）
        self._load_model_configs()

        self._update_model_selector_btn()
        self._refresh_context_usage_indicator()
        self._update_balance_display()

    def _update_model_selector_btn(self):
        """更新模型选择按钮的图标和文字显示"""
        if not hasattr(self, "current_model_btn"):
            return
        # 设置图标
        icon = None
        if self._current_provider_name:
            icon_name = PROVIDER_ICONS.get(self._current_provider_name, "")
            if icon_name:
                icon = get_icon(icon_name)

        if icon and not icon.isNull():
            self._model_btn_icon.setPixmap(icon.pixmap(18, 18))
        else:
            self._model_btn_icon.clear()

        # 设置文字
        if self._current_provider_name and self._current_model_name:
            self._model_btn_text.setText(self._current_model_name)
            self.current_model_btn.setToolTip(f"{self._current_provider_name} · {self._current_model_name}")
        elif self._current_provider_name:
            self._model_btn_text.setText(self._current_provider_name)
            self.current_model_btn.setToolTip(self._current_provider_name)
        else:
            self._model_btn_text.setText("选择模型...")
            self.current_model_btn.setToolTip("")

        self._update_balance_display()

    def _on_context_selection_changed(self, _selected_keys=None):
        self._refresh_context_usage_indicator()

    def _refresh_context_usage_indicator(self):
        ring = getattr(self, "context_usage_ring", None)
        if not ring:
            return

        session = self.session_manager.get_current_session()
        llm_config = self._get_current_model_config()
        snapshot = self.backend.get_context_usage_snapshot(session, llm_config)
        ring.set_usage(
            snapshot.get("percent", 0),
            snapshot.get("used_tokens", 0),
            snapshot.get("budget_tokens", 0),
            snapshot.get("compaction", {}),
            snapshot.get("normal_tokens", 0),
            snapshot.get("compacted_tokens", 0),
        )

    def _update_balance_display(self):
        """更新余额显示"""
        balance_display = getattr(self, "balance_display", None)
        if not balance_display:
            return

        # 获取当前选中的服务商配置
        provider_name = getattr(self, "_current_provider_name", "")
        if not provider_name:
            balance_display.clear()
            return

        config = self._valid_configs.get(provider_name, {})
        api_key = config.get("API_KEY", "")

        balance_display.set_provider(provider_name, api_key)

    def _open_settings_popup(self):
        """打开设置卡片"""
        self._hide_main_popups()
        self._settings_popup.show()
        self._settings_popup.raise_()
        self._settings_popup.activateWindow()

    def _on_provider_edit_saved(self, provider_name: str, provider_info: dict):
        """服务商编辑保存后的回调"""
        saved_providers = self.cfg.llm_saved_providers.value or {}
        saved_providers[provider_name] = provider_info
        self.cfg.set(self.cfg.llm_saved_providers, saved_providers, save=True)

        # 隐藏服务商编辑卡片，显示设置卡片
        self._provider_edit_card.hide()
        self._settings_popup.show()

        # 关闭模型选择器popup，下次打开会重新加载数据
        if hasattr(self, '_model_selector_popup') and self._model_selector_popup:
            self._model_selector_popup.close()

        # 刷新配置
        self._load_model_configs()
        InfoBar.success("已保存", f"服务商 '{provider_name}' 已保存", parent=self, duration=2000,
                        position=InfoBarPosition.BOTTOM)

    def _on_provider_edit_closed(self):
        """服务商编辑关闭后的回调"""
        # 隐藏服务商编辑卡片，显示设置卡片
        self._provider_edit_card.hide()
        self._settings_popup.show()
        # 关闭模型选择器popup，确保下次打开重新加载数据
        if hasattr(self, '_model_selector_popup') and self._model_selector_popup:
            self._model_selector_popup.close()
        self._restore_after_system_close()

    def _show_provider_add_card(self):
        """显示添加服务商卡片"""
        # 隐藏设置卡片，显示服务商编辑卡片
        self._settings_popup.hide()
        # 设置卡片标题
        self._provider_edit_card.set_title("⚙️ 添加服务商")
        # 重新创建 ProviderEditCard 用于添加
        self._provider_edit_popup = ProviderEditCard(provider_name="", provider_info={}, is_new=True, parent=self)
        self._provider_edit_popup.saved.connect(self._on_provider_edit_saved)
        self._provider_edit_popup.closed.connect(self._on_provider_edit_closed)
        # 替换卡片内容
        while self._provider_edit_card.content_layout.count():
            item = self._provider_edit_card.content_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._provider_edit_card.content_layout.addWidget(self._provider_edit_popup)
        # 重新绑定保存按钮
        self._provider_edit_card.set_save_button_handler(
            lambda: self._provider_edit_popup._on_save()
        )
        self._provider_edit_card.show()

    # ========== Hook 编辑卡片 ==========

    def _show_hook_add_card(self):
        """显示添加 Hook 卡片"""
        from app.widgets.cards.settings.hook_setting_card import HookEditCard
        self._settings_popup.hide()
        self._hook_edit_card.set_title("➕ 添加 Hook")
        # 重新创建 HookEditCard
        self._hook_edit_popup = HookEditCard(parent=self)
        self._hook_edit_popup.saved.connect(self._on_hook_edit_saved)
        self._hook_edit_popup.closed.connect(self._on_hook_edit_closed)
        while self._hook_edit_card.content_layout.count():
            item = self._hook_edit_card.content_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._hook_edit_card.content_layout.addWidget(self._hook_edit_popup)
        self._hook_edit_card.set_save_button_handler(
            lambda: self._hook_edit_popup._on_save()
        )
        self._hook_edit_card.show()

    def _show_hook_edit_card(self, event: str, hook_data: dict):
        """显示编辑 Hook 卡片"""
        from app.widgets.cards.settings.hook_setting_card import HookEditCard
        self._settings_popup.hide()
        self._hook_edit_card.set_title("✏️ 编辑 Hook")
        # 创建携带原始数据的 HookEditCard
        self._hook_edit_popup = HookEditCard(hook_data=hook_data, parent=self)
        self._hook_edit_popup.saved.connect(self._on_hook_edit_saved)
        self._hook_edit_popup.closed.connect(self._on_hook_edit_closed)
        while self._hook_edit_card.content_layout.count():
            item = self._hook_edit_card.content_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._hook_edit_card.content_layout.addWidget(self._hook_edit_popup)
        self._hook_edit_card.set_save_button_handler(
            lambda: self._hook_edit_popup._on_save()
        )
        self._hook_edit_card.show()

    def _on_hook_edit_saved(self, values: dict):
        """Hook 保存回调"""
        self._hook_edit_card.hide()
        if hasattr(self._settings_popup, 'hookListCard'):
            original = self._hook_edit_popup.get_original_data()
            if original:
                # 编辑已有 hook
                orig_event = original.get("_event", values["event"])
                orig_command = original.get("command", "")
                orig_matcher = original.get("matcher", "")
                self._settings_popup.hookListCard._update_hook(
                    original_event=orig_event,
                    original_command=orig_command,
                    original_matcher=orig_matcher,
                    new_values=values
                )
            else:
                # 新增 hook
                self._settings_popup.hookListCard._add_hook(
                    event=values["event"],
                    command=values["command"],
                    matcher=values["matcher"],
                    hook_type=values["type"],
                    enabled=values["enabled"]
                )
        self._settings_popup.show()

    def _on_hook_edit_closed(self):
        """Hook 编辑关闭回调"""
        self._hook_edit_card.hide()
        self._settings_popup.show()
        self._restore_after_system_close()

    def _show_provider_edit_card(self, provider_name: str, provider_info: dict):
        """显示编辑服务商卡片"""
        # 隐藏设置卡片，显示服务商编辑卡片
        self._settings_popup.hide()
        # 设置卡片标题
        self._provider_edit_card.set_title(f"⚙️ 编辑: {provider_name}")
        # 重新创建 ProviderEditCard 用于编辑
        self._provider_edit_popup = ProviderEditCard(
            provider_name=provider_name, provider_info=provider_info, is_new=False, parent=self
        )
        self._provider_edit_popup.saved.connect(self._on_provider_edit_saved)
        self._provider_edit_popup.closed.connect(self._on_provider_edit_closed)
        # 替换卡片内容
        while self._provider_edit_card.content_layout.count():
            item = self._provider_edit_card.content_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._provider_edit_card.content_layout.addWidget(self._provider_edit_popup)
        # 重新绑定保存按钮
        self._provider_edit_card.set_save_button_handler(
            lambda: self._provider_edit_popup._on_save()
        )
        self._provider_edit_card.show()

    # ========== MCP 编辑卡片 ==========

    def _show_mcp_add_card(self):
        """显示添加 MCP 服务器卡片"""
        self._settings_popup.hide()
        self._mcp_edit_card.set_title("🔌 添加 MCP 服务器")
        self._mcp_edit_popup = MCPEditCard(server_data=None, parent=self)
        self._mcp_edit_popup.saved.connect(self._on_mcp_edit_saved)
        self._mcp_edit_popup.closed.connect(self._on_mcp_edit_closed)
        while self._mcp_edit_card.content_layout.count():
            item = self._mcp_edit_card.content_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._mcp_edit_card.content_layout.addWidget(self._mcp_edit_popup)
        self._mcp_edit_card.set_save_button_handler(
            lambda: self._mcp_edit_popup._on_save()
        )
        self._setup_mcp_edit_mode_buttons()
        self._mcp_edit_card.show()

    def _show_mcp_edit_card(self, name: str, server_data: dict):
        """显示编辑 MCP 服务器卡片"""
        self._settings_popup.hide()
        self._mcp_edit_card.set_title(f"🌐 编辑: {name}")
        self._mcp_edit_popup = MCPEditCard(server_data=server_data, parent=self)
        self._mcp_edit_popup.saved.connect(self._on_mcp_edit_saved)
        self._mcp_edit_popup.closed.connect(self._on_mcp_edit_closed)
        while self._mcp_edit_card.content_layout.count():
            item = self._mcp_edit_card.content_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._mcp_edit_card.content_layout.addWidget(self._mcp_edit_popup)
        self._mcp_edit_card.set_save_button_handler(
            lambda: self._mcp_edit_popup._on_save()
        )
        self._setup_mcp_edit_mode_buttons()
        self._mcp_edit_card.show()

    def _setup_mcp_edit_mode_buttons(self):
        """设置 MCP 编辑卡头的模式切换按钮"""
        self._mcp_edit_popup.modeChanged.connect(self._refresh_mcp_mode_buttons)
        self._refresh_mcp_mode_buttons(self._mcp_edit_popup._json_mode)

    def _refresh_mcp_mode_buttons(self, is_json: bool):
        """刷新 MCP 编辑卡头的模式切换按钮状态"""
        self._mcp_edit_card.set_mode_buttons([
            {"label": "表单", "active": not is_json, "handler": lambda: self._try_toggle_to_form()},
            {"label": "JSON", "active": is_json, "handler": lambda: self._try_toggle_to_json()},
        ])

    def _try_toggle_to_form(self):
        if self._mcp_edit_popup and not self._mcp_edit_popup._json_mode:
            return
        self._mcp_edit_popup._toggle_mode()

    def _try_toggle_to_json(self):
        if self._mcp_edit_popup and self._mcp_edit_popup._json_mode:
            return
        self._mcp_edit_popup._toggle_mode()

    def _on_mcp_edit_saved(self, server_data: dict):
        """MCP 编辑保存回调"""
        self._mcp_edit_card.hide()
        self._settings_popup.show()
        if hasattr(self._settings_popup, 'mcpListCard'):
            name = server_data.get("name", "")
            servers = list(self._settings_popup.mcpListCard.cfg.mcp_servers.value or [])
            is_edit = any(s.get("name") == name for s in servers)
            if is_edit:
                self._settings_popup.mcpListCard.update_server(name, server_data)
            else:
                self._settings_popup.mcpListCard.add_server(server_data)
            # 确保连接状态同步
            QTimer.singleShot(500, self._settings_popup.mcpListCard.refresh_connections)

    def _on_mcp_edit_closed(self):
        """MCP 编辑关闭回调"""
        self._mcp_edit_card.hide()
        if hasattr(self._settings_popup, 'mcpListCard'):
            self._settings_popup.mcpListCard.refresh_connections()
        self._card_manager.restore_after_hide(excluded_card_id="mcp_edit")

    def _on_hook_edit_card_closed(self):
        """Hook 编辑卡片（SystemCardFrame）关闭回调 → 回到设置面板"""
        self._hook_edit_card.hide()
        self._card_manager.restore_after_hide(excluded_card_id="hook_edit")

    def _on_provider_edit_card_closed(self):
        """服务商编辑卡片（SystemCardFrame）关闭回调 → 回到设置面板"""
        self._provider_edit_card.hide()
        self._card_manager.restore_after_hide(excluded_card_id="provider_edit")

    def _on_mcp_edit_card_closed(self):
        """MCP 编辑卡片（SystemCardFrame）关闭回调 → 回到设置面板"""
        self._mcp_edit_card.hide()

    def _hide_main_popups(self):
        """隐藏主要的悬浮面板（互斥显示）

        包括：系统设置、模型配置、历史会话、记忆管理、AutoLoop
        现在也保存并隐藏 todo/tool/sub_agent 实时卡片
        """
        # 标记系统卡片打开状态，阻止实时卡片自行显示
        self._is_system_card_visible = True
        # 保存 todo 可见状态，用于系统卡片关闭后恢复
        self._todo_was_visible_before_system = self._todo_floating_widget.isVisible()
        # 隐藏实时卡片
        self._todo_floating_widget.setVisible(False)
        self._tool_floating_widget.setVisible(False)
        self._tool_floating_widget.set_suppress_visible(True)  # 压制工具卡片显示
        self._sub_agent_floating_widget.setVisible(False)
        # 隐藏系统卡片
        self._model_config_card.hide()
        self._history_card.hide()
        self._settings_popup.hide()
        self._memory_card.hide()
        self._provider_edit_card.hide()
        self._auto_loop_config_card.hide()
        if not self._is_auto_loop_running:
            self._auto_loop_running_card.hide()

    def _system_cards(self) -> list:
        """返回所有系统卡片的列表，用于检查是否有系统卡片可见"""
        return [
            self._model_config_card,
            self._history_card,
            self._settings_popup,
            self._memory_card,
            self._provider_edit_card,
            self._auto_loop_config_card,
            self._hook_edit_card,
        ]

    def _is_any_system_card_visible(self) -> bool:
        """检查是否有任何系统卡片可见"""
        for card in self._system_cards():
            if card.isVisible():
                return True
        return False

    def _apply_bg_from_theme(self):
        """从当前主题配置加载背景图片"""
        try:
            from app.utils.theme_manager import theme_manager
            from app.utils.design_tokens import Colors
            Colors.refresh()
            bg_config = theme_manager.get_theme_background(theme_manager.get_current_theme_id())
            chat_list = bg_config.get("chat_list", {})
            if chat_list.get("enabled", True):
                image = chat_list.get("image", ":/icons/fox_bg.png")
                opacity = chat_list.get("opacity", 0.1)
                viewport = self.chat_scroll_area.viewport()
                # 先清除旧背景
                if hasattr(self, '_bg_label') and self._bg_label is not None:
                    self._bg_label.deleteLater()
                    self._bg_label = None
                self._bg_label = QLabel(viewport)
                self._bg_label.setPixmap(QPixmap(image))
                self._bg_label.setScaledContents(True)
                self._bg_opacity = QGraphicsOpacityEffect(self._bg_label)
                self._bg_opacity.setOpacity(opacity)
                self._bg_label.setGraphicsEffect(self._bg_opacity)
                self._bg_label.lower()
                self._bg_label.setAttribute(Qt.WA_TransparentForMouseEvents)
                self._bg_label.resize(viewport.size())
                self._bg_label.show()
                viewport.installEventFilter(self)
            else:
                # 主题禁用背景图，清除旧背景
                if hasattr(self, '_bg_label') and self._bg_label is not None:
                    self._bg_label.deleteLater()
                    self._bg_label = None
        except Exception:
            pass
    def _restore_after_system_close(self):
        """系统卡片关闭后，恢复之前被隐藏的卡片
        现在由 CardManager 的 restore_after_hide 处理
        """
        pass  # CardManager 会自动处理恢复

    def _toggle_model_config_card(self):
        """切换模型配置卡片的显示"""
        self._card_manager.toggle_card("model_config")

    def _load_model_config_to_card(self):
        """加载当前模型配置到卡片（仅参数配置，不显示连接信息）"""
        current_name = self._current_provider_name if self._current_provider_name else "无"

        saved_providers = self.cfg.llm_saved_providers.value or {}
        provider_config = saved_providers.get(current_name, {})
        config = provider_config.copy()
        # 合并默认配置，确保新增字段（如思考模式）在已保存的配置中也存在
        default_config = FREE_PROVIDERS.get(current_name, {})
        for default_key, default_value in default_config.items():
            if default_key not in config:
                config[default_key] = default_value
        config.pop("备注", None)
        config.pop("获取地址", None)
        # 只保留参数配置，移除连接信息
        config.pop("模型名称", None)
        config.pop("API_URL", None)
        config.pop("API_KEY", None)
        config.pop("模型列表", None)

        self._model_config_popup.set_config(current_name, config)

    def _create_agent_switch_buttons(self) -> QWidget:
        """创建智能体切换按钮 - 单胶囊设计，中间用分隔线"""
        Colors.refresh()
        container = QWidget()
        container.setFixedHeight(26)
        container.setStyleSheet(f"""
            background: rgba(255,255,255,0.05);
            border: none;
            border-radius: 8px;
        """)
        layout = QHBoxLayout(container)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(0)

        # 加载智能体列表
        agents = self.backend.get_primary_agents() if self.backend else []

        # 如果没有智能体，显示占位文本
        if not agents:
            placeholder = QLabel("无可用智能体")
            placeholder.setStyleSheet(f"""
                QLabel {{
                    color: #8FA4C2;
                    font-size: 12px;
                    padding: 0 12px;
                    {get_font_family_css()}
                }}
            """)
            layout.addWidget(placeholder)
            return container

        # 默认选中的智能体
        default_agent = getattr(self, '_current_agent', 'plan')

        self._agent_buttons = {}
        self._agent_btn_group = QButtonGroup()
        self._agent_btn_group.buttonClicked[int].connect(self._on_agent_btn_clicked)

        # 默认样式
        default_style = self._build_agent_btn_style(active=False)

        # 选中样式
        selected_style = self._build_agent_btn_style(active=True)

        for i, agent in enumerate(agents):
            # 添加分隔线（在按钮之前，除了第一个）
            if i > 0:
                sep = QFrame()
                sep.setFrameShape(QFrame.VLine)
                sep.setFixedWidth(1)
                Colors.refresh()
                sep.setStyleSheet(f"background: {Colors.AGENT_BTN_SEPARATOR}; margin: 4px 0;")
                layout.addWidget(sep)

            btn = QPushButton(agent.name)
            btn.setFixedHeight(22)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setCheckable(True)
            btn.setStyleSheet(default_style)
            btn.setToolTip(agent.description)

            self._agent_btn_group.addButton(btn, i)
            self._agent_buttons[agent.name] = {"btn": btn, "style": default_style, "selected_style": selected_style}
            layout.addWidget(btn)

            # 如果是默认选中的智能体，则选中它
            if agent.name == default_agent:
                btn.setChecked(True)
                btn.setStyleSheet(selected_style)

        # 如果没有匹配默认智能体，选中第一个
        if default_agent not in self._agent_buttons and agents:
            btn = self._agent_buttons[agents[0].name]["btn"]
            btn.setChecked(True)
            btn.setStyleSheet(selected_style)
            self._current_agent = agents[0].name

        return container

    def _on_agent_btn_clicked(self, btn_id: int):
        """智能体按钮点击处理"""
        agents = self.backend.get_primary_agents()
        if btn_id >= len(agents):
            return

        agent = agents[btn_id]
        agent_name = agent.name

        logger.info(
            f"[_on_agent_btn_clicked] btn_id={btn_id}, agent_name={agent_name}, _current_agent before={self._current_agent}")

        # 更新按钮样式
        for name, data in self._agent_buttons.items():
            btn = data["btn"]
            if name == agent_name:
                btn.setStyleSheet(data["selected_style"])
            else:
                btn.setStyleSheet(data["style"])

        # 触发智能体切换
        self._on_agent_changed(agent_name)

    def _build_agent_btn_style(self, active: bool = False) -> str:
        """构建智能体按钮样式（动态从 Colors 读取）"""
        Colors.refresh()
        if active:
            return f"""
                QPushButton {{
                    background: {Colors.AGENT_BTN_BG_ACTIVE};
                    color: {Colors.AGENT_BTN_TEXT_ACTIVE};
                    border: none;
                    border-radius: 8px;
                    padding: 4px 12px;
                    font-size: 12px;
                    font-weight: 600;
                    {get_font_family_css()}
                }}
                QPushButton:hover {{
                    background: {Colors.AGENT_BTN_BG_ACTIVE};
                }}
            """
        return f"""
            QPushButton {{
                background: transparent;
                color: {Colors.AGENT_BTN_TEXT};
                border: none;
                border-radius: 8px;
                padding: 4px 12px;
                font-size: 12px;
                font-weight: 500;
                {get_font_family_css()}
            }}
            QPushButton:hover {{
                background: rgba(255, 255, 255, 0.05);
                color: {Colors.TEXT_PRIMARY};
            }}
        """

    def _refresh_agent_button_styles(self):
        """刷新所有智能体按钮样式（响应主题切换）"""
        if not hasattr(self, "_agent_buttons"):
            return
        Colors.refresh()
        for name, data in self._agent_buttons.items():
            is_active = name == self._current_agent
            new_style = self._build_agent_btn_style(active=is_active)
            if is_active:
                data["selected_style"] = new_style
            else:
                data["style"] = new_style
            data["btn"].setStyleSheet(new_style)

    def _toggle_history_card(self):
        """切换历史会话卡片的显示"""
        self._card_manager.toggle_card("history")

    def _sync_search_box_visibility(self):
        """同步搜索框：两个标签页都显示搜索框"""
        search_input = getattr(self._history_card, '_search_input', None)
        if not search_input:
            return
        search_input.setVisible(True)
        search_input.setFocus()

        # 根据当前标签更新占位文本
        current_tab = self._history_card._current_tab if hasattr(self._history_card, '_current_tab') else "history"
        search_input.setPlaceholderText("🔍 搜索历史会话..." if current_tab == "history" else "🔍 搜索归档会话...")

    def _refresh_history_toggle_panel(self, is_archived: bool = False):
        """刷新历史面板数据"""
        if not self._history_card:
            return

        current_tab = self._history_card._current_tab if hasattr(self._history_card, '_current_tab') else "history"

        if current_tab == "history" or is_archived:
            # 获取当前项目的历史会话列表
            history_list = self.history_manager.get_history_list(self._current_project) if self.history_manager else []
            # 在项目过滤后的列表中查找当前会话的位置
            current_idx = None
            if self._current_session_id and self.history_manager:
                for i, session in enumerate(history_list):
                    if session.get("session_id") == self._current_session_id:
                        current_idx = i
                        break
            # 归档操作后需要清理归档会话列表
            if is_archived:
                self._history_popup_card.set_history(history_list, current_idx, clear_archived=True)
            else:
                self._history_popup_card.set_history(history_list, current_idx)
        else:
            # 刷新归档会话
            self._refresh_archived_sessions()

    def _on_history_project_selected(self, project: str):
        """历史面板项目切换（现在和标题栏同步）"""
        self._current_project = project
        self.backend._current_project = project
        self._current_history_project = project
        self._history_popup_card.set_current_project(project)
        self._refresh_history_toggle_panel()

    def _on_session_dropped_on_project(self, project: str, session_index: int):
        """将会话拖拽到指定项目"""
        if not self.history_manager:
            return
        history_list = self.history_manager.get_history_list(self._current_project) if self.history_manager else []
        if 0 <= session_index < len(history_list):
            # 获取 session_id
            session = history_list[session_index]
            session_id = session.get("session_id")
            if session_id:
                # 更新项目的 session 记录
                idx = self.history_manager.find_index_by_session_id(session_id)
                if idx is not None:
                    self.history_manager.move_to_project(idx, project)
                    # 刷新
                    self._history_popup_card.refreshRequested.emit()
                    InfoBar.success("已移动", f"会话已移至「{project}」项目", duration=2000, parent=self,
                                    position=InfoBarPosition.BOTTOM)

    def _refresh_archived_sessions(self):
        """刷新归档会话列表"""
        if not self.history_manager:
            return

        archived_list = self.history_manager.get_archived_sessions()
        # 为每个归档会话添加预览信息
        for session in archived_list:
            try:
                with open(session["path"], "r", encoding="utf-8") as f:
                    content = f.read()
                    data = json.loads(content)
                    messages = data.get("messages", [])
                    session["message_count"] = data.get("message_count",
                                                        len([m for m in messages if m.get("role") == "user"]))
                    session["last_time"] = data.get("last_time", data.get("saved_at", ""))
                    session["preview"] = get_message_preview(messages) if messages else ""
            except Exception:
                pass

        self._history_popup_card.set_archived_sessions(archived_list)

    def _on_history_tab_changed(self, tab_id: str):
        """处理历史/归档标签切换"""
        self._sync_search_box_visibility()

        # 切换标签时清空搜索
        search_input = getattr(self._history_card, '_search_input', None)
        if search_input:
            search_input.clear()

        self._history_popup_card.switch_tab(tab_id)

        if tab_id == "archived":
            self._refresh_archived_sessions()
        else:
            self._refresh_history_toggle_panel()

    def _on_history_session_selected(self, index: int):
        """从历史面板选择会话"""
        if getattr(self, '_is_destroyed', False):
            return
        if index == -1:
            # 新建会话
            self._create_new_session()
        else:
            self._load_history_session_from_popup(index)
        # 关闭历史会话卡片
        self._history_card.hide()
        self._restore_after_system_close()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._set_cards_resize_preview_mode(True)
        # resize 期间持续重置防抖，避免在拖拽过程中提前批量重排
        self._pending_resize_sync = True
        self._resize_debounce_timer.stop()
        self._resize_debounce_timer.start()
        self._resize_complete_timer.stop()
        self._resize_complete_timer.start()

    def _set_cards_resize_preview_mode(self, enabled: bool):
        if enabled == self._resize_preview_active:
            return

        self._resize_preview_active = enabled
        for i in range(self.chat_layout.count()):
            item = self.chat_layout.itemAt(i)
            if not (item and item.widget() and isinstance(item.widget(), MessageCard)):
                continue
            item.widget().set_resize_preview_mode(enabled)

    def _do_debounced_resize(self):
        """防抖执行卡片宽度同步 - resize 期间同步所有可见卡片宽度"""
        self._pending_resize_sync = False

        # 获取滚动区域视口
        scroll_area = getattr(self, 'chat_scroll_area', None)
        if scroll_area:
            viewport_width = scroll_area.viewport().width()
            if viewport_width <= 0:
                return
            self._last_chat_viewport_width = viewport_width
            viewport_rect = scroll_area.viewport().rect()
            viewport_top = scroll_area.verticalScrollBar().value()
            viewport_bottom = viewport_top + viewport_rect.height()

        for i in range(self.chat_layout.count()):
            item = self.chat_layout.itemAt(i)
            if not (item and item.widget() and isinstance(item.widget(), MessageCard)):
                continue

            card = item.widget()

            # resize 期间同步所有卡片的宽度，不做可见性过滤
            # 占位符模式下只更新宽高，不触发复杂重绘
            card.sync_width()

    def _sync_all_cards_width(self):
        """resize 完成后更新所有卡片的宽度（包括非可见区域的）"""
        scroll_area = getattr(self, 'chat_scroll_area', None)
        if scroll_area:
            viewport_width = scroll_area.viewport().width()
            if viewport_width > 0:
                self._last_chat_viewport_width = viewport_width
        for i in range(self.chat_layout.count()):
            item = self.chat_layout.itemAt(i)
            if item and item.widget() and isinstance(item.widget(), MessageCard):
                item.widget().sync_width(force=True)
        self._set_cards_resize_preview_mode(False)

    def _sync_visible_cards_on_scroll(self):
        """滚动时更新新进入可见区域的卡片"""
        scroll_area = getattr(self, 'chat_scroll_area', None)
        if not scroll_area:
            return

        viewport_rect = scroll_area.viewport().rect()
        viewport_top = scroll_area.verticalScrollBar().value()
        viewport_bottom = viewport_top + viewport_rect.height()

        for i in range(self.chat_layout.count()):
            item = self.chat_layout.itemAt(i)
            if not (item and item.widget() and isinstance(item.widget(), MessageCard)):
                continue

            card = item.widget()
            card_rect = card.geometry()
            card_top = card_rect.top()
            card_bottom = card_rect.bottom()

            # 只更新可见区域附近（缓冲200px）的卡片
            if card_bottom < viewport_top - 200 or card_top > viewport_bottom + 200:
                continue

            card.sync_width()

    def _on_config_applied(self, new_config: dict):
        if getattr(self, '_is_destroyed', False):
            return
        current_name = self._current_provider_name
        if not current_name:
            return
        # 只更新参数，保留连接信息
        saved_providers = self.cfg.llm_saved_providers.value or {}
        old_config = saved_providers.get(current_name, self._valid_configs.get(current_name, {}))
        old_config.update(new_config)
        self._valid_configs[current_name] = old_config
        saved_providers[current_name] = old_config
        self.cfg.set(self.cfg.llm_saved_providers, saved_providers, save=True)
        self._load_model_configs()
        InfoBar.success("已保存", "配置已保存到本地。", parent=self, duration=1500, position=InfoBarPosition.BOTTOM)

    def _on_settings_config_changed(self):
        self._load_model_configs()
        self._apply_runtime_ui_settings()

    def _apply_runtime_ui_settings(self):
        Colors.refresh()
        from app.utils.theme_manager import theme_manager
        colors = theme_manager.get_current_colors()
        
        # 窗口淡背景（保留原始 alpha）
        window_bg = colors.get('window_bg', 'rgba(102, 198, 255, 0.04)')
        self._window_bg_color = window_bg  # 保存供透明度变化时使用
        
        import re
        m = re.match(r'rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?\)', window_bg)
        if m:
            r, g, b = int(m.group(1)), int(m.group(2)), int(m.group(3))
            alpha = int(float(m.group(4)) * 255) if m.group(4) else 10
            from PyQt5.QtGui import QColor, QPalette
            color = QColor(r, g, b, alpha)
            p = QPalette()
            p.setColor(QPalette.Window, color)
            self.setPalette(p)
            self.setAutoFillBackground(True)
        
        if hasattr(self, "_project_label"):
            self._update_project_label_style()
        if hasattr(self, "title_edit"):
            title_style = TITLE_STYLE.replace("    QLabel {", f"    QLabel {{\n        {get_font_family_css()}")
            title_style = title_style.replace("font-size: 15px;", font_size_css(15))
            title_style = title_style.replace("#f3f6fc", Colors.TEXT_PRIMARY)
            self.title_edit.setStyleSheet(title_style)
        # 刷新输入卡片背景
        if hasattr(self, '_input_card'):
            self._input_card.setStyleSheet(f"""
                QWidget {{
                    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                        stop:0 {Colors.INPUT_BG_START},
                        stop:1 {Colors.INPUT_BG_END});
                    border: 1px solid {Colors.INPUT_BORDER};
                    border-radius: 16px;
                }}
            """)
        if hasattr(self, "_model_btn_container"):
            self._model_btn_container.setStyleSheet(f"""
                background: rgba(255,255,255,0.05);
                border: none;
                border-radius: 8px;
            """)
        if hasattr(self, "_model_btn_text"):
            btn_text_style = MODEL_BTN_TEXT_STYLE.replace("font-size: 13px;", font_size_css(13))
            btn_text_style = btn_text_style.replace("#f3f6fc", Colors.TEXT_PRIMARY)
            self._model_btn_text.setStyleSheet(btn_text_style)
        if hasattr(self, "_toolbar_capsule"):
            self._toolbar_capsule.setStyleSheet(f"""
                background: rgba(255,255,255,0.05);
                border: none;
                border-radius: 8px;
            """)
        if hasattr(self, "input_area"):
            setFont(self.input_area, scale_font_size(15))
            if hasattr(self.input_area, "refresh_style"):
                self.input_area.refresh_style()
        # 刷新设置弹出层 — 递归刷新 qfluentwidgets 组件字体大小
        # 刷新发送按钮
        if hasattr(self, "input_area") and hasattr(self.input_area, "_apply_send_btn_style"):
            self.input_area._apply_send_btn_style()
        # 刷新背景图片
        self._apply_bg_from_theme()
        # 刷新分支标签
        if hasattr(self, "_update_branch"):
            self._update_branch()
        # 刷新时间线节点
        if hasattr(self, "node_preview") and hasattr(self.node_preview, "refresh_theme"):
            self.node_preview.refresh_theme()

        if self._settings_popup:
            apply_font_size_to_widget(self._settings_popup, 14)
            # 同时刷新所有子设置卡片的主题样式
            for frame in self._settings_popup.findChildren(SystemCardFrame):
                if hasattr(frame, 'refresh_style'):
                    frame.refresh_style()
        # 刷新智能体切换按钮样式
        if hasattr(self, "_agent_switch_widget"):
            Colors.refresh()
            self._agent_switch_widget.setStyleSheet(f"""
                background: rgba(255,255,255,0.05);
                border: none;
                border-radius: 8px;
            """)
        self._refresh_agent_button_styles()
        # 刷新设置卡片
        for card in self.findChildren(BaseSettingsCard):
            if hasattr(card, "refresh_style"):
                card.refresh_style()
        if self._settings_popup and hasattr(self._settings_popup, "refresh_style"):
            self._settings_popup.refresh_style()
        # 刷新 AutoLoop 卡片主题
        if self._auto_loop_config_card and hasattr(self._auto_loop_config_card, '_refresh_theme_style'):
            self._auto_loop_config_card._refresh_theme_style()
        if self._auto_loop_running_card and hasattr(self._auto_loop_running_card, '_refresh_theme_style'):
            self._auto_loop_running_card._refresh_theme_style()
        # 刷新实时卡片主题（todo/tool/question/sub_agent）
        for card in (self._todo_floating_widget, self._tool_floating_widget,
                     self._question_floating_widget, self._sub_agent_floating_widget):
            if card and hasattr(card, 'refresh_style'):
                card.refresh_style()
        # 刷新消息卡片主题
        for card in self.findChildren(MessageCard):
            if hasattr(card, "refresh_theme"):
                card.refresh_theme()
            viewer = getattr(card, "viewer", None)
            if viewer and hasattr(viewer, "_schedule_render"):
                viewer._schedule_render(immediate=True)
        # 递归刷新所有 qfluentwidgets 组件字体大小
        apply_font_size_to_widget(self, 14)
        # 刷新 WorktreeSectionWidget 主题（用于系统字体大小切换）
        from app.widgets.worktree_section import WorktreeSectionWidget
        for wt_widget in self.findChildren(WorktreeSectionWidget):
            wt_widget.refresh_style()
        # 刷新模型选择弹窗和项目弹窗主题
        if hasattr(self, '_model_selector_popup') and self._model_selector_popup:
            self._model_selector_popup.refresh_style()
        if hasattr(self, '_project_selector_popup') and self._project_selector_popup:
            self._project_selector_popup.refresh_style()
        # 刷新记忆卡片主题
        if hasattr(self, '_memory_card_popup') and hasattr(self._memory_card_popup, 'refresh_style'):
            self._memory_card_popup.refresh_style()

    def _load_model_configs(self):
        # 检查窗口是否仍然有效，防止在初始化期间窗口被关闭后继续执行
        if getattr(self, '_is_destroyed', False):
            return
        
        # 保存当前窗口的实例级选择状态（多窗口隔离的关键：优先保持自身选择）
        old_provider = self._current_provider_name
        old_model = self._current_model_name

        self._valid_configs.clear()

        saved_providers = self.cfg.llm_saved_providers.value or {}
        for provider_name in saved_providers:
            config = saved_providers[provider_name].copy()
            config.pop("备注", None)
            config.pop("获取地址", None)
            # 合并默认配置，确保新增字段（如思考模式）在已保存的配置中也存在
            default_config = FREE_PROVIDERS.get(provider_name, {})
            for default_key, default_value in default_config.items():
                if default_key not in config:
                    config[default_key] = default_value
            self._valid_configs[provider_name] = config

        # 恢复或设置当前选中的服务商和模型
        # 优先级：窗口自身选择 > 全局默认 > 列表第一个
        # 关键修复：多窗口场景下，不应让全局配置覆盖窗口自己的选择
        if old_provider and old_provider in self._valid_configs:
            # 优先保持当前窗口已有的选择（多窗口独立）
            self._current_provider_name = old_provider
            self._current_model_name = old_model
        else:
            # 当前选择无效（可能被删除），回退到全局默认或列表第一个
            saved_model = self.cfg.llm_selected_model.value
            if saved_model and saved_model in self._valid_configs:
                self._current_provider_name = saved_model
            else:
                self._current_provider_name = list(self._valid_configs.keys())[0] if self._valid_configs else ""
            
            # 回退时从配置中获取默认模型名称
            if self._current_provider_name:
                provider_config = self._valid_configs.get(self._current_provider_name, {})
                self._current_model_name = provider_config.get("模型名称", "")
            else:
                self._current_model_name = ""

        self._update_model_selector_btn()
        self._refresh_context_usage_indicator()

    def _load_agent_list(self):
        """加载智能体列表到按钮组（仅显示 primary agents）"""
        # 检查窗口是否仍然有效，防止在初始化期间窗口被关闭后继续执行
        if getattr(self, '_is_destroyed', False):
            return
        
        if not self.backend.agent_manager:
            return
        if not hasattr(self, "_agent_btn_group"):
            return  # 按钮组还未创建

        self._suppress_agent_intro = True
        agents = self.backend.get_primary_agents()
        buttons = self._agent_btn_group.buttons()
        default_agent = getattr(self, '_current_agent', 'build')

        # 更新按钮文本和提示
        for i, agent in enumerate(agents):
            if i < len(buttons):
                btn = buttons[i]
                btn.setText(agent.name)
                btn.setToolTip(agent.description)

        # 根据当前智能体选中对应按钮
        found = False
        for i, agent in enumerate(agents):
            if i < len(buttons) and agent.name == default_agent:
                buttons[i].setChecked(True)
                self._update_agent_button_style(default_agent)
                found = True
                logger.info(f"[_load_agent_list] Found match for {default_agent}, btn_id={i}")
                break

        if not found:
            # 如果没找到匹配的，默认选中第一个
            logger.warning(
                f"[_load_agent_list] {default_agent} not found, using agents[0]={agents[0].name if agents else 'None'}")
            if buttons:
                buttons[0].setChecked(True)
                self._current_agent = agents[0].name if agents else "build"
                self._update_agent_button_style(self._current_agent)

        # 同步 ChatEngine 的 agent
        if self.backend.chat_engine:
            self.backend.set_current_agent(self._current_agent)
            logger.info(f"[_load_agent_list] Synced ChatEngine._current_agent = {self._current_agent}")

        self._suppress_agent_intro = False

    def _update_agent_button_style(self, active_agent: str):
        """更新智能体按钮样式"""
        if not hasattr(self, "_agent_buttons"):
            return
        for name, data in self._agent_buttons.items():
            btn = data["btn"]
            if name == active_agent:
                btn.setStyleSheet(data["selected_style"])
            else:
                btn.setStyleSheet(data["style"])

    def _on_agent_changed(self, agent_name: str):
        """智能体切换处理"""
        if getattr(self, '_is_destroyed', False):
            return
        if not agent_name or not self.backend.chat_engine:
            return

        logger.info(f"[_on_agent_changed] Switching from {self._current_agent} to {agent_name}")

        self._current_agent = agent_name
        self.backend.switch_agent(agent_name)
        self._update_agent_status(agent_name)

    def _on_input_area_height_changed(self):
        """根据输入框内容自动调整卡片高度"""
        if not hasattr(self, '_input_card'):
            return
        if getattr(self, '_is_destroyed', False):
            return
        try:
            input_height = self.input_area.height()
            toolbar_height = 34
            separator_height = 1
            card_padding = 4  # 上下各2px
            card_height = input_height + separator_height + toolbar_height + card_padding
            if self._input_card.height() != card_height:
                self._input_card.setFixedHeight(card_height)
        except Exception:
            pass

    def _show_agent_intro(self, agent_name: str):
        """显示智能体介绍卡片"""
        if not self.backend.agent_manager:
            return
        agent = self.backend.get_agent(agent_name)
        if not agent:
            return

        intro_md = f"""\
### 🤖 已切换到智能体：{agent.name}

{agent.description}

"""
        card = MessageCard(parent=self, role="assistant", timestamp="系统")
        card.update_content(intro_md)
        card.finish_streaming()
        self._add_chat_widget(card)
        self._scroll_to_bottom()

    def _update_agent_status(self, agent_name: str):
        """更新智能体状态显示（按钮组模式下主要更新按钮提示）"""
        if getattr(self, '_is_destroyed', False):
            return
        agent = self.backend.get_agent(agent_name)
        if agent:
            mode = agent.mode
            hidden = "hidden" if agent.hidden else "visible"
            tooltip = f"{agent.name}: {agent.description}\nMode: {mode}, {hidden}"
            # 更新按钮组的 tooltip
            if hasattr(self, "_agent_buttons") and agent_name in self._agent_buttons:
                self._agent_buttons[agent_name]["btn"].setToolTip(tooltip)
            # 更新模型选择按钮的 tooltip
            if hasattr(self, "current_model_btn"):
                self.current_model_btn.setToolTip(f"{agent.name}: {agent.description}\nMode: {mode}, {hidden}")

    def _create_new_session(self):
        # 检查窗口是否仍然有效，防止在初始化期间窗口被关闭后继续执行
        if getattr(self, '_is_destroyed', False):
            logger.debug("[OpenAIChatToolWindow] Window destroyed before session creation, skipping")
            return
        try:
            from PyQt5 import sip
            if sip.isdeleted(self):
                logger.debug("[OpenAIChatToolWindow] C++ object deleted before session creation, skipping")
                return
        except Exception:
            pass
        
        if self._is_auto_loop_running:
            InfoBar.warning("AutoLoop", "运行中无法新建会话，请先停止 AutoLoop", parent=self, duration=3000, position=InfoBarPosition.BOTTOM)
            return
        if self._is_streaming and self.backend.chat_engine:
            self.backend.stop_streaming()

        self._is_streaming = False
        self._topic_summary_cancelled = True  # 🛡️ 取消标题生成重试
        self._tool_cancelled_by_user = False
        self._toggle_send_stop(False)

        if self._tool_floating_widget:
            self._tool_floating_widget.clear()
            self._tool_floating_widget.setVisible(False)

        if self._sub_agent_floating_widget:
            self._sub_agent_floating_widget.setVisible(False)

        try:
            self._auto_save_current_session()
        except Exception:
            logger.exception(
                "Failed to auto-save current session before creating a new session"
            )

        self._cache_current_session_cards()
        session = self.backend.create_session()
        self._current_session_id = session.session_id
        self._history_preview_messages = None
        self._clear_chat_area()
        self.title_edit.setText("新对话")
        self.node_preview.clear_nodes()
        if self._todo_floating_widget:
            self._todo_floating_widget.clear()
        self.backend.clear_todo_list()
        self.backend.set_session_context(self._current_session_id)
        if self._question_floating_widget:
            self._question_floating_widget.clear()
        self._question_tool_call_id = None
        self._load_agent_list()

        QTimer.singleShot(0, lambda: self._safe_timer_call(self._show_initial_welcome))
        self._refresh_context_usage_indicator()

    def _display_current_session(self):
        session = self.session_manager.get_current_session()
        if not session:
            self._clear_chat_area()
            return

        self.title_edit.setText(session.topic_summary or session.name or "新对话")

        # 关键修复：同步 _current_session_id 与实际显示的会话
        self._current_session_id = session.session_id

        if self._restore_cached_session_cards(session):
            self._update_node_preview()
            self._refresh_context_usage_indicator()
            # 恢复缓存卡片后，多次滚动确保在底部
            self._scroll_to_bottom(sticky_ms=900)
            # 滚动完成后同步时间线节点到最后一个
            QTimer.singleShot(100, self._sync_node_preview_to_last)
            return

        self._clear_chat_area()
        self._message_batch = group_messages_for_display(session.messages)
        # 初始化 batch_cards：每个batch对应一个卡片列表，None表示已回收
        self._batch_cards = [None for _ in self._message_batch]
        # 重建 user 前缀和缓存（用于 O(1) 的 round_index 计算）
        self._build_user_prefix_cache()
        self._visible_batch_end = len(self._message_batch)
        self._visible_batch_start = max(
            0, self._visible_batch_end - self._initial_visible_batch_count
        )

        if not self._message_batch:
            self._show_initial_welcome()
            return

        self._load_message_batch(initial=True)
        # 同步 batch 结构：确保 _batch_cards 和 _message_index 与布局一致
        self._sync_batch_structures()

    def _show_initial_welcome(self):
        """仅在UI上显示欢迎卡片，不改动Session数据"""
        self._clear_chat_area(delete_widgets=False)
        welcome_card = self._get_or_create_welcome_card()
        self._displayed_session_id = None
        self._add_chat_widget(welcome_card)

    def _hide_welcome_cards(self):
        """隐藏所有欢迎卡片"""
        for i in range(self.chat_layout.count()):
            item = self.chat_layout.itemAt(i)
            if item and item.widget():
                widget = item.widget()
                if getattr(widget, "_is_welcome", False):
                    widget.hide()

    def _load_message_batch(self, initial: bool = False):
        """按当前可见窗口加载消息。"""
        session = self.session_manager.get_current_session()
        if session:
            self._displayed_session_id = session.session_id
            # 关键修复：同步 _current_session_id 与实际显示的会话
            self._current_session_id = session.session_id

        visible_batches = self._message_batch[
                          self._visible_batch_start:self._visible_batch_end
                          ]
        self._suspend_auto_scroll = not initial
        self._loading_session = True  # 标记加载状态，懒渲染期间保持滚动
        self._initial_scroll_to_bottom = False  # 重置滚底标记，让首次懒渲染强制滚底
        try:
            self._render_message_to_card(
                visible_batches,
                batch_offset=self._visible_batch_start,
            )
        finally:
            self._suspend_auto_scroll = False

        # 节点预览和滚动同步在懒渲染完成后处理
        self._update_node_preview()
        QTimer.singleShot(100, self._sync_node_preview_to_last)
        self._refresh_context_usage_indicator()
        QTimer.singleShot(500, lambda: gc.collect())

    def _has_more_history_batches(self) -> bool:
        return self._visible_batch_start > 0

    def _load_more_history_batches(self):
        if self._is_loading_history_batches or not self._has_more_history_batches():
            return

        scroll_bar = self.chat_scroll_area.verticalScrollBar()
        previous_value = scroll_bar.value()
        previous_height = self.chat_container.sizeHint().height()

        new_start = max(
            0, self._visible_batch_start - self._incremental_visible_batch_count
        )
        prepend_batches = self._message_batch[new_start:self._visible_batch_start]
        if not prepend_batches:
            return

        self._is_loading_history_batches = True
        self._visible_batch_start = new_start
        self._render_message_to_card(
            prepend_batches,
            insert_at_top=True,
            batch_offset=new_start,
        )
        self._sync_node_preview_to_scroll()

        def restore_anchor():
            try:
                new_height = self.chat_container.sizeHint().height()
                scroll_bar.setValue(previous_value + max(0, new_height - previous_height))
            finally:
                self._is_loading_history_batches = False

        QTimer.singleShot(0, restore_anchor)

    def _recycle_out_of_view_batches(self):
        """回收超出可视缓冲区范围的批次UI，只保留数据，节省内存
        同时确保当前可视范围内的批次都已经懒渲染完成
        """
        if self._is_virtual_recycling or len(self._batch_cards) == 0:
            return

        self._is_virtual_recycling = True
        try:
            # 计算可视缓冲区范围
            buffer_batches = self._incremental_visible_batch_count * self._virtual_scroll_buffer
            active_start = 0 if self._visible_batch_start <= buffer_batches else self._visible_batch_start - buffer_batches
            active_end = self._visible_batch_end + buffer_batches

            # 第一步：确保当前激活范围内所有卡片都已经懒渲染完成
            lazy_render_count = 0
            for batch_idx in range(active_start, active_end):
                if batch_idx >= len(self._batch_cards):
                    continue
                cards = self._batch_cards[batch_idx]
                if not cards:
                    continue
                for card in cards:
                    if isinstance(card, MessageCard) and not getattr(card, '_lazy_rendered', True):
                        card.ensure_rendered()
                        lazy_render_count += 1

            # 第二步：回收超出缓冲区的批次
            recycled_count = 0
            # 先收集需要回收的卡片ID，用于清理懒渲染队列
            recycled_card_ids = set()
            # 回收前面超出缓冲区的批次
            for batch_idx in range(0, active_start):
                if self._batch_cards[batch_idx] is not None:
                    cards = self._batch_cards[batch_idx]
                    # 如果批次包含当前流式输出的助手卡片，跳过整个批次
                    if cards and self._current_assistant_card in cards:
                        continue
                    if cards:
                        for card in cards:
                            recycled_card_ids.add(id(card))
                            if isinstance(card, MessageCard) and self._is_widget_alive(card):
                                card.cleanup()
                                card.deleteLater()
                        recycled_count += 1
                    self._batch_cards[batch_idx] = None

            # 回收后面超出缓冲区的批次
            for batch_idx in range(active_end, len(self._batch_cards)):
                if self._batch_cards[batch_idx] is not None:
                    cards = self._batch_cards[batch_idx]
                    # 如果批次包含当前流式输出的助手卡片，跳过整个批次
                    if cards and self._current_assistant_card in cards:
                        continue
                    if cards:
                        for card in cards:
                            recycled_card_ids.add(id(card))
                            if isinstance(card, MessageCard) and self._is_widget_alive(card):
                                card.cleanup()
                                card.deleteLater()
                        recycled_count += 1
                    self._batch_cards[batch_idx] = None

            # 从懒渲染队列中移除已回收的卡片，避免对已销毁的 widget 调用 ensure_rendered
            if recycled_card_ids:
                self._pending_lazy_cards = [
                    c for c in self._pending_lazy_cards
                    if id(c) not in recycled_card_ids and self._is_widget_alive(c)
                ]

            # 回收完成，如果有回收触发GC
            if recycled_count > 0 or lazy_render_count > 0:
                logger.debug(f"[virtual-scroll] 懒渲染 {lazy_render_count}，回收 {recycled_count} 个离屏批次")
                if recycled_count > 0:
                    QTimer.singleShot(100, lambda: gc.collect())

        finally:
            self._is_virtual_recycling = False

    def _open_diff_viewer(self):
        """打开差异查看窗口，显示当前会话修改文件的 git diff"""
        try:
            # 获取当前会话 ID
            session_id = self._current_session_id
            if not session_id:
                InfoBar.warning("提示", "当前没有活动会话", parent=self, position=InfoBarPosition.BOTTOM)
                return

            # 从 ToolExecutor 获取当前会话的文件操作记录
            if not self.backend.tool_executor:
                InfoBar.warning("提示", "工具执行器未初始化", parent=self, position=InfoBarPosition.BOTTOM)
                return

            from app.utils.file_operation_recorder import FileOperationRecorder
            file_recorder = FileOperationRecorder(self.session_store)

            # 获取当前会话的所有文件操作
            operations = file_recorder.get_all_operations_for_session(session_id)

            if not operations:
                InfoBar.info("提示", "当前会话没有文件修改记录", parent=self, position=InfoBarPosition.BOTTOM)
                return

            # 提取文件路径列表（去重）
            file_paths = list({op.get("file_path") for op in operations if op.get("file_path")})

            if not file_paths:
                InfoBar.info("提示", "未找到修改的文件", parent=self, position=InfoBarPosition.BOTTOM)
                return

            # 生成 git diff
            try:
                from app.utils.diff_viewer import DiffHtmlGenerator
                diff_output = DiffHtmlGenerator.get_diff_for_files(file_paths, session_id)
            except Exception as e:
                logger.warning(f"[DiffViewer] 获取 git diff 失败: {e}")
                diff_output = ""

            # 生成 HTML 报告
            from app.utils.diff_viewer import DiffViewerWindow
            html = DiffHtmlGenerator.generate_html_report(diff_output or "", session_id)

            # 创建并显示差异查看窗口
            viewer = DiffViewerWindow(parent=self)
            viewer.load_html(html)
            viewer.show()

            logger.info(f"[DiffViewer] 已打开差异查看窗口，文件数: {len(file_paths)}")

        except ImportError as e:
            logger.error(f"[DiffViewer] 导入模块失败: {e}")
            InfoBar.error("错误", f"功能加载失败: {str(e)}", parent=self, position=InfoBarPosition.BOTTOM)
        except Exception as e:
            logger.exception(f"[DiffViewer] 打开差异查看器失败: {e}")
            InfoBar.error("错误", f"打开差异查看器失败: {str(e)}", parent=self, position=InfoBarPosition.BOTTOM)

    def _clear_chat_area(self, delete_widgets: bool = True):
        self._current_assistant_card = None
        self._displayed_session_id = None
        self._visible_batch_start = 0
        self._visible_batch_end = 0
        self._is_loading_history_batches = False
        while self.chat_layout.count():
            item = self.chat_layout.takeAt(0)
            if item.widget():
                if delete_widgets:
                    item.widget().deleteLater()
                else:
                    item.widget().hide()

    def _take_chat_widgets(self) -> List[QWidget]:
        """从布局中取出所有 widgets，返回列表（不删除，由调用方负责删除）"""
        widgets: List[QWidget] = []
        self._current_assistant_card = None
        self._displayed_session_id = None
        while self.chat_layout.count():
            item = self.chat_layout.takeAt(0)
            if item and item.widget():
                item.widget().hide()
                widgets.append(item.widget())
        return widgets

    def _cache_current_session_cards(self):
        """
        切换会话时彻底清理当前会话的卡片，不再缓存。
        直接删除卡片，释放内存。
        """
        # 从布局中取出所有 widgets
        widgets = self._take_chat_widgets()

        # 彻底删除所有卡片及其子资源
        for widget in widgets:
            if isinstance(widget, MessageCard) and self._is_widget_alive(widget):
                # 调用卡片自己的 cleanup 方法
                widget.cleanup()
            widget.deleteLater()

        # 清理可能残留的卡片缓存，并清理卡片对象
        session = self.session_manager.get_current_session()
        if session:
            cache_entry = self._session_card_cache.pop(session.session_id, None)
            if cache_entry and isinstance(cache_entry, dict):
                cards = cache_entry.get("cards", [])
                for card in cards:
                    if hasattr(card, 'cleanup'):
                        try:
                            card.cleanup()
                        except Exception:
                            pass
                    if hasattr(card, 'deleteLater'):
                        try:
                            card.deleteLater()
                        except Exception:
                            pass

    def _cleanup_session_card_cache(self):
        from app.constants import (
            MAX_SESSION_CARD_CACHE_SIZE,
        )
        all_session_ids = {
            s.session_id for s in self.session_manager.get_all_sessions()
        }
        cleanup_stale_card_cache(
            self._session_card_cache,
            all_session_ids,
            MAX_SESSION_CARD_CACHE_SIZE
        )

    def _is_widget_alive(self, widget: Optional[QWidget]) -> bool:
        """检查 widget 是否存活（保留向后兼容）"""
        return is_widget_alive(widget)

    def _restore_cached_session_cards(self, session: ChatSession) -> bool:
        if not session.messages:
            return False

        cache_entry = self._session_card_cache.get(session.session_id)
        if not cache_entry:
            return False
        cached_cards = cache_entry.get("cards") if isinstance(cache_entry, dict) else None
        if not cached_cards:
            return False
        batch_count = len(group_messages_for_display(session.messages))
        if cache_entry.get("batch_count") != batch_count:
            self._session_card_cache.pop(session.session_id, None)
            return False

        alive_cards, removed = filter_alive_cards(cached_cards)
        if removed:
            self._session_card_cache.pop(session.session_id, None)
        if not alive_cards:
            return False

        self._clear_chat_area(delete_widgets=False)
        for card in alive_cards:
            self._add_chat_widget(card)
        self._displayed_session_id = session.session_id
        # 关键修复：同步 _current_session_id 与实际显示的会话
        self._current_session_id = session.session_id
        self._current_assistant_card = (
            alive_cards[-1]
            if alive_cards and alive_cards[-1].role == "assistant"
            else None
        )
        self._message_batch = group_messages_for_display(session.messages)
        # 重建 user 前缀和缓存（用于 O(1) 的 round_index 计算）
        self._build_user_prefix_cache()
        # 同步 _batch_cards 长度
        self._sync_batch_structures()
        # 从缓存卡片的 _message_index 重建 _batch_cards 引用（缓存卡片已有正确的索引）
        for card in alive_cards:
            mi = getattr(card, '_message_index', None)
            if mi is not None and 0 <= mi < len(self._batch_cards):
                if self._batch_cards[mi] is None:
                    self._batch_cards[mi] = []
                if card not in self._batch_cards[mi]:
                    self._batch_cards[mi].append(card)
        self._visible_batch_start = max(
            0, int(cache_entry.get("visible_batch_start", 0))
        )
        self._visible_batch_end = min(
            len(self._message_batch),
            int(cache_entry.get("visible_batch_end", len(self._message_batch))),
        )
        return True

    def _get_or_create_welcome_card(self) -> MessageCard:
        agent = self.backend.get_agent(self._current_agent)
        agent_name = agent.name if agent else ""
        agent_desc = agent.description if agent else ""

        # 获取最近会话和最多消息的会话用于欢迎卡片（按当前项目过滤）
        history_list = self.history_manager.get_history_list(self._current_project)

        # 最近会话（按时间排序，取前3）
        recent_sessions = []
        for session in history_list[:3]:
            recent_sessions.append({
                "title": session.get("title"),
                "last_time": session.get("last_time"),
                "session_id": session.get("session_id"),
                "message_count": session.get("message_count", 0),
            })

        # 最多消息的会话（按消息数量排序，取前3）
        top_sessions = sorted(history_list, key=lambda x: x.get("message_count", 0), reverse=True)[:3]
        top_by_count = []
        for session in top_sessions:
            top_by_count.append({
                "title": session.get("title"),
                "last_time": session.get("last_time"),
                "session_id": session.get("session_id"),
                "message_count": session.get("message_count", 0),
            })

        # 每次都重新创建，确保会话列表是最新的
        welcome_card = create_welcome_card(
            self, agent_name, agent_desc, recent_sessions, top_by_count
        )
        welcome_card._is_welcome = True
        welcome_card.contextActionRequested.connect(
            self.handle_recommended_question
        )
        return welcome_card

    def _sanitize_user_message_for_display(self, content: str) -> str:
        """清理用户消息用于显示（保留向后兼容）"""
        return sanitize_user_message_for_display(content)

    def _get_user_round_index_for_batch_index(
            self, batch_index: int, batch_offset: int = 0
    ) -> int:
        """
        计算给定 batch 索引对应的 round_index（user 轮次索引）

        逻辑：
        - 对于 user batch：round_index = 前面有多少个 user batch
        - 对于 assistant batch：round_index = 前面有多少个 user batch - 1

        优化：使用前缀和缓存实现 O(1) 复杂度（每次 _message_batch 更新时重建缓存）

        Args:
            batch_index: batch 在 _message_batch 中的索引
            batch_offset: 当前加载批次的起始偏移量（用于分批加载历史消息）

        Returns:
            round_index：从 0 开始的用户轮次索引
        """
        global_batch_index = batch_index

        # 边界检查
        if global_batch_index >= len(self._message_batch):
            return 0

        # 使用前缀和缓存 O(1) 获取 user 数量
        user_count = self._user_prefix_cache[global_batch_index]

        # 对于 assistant/tool batch，round_index 需要减 1
        current_role = self._message_batch[global_batch_index][0].get("role")
        if current_role != "user":
            user_count = max(0, user_count - 1)

        return user_count

    def _build_user_prefix_cache(self) -> None:
        """
        构建 user 数量的前缀和缓存数组

        _user_prefix_cache[i] = 前 i 个 batch 中有多少个 user
        prefix[0] = 0（表示"前 0 个 batch 中的 user 数量"）
        prefix 长度 = len(_message_batch) + 1
        """
        self._user_prefix_cache = [0]
        for batch in self._message_batch:
            is_user = batch and batch[0].get("role") == "user"
            self._user_prefix_cache.append(
                self._user_prefix_cache[-1] + (1 if is_user else 0)
            )

    # ==================== Batch 结构同步 ====================

    def _sync_batch_structures(self):
        """
        同步 _message_batch 与 session.messages 的当前状态。
        仅在发送新消息或流式完成后调用。
        不改变任何卡片上的 _message_index（渲染代码已正确设置），
        只保持 _message_batch / _batch_cards 长度与 session 一致。
        """
        session = self.session_manager.get_current_session()
        if not session:
            return

        new_batch = group_messages_for_display(session.messages)
        self._message_batch = new_batch
        self._build_user_prefix_cache()

        # 扩展/裁剪 _batch_cards 到新长度
        new_len = len(new_batch)
        if new_len > len(self._batch_cards):
            self._batch_cards.extend([None] * (new_len - len(self._batch_cards)))
        elif new_len < len(self._batch_cards):
            self._batch_cards = self._batch_cards[:new_len]

    def _fix_new_card_message_index(self, user_text: str = None):
        """
        为布局中尚未设置 _message_index 的卡片分配正确的 batch index。
        在发送新消息后调用，因为 _append_user_message / _append_assistant_message
        不设置 _message_index（渲染路径 _render_message_to_card 才设置）。

        关键：必须处理所有无 _message_index 的卡片，而非只处理第一个。
        """
        # 收集所有无 _message_index 的卡片（按布局顺序，即时间顺序）
        unassigned = []
        for i in range(self.chat_layout.count()):
            item = self.chat_layout.itemAt(i)
            if not item or not item.widget():
                continue
            widget = item.widget()
            if not isinstance(widget, MessageCard):
                continue
            if getattr(widget, '_is_welcome', False):
                continue
            if getattr(widget, '_message_index', None) is not None:
                continue
            unassigned.append(widget)

        if not unassigned:
            return

        # 构建 _message_batch 中每个 batch 的分配状态：
        # 已有 _batch_cards 引用且非 None 的 batch 表示已被占用
        assigned_batch_indices = set()
        for batch_idx, cards in enumerate(self._batch_cards):
            if cards is not None and len(cards) > 0:
                # 检查 cards 中是否有存活的 widget
                for c in cards:
                    if self._is_widget_alive(c):
                        assigned_batch_indices.add(batch_idx)
                        break

        # 对每个未分配的卡片，按角色从后向前在 _message_batch 中找到
        # 未被占用的同角色 batch
        for widget in unassigned:
            target_batch_idx = -1
            for batch_idx in range(len(self._message_batch) - 1, -1, -1):
                if batch_idx in assigned_batch_indices:
                    continue
                batch = self._message_batch[batch_idx]
                if not batch:
                    continue
                if batch[0].get("role") == widget.role:
                    target_batch_idx = batch_idx
                    break

            if target_batch_idx >= 0:
                # 找到了未被占用的同角色 batch
                widget._message_index = target_batch_idx
                assigned_batch_indices.add(target_batch_idx)
                if target_batch_idx < len(self._batch_cards):
                    if self._batch_cards[target_batch_idx] is None:
                        self._batch_cards[target_batch_idx] = []
                    if widget not in self._batch_cards[target_batch_idx]:
                        self._batch_cards[target_batch_idx].append(widget)
            else:
                # 没有可匹配的 batch，在末尾追加独占槽位
                new_batch_idx = len(self._message_batch)
                self._message_batch.append([])
                self._batch_cards.append([])
                widget._message_index = new_batch_idx
                self._batch_cards[new_batch_idx].append(widget)
                assigned_batch_indices.add(new_batch_idx)

    def _render_message_to_card(
            self,
            batches: List[List[Dict[str, Any]]],
            insert_at_top: bool = False,
            batch_offset: int = 0,
    ):
        insert_index = 0 if insert_at_top else None
        for local_index, batch in enumerate(batches):
            role = batch[0].get("role")
            timestamp = batch[0].get("timestamp") or get_default_timestamp()
            model_name = batch[0].get("model_name")
            global_batch_index = batch_offset + local_index
            round_index = self._get_user_round_index_for_batch_index(
                global_batch_index, batch_offset
            )

            # 检查该batch是否已经渲染过并且被回收了
            if self._batch_cards[global_batch_index] is not None:
                # 已经渲染过，卡片已经存在，不需要重新创建
                # 但是需要确保已经添加到布局（如果是滚动回来加载更多）
                cards = self._batch_cards[global_batch_index]
                if cards and insert_index is not None:
                    for card in cards:
                        if self._is_widget_alive(card):
                            if role == "user":
                                self.chat_layout.insertWidget(insert_index, card, 0, Qt.AlignRight)
                            else:
                                self.chat_layout.insertWidget(insert_index, card)
                            insert_index += 1
                continue

            # 需要重新创建
            cards = []
            if role == "user":
                content = self._sanitize_user_message_for_display(
                    batch[0].get("content", "")
                )
                user_card = self._append_user_message(
                    content,
                    timestamp=timestamp,
                    scroll=False,
                    insert_index=insert_index,
                    user_round_index=round_index,
                    update_preview=not insert_at_top,
                )
                if user_card:
                    # 设置 message_index 用于卡片差异功能
                    user_card._message_index = global_batch_index
                    cards.append(user_card)
                if insert_index is not None and user_card:
                    insert_index += 1

            if role == "assistant" or role == "tool":
                assistant_card = self._append_assistant_message(
                    timestamp=timestamp,
                    scroll=False,
                    insert_index=insert_index,
                    round_index=round_index,
                    model_name=model_name if role == "assistant" else None,
                )
                if assistant_card:
                    # 设置 message_index 用于卡片差异功能
                    assistant_card._message_index = global_batch_index
                    cards.append(assistant_card)
                    # 使用辅助函数渲染消息
                    render_batch_to_assistant_card(assistant_card, batch)
                if insert_index is not None and assistant_card:
                    insert_index += 1

            # 保存卡片引用到 batch_cards
            self._batch_cards[global_batch_index] = cards if cards else None

        # 批量处理懒渲染：渲染完所有卡片后再统一触发，避免每次都触发滚动
        # 收集需要懒渲染的卡片
        pending_lazy_cards = []
        for batch_idx in range(batch_offset, batch_offset + len(batches)):
            if batch_idx >= len(self._batch_cards):
                break
            cards = self._batch_cards[batch_idx]
            if cards:
                for card in cards:
                    if isinstance(card, MessageCard) and not getattr(card, '_lazy_rendered', False):
                        pending_lazy_cards.append(card)

        # 批量触发懒渲染，使用延迟加载减少卡顿
        # 同一卡片可能已在队列中（如滚动回来触发的重新加载），需去重
        existing_ids = {id(c) for c in self._pending_lazy_cards}
        for card in pending_lazy_cards:
            if id(card) not in existing_ids:
                self._pending_lazy_cards.append(card)
                existing_ids.add(id(card))
        if pending_lazy_cards and not self._lazy_card_timer_active:
            self._lazy_card_timer_active = True
            QTimer.singleShot(0, self._process_next_lazy_card)

    def _get_rendered_message_cards(self) -> List[MessageCard]:
        def is_user_or_assistant(widget):
            return widget.role in ("user", "assistant")

        return collect_message_cards_from_layout(self.chat_layout, is_user_or_assistant)

    def _process_next_lazy_card(self):
        """分批处理懒渲染卡片，每次处理一个卡片并触发滚动
        关键：每次渲染完成后立即同步滚动，不依赖定时器延迟
        """
        if not self._pending_lazy_cards:
            # 所有懒渲染完成
            self._loading_session = False
            self._lazy_card_timer_active = False
            return

        card = self._pending_lazy_cards.pop(0)
        # 检查卡片是否仍然有效且未被回收
        if self._is_widget_alive(card) and not getattr(card, '_lazy_rendered', False):
            card.ensure_rendered()

        # 每次渲染完一个卡片后立即同步滚动到底部，无论加载多慢都生效
        # 直接设置滚动条值，不依赖定时器
        # 关键修复：首次强制滚底后，只在用户未主动滚上去时才继续滚底
        if self._loading_session:
            scroll_bar = self.chat_scroll_area.verticalScrollBar()
            if not self._initial_scroll_to_bottom or scroll_bar.value() >= scroll_bar.maximum() - 50:
                scroll_bar.setValue(scroll_bar.maximum())
                self._initial_scroll_to_bottom = True

        # 继续处理下一个，使用 QTimer 异步调度释放事件循环
        if self._pending_lazy_cards:
            QTimer.singleShot(0, self._process_next_lazy_card)
        else:
            self._loading_session = False
            self._lazy_card_timer_active = False

    def _get_current_user_round_index(self) -> int:
        """获取当前 user message 应该是第几个 user（从 0 开始）
        基于session消息计算，而非布局中渲染的卡片数量，避免动态加载导致索引错误
        """
        session = self.session_manager.get_current_session()
        if session:
            return sum(1 for msg in session.messages if msg.get("role") == "user")
        # fallback: 从布局计数
        return count_user_cards_in_layout(self.chat_layout)

    def _find_user_round_index_for_card(self, card: MessageCard) -> Optional[int]:
        """
        通过遍历布局找到 user card 对应的 round_index
        """
        # 直接通过布局遍历确定位置（更可靠）
        user_card_idx = 0
        for i in range(self.chat_layout.count()):
            item = self.chat_layout.itemAt(i)
            if not item or not item.widget():
                continue
            widget = item.widget()
            if not isinstance(widget, MessageCard):
                continue
            if getattr(widget, "_is_welcome", False):
                continue
            if widget is card:
                return user_card_idx
            if widget.role == "user":
                user_card_idx += 1
        return None

    def findRoundIndexForCard(self, card: MessageCard) -> Optional[int]:
        """
        供 MessageCard 回调使用，根据 assistant card 查找对应的 round_index。
        通过遍历布局找到该 assistant card 前面的 user card 数量来确定 round_index。
        """
        if not card or card.role != "assistant":
            return None
        # 遍历布局，统计该 assistant card 之前有多少 user card
        round_index = 0
        for i in range(self.chat_layout.count()):
            item = self.chat_layout.itemAt(i)
            if not item or not item.widget():
                continue
            widget = item.widget()
            if not isinstance(widget, MessageCard):
                continue
            if widget is card:
                # 找到了，返回当前 round_index
                return round_index
            if widget.role == "user":
                round_index += 1
        return None

    def _find_user_round_index_from_session(
            self,
            session,
            user_text: str,
            timestamp: str,
    ) -> Optional[int]:
        """
        从 session 数据中找到 user 消息对应的 round_index。

        通过在 session.messages 中定位 user 消息，然后计算它是第几个 user。

        Args:
            session: ChatSession 对象
            user_text: 用户消息的纯文本内容
            timestamp: 用户消息的时间戳

        Returns:
            round_index 或 None
        """
        return find_user_round_index(session, user_text, timestamp)

    def _remove_cards_for_round(self, round_index: int) -> bool:
        session = self.session_manager.get_current_session()
        if not session:
            return False

        canonical_messages = consolidate_messages(session.messages)
        round_ranges = get_user_round_ranges(canonical_messages)
        if round_index < 0 or round_index >= len(round_ranges):
            return False

        start_idx, end_idx = round_ranges[round_index]
        cards_to_remove = end_idx - start_idx

        user_card_idx = 0
        removed = 0
        removing = False
        widgets_to_remove = []

        # 遍历 chat_layout
        for i in range(self.chat_layout.count()):
            item = self.chat_layout.itemAt(i)
            if not item or not item.widget():
                continue
            widget = item.widget()
            if not isinstance(widget, MessageCard):
                continue
            if getattr(widget, "_is_welcome", False):
                continue
            if widget.role not in ("user", "assistant"):
                continue

            if widget.role == "user":
                if user_card_idx == round_index:
                    widgets_to_remove.append(widget)
                    removed += 1
                    removing = True
                else:
                    removing = False
                user_card_idx += 1
            elif widget.role == "assistant" and removing:
                widgets_to_remove.append(widget)
                removed += 1

            if removed >= cards_to_remove:
                break

        logger.info(
            f"[DELETE] Cards to remove: {len(widgets_to_remove)}, cards_to_remove: {cards_to_remove}"
        )

        # 使用辅助函数执行删除
        delete_widgets_from_layout(widgets_to_remove, self.chat_layout)
        return removed > 0

    def _remove_cards_from_round(self, round_index: int) -> bool:
        """从指定 round 开始删除所有卡片（包括后续卡片）"""
        # 计算预期删除的卡片数量
        session = self.session_manager.get_current_session()
        cards_to_remove_hint = 0
        if session:
            from app.core import consolidate_messages, get_user_round_ranges
            canonical_messages = consolidate_messages(session.messages)
            round_ranges = get_user_round_ranges(canonical_messages)
            if round_index < len(round_ranges):
                start_idx, end_idx = round_ranges[round_index]
                cards_to_remove_hint = end_idx - start_idx

        widgets_to_remove = find_widgets_to_remove_from_round(
            self.chat_layout, round_index, cards_to_remove_hint
        )
        delete_widgets_from_layout(widgets_to_remove, self.chat_layout)

        # 关键修复：如果 UI 删除的卡片数量少于预期，清空整个聊天区域并重新渲染
        if cards_to_remove_hint > 0 and len(widgets_to_remove) < cards_to_remove_hint:
            from loguru import logger
            logger.warning(
                f"[UNDO] UI cards incomplete: deleting {len(widgets_to_remove)}/{cards_to_remove_hint}. "
                f"Clearing and re-rendering session view."
            )
            self._clear_chat_area()
            self._display_current_session()
            return False
        return len(widgets_to_remove) > 0

    def _invalidate_current_session_card_cache(self):
        invalidate_session_card_cache(
            self.session_manager.get_current_session(),
            self._session_card_cache
        )

    def _persist_session_after_mutation(self):
        session = self.session_manager.get_current_session()
        if not session:
            return

        session.set_messages(session.messages, preserve_compaction=False)

        if is_session_empty(session):
            if self._current_session_id is not None and self.history_manager:
                idx = self.history_manager.find_index_by_session_id(
                    self._current_session_id
                )
                if idx is not None:
                    self.history_manager.archive_history(idx)
                self._current_session_id = None
            return

        if self.history_manager:
            # 使用辅助函数保存会话
            self._current_session_id = save_or_archive_session(
                self.history_manager,
                session,
                self._current_session_id
            )

    def _refresh_session_view_after_mutation(self):
        # 使用辅助函数刷新视图
        refresh_session_view(
            self,
            self._invalidate_current_session_card_cache,
            self._display_current_session,
            self._refresh_context_usage_indicator
        )

    def _sync_current_assistant_card_ref(self):
        self._current_assistant_card = find_last_assistant_card(self.chat_layout)

    def _finalize_local_session_mutation(self):
        self._invalidate_current_session_card_cache()
        self._history_preview_messages = None
        session = self.session_manager.get_current_session()

        if is_session_empty(session):
            self._clear_chat_area()
            self.node_preview.clear_nodes()
            self._current_assistant_card = None
            self._show_initial_welcome()
            self._refresh_context_usage_indicator()
            return

        self._sync_current_assistant_card_ref()
        self._update_node_preview()
        self._refresh_context_usage_indicator()
        self._sync_node_preview_to_scroll()

    def _on_clear_shortcut(self):
        if getattr(self, '_is_destroyed', False):
            return
        # 使用辅助函数清空并显示欢迎
        clear_and_show_welcome(
            session=self.session_manager.get_current_session(),
            session_card_cache=self._session_card_cache,
            clear_chat_func=self._clear_chat_area,
            clear_preview_func=self.node_preview.clear_nodes,
            get_welcome_func=self._get_or_create_welcome_card,
            add_widget_func=lambda w: QTimer.singleShot(0, lambda: self._add_chat_widget(w))
        )
        self.title_edit.setText("新对话")

    def _add_chat_widget(self, widget: QWidget, insert_index: Optional[int] = None):
        if getattr(self, '_is_destroyed', False):
            return
        if insert_index is None:
            add_message_to_layout(widget, self.chat_layout, is_widget_alive)
        else:
            if is_widget_alive(widget):
                widget.setParent(self.chat_container)
                if isinstance(widget, MessageCard) and widget.role == "user":
                    self.chat_layout.insertWidget(insert_index, widget, 0, Qt.AlignRight)
                elif isinstance(widget, MessageCard):
                    self.chat_layout.insertWidget(insert_index, widget, 0, Qt.AlignLeft)
                else:
                    self.chat_layout.insertWidget(insert_index, widget)
                widget.show()
        if isinstance(widget, MessageCard):
            try:
                widget.heightChanged.disconnect(self._on_message_card_height_changed)
            except Exception:
                pass
            widget.heightChanged.connect(self._on_message_card_height_changed)
            if self._resize_preview_active:
                widget.set_resize_preview_mode(True)
            widget.sync_width()

    def _archive_history_session(self, index: int):
        history_list = self.history_manager.get_history_list(self._current_project)
        if index < 0 or index >= len(history_list):
            return

        session_record = history_list[index]
        session_id = session_record.get("session_id")
        # 通过 session_id 找到全量列表中的真实 index
        full_index = self.history_manager.find_index_by_session_id(session_id)
        if full_index is None:
            return

        archived_current = (
                self._current_session_id is not None
                and session_id == self._current_session_id
        )

        old_session_manager = self.session_manager
        old_chat_engine = self.backend.chat_engine

        # 清理归档会话的文件操作记录和备份
        if self.backend.tool_executor and self.backend.file_recorder:
            self.backend.file_recorder.clear_session(session_id)
            logger.info(f"[FileRecorder] 已清理归档会话的文件操作记录: {session_id}")

        archived = self.history_manager.archive_history(full_index)

        if archived_current and archived:
            # 使用辅助函数创建新会话状态
            new_state = create_new_session_state(old_session_manager, old_chat_engine)
            init_new_session_after_archive(
                self, new_state, self.backend,
                self._clear_chat_area, self._show_initial_welcome
            )

        # 刷新历史会话卡片
        current_tab = self._history_popup_card._current_tab if hasattr(self._history_popup_card,
                                                                       '_current_tab') else "history"
        if current_tab == "archived":
            # 如果当前在归档标签页，需要清理并刷新
            refresh_history_card_if_visible(self._history_card,
                                            lambda: self._refresh_history_toggle_panel(is_archived=True))
        else:
            refresh_history_card_if_visible(self._history_card, self._refresh_history_toggle_panel)

    def _rename_history_session(self, index: int, new_title: str):
        if not self.history_manager:
            return
        history_list = self.history_manager.get_history_list(self._current_project)
        if 0 <= index < len(history_list):
            session_record = history_list[index]
            session_id = session_record.get("session_id")
            if session_id:
                session = self.history_manager.get_session_by_session_id(session_id)
                if session:
                    idx = self.history_manager.find_index_by_session_id(session_id)
                    if idx is not None:
                        self.history_manager.update_session_title(idx, new_title)
        # 刷新历史会话卡片
        refresh_history_card_if_visible(self._history_card, self._refresh_history_toggle_panel)

    def _on_session_imported(self, data: dict):
        """处理导入的会话文件"""
        if not self.history_manager:
            return

        file_path = data.get("file_path")
        if not file_path:
            return

        imported_session = self.history_manager.import_from_json(file_path)
        if imported_session:
            # 刷新历史会话卡片
            self._refresh_history_toggle_panel()
            # 显示提示信息
            InfoBar.success(
                title="导入成功",
                content=f"已导入会话：{imported_session.get('title', '新对话')}",
                position=InfoBarPosition.BOTTOM,
                duration=3000,
                parent=self
            )
            logger.info(f"[导入会话] 成功: {file_path}")
        else:
            InfoBar.error(
                title="导入失败",
                content="无法解析会话文件，请确认文件格式正确",
                position=InfoBarPosition.BOTTOM,
                duration=3000,
                parent=self
            )
            logger.warning(f"[导入会话] 失败: {file_path}")

    def _on_archived_session_restored(self, file_path: str):
        """恢复归档会话到历史会话"""
        if not self.history_manager:
            return

        # 导入归档的会话
        imported_session = self.history_manager.import_from_json(file_path)
        if imported_session:
            # 删除归档文件
            try:
                import os
                os.remove(file_path)
                logger.info(f"[恢复会话] 已删除归档文件: {file_path}")
            except Exception as e:
                logger.warning(f"[恢复会话] 删除归档文件失败: {e}")

            # 刷新归档列表
            self._refresh_archived_sessions()

            InfoBar.success(
                title="恢复成功",
                content=f"已恢复会话：{imported_session.get('title', '新对话')}",
                position=InfoBarPosition.BOTTOM,
                duration=3000,
                parent=self
            )
            logger.info(f"[恢复会话] 成功: {file_path}")
        else:
            InfoBar.error(
                title="恢复失败",
                content="无法恢复该会话，文件可能已损坏",
                position=InfoBarPosition.BOTTOM,
                duration=3000,
                parent=self
            )

    def _on_archived_session_deleted(self, file_path: str):
        """彻底删除归档会话"""
        from qfluentwidgets import MessageBox

        # 确认对话框
        msg_box = MessageBox(
            "确认删除",
            "确定要彻底删除这个归档会话吗？此操作不可恢复。",
            self
        )
        msg_box.yesButton.setText("删除")
        msg_box.cancelButton.setText("取消")

        if msg_box.exec() != MessageBox.Accepted:
            return

        try:
            import os
            os.remove(file_path)
            logger.info(f"[彻底删除] 成功: {file_path}")

            # 刷新归档列表
            self._refresh_archived_sessions()

            InfoBar.success(
                title="删除成功",
                content="归档会话已彻底删除",
                position=InfoBarPosition.BOTTOM,
                duration=2000,
                parent=self
            )
        except Exception as e:
            logger.error(f"[彻底删除] 失败: {e}")
            InfoBar.error(
                title="删除失败",
                content=f"无法删除文件：{str(e)}",
                position=InfoBarPosition.BOTTOM,
                duration=3000,
                parent=self
            )

    def _on_archived_session_renamed(self, file_path: str, new_title: str):
        """重命名归档会话"""
        try:
            # 读取文件
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
                data = json.loads(content)

            # 更新标题
            data["title"] = new_title

            # 写回文件
            with open(file_path, "wb") as f:
                f.write(json.dumps(data, option=json.OPT_INDENT_2))

            logger.info(f"[归档会话重命名] 成功: {file_path} -> {new_title}")

            # 刷新归档列表
            self._refresh_archived_sessions()

            InfoBar.success(
                title="重命名成功",
                content=f"已更名为：{new_title}",
                position=InfoBarPosition.BOTTOM,
                duration=2000,
                parent=self
            )
        except Exception as e:
            logger.error(f"[归档会话重命名] 失败: {e}")
            InfoBar.error(
                title="重命名失败",
                content=str(e),
                position=InfoBarPosition.BOTTOM,
                duration=3000,
                parent=self
            )

    def _load_history_session(self, index: int):
        self._load_history_session_from_popup(index)

    def _load_history_session_from_popup(self, index: int):
        if self._is_streaming:
            self._on_stop_clicked()

        try:
            self._auto_save_current_session()
        except Exception:
            logger.exception(
                "Failed to auto-save current session before loading history"
            )

        self.backend.reset_session_state()

        history_list = self.history_manager.get_history_list(self._current_project)
        if index < 0 or index >= len(history_list):
            return

        session_record = history_list[index]
        session_id = session_record.get("session_id")
        # 通过 session_id 获取会话消息，而非通过 index
        messages = self.history_manager.get_session_messages(session_id)
        if not messages:
            return

        title = session_record.get("title") or session_record.get("name") or "历史对话"

        # 使用辅助函数创建会话
        restored = create_session_from_record(session_record, messages, title)

        # 使用辅助函数初始化
        init_after_loading_session(
            self, restored, session_id, title,
            self.backend
        )

        # 如果会话有自己的项目，显示在标题上
        session_project = session_record.get("project", "默认项目") or "默认项目"
        self._current_project = session_project
        self.backend._current_project = session_project
        self._project_label.setText(session_project)
        self._update_project_label_style()
        self._update_branch()

        self._display_current_session()

        # 刷新历史会话卡片
        if self._history_card.isVisible():
            self._refresh_history_toggle_panel()

    def _append_user_message(
            self,
            content: str,
            timestamp: str = None,
            scroll: bool = True,
            insert_index: Optional[int] = None,
            user_round_index: Optional[int] = None,
            update_preview: bool = True,
    ):
        session = self.session_manager.get_current_session()
        if session:
            self._displayed_session_id = session.session_id

        # 计算当前 user message 的 round_index
        if user_round_index is None:
            user_round_index = self._get_current_user_round_index()

        card = MessageCard(
            parent=self,
            role="user",
            timestamp=timestamp
        )
        card._round_index = user_round_index
        card.update_content(content)
        card.finish_streaming()

        # 设置卡片信号
        setup_user_card_signals(card, self._delete_message, self._undo_from_message, self._on_code_action)

        self._add_chat_widget(card, insert_index=insert_index)
        if scroll and not self._suspend_auto_scroll:
            self._scroll_to_bottom()

        # 使用辅助函数后处理
        post_append_user_message(
            self,
            user_round_index,
            self._update_node_preview if update_preview else None,
        )
        return card

    def _append_assistant_message(
            self,
            timestamp: str = None,
            scroll: bool = True,
            insert_index: Optional[int] = None,
            round_index: Optional[int] = None,
            model_name: str = None,
    ) -> MessageCard:
        session = self.session_manager.get_current_session()
        if session:
            self._displayed_session_id = session.session_id

        # 使用辅助函数创建卡片
        def on_context_action(action, context):
            self.handle_recommended_question(action, context)
            self.contextActionRequested.emit(action, context)

        card = create_assistant_card_widget(
            parent=self,
            timestamp=timestamp,
            round_index=(
                round_index
                if round_index is not None
                else self._current_assistant_round_index
            ),
            model_name=model_name,
            on_action=self._on_code_action,
            on_context_action=on_context_action,
            on_tool_diff=self._on_tool_diff_requested,
            on_card_diff=self._on_card_diff_requested,
            on_save_file=self._on_save_file_requested,
            on_subagent_log=self._on_subagent_log_requested,
            immediate_render=scroll,  # 流式(scroll=True)立即渲染，加载(scroll=False)走懒渲染队列
        )

        self._add_chat_widget(card, insert_index=insert_index)
        if scroll and not self._suspend_auto_scroll:
            self._scroll_to_bottom()
        return card

    def _update_assistant_message(self, card: MessageCard, new_content: str):
        card.update_content(new_content)
        scroll_to_bottom_if_streaming(self.chat_scroll_area, self._is_streaming)

    def _update_node_preview(self):
        session = self.session_manager.get_current_session()
        if not session:
            return

        # 使用辅助函数构建 node preview 数据
        node_data = build_node_preview_from_session(session, content_to_text, max_len=30)

        self.node_preview.update_nodes(node_data)
        self._sync_node_preview_to_scroll()

    def _sync_node_preview_to_scroll(self):
        """根据当前滚动位置同步时间线节点的高亮和进度条
        新逻辑：进度条显示节点与节点之间的进度，和实际卡片高度绑定
        """
        # 加载历史时抑制滚动同步
        if self._suppress_scroll_sync_count > 0:
            self._suppress_scroll_sync_count -= 1
            return

        if not hasattr(self, "chat_scroll_area") or not hasattr(self, "node_preview"):
            return

        session = self.session_manager.get_current_session()
        if not session:
            return

        total_nodes = len(self.node_preview._nodes) if hasattr(self.node_preview, '_nodes') else 0
        if total_nodes == 0:
            self._last_visible_user_pair_index = -1
            self.node_preview.set_visible_node(-1)
            self.node_preview.set_progress_position(-1)
            return

        # 如果有待更新的目标索引（点击跳转中），直接使用它
        if self._pending_scroll_to_update is not None:
            highlighted_index = self._pending_scroll_to_update
            self._pending_scroll_to_update = None
            highlighted_index = max(0, min(highlighted_index, total_nodes - 1))
            self.node_preview.set_progress_position(highlighted_index)
            if highlighted_index != self._last_visible_user_pair_index:
                self._last_visible_user_pair_index = highlighted_index
                self.node_preview.set_visible_node(highlighted_index)
            return

        scroll_bar = self.chat_scroll_area.verticalScrollBar()
        visible_top = scroll_bar.value()

        # 收集所有已渲染用户卡片的位置信息
        user_card_info = []
        user_node_index = 0  # 节点预览中的索引

        for batch_idx, batch in enumerate(self._message_batch):
            if not batch or batch[0].get("role") != "user":
                continue
            if user_node_index >= total_nodes:
                break

            # 尝试从 batch_cards 中找到对应的卡片
            cards = self._batch_cards[batch_idx] if batch_idx < len(self._batch_cards) else None
            if cards:
                for card in cards:
                    if sip.isdeleted(card):
                        continue
                    if isinstance(card, MessageCard) and card.role == "user":
                        user_card_info.append({
                            'index': user_node_index,
                            'y': card.y(),
                            'bottom': card.y() + card.height()
                        })
                        break
            user_node_index += 1

        # 如果没有找到任何已渲染卡片，fallback到估算
        if not user_card_info and scroll_bar.maximum() > 0:
            scroll_ratio = visible_top / scroll_bar.maximum()
            highlighted_index = int(round(scroll_ratio * (total_nodes - 1)))
            progress_position = highlighted_index
        else:
            # 找到当前可视区域所在的节点区间
            # 找出当前可视top落在哪个区间 [user_i, user_{i+1}]
            current_segment_index = -1
            for i in range(len(user_card_info)):
                if i < len(user_card_info) - 1:
                    # 当前区间是从这个user卡片开始，到下一个user卡片之前
                    # 包含这个user问题和它对应的所有大模型回答卡片
                    segment_start_y = user_card_info[i]['y']
                    segment_end_y = user_card_info[i + 1]['y']

                    if visible_top < segment_end_y:
                        current_segment_index = i
                        break
                else:
                    # 最后一个节点，一直到最后
                    current_segment_index = i
                    break

            # 计算进度
            if current_segment_index >= 0:
                start_node_index = user_card_info[current_segment_index]['index']
                start_y = user_card_info[current_segment_index]['y']

                if current_segment_index < len(user_card_info) - 1:
                    end_y = user_card_info[current_segment_index + 1]['y']
                    end_node_index = user_card_info[current_segment_index + 1]['index']
                else:
                    # 最后一个区间，使用最后一个卡片的bottom作为终点
                    if len(user_card_info) > 0:
                        last_item = user_card_info[-1]
                        end_y = last_item['bottom'] + 500  # 增加一点余量
                        end_node_index = last_item['index']
                    else:
                        end_y = start_y
                        end_node_index = start_node_index

                # 计算在当前区间的比例
                if end_y > start_y:
                    segment_progress = (visible_top - start_y) / (end_y - start_y)
                else:
                    segment_progress = 0

                # 转换到节点坐标
                progress_position = start_node_index + segment_progress
                visible_node_index = start_node_index
            else:
                # 都没找到，fallback
                progress_position = 0
                visible_node_index = 0
                highlighted_index = 0

            highlighted_index = visible_node_index

        # 确保在有效范围内
        progress_position = max(0, min(progress_position, total_nodes - 1))
        highlighted_index = max(0, min(highlighted_index, total_nodes - 1))

        # 更新进度条和高亮节点
        self.node_preview.set_progress_position(progress_position)
        if highlighted_index != self._last_visible_user_pair_index:
            self._last_visible_user_pair_index = highlighted_index
            self.node_preview.set_visible_node(highlighted_index)

    def _sync_node_preview_to_last(self):
        """滚动完成后同步到最后一个节点"""
        if not hasattr(self, "node_preview"):
            return

        # 使用 message_batch 计算实际的最后一个 user message 索引
        # 而不是依赖当前渲染的卡片数量（可能只渲染了部分）
        session = self.session_manager.get_current_session()
        if session:
            # 从 session 消息计算所有 user 数量
            user_count = sum(
                1 for msg in session.messages
                if msg.get("role") == "user"
            )
            if user_count > 0:
                last_index = user_count - 1
                self.node_preview.set_visible_node(last_index)
                self.node_preview.set_progress_position(last_index)
                self._last_visible_user_pair_index = last_index

    def _scroll_to_target_node_index(self, target_index: int):
        """
        滚动到指定节点索引的位置。如果目标卡片未渲染，先加载历史批次。

        Args:
            target_index: 目标节点索引（0-based）
        """
        session = self.session_manager.get_current_session()
        if not session:
            return

        # 计算目标 user 在 _message_batch 中的 batch 索引
        # 找到 _message_batch 中第 target_index 个 user batch
        target_batch_index = -1
        user_count = 0
        for idx, batch in enumerate(self._message_batch):
            if batch and batch[0].get("role") == "user":
                if user_count == target_index:
                    target_batch_index = idx
                    break
                user_count += 1

        if target_batch_index < 0:
            logger.warning(f"[NodePreview] Cannot find batch for target_index={target_index}")
            return

        # 检查目标 batch 是否已经加载（可见）
        if target_batch_index >= self._visible_batch_start:
            # 已加载，直接滚动到目标位置
            self._scroll_to_batch_index(target_batch_index, node_index=target_index)
            return

        # 需要加载更多历史批次
        # 计算需要加载的起始位置（预留一些缓冲批次）
        new_start = max(0, target_batch_index - self._incremental_visible_batch_count // 2)

        # 标记要滚动的目标索引，以便加载完成后使用
        self._pending_scroll_to_index = target_index
        self._pending_scroll_to_batch = target_batch_index

        # 加载历史批次到目标位置
        self._load_history_to_index(new_start)

    def _load_history_to_index(self, target_batch_start: int):
        """
        加载历史批次直到到达目标 batch 起始位置

        Args:
            target_batch_start: 目标批次起始索引
        """
        if target_batch_start >= self._visible_batch_start:
            # 已经到达目标位置，滚动到目标节点
            self._scroll_to_pending_target()
            return

        # 计算需要加载多少批次
        batch_count = self._visible_batch_start - target_batch_start
        if batch_count > 0:
            # 触发分批加载
            self._render_message_to_card(
                self._message_batch[target_batch_start:self._visible_batch_start],
                insert_at_top=True,
                batch_offset=target_batch_start,
            )

            # 更新可见范围
            self._visible_batch_start = target_batch_start

            # 延迟检查是否需要继续加载（使用 QTimer.singleShot 避免重复）
            QTimer.singleShot(100, lambda: self._load_history_to_index(target_batch_start))

    def _scroll_to_pending_target(self):
        """滚动到待处理的目标节点"""
        if self._pending_scroll_to_index is None:
            return

        target_index = self._pending_scroll_to_index
        target_batch_index = self._pending_scroll_to_batch
        self._pending_scroll_to_index = None
        self._pending_scroll_to_batch = None

        self._pending_scroll_to_update = target_index

        # 如果 target_batch_index 未提供，从 _message_batch 查找
        if target_batch_index is None:
            user_count = 0
            for idx, batch in enumerate(self._message_batch):
                if batch and batch[0].get("role") == "user":
                    if user_count == target_index:
                        target_batch_index = idx
                        break
                    user_count += 1

        if target_batch_index < 0:
            return

        # 优先从 _batch_cards 查找（更可靠）
        if 0 <= target_batch_index < len(self._batch_cards):
            cards = self._batch_cards[target_batch_index]
            if cards:
                for card in cards:
                    if self._is_widget_alive(card) and isinstance(card, MessageCard):
                        if card.role == "user":
                            self.chat_scroll_area.verticalScrollBar().setValue(card.y())
                            return

        # 回退：遍历布局查找
        for i in range(self.chat_layout.count()):
            item = self.chat_layout.itemAt(i)
            if not item or not item.widget():
                continue
            widget = item.widget()
            if not isinstance(widget, MessageCard):
                continue
            if getattr(widget, '_is_welcome', False):
                continue
            if widget.role == "user" and getattr(widget, '_message_index', None) == target_batch_index:
                self.chat_scroll_area.verticalScrollBar().setValue(widget.y())
                return

        logger.warning(f"[NodePreview] Card not found after history load, index={target_index}")

    def _find_user_card_in_batch(self, target_batch_index):
        """从 _batch_cards 中查找指定 batch 的 user card"""
        if target_batch_index is None or target_batch_index < 0:
            return None
        if target_batch_index >= len(self._batch_cards):
            return None
        cards = self._batch_cards[target_batch_index]
        if not cards:
            return None
        for card in cards:
            if sip.isdeleted(card):
                continue
            if isinstance(card, MessageCard) and card.role == "user":
                return card
        return None

    def _find_user_card_by_message_index(self, target_batch_index):
        """在布局中查找 _message_index 等于目标 batch 的 user card"""
        for i in range(self.chat_layout.count()):
            item = self.chat_layout.itemAt(i)
            if not item or not item.widget():
                continue
            widget = item.widget()
            if isinstance(widget, MessageCard) and widget.role == "user":
                if getattr(widget, '_message_index', None) == target_batch_index:
                    return widget
        return None

    def _scroll_to_batch_index(self, batch_index: int, node_index: int = -1):
        """
        滚动到指定 batch 索引的位置。
        优先使用 _batch_cards 查找，如果找不到再遍历布局作为回退。
        """
        if node_index >= 0:
            self._pending_scroll_to_update = node_index

        # 优先从 _batch_cards 查找（更可靠，避免虚拟回收后布局遍历失效）
        if 0 <= batch_index < len(self._batch_cards):
            cards = self._batch_cards[batch_index]
            if cards:
                for card in cards:
                    if self._is_widget_alive(card) and isinstance(card, MessageCard):
                        # 确保是 user card
                        if card.role == "user":
                            self.chat_scroll_area.verticalScrollBar().setValue(card.y())
                            return

        # 回退：遍历布局查找（处理边界情况，如刚创建但尚未加入 _batch_cards）
        for i in range(self.chat_layout.count()):
            item = self.chat_layout.itemAt(i)
            if not item or not item.widget():
                continue
            widget = item.widget()
            if not isinstance(widget, MessageCard):
                continue
            if getattr(widget, '_is_welcome', False):
                continue
            if widget.role == "user" and getattr(widget, '_message_index', None) == batch_index:
                self.chat_scroll_area.verticalScrollBar().setValue(widget.y())
                return

        logger.warning(f"[NodePreview] Cannot find card for batch_index={batch_index}, node_index={node_index}")

    def _on_node_preview_clicked(self, index: int):
        """
        点击时间线节点，滚动到对应的 user 卡片。
        """
        target_batch_index = -1
        user_count = 0
        for idx, batch in enumerate(self._message_batch):
            if batch and batch[0].get("role") == "user":
                if user_count == index:
                    target_batch_index = idx
                    break
                user_count += 1

        if target_batch_index < 0:
            return

        # 优先从 _batch_cards 查找（更可靠，避免虚拟回收后布局遍历失效）
        if 0 <= target_batch_index < len(self._batch_cards):
            cards = self._batch_cards[target_batch_index]
            if cards:
                for card in cards:
                    if self._is_widget_alive(card) and isinstance(card, MessageCard):
                        if card.role == "user":
                            self._pending_scroll_to_update = index
                            self.chat_scroll_area.verticalScrollBar().setValue(card.y())
                            return

        # 回退：遍历布局查找（处理边界情况）
        for i in range(self.chat_layout.count()):
            item = self.chat_layout.itemAt(i)
            if not item or not item.widget():
                continue
            widget = item.widget()
            if not isinstance(widget, MessageCard):
                continue
            if getattr(widget, '_is_welcome', False):
                continue
            if widget.role == "user" and getattr(widget, '_message_index', None) == target_batch_index:
                self._pending_scroll_to_update = index
                self.chat_scroll_area.verticalScrollBar().setValue(widget.y())
                return

        # 目标 batch 未渲染，触发加载
        self._scroll_to_target_node_index(index)

    def _is_card_index_valid(self, card, expected_batch_index: int) -> bool:
        """
        验证卡片是否真的对应目标 batch index。
        用于防止虚拟回收后计数错位返回错误卡片。
        """
        if card is None or expected_batch_index < 0:
            return False
        card_index = getattr(card, '_message_index', None)
        return card_index == expected_batch_index

    def _on_chat_scrolled(self, value):
        """聊天区域滚动时，触发虚拟滚动回收并通知所有 MessageCard 更新浮动头"""
        pass
        # self._virtual_scroll_timer.start()
        # for card in self.findChildren(MessageCard):
        #     card._scroll_position_changed(value)

    def _on_scroll_changed(self, value):
        self._sync_node_preview_to_scroll()
        scroll_bar = self.chat_scroll_area.verticalScrollBar()
        if self._bottom_anchor_deadline > 0:
            if value < scroll_bar.maximum():
                self._bottom_anchor_deadline = 0.0
                self._bottom_anchor_timer.stop()
        # 检测用户是否主动滚离底部（距离底部超过 30px）
        if value < scroll_bar.maximum() - 30:
            self._user_intentionally_away_from_bottom = True
        if value <= self._history_load_threshold:
            self._load_more_history_batches()
        # 滚动时复用单个防抖定时器，避免堆积大量 singleShot 回调
        self._scroll_sync_timer.stop()
        self._scroll_sync_timer.start()

    def _truncate_session_from_user_round(self, round_index: int, card: MessageCard = None) -> bool:
        """
        截断 session 数据到指定 round 之前，并删除 UI 卡片

        UI 删除策略：基于 card widget 对象在 chat_layout 中的位置精准删除，
        不依赖 round_index 遍历（解决懒加载时卡片序号对不上的问题）
        """
        from loguru import logger

        session = self.session_manager.get_current_session()
        if not session:
            logger.error("[UNDO] No session found")
            return False

        # === 1. 删除 UI 卡片：从 card 到末尾 ===
        if card is not None:
            # 找到 card 在 chat_layout 中的索引
            card_layout_idx = -1
            for i in range(self.chat_layout.count()):
                item = self.chat_layout.itemAt(i)
                if item and item.widget() is card:
                    card_layout_idx = i
                    break

            if card_layout_idx >= 0:
                # 收集要删除的 widgets：从 card 到末尾（撤销 = 删除之后所有）
                widgets_to_remove = []
                for i in range(card_layout_idx, self.chat_layout.count()):
                    item = self.chat_layout.itemAt(i)
                    if item and item.widget():
                        w = item.widget()
                        if hasattr(w, '_is_welcome') and w._is_welcome:
                            continue
                        widgets_to_remove.append(w)

                from app.widgets.ui_helpers import delete_widgets_from_layout
                # 注意：不调用 cleanup，因为撤销操作需要在删除后仍能访问卡片数据
                deleted_count = delete_widgets_from_layout(widgets_to_remove, self.chat_layout, call_cleanup=False)
                logger.info(f"[UNDO] Removed {deleted_count} cards from UI")
            else:
                logger.warning("[UNDO] Card not found in layout, UI cards not deleted")
        else:
            logger.warning("[UNDO] No card provided, skipping UI deletion")

        # === 2. 基于 session.messages 计算截断位置 ===
        canonical_messages = consolidate_messages(session.messages)
        round_ranges = get_user_round_ranges(canonical_messages)

        if round_index < 0 or round_index >= len(round_ranges):
            logger.error(f"[UNDO] Invalid round_index: {round_index}, available: {len(round_ranges)}")
            return False

        cutoff_index = round_ranges[round_index][0]
        logger.info(f"[UNDO] Truncating session: round_index={round_index}, cutoff_index={cutoff_index}")

        # === 3. 截断 session.messages ===
        session.set_messages(
            session.messages[:cutoff_index], preserve_compaction=False
        )

        # === 4. 同步 _message_batch ===
        self._message_batch = group_messages_for_display(session.messages)
        # 重建 user 前缀和缓存
        self._build_user_prefix_cache()

        # === 5. 保存 session ===
        self._persist_session_after_mutation()

        # === 6. 收尾 ===
        self._finalize_local_session_mutation()

        return True

    def _delete_message(self, card: MessageCard):
        if card.role != "user":
            return
        # 直接传 card 对象，不依赖 round_index 定位 UI 卡片
        self._delete_user_round(card)

    def _delete_user_round(self, card: MessageCard):
        """
        删除单个 round：找到 card 在 chat_layout 中的位置，
        删除该 user card 及其后直到下一个 user card 之间的所有卡片
        """
        from loguru import logger

        logger.info(f"[DELETE] Starting deletion for card at round_index={card._round_index}")

        # === 1. 删除 UI 卡片：基于 card widget 对象在 layout 中的位置 ===
        # 找到 card 在 chat_layout 中的索引
        card_layout_idx = -1
        for i in range(self.chat_layout.count()):
            item = self.chat_layout.itemAt(i)
            if item and item.widget() is card:
                card_layout_idx = i
                break

        if card_layout_idx < 0:
            logger.warning("[DELETE] Card not found in layout")
            return

        # 收集要删除的 widgets：从 card 开始，直到下一个 user card 或末尾
        widgets_to_remove = [card]
        for i in range(card_layout_idx + 1, self.chat_layout.count()):
            item = self.chat_layout.itemAt(i)
            if not item or not item.widget():
                continue
            w = item.widget()
            # 遇到下一个 user card 就停止
            if hasattr(w, 'role') and w.role == "user" and not getattr(w, "_is_welcome", False):
                break
            widgets_to_remove.append(w)

        from app.widgets.ui_helpers import delete_widgets_from_layout
        delete_widgets_from_layout(widgets_to_remove, self.chat_layout)
        logger.info(f"[DELETE] Removed {len(widgets_to_remove)} cards from UI")

        # === 2. 更新 session 数据 ===
        session = self.session_manager.get_current_session()
        if not session:
            logger.error("[DELETE] No session found")
            return

        # 从 card._round_index 计算 session 截断位置
        round_index = card._round_index
        if round_index is None:
            logger.error("[DELETE] Card has no _round_index")
            return

        canonical_messages = consolidate_messages(session.messages)
        round_ranges = get_user_round_ranges(canonical_messages)

        if round_index < 0 or round_index >= len(round_ranges):
            logger.warning(f"[DELETE] Invalid round_index: {round_index}")
            return

        success, old_count, new_count = truncate_and_remove_round(
            session, round_index, round_ranges
        )
        if not success:
            return

        log_deletion_stats(round_index, len(widgets_to_remove), old_count, new_count)

        # === 3. 同步 _message_batch ===
        self._message_batch = group_messages_for_display(session.messages)
        # 重建 user 前缀和缓存
        self._build_user_prefix_cache()

        # === 4. 保存 session ===
        if self._current_session_id != session.session_id:
            self._current_session_id = session.session_id

        try:
            self._persist_session_after_mutation()
        except Exception as e:
            logger.error(f"[DELETE] Failed to persist session: {e}")

        self._finalize_local_session_mutation()

    def _undo_from_message(self, card: MessageCard):
        if card.role != "user":
            return

        session = self.session_manager.get_current_session()
        if not session:
            return

        # 优先使用 card._round_index（卡创建时已正确设置）
        # 避免使用 session 模糊匹配导致的错误（重复文本、时间戳格式差异）
        round_index = card._round_index
        if round_index is None:
            # 降级：尝试使用 _message_index 计算 round_index
            if card._message_index is not None:
                # 计算该 batch 之前的 user batch 数量
                round_index = 0
                for idx in range(card._message_index):
                    if idx < len(self._message_batch) and self._message_batch[idx] and \
                       self._message_batch[idx][0].get("role") == "user":
                        round_index += 1
            else:
                # 最终降级：使用 session 文本匹配
                user_text = card.get_plain_text()
                timestamp = card.timestamp
                round_index = self._find_user_round_index_from_session(
                    session, user_text, timestamp
                )

        if round_index is None or round_index < 0:
            logger.warning("[UNDO] Cannot determine round_index for card")
            return

        if self._is_streaming:
            self._on_stop_clicked()

        # 获取待回滚的文件操作（从该轮次到最后的全部）
        all_call_ids = self._get_all_tool_call_ids_from_round(round_index)

        # 如果有文件操作，显示预览对话框
        if all_call_ids and self.backend.tool_executor and self.backend.file_recorder:
            # 使用辅助函数收集操作
            operations = collect_operations_for_round(
                self.backend.file_recorder,
                self._current_session_id,
                all_call_ids
            )

            if operations:
                dialog = FileUndoPreviewDialog(operations, self.backend.file_recorder, self)
                result = dialog.exec_()

                if result == FileUndoPreviewDialog.CANCEL:
                    return  # 取消撤销，什么都不做

                # 执行回滚 - 只还原选中的操作
                selected_ops = dialog.get_selected_operations()
                if selected_ops:
                    result = self.backend.file_recorder.rollback_operations(selected_ops)
                    self._show_undo_result(result)

        if not self._truncate_session_from_user_round(round_index=round_index, card=card):
            return

        # 恢复输入框内容
        restore_input_from_card(self.input_area, card)

    def _get_last_tool_call_id_after_round(self, round_index: int) -> Optional[str]:
        """获取指定 round_index 之后最后一个 tool_call_id"""
        session = self.session_manager.get_current_session()
        if not session:
            return None

        canonical_messages = consolidate_messages(session.messages)
        round_ranges = get_user_round_ranges(canonical_messages)

        return find_last_tool_call_id_after_round(canonical_messages, round_ranges, round_index)

    def _get_all_tool_call_ids_from_round(self, round_index: int) -> List[str]:
        """获取从指定 round 到最后的所有 tool_call_id"""
        session = self.session_manager.get_current_session()
        if not session:
            return []

        start_idx, _ = get_round_message_indices(session, round_index)
        if start_idx is None:
            return []

        canonical_messages = consolidate_messages(session.messages)

        # 使用辅助函数收集剩余的 tool_call_id
        return collect_tool_call_ids(canonical_messages, start_idx, len(canonical_messages))

    def _get_tool_call_ids_in_round(self, round_index: int) -> List[str]:
        """获取指定 round 范围内的所有 tool_call_id"""
        session = self.session_manager.get_current_session()
        if not session:
            return []

        start_idx, end_idx = get_round_message_indices(session, round_index)
        if start_idx is None:
            logger.warning(f"[card-diff] get_round_message_indices returned None for round_index={round_index}")
            return []

        canonical_messages = consolidate_messages(session.messages)
        logger.debug(
            f"[card-diff] round_index={round_index}, start_idx={start_idx}, end_idx={end_idx}, total_msgs={len(canonical_messages)}")

        # 使用辅助函数收集 tool_call_id
        return collect_tool_call_ids(canonical_messages, start_idx, end_idx)

    def _show_undo_result(self, result):
        """显示撤销结果"""
        if result.failed_count > 0:
            failed_list = format_file_list(result.failed_files, max_count=5)
            InfoBar.warning(
                "部分文件回滚失败",
                f"成功: {result.success_count}, 失败: {result.failed_count}\n{failed_list}",
                parent=self,
                duration=5000,
                position=InfoBarPosition.BOTTOM,
            )
        elif result.success_count > 0:
            InfoBar.success(
                "文件已回滚",
                f"已恢复 {result.success_count} 个文件",
                parent=self,
                duration=3000,
                position=InfoBarPosition.BOTTOM,
            )

    def _on_tool_diff_requested(self, tool_call_id: str):
        """
        处理工具差异对比请求

        Args:
            tool_call_id: 工具调用 ID
        """
        if not tool_call_id:
            return

        session = self.session_manager.get_current_session()
        if not session:
            return

        session_id = session.session_id

        # 检查是否有 file_recorder
        if not self.backend.tool_executor or not self.backend.file_recorder:
            logger.warning("[LLMChatter] file_recorder 未初始化")
            return

        try:
            # 获取该 tool_call_id 对应的文件操作记录
            operations = self.backend.file_recorder.get_operations_for_preview(
                session_id=session_id,
                call_id=tool_call_id
            )

            # 使用辅助函数获取第一个文件操作
            success, backup_path, _ = get_first_file_operation(operations)
            if not success:
                InfoBar.warning(
                    "无差异信息",
                    "此工具没有修改任何文件，或备份信息已丢失",
                    duration=3000,
                    parent=self,
                    position=InfoBarPosition.BOTTOM,
                )
                return

            # 使用辅助函数读取备份文件和生成 diff
            old_content, new_content, backup_file = read_backup_files(backup_path)
            html = generate_diff_html(old_content, new_content, backup_file)

            # 显示差异
            show_diff_viewer(self, html)

        except Exception as e:
            logger.error(f"[LLMChatter] 显示工具差异失败: {e}")
            InfoBar.error(
                "差异显示失败",
                str(e),
                duration=3000,
                parent=self,
                position=InfoBarPosition.BOTTOM,
            )

    def _on_subagent_log_requested(self, task_ids_str: str):
        """
        处理子智能体日志查看请求

        Args:
            task_ids_str: 逗号分隔的任务ID列表
        """
        if not task_ids_str:
            return

        # 解析 task_ids
        task_ids = [tid.strip() for tid in task_ids_str.split(",") if tid.strip()]
        if not task_ids:
            return

        # 获取 sub_agent_manager
        sub_agent_mgr = self.backend.sub_agent_manager
        if not sub_agent_mgr:
            logger.warning("[LLMChatter] sub_agent_manager 未初始化")
            return

        # 如果只有一个任务，直接显示
        if len(task_ids) == 1:
            task_id = task_ids[0]
            task_data = sub_agent_mgr.get_task_logs(task_id)
            if not task_data.get("found"):
                InfoBar.warning(
                    "任务不存在",
                    f"未找到任务: {task_id[:8]}...",
                    duration=3000,
                    parent=self,
                    position=InfoBarPosition.BOTTOM,
                )
                return

            # 显示日志
            self._sub_agent_floating_widget.show_task_from_data(task_data)
            return

        # 多个任务：收集所有任务的日志
        all_logs = []
        for task_id in task_ids:
            task_data = sub_agent_mgr.get_task_logs(task_id)
            if task_data.get("found"):
                all_logs.append(task_data)

        if not all_logs:
            InfoBar.warning(
                "任务不存在",
                "未找到任何任务日志",
                duration=3000,
                parent=self,
                position=InfoBarPosition.BOTTOM,
            )
            return

        # 清空现有面板并逐个添加任务
        for i, task_data in enumerate(all_logs):
            task_id = task_data.get("task_id", task_data.get("summary", {}).get("task_id", "unknown"))
            # 首次清空，后续追加
            self._sub_agent_floating_widget.show_task_from_data(task_data, clear_first=(i == 0))

        # 显示面板
        self._sub_agent_floating_widget.setVisible(True)

    def _on_card_diff_requested(self, round_index: int, message_index: int = -1):
        """
        处理卡片级差异对比请求，汇总一次对话中所有工具调用的文件修改。

        Args:
            round_index: 用户回合索引
            message_index: 消息在 _message_batch 中的索引（用于 fallback）
        """
        if round_index < 0 and message_index < 0:
            return

        session = self.session_manager.get_current_session()
        if not session:
            return

        # 验证 round_index 是否有效，如果无效则尝试从 message_index 重新计算
        canonical_messages = consolidate_messages(session.messages)
        round_ranges = get_user_round_ranges(canonical_messages)
        if round_index < 0 or round_index >= len(round_ranges):
            logger.debug(f"[card-diff] round_index={round_index} out of range ({len(round_ranges)}), recomputing")
            round_index = -1

        if round_index < 0 and message_index >= 0:
            # 从 _message_batch 中的位置计算 round_index
            computed = 0
            for idx in range(message_index):
                if idx < len(self._message_batch) and self._message_batch[idx] and \
                   self._message_batch[idx][0].get("role") == "user":
                    computed += 1
            if computed < len(round_ranges):
                round_index = computed
                logger.debug(f"[card-diff] recomputed round_index={round_index} from message_index={message_index}")

        if round_index < 0 or round_index >= len(round_ranges):
            logger.warning(f"[card-diff] cannot determine valid round_index")
            return

        session_id = session.session_id
        logger.debug(
            f"[card-diff] requested round_index={round_index}, session_id={session_id}, msg_count={len(session.messages)}")

        # 检查是否有 file_recorder
        if not self.backend.tool_executor or not self.backend.file_recorder:
            logger.warning("[LLMChatter] file_recorder 未初始化")
            return

        try:
            # 获取该 round 范围内的所有 tool_call_id
            all_call_ids = self._get_tool_call_ids_in_round(round_index)
            logger.debug(f"[card-diff] found call_ids: {all_call_ids}")

            if not all_call_ids:
                InfoBar.warning(
                    "无差异信息",
                    "此对话没有修改任何文件",
                    duration=3000,
                    parent=self,
                    position=InfoBarPosition.BOTTOM,
                )
                return

            # 使用辅助函数收集所有工具的文件操作
            all_operations = collect_operations_for_round(
                self.backend.file_recorder,
                session_id,
                all_call_ids
            )

            if not all_operations:
                InfoBar.warning(
                    "无差异信息",
                    "此对话没有修改任何文件，或备份信息已丢失",
                    duration=3000,
                    parent=self,
                    position=InfoBarPosition.BOTTOM,
                )
                return

            # 使用辅助函数生成合并的 diff HTML
            html = generate_multi_file_diff_html(all_operations)

            # 显示差异
            show_diff_viewer(self, html)

        except Exception as e:
            logger.error(f"[LLMChatter] 显示卡片差异失败: {e}")
            InfoBar.error(
                "差异显示失败",
                str(e),
                duration=3000,
                parent=self,
                position=InfoBarPosition.BOTTOM,
            )

    def _on_save_file_requested(self, code: str, lang: str):
        """
        处理保存文件请求

        Args:
            code: 代码内容
            lang: 代码语言
        """
        # 使用辅助函数获取默认文件名和扩展名
        ext = get_language_extension(lang)
        default_name = get_default_save_filename(lang, code)

        # 弹出文件保存对话框
        from PyQt5.QtWidgets import QFileDialog

        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "保存代码文件",
            default_name,
            f"代码文件 (*{ext});;所有文件 (*.*)"
        )

        if not file_path:
            return

        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(code)
            InfoBar.success(
                "文件已保存",
                file_path,
                duration=3000,
                parent=self,
                position=InfoBarPosition.BOTTOM,
            )
        except Exception as e:
            logger.error(f"[LLMChatter] 保存文件失败: {e}")
            InfoBar.error(
                "保存失败",
                str(e),
                duration=3000,
                parent=self,
                position=InfoBarPosition.BOTTOM,
            )

    def _on_code_action(self, code: str, action: str = "copy"):
        from loguru import logger
        logger.info(f"[_on_code_action] action={action}, code_len={len(code)}")
        if action == "insert":
            self.insertResponse.emit(code)
        elif action == "create":
            self.createResponse.emit(code)
        elif action == "copy":
            clipboard = QApplication.clipboard()
            clipboard.setText(code)
            # 复制成功提示 - 使用 self 作为 parent
            logger.info("[_on_code_action] showing InfoBar")
            InfoBar.success(
                "已复制",
                "",
                duration=1500,
                parent=self,
                position=InfoBarPosition.BOTTOM,
            )

    def _scroll_to_bottom(self, sticky_ms: int = 0):
        self._pending_scroll_to_bottom = True
        if sticky_ms > 0:
            self._bottom_anchor_deadline = max(
                self._bottom_anchor_deadline,
                time.monotonic() + sticky_ms / 1000.0,
            )
        self._scroll_bottom_timer.start()

    def _do_scroll_to_bottom(self):
        if not self._pending_scroll_to_bottom:
            return
        scroll_bar = self.chat_scroll_area.verticalScrollBar()
        max_val = scroll_bar.maximum()
        scroll_bar.setValue(max_val)
        # 再次设置确保卡片高度变化后仍在底部
        scroll_bar.setValue(max_val)
        self._pending_scroll_to_bottom = False
        self._user_intentionally_away_from_bottom = False
        if self._bottom_anchor_deadline > time.monotonic():
            self._bottom_anchor_timer.start()
        else:
            # 即使anchor到期，也再延迟多次检查，防止多批懒渲染卡片撑开高度导致没到底
            QTimer.singleShot(150, lambda: self._ensure_at_bottom(retries=3))
        # 加载完成后抑制滚动同步，避免节点跑到渲染的卡片数量位置
        self._suppress_scroll_sync_count = 0

    def _ensure_at_bottom(self, retries: int = 3):
        """确保滚动条在底部，用于懒渲染卡片高度变化后的二次修正
        
        Args:
            retries: 剩余重试次数，即使 bottom anchor 过期，也重试几次处理懒加载
        """
        # 如果用户已经主动滚离底部，不再强制拉回
        if self._user_intentionally_away_from_bottom:
            return
        scroll_bar = self.chat_scroll_area.verticalScrollBar()
        if scroll_bar.value() < scroll_bar.maximum() - 20:
            scroll_bar.setValue(scroll_bar.maximum())
            # 懒渲染可能需要更长时间，延迟再次检查
            # 如果还有重试次数，即使 bottom anchor 过期也继续重试
            if retries > 0:
                QTimer.singleShot(300, lambda: self._ensure_at_bottom(retries - 1))
            elif self._bottom_anchor_deadline > time.monotonic():
                QTimer.singleShot(300, self._ensure_at_bottom)

    def _maintain_bottom_anchor(self):
        if self._bottom_anchor_deadline <= time.monotonic():
            self._bottom_anchor_deadline = 0.0
            self._suppress_scroll_sync_count = 0
            return
        scroll_bar = self.chat_scroll_area.verticalScrollBar()
        scroll_bar.setValue(scroll_bar.maximum())
        self._bottom_anchor_timer.start()

    def _on_message_card_height_changed(self, _height: int):
        """卡片高度变化时的滚动处理
        仅当卡片 _content_just_loaded 标记为 True 时触发滚动并清除标记。
        这样内容加载触发的高度变化会滚底，而用户折叠操作不会。
        
        修复规则：
        1. 如果正在往顶部加载历史批次（用户主动向上滚动） → 不滚动
        2. 如果这是整个会话最后一张卡片 → 强制滚动到底（初始加载完成保证到最底端）
        3. 如果正在流式输出 → 滚动到底
        4. 如果滚动条已经在底部附近 → 滚动到底
        """
        sender = self.sender()
        if not isinstance(sender, MessageCard):
            return
        if not sender._content_just_loaded:
            return
        
        # 如果正在往顶部加载历史批次（用户主动向上滚动触发），不滚动到底部
        if self._is_loading_history_batches:
            sender._content_just_loaded = False
            return
        
        # 检查是否是整个会话的最后一张卡片
        is_last_card = False
        if hasattr(self, '_batch_cards') and self._batch_cards:
            # 遍历最后一个非空批次
            for batch in reversed(self._batch_cards):
                if batch is not None and batch:
                    # 检查当前 sender 是否在最后批次中
                    if sender in batch:
                        # 检查是否是最后批次的最后一张卡片
                        if sender is batch[-1]:
                            is_last_card = True
                    break
        
        # 判断规则
        if is_last_card or self._is_streaming:
            # 如果是最后一张卡片，或者正在流式输出 → 强制滚底
            self._scroll_to_bottom()
        else:
            scroll_bar = self.chat_scroll_area.verticalScrollBar()
            max_val = scroll_bar.maximum()
            current_val = scroll_bar.value()
            # 如果滚动条已经在底部附近 → 滚底（严格阈值，避免误触发）
            if max_val - current_val < 20:
                self._scroll_to_bottom()
        
        sender._content_just_loaded = False

    def handle_recommended_question(self, content: str, action: str):
        if action == "ask":
            self.input_area.clear()
            self.send_preset_question(content)
        elif action == "session":
            # session_id 直接就是 content
            session_id = content.strip()
            self._switch_to_session_by_id(session_id)

    def _switch_to_session_by_id(self, session_id: str):
        """根据 session_id 切换到对应会话"""
        if not session_id:
            return

        # 切换前先清理当前会话的资源
        if self._is_streaming and self.backend.chat_engine:
            self.backend.stop_streaming()
            self._is_streaming = False
            self._topic_summary_cancelled = True  # 🛡️ 取消标题生成重试
        elif self.backend.chat_engine:
            self.backend.cleanup_worker()

        # 清理旧会话的卡片
        self._cache_current_session_cards()
        # 只重置会话状态，保留 tool_executor
        self.backend.reset_session_state()

        # 先在当前 session_manager 中查找
        for i, session in enumerate(self.session_manager.get_all_sessions()):
            if session.session_id == session_id:
                self.backend.switch_session(i)
                self._display_current_session()
                self._hide_welcome_cards()
                return

        # 再从 history_manager 查找并恢复（通过 session_id 直接获取）
        session_record = self.history_manager.get_session_by_session_id(session_id)
        if session_record:
            messages = self.history_manager.get_session_messages(session_id)
            title = session_record.get("title") or session_record.get("name") or "历史对话"
            from app.widgets.ui_helpers import create_session_from_record, init_after_loading_session
            restored = create_session_from_record(session_record, messages, title)
            init_after_loading_session(self, restored, session_id, title, self.backend)
            # 同步项目
            session_project = session_record.get("project", "默认项目") or "默认项目"
            self._current_project = session_project
            self._project_label.setText(session_project)
            self._update_project_label_style()
            self._update_branch()
            self._display_current_session()
            self._hide_welcome_cards()
        else:
            logger.warning(f"未找到 session_id: {session_id}")

    def send_preset_question(self, question: str):
        if not isinstance(question, str) or not question.strip():
            return
        self._on_send_clicked(user_text=question.strip())

    def _on_send_clicked(self, user_text: str = ""):
        if getattr(self, '_is_destroyed', False):
            return
        if self._is_auto_loop_running:
            InfoBar.warning("AutoLoop", "运行中无法发送消息，请先停止 AutoLoop", parent=self, duration=3000, position=InfoBarPosition.BOTTOM)
            self.input_area.clear()
            return
        if self._is_streaming:
            self._on_stop_clicked()

        if not user_text:
            user_text = self.input_area.toPlainText().strip()

        if not user_text:
            return

        # 检查模型配置
        llm_config = self._get_current_model_config()
        if not llm_config or not llm_config.get("API_KEY"):
            InfoBar.warning(
                "请先选择模型",
                "请在设置中选择一个可用的模型后再发送消息",
                parent=self,
                duration=3000,
                position=InfoBarPosition.BOTTOM,
            )
            return

        self._hide_welcome_cards()

        self.input_area.clear()
        self._append_user_message(user_text)

        assistant_card = self._append_assistant_message(
            model_name=self._current_model_name,
        )

        # 先设置当前卡片（必须在 send_message 之前，否则回调触发时 _current_assistant_card 为 None）
        self._current_assistant_card = assistant_card
        # 记录响应开始时间（供 _on_stream_finished 计算持续时间）
        self._response_start_time = time.time()
        self._is_streaming = True
        self._toggle_send_stop(True)

        # 关键修复：确保 ToolExecutor 使用正确的 session_id
        session = self.session_manager.get_current_session()
        if session and self.backend.tool_executor:
            self.backend.set_session_context(session.session_id)

        # 如果 send_message 返回 False（通常是 LLM 配置无效），回滚 UI 状态
        if not self.backend.send_message_to_engine(user_text):
            self._is_streaming = False
            self._toggle_send_stop(False)
            assistant_card.deleteLater()
            return

        # 同步 batch 结构：_message_batch 已包含新 user batch
        self._sync_batch_structures()
        # 给新创建的用户卡片设置正确的 _message_index（_append_user_message 中未设置）
        self._fix_new_card_message_index(user_text=user_text)
        # 修复：扩展可见范围以覆盖新增的 batch，否则回收机制会误删新卡片
        self._visible_batch_end = len(self._message_batch)

        self._maybe_generate_topic_summary()

    def _on_stream_started(self):
        if getattr(self, '_is_destroyed', False):
            return
        self._is_streaming = True
        self._response_start_time = time.time()
        self._accumulated_content = ""
        if self._current_assistant_card:
            self._current_assistant_card.start_streaming_anim()

    def _on_content_received(self, content_piece: str):
        if getattr(self, '_is_destroyed', False):
            return
        if self._current_assistant_card:
            self._update_assistant_message(self._current_assistant_card, content_piece)

        if not hasattr(self, "_accumulated_content"):
            self._accumulated_content = ""
        self._accumulated_content += content_piece

    def _on_reasoning_content_received(self, reasoning_piece: str):
        """处理 DeepSeek 思考内容（流式接收）"""
        if getattr(self, '_is_destroyed', False):
            return
        card = self._current_assistant_card
        if card and getattr(card, '_content_data', None) is not None:
            card.start_streaming_anim()
            card.append_reasoning(reasoning_piece)

    def _on_thinking_started(self):
        """新轮次思考开始，为当前助手卡片创建新的独立思考块"""
        if getattr(self, '_is_destroyed', False):
            return
        card = self._current_assistant_card
        if card and getattr(card, '_content_data', None) is not None:
            card.start_streaming_anim()
            card.start_new_thinking_block()

    def _on_tool_args_updated(self, tool_call_id: str, tool_name: str, partial_args: dict):
        """工具参数流式更新 — 流式接收过程中，参数逐块解析完成后触发"""
        if getattr(self, '_is_destroyed', False):
            return
        if not self._tool_floating_widget:
            return
        # 如果是内部进度消息（带 _preview_hint），显示友好文字
        hint = partial_args.get("_preview_hint")
        if hint:
            self._tool_floating_widget.update_progress(hint)
            return
        # 过滤掉内部字段，只显示实际参数
        display_args = {k: v for k, v in partial_args.items() if not k.startswith("_")}
        if display_args:
            args_str = json.dumps(display_args).decode('utf-8')
            if len(args_str) > 80:
                args_str = args_str[:80] + "..."
            self._tool_floating_widget.update_progress(f"参数: {args_str}")
        else:
            # 全是内部字段，显示友好消息
            self._tool_floating_widget.update_progress("正在准备参数...")

    def _on_tool_call_started(
            self, tool_call_id: str, tool_name: str, arguments: dict, round_id: str = None
    ):
        if getattr(self, '_is_destroyed', False):
            return
        self._current_tool_start_time = time.time()
        self._current_tool_call_id = tool_call_id
        self._current_tool_name = tool_name
        self._current_tool_args = arguments

        # 模型开始调用工具时激活彩虹边框（即使返回内容不含文本）
        if self._current_assistant_card:
            self._current_assistant_card.start_streaming_anim()

        # AutoLoop 运行期间不弹出浮动组件
        if self._is_auto_loop_running:
            return

        if tool_name == "question":
            self._hide_all_cards_for_question()
            question_text = arguments.get("question", "")
            options = arguments.get("options", [])
            multiple = arguments.get("multiple", False)
            if question_text:
                self._question_tool_call_id = tool_call_id
                if not isinstance(options, list):
                    options = []
                self._question_floating_widget.show_question(
                    question_text, options, multiple
                )
            return

        if tool_name in ("todowrite", "todoread"):
            # 更新数据，但只有在系统卡片未打开时才能显示
            if not self._is_system_card_visible:
                self._todo_floating_widget.setVisible(True)
            return

        # 即使系统卡片打开也要调用 start_tool 记录任务状态
        # 卡片内部会根据 _suppress_visible 决定是否显示
        self._tool_floating_widget.start_tool(tool_name, arguments)

    def _on_sub_agent_task_started(self, task_id: str, agent_name: str, task_description: str):
        """子智能体任务启动（通过 SubAgentManager 信号触发）"""
        if getattr(self, '_is_destroyed', False):
            return
        widget = self._sub_agent_floating_widget

        # 系统卡片打开时，阻止子智能体卡片显示
        if self._is_system_card_visible:
            return

        # 新批次开始时清空面板（_batch_started 为 False 表示新批次）
        if not widget._batch_started:
            widget.clear()
            widget.setVisible(True)

        widget._batch_started = True  # 标记批次已开始
        widget.add_task(task_id, agent_name, task_description)

        # 连接 executor 信号
        sub_agent_mgr = self.backend.sub_agent_manager
        executor = sub_agent_mgr._running_tasks.get(task_id)
        if executor:
            executor.progress_updated.connect(
                lambda tid, msg: self._sub_agent_floating_widget.update_progress(tid, msg))
            executor.tool_call_started.connect(
                lambda tid, name, args: self._sub_agent_floating_widget.add_tool_call(tid, name, args))
            executor.tool_result_received.connect(
                lambda tid, name, result, success: self._sub_agent_floating_widget.add_tool_result(tid, name, result,
                                                                                                   success))
            executor.finished_with_result.connect(lambda tid, result: self._on_sub_agent_finished(tid, result))

    def _on_sub_agent_task_finished(self, task_id: str, result: str):
        """子智能体任务完成"""
        if getattr(self, '_is_destroyed', False):
            return
        # 从管理器获取执行器，检查是否有真实的错误状态
        sub_agent_mgr = self.backend.sub_agent_manager
        executor = sub_agent_mgr._running_tasks.get(task_id)

        # 优先使用 executor 中记录的 _execution_error 来判断成功/失败
        # 而不是依赖结果内容中的关键词（这会导致误判）
        execution_error = getattr(executor, "_execution_error", None) if executor else None
        success = execution_error is None or execution_error == ""

        self._sub_agent_floating_widget.finish_task(task_id, result, success)

        # 从管理器移除并记录结果
        if executor:
            agent_name = getattr(executor, "agent_name", "")
            del sub_agent_mgr._running_tasks[task_id]
        else:
            agent_name = ""
        sub_agent_mgr._finished_tasks[task_id] = {"result": result, "error": execution_error or "",
                                                  "agent_name": agent_name}

        # 批次完成检查：只有当所有任务都完成时才触发回调
        sub_agent_mgr._batch_completed += 1
        if sub_agent_mgr._batch_completed >= sub_agent_mgr._batch_total and sub_agent_mgr._batch_total > 0:
            # 全部任务完成，发送回调通知
            self._do_trigger_callback(sub_agent_mgr)
            # 重置计数器
            sub_agent_mgr._batch_total = 0
            sub_agent_mgr._batch_completed = 0

    def _do_trigger_callback(self, sub_agent_mgr):
        """执行回调触发"""
        total = len(sub_agent_mgr._finished_tasks)
        failed = sum(1 for t in sub_agent_mgr._finished_tasks.values() if t.get("error") and t.get("error") != "")

        callback_text = f"""[后台任务状态]
所有子智能体任务执行完成。
- 总任务数: {total}
- 已完成: {total - failed}
- 失败: {failed}

请使用 task_status 工具获取详细结果。"""

        # 绕过 streaming 检查直接发送
        self.backend.set_streaming_state(False)
        self.backend.send_message_to_engine(callback_text)

    def _on_sub_agent_finished(self, task_id: str, result: str):
        """单个子智能体执行完成"""
        if getattr(self, '_is_destroyed', False):
            return
        self._sub_agent_manager.task_finished.emit(task_id, result)

    def _on_tool_cancelled(self):
        """工具执行被用户中止 — 连接自 tool_floating_widget.cancelled 信号"""
        if getattr(self, '_is_destroyed', False):
            return
        logger.info("[ToolFloatingWidget] Tool execution cancelled by user")

        self._tool_cancelled_by_user = True
        self._cancelled_tool_call_id = getattr(self, "_current_tool_call_id", None)

        # 不停止流式接收，只标记 worker 跳过工具执行
        # worker 收到标记后，在 _execute_all_tools 中会跳过剩余工具
        # 并自动发送 "用户中止" 的 tool_result 给模型继续迭代
        if self.backend and self.backend._chat_engine and self.backend._chat_engine._current_worker:
            worker = self.backend._chat_engine._current_worker
            worker._is_cancelled = True
            worker._tool_execution_cancelled = True

        self._tool_floating_widget.finish_tool("用户中止", success=False)

        tool_call_id = getattr(self, "_current_tool_call_id", None)
        tool_name = getattr(self, "_current_tool_name", "unknown")
        tool_args = getattr(self, "_current_tool_args", {})

        if tool_call_id and self._current_assistant_card:
            self._current_assistant_card.append_tool_result(
                tool_name=tool_name,
                arguments=tool_args,
                result="[工具执行已被用户中止]",
                success=False,
                tool_call_id=tool_call_id,
            )
            self._scroll_to_bottom()

    def _on_tool_result_received(
            self, tool_call_id: str, tool_name: str, arguments: dict, result: Any
    ):
        if getattr(self, '_is_destroyed', False):
            return
        import time

        if self._is_auto_loop_running:
            # AutoLoop 模式：只记录日志，不操作 UI
            if self._auto_loop_running_card:
                self._auto_loop_running_card.append_log(f"工具完成: {tool_name}")

        if (
                self._tool_cancelled_by_user
                and tool_call_id == self._cancelled_tool_call_id
        ):
            # 支持 dict 和 ToolResult 两种格式
            if isinstance(result, dict):
                error_msg = result.get("error", "") or ""
            else:
                error_msg = str(getattr(result, "error", "") or "")
            if "用户中止" in error_msg:
                self._tool_floating_widget.finish_tool("用户中止", success=False)
                return
            return

        elapsed = (
            time.time() - self._current_tool_start_time
            if hasattr(self, "_current_tool_start_time")
            else 0
        )

        # 支持 ToolResult 对象和 dict 格式的 result
        if isinstance(result, dict):
            success = result.get("success", True)
            error_msg = result.get("error", "") or ""
            content = error_msg if not success else (
                str(result.get("content", "")) if result.get("content") is not None else "")
        else:
            success = getattr(result, "success", True) if hasattr(result, "success") else True
            error_msg = str(getattr(result, "error", "") or "")
            content = str(result) if result else ""

        # 统一处理工具完成状态
        if tool_name in ("todowrite", "todoread"):
            todos = self.backend.get_todos()
            self._todo_floating_widget.update_todos(todos)
            if self._is_system_card_visible:
                # 不显示 todo，等系统卡片关闭后由 _restore_after_system_close 统一恢复
                pass
        elif tool_name not in ("question",):
            # 其他工具：始终调用 finish_tool 记录完成状态
            # 卡片内部会根据 _suppress_visible 决定是否显示（及2秒后自动隐藏）
            self._tool_floating_widget.finish_tool(content[:200], success)
            if not self._is_system_card_visible:
                self._tool_floating_widget.show_if_needed(elapsed)
                self._tool_floating_widget.show_when_ready()

        # 提取 diff 字段（ToolResult 对象或 dict 格式）
        diff_val = None
        if isinstance(result, dict):
            diff_val = result.get("diff", None)
        else:
            diff_val = getattr(result, "diff", None) if result else None

        if self._current_assistant_card:
            self._current_assistant_card.append_tool_result(
                tool_name=tool_name,
                arguments=arguments or {},
                result=content,
                success=success,
                tool_call_id=tool_call_id,
                diff=diff_val,
            )

        self._scroll_to_bottom()

    def _find_latest_assistant_card(self) -> Optional[MessageCard]:
        for i in range(self.chat_layout.count() - 1, -1, -1):
            item = self.chat_layout.itemAt(i)
            if item and item.widget():
                widget = item.widget()
                if isinstance(widget, MessageCard) and widget.role == "assistant":
                    return widget
        return None

    def _notify_if_inactive(self, title: str, message: str):
        if not self.cfg.llm_notify_enabled.value:
            return

        if not self._should_show_inactive_notification():
            return

        sound_type = self.cfg.llm_notify_sound.value
        if sound_type != "none":
            QApplication.beep()

        # 使用全局 TrayManager 发送通知（避免多窗口多个托盘图标的问题）
        from app.tray_manager import TrayManager
        TrayManager.get_instance().notify(title, message)

    def _should_show_inactive_notification(self) -> bool:
        """Only notify when the app window is not effectively visible to the user."""
        window = None
        if self.homepage and self.homepage.window():
            window = self.homepage.window()
        else:
            window = self.window()

        if window is None:
            return True

        if not window.isVisible() or window.isMinimized():
            return True

        native_result = self._is_window_in_foreground_native(window)
        if native_result is not None:
            return not native_result

        app = QApplication.instance()
        active_window = app.activeWindow() if app is not None else None

        # When the current app is not foreground, the chat reply should notify.
        if active_window is None:
            return True

        if active_window is window:
            return False

        return not window.isActiveWindow()

    def _is_window_in_foreground_native(self, window) -> Optional[bool]:
        """Use the OS foreground window when available to avoid Qt focus misreads."""
        if os.name != "nt":
            return None

        try:
            user32 = ctypes.windll.user32
            foreground_hwnd = user32.GetForegroundWindow()
            if not foreground_hwnd:
                return None

            foreground_pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(
                foreground_hwnd, ctypes.byref(foreground_pid)
            )
            return foreground_pid.value == os.getpid()
        except Exception:
            return None

    def _on_notification_clicked(self):
        window = self.window()
        if window:
            window.show()
            if window.isMinimized():
                window.showNormal()
            window.activateWindow()

    def _on_stream_finished(self, response: str):
        if getattr(self, '_is_destroyed', False):
            return
        self._is_streaming = False
        self._tool_cancelled_by_user = False
        self._cancelled_tool_call_id = None
        self._toggle_send_stop(False)

        # 写入模型名称到卡片和 session 消息
        if self._current_assistant_card:
            current_model_name = getattr(self, '_current_model_name', '') or ''
            if current_model_name:
                self._current_assistant_card.set_model_name(current_model_name)
                # 写入 session 的最后一条 assistant 消息
                session = self.session_manager.get_current_session()
                if session and session.messages:
                    for msg in reversed(session.messages):
                        if msg.get("role") == "assistant":
                            msg["model_name"] = current_model_name
                            break
        self._response_start_time = None

        if self._current_assistant_card:
            self._current_assistant_card.finish_streaming()
        if self.history_manager:
            self._save_current_session_to_history()
            # 流式完成后同步 batch 结构，确保 _message_batch 包含完整的 assistant batch
            self._sync_batch_structures()
            # 修复：同步可见范围，避免回收机制误删当前轮次的卡片
            self._visible_batch_end = len(self._message_batch)

        if self.input_area:
            self.input_area.setFocus()

        session = self.session_manager.get_current_session()
        if session and session.messages:
            last_msg = session.messages[-1] if session.messages else None
            if last_msg and last_msg.get("role") == "assistant":
                content = last_msg.get("content", "")
                if isinstance(content, list):
                    from app.core import content_to_text

                    content = content_to_text(content)
                # 如果最后一条消息是hook消息，不触发通知（例如新建会话时的SessionStart）
                if content.strip().startswith("<hook "):
                    logger.debug("[Notify] Skip notification for hook message")
                else:
                    preview = content[:50] + "..." if len(content) > 50 else content
                    current_title = self.title_edit.text() if self.title_edit else "对话完成"
                    self._notify_if_inactive(current_title, preview)

        # 对话完成后更新缓存统计显示
        self._refresh_cache_stats()

        # 对话完成后刷新余额显示
        self._refresh_balance()



    def _refresh_balance(self):
        """刷新余额显示（对话完成后调用）"""
        logger.debug(f"[Balance] _refresh_balance called, provider={getattr(self, '_current_provider_name', 'None')}")
        balance_display = getattr(self, "balance_display", None)
        if balance_display:
            # 如果当前服务商支持余额查询，则刷新
            provider_name = getattr(self, "_current_provider_name", "")
            logger.debug(f"[Balance] provider_name={provider_name}")
            if provider_name in ("DeepSeek", "SiliconFlow (硅基流动)"):
                config = self._valid_configs.get(provider_name, {})
                api_key = config.get("API_KEY", "")
                logger.debug(f"[Balance] api_key exists: {bool(api_key)}")
                if api_key:
                    balance_display.set_provider(provider_name, api_key)
                    return
            # 如果不支持余额查询，隐藏
            balance_display.setVisible(False)

    def _save_current_session_to_history(self):
        session = self.session_manager.get_current_session()
        saved_messages = list(session.messages or []) if session else []
        if not saved_messages:
            return

        system_prompt = getattr(session, "system_prompt", "") or ""
        # 优先使用已有的 topic_summary，避免被用户消息前30字覆盖
        session_title = getattr(session, "topic_summary", "") or ""

        if self._current_session_id is not None:
            idx = self.history_manager.find_index_by_session_id(
                self._current_session_id
            )
            if idx is not None:
                self.history_manager.update_session(
                    idx,
                    saved_messages,
                    compaction_state=getattr(session, "compaction_state", {}),
                    compaction_cache=getattr(session, "compaction_cache", {}),
                    system_prompt=system_prompt,
                    project=self._current_project,
                )
            else:
                self.history_manager.save_session(
                    saved_messages,
                    title=session_title,  # 使用已有的 topic_summary
                    session_id=session.session_id if session else None,
                    compaction_state=getattr(session, "compaction_state", {}),
                    compaction_cache=getattr(session, "compaction_cache", {}),
                    system_prompt=system_prompt,
                    project=self._current_project,
                )
                self._current_session_id = session.session_id if session else None
        else:
            self.history_manager.save_session(
                saved_messages,
                title=session_title,  # 使用已有的 topic_summary
                session_id=session.session_id if session else None,
                compaction_state=getattr(session, "compaction_state", {}),
                compaction_cache=getattr(session, "compaction_cache", {}),
                system_prompt=system_prompt,
                project=self._current_project,
            )
            self._current_session_id = session.session_id if session else None

        self._update_node_preview()

    def _on_messages_updated(self, messages: List[Dict[str, Any]]):
        session = self.session_manager.get_current_session()
        if not session:
            return

        self._history_preview_messages = None
        # 注意：preserve_compaction=False
        # worker 送回来的 current_session_messages 是原始未压缩消息，
        # 保留旧的压缩缓存会导致 state 不一致（缓存说"已压缩"但消息已膨胀）。
        # 清空缓存让下一次 ContextBudgetAllocator 从原始消息正确重新压缩。
        session.set_messages(messages or [], preserve_compaction=False)
        self._refresh_context_usage_indicator()

    def _on_engine_error(self, error: str):
        self._tool_cancelled_by_user = False

        if self._current_assistant_card:
            self._current_assistant_card.stop_streaming_anim()
            self._current_assistant_card.set_error_state(True, error_message=error)
            self._current_assistant_card.update_content(error)

        self._is_streaming = False

        if self._tool_floating_widget:
            self._tool_floating_widget.clear()
            self._tool_floating_widget.setVisible(False)

        self._toggle_send_stop(False)

        current_title = self.title_edit.text() if self.title_edit else "对话"
        self._notify_if_inactive(f"{current_title} - 错误", error[:100])

    def _on_retry_status(self, error_type: str, attempt: int, max_retries: int, wait_time: float):
        """API 重试状态通知 - 更新卡片边框和状态栏"""
        if getattr(self, '_is_destroyed', False):
            return
        # 🛡️ 不在流式状态时忽略重试信号（说明已停止或不在对话中）
        if not self._is_streaming:
            return
        if self._current_assistant_card:
            if not self._current_assistant_card._retrying:
                self._current_assistant_card.start_retry_anim(error_type, attempt, max_retries, wait_time)
            else:
                self._current_assistant_card.update_retry_status(error_type, attempt, max_retries, wait_time)

    def _on_retry_resolved(self):
        """API 重试成功 - 恢复卡片彩虹边框"""
        if getattr(self, '_is_destroyed', False):
            return
        if self._current_assistant_card:
            self._current_assistant_card.stop_retry_anim()

    def _on_user_message_added(self, user_text: str):
        """TODO: 实现用户消息添加时的回调处理"""
        pass

    def _on_skill_requested(self, method: str, params: dict):
        if getattr(self, '_is_destroyed', False):
            return
        result = self.backend.execute_skill(method, params)
        content = (
            f"[Skill Result] {result}"
            if "error" not in result
            else f"[Skill Error] {result.get('error')}"
        )
        # 确保 round_index 正确
        self._current_assistant_round_index = self._get_current_user_round_index()
        new_card = self._append_assistant_message(
            model_name=self._current_model_name,
        )
        new_card.update_content(str(content))
        new_card.finish_streaming()
        self._scroll_to_bottom()

    def _hide_all_cards_for_question(self):
        """Question 卡片显示时，隐藏所有其他卡片（最高优先级）"""
        # 保存 todo 可见状态（用于 question 关闭后恢复）
        self._todo_was_visible_before_system = self._todo_floating_widget.isVisible()
        # 隐藏所有卡片
        self._todo_floating_widget.setVisible(False)
        self._tool_floating_widget.setVisible(False)
        self._sub_agent_floating_widget.setVisible(False)
        self._model_config_card.hide()
        self._history_card.hide()
        self._settings_popup.hide()
        self._memory_card.hide()
        self._provider_edit_card.hide()
        self._auto_loop_config_card.hide()
        self._hook_edit_card.hide()
        if not self._is_auto_loop_running:
            self._auto_loop_running_card.hide()

    def _restore_after_question_close(self):
        """Question 卡片关闭后，恢复非系统卡片的显示状态"""
        # 恢复 todo（如果之前是显示的且还有内容）
        if self._todo_was_visible_before_system and self._todo_floating_widget._todo_list:
            self._todo_floating_widget.setVisible(True)
        # tool 和 sub_agent 有自我生命周期管理，不需要强制恢复

    def _on_question_asked(
            self, tool_call_id: str, question: str, options: list, multiple: bool = False
    ):
        if getattr(self, '_is_destroyed', False):
            return
        self._hide_all_cards_for_question()
        self._question_tool_call_id = tool_call_id
        if not isinstance(options, list):
            options = []
        self._question_floating_widget.show_question(question, options, multiple)
        self._notify_if_inactive("需要回答问题", question[:100])

    def _on_question_answered(self, answer: str):
        if getattr(self, '_is_destroyed', False):
            return
        self._restore_after_question_close()
        if self._pending_permission_tool_call_id:
            tool_call_id = self._pending_permission_tool_call_id
            self._pending_permission_tool_call_id = None
            if answer == "允许":
                self.backend.approve_tool_permission(tool_call_id, False, False)
            elif answer == "允许且该轮对话自动允许":
                self.backend.approve_tool_permission(tool_call_id, True, False)
            elif answer == "本次会话允许":
                self.backend.approve_tool_permission(tool_call_id, False, True)
            else:
                self.backend.deny_tool_permission(tool_call_id)
            if self.input_area:
                self.input_area.setFocus()
            return

        if not self._question_tool_call_id:
            return

        tool_call_id = self._question_tool_call_id
        self._question_tool_call_id = None

        if self.backend.chat_engine:
            self.backend.provide_question_answer(answer)

        if self.input_area:
            self.input_area.setFocus()

    def _on_question_cancelled(self):
        """用户关闭问题窗口时，返回空答案让大模型继续"""
        if getattr(self, '_is_destroyed', False):
            return
        self._restore_after_question_close()
        
        # 恢复之前被 Question 隐藏的卡片
        self._card_manager.restore_after_hide(excluded_card_id="question")
        
        if self._pending_permission_tool_call_id:
            tool_call_id = self._pending_permission_tool_call_id
            self._pending_permission_tool_call_id = None
            self.backend.deny_tool_permission(tool_call_id)
            if self.input_area:
                self.input_area.setFocus()
            return

        if not self._question_tool_call_id:
            return

        self._question_tool_call_id = None

        if self.backend.chat_engine:
            self.backend.provide_question_answer("")

        if self.input_area:
            self.input_area.setFocus()

    def _on_agent_switched(self, agent_name: str):
        """TODO: 实现智能体切换时的状态同步"""
        if getattr(self, '_is_destroyed', False):
            return
        pass

    def _on_permission_approval_requested(
            self, tool_call_id: str, tool_name: str, arguments: dict
    ):
        if getattr(self, '_is_destroyed', False):
            return
        self._pending_permission_tool_call_id = tool_call_id
        self._pending_permission_auto_allow = False
        self._hide_all_cards_for_question()  # question卡片最高优先级
        try:
            arg_str = str(arguments)[:200] if arguments else ""
            question_text = f"工具 `{tool_name}` 需要权限执行。\n\n参数: {arg_str}"
            options = ["允许", "允许且该轮对话自动允许", "本次会话允许", "不允许"]
            self._question_floating_widget.show_question(question_text, options, False)
        except Exception as e:
            logger.error(f"[Permission] Approval error: {e}")
            self.backend.deny_tool_permission(tool_call_id)
            self._pending_permission_tool_call_id = None
            self._restore_after_question_close()

    def _on_compaction_updated(self, task_id: str, new_summary: str):
        if getattr(self, '_is_destroyed', False):
            return
        """
        子智能体压缩完成回调
        
        当后台压缩子智能体完成任务时：
        1. 存储压缩结果供后续查询
        2. 通知用户压缩已完成
        
        主智能体可以在适当时机查询并应用新的压缩结果。
        """
        logger.info(f"[MainWidget] 压缩结果已更新，task_id={task_id[:8]}...")
        
        # 存储最新的压缩结果
        self._latest_compaction_result = {
            "task_id": task_id,
            "new_summary": new_summary,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        
        # 检查是否有正在进行的流式对话
        if not self._is_streaming:
            # 如果没有正在进行的对话，可以记录但不立即应用
            logger.info("[MainWidget] 当前无流式对话，压缩结果已存储待用")
        else:
            # 如果有对话在进行，通知用户压缩已完成
            compaction_msg = (
                f"📋 上下文压缩已完成\n"
                f"后台子智能体已生成更好的压缩摘要。"
            )
            
            # 如果有日志面板，可以在这里显示
            if hasattr(self, '_log_floating_widget') and self._log_floating_widget.isVisible():
                self._log_floating_widget.append_log("system", compaction_msg)

    def _get_pending_compaction_result(self) -> Optional[Dict]:
        """
        获取待应用的压缩结果
        
        供其他模块查询是否有新的压缩结果需要应用。
        
        Returns:
            Dict: 压缩结果（包含 task_id, new_summary, timestamp）
            None: 如果没有待应用的压缩结果
        """
        return getattr(self, '_latest_compaction_result', None)

    def _clear_pending_compaction_result(self):
        """清除待应用的压缩结果（在应用后调用）"""
        self._latest_compaction_result = None
    
    def _maybe_generate_topic_summary(self):
        # 🛡️ 每次启动新的标题生成任务时重置取消标记
        self._topic_summary_cancelled = False
        selected_name = self._current_provider_name if self._current_provider_name else "系统默认配置"
        llm_config = self._valid_configs.get(selected_name)
        if not llm_config:
            logger.warning("[Topic Summary] No LLM config found, skipping")
            return
        session = self.session_manager.get_current_session()
        if not session:
            logger.warning("[Topic Summary] No session found, skipping")
            return

        user_messages = [m for m in session.messages if m.get("role") == "user"]
        if not user_messages:
            logger.warning("[Topic Summary] No user messages found, skipping")
            return
        previous_summary = ""
        if self._current_session_id is not None:
            idx = self.history_manager.find_index_by_session_id(
                self._current_session_id
            )
            if idx is not None:
                previous_summary = self.history_manager.get_topic_summary(idx)

        task = TopicSummaryTask(
            messages=session.messages,
            llm_config=llm_config,
            callback=self._on_topic_summary_generated,
            previous_summary=previous_summary if previous_summary else None,
            cancel_check=lambda: self._topic_summary_cancelled,
        )
        self._gen_thread_pool.start(task)

    def _on_topic_summary_generated(self, result, error: str = None):
        if error:
            logger.error(f"[Topic Summary] Failed to generate: {error}")
            return
        if not result:
            return

        if isinstance(result, dict):
            summary = result.get("topic_summary", "")
        else:
            summary = result

        if not summary:
            return

        clean_summary = summary.strip()

        # 校验标题长度，超长说明解析异常或LLM输出异常，跳过更新
        MAX_TITLE_LENGTH = 50
        if len(clean_summary) > MAX_TITLE_LENGTH:
            logger.warning(f"[Topic Summary] 标题过长({len(clean_summary)}字)，跳过更新")
            return

        session = self.session_manager.get_current_session()

        # 先设置 session 的 topic_summary，避免 save_session 时 title 为空
        if session:
            session.set_topic_summary(clean_summary)

        if self._current_session_id is None and session and session.messages:
            self.history_manager.save_session(
                session.messages if session else [],
                title=clean_summary,  # 使用生成的摘要作为标题
                session_id=session.session_id if session else None,
                compaction_state=getattr(session, "compaction_state", {}),
                compaction_cache=getattr(session, "compaction_cache", {}),
            )
            self._current_session_id = session.session_id if session else None

        if self._current_session_id is not None:
            idx = self.history_manager.find_index_by_session_id(
                self._current_session_id
            )
            if idx is not None:
                self.history_manager.update_topic_summary(idx, clean_summary)

        self._update_title_display(clean_summary)

    def _update_title_display(self, title: str):
        self.title_edit.setText(title)

    def _update_project_display(self, project: str):
        """更新项目名称显示"""
        self._current_project = project
        self.backend._current_project = project
        self._project_label.setText(project)
        self._update_project_label_style()

    def _on_project_label_clicked(self, event):
        """项目标签点击 - 显示项目选择 popup"""
        event.accept()
        self._show_project_selector_popup()

    def _update_project_label_style(self):
        """更新项目标签样式（跟随主题色）"""
        self._project_label.setStyleSheet(f"""
            QLabel {{
                color: {Colors.TEXT_ACCENT};
                {get_font_family_css()}
                {font_size_css(13)}
                font-weight: bold;
                padding: 2px 6px;
                border-radius: 4px;
                background: {Colors.HOVER_BG};
            }}
            QLabel:hover {{
                background: {Colors.SELECTED_BG};
            }}
        """)

    def _update_branch(self):
        """从工作目录检测 git 分支并更新分支标签"""
        branch = None
        try:
            # 多窗口隔离：优先使用实例缓存（与 _sync_working_directory 一致）
            workdir = None
            project_workdir = self._current_workdir.get(self._current_project)
            if project_workdir:
                workdir = project_workdir
            # 其次从 memory_manager 获取（DB 默认值）
            if not workdir and self.backend and self.backend.memory_manager:
                workdir = self.backend.memory_manager.get_working_directory(self._current_project)
            # 最后从 tool_executor 获取
            if not workdir and self.backend and self.backend.tool_executor:
                workdir = getattr(self.backend.tool_executor, '_workdir', None)
            # workdir 为空时直接跳过 git 检测，分支标签隐藏
            if not workdir:
                pass  # branch 保持 None，分支标签隐藏
            if workdir and os.path.isdir(str(workdir)):
                workdir = str(workdir)
                from app.utils.git_worktree import GitWorktreeDetector
                git_root = GitWorktreeDetector.detect_git(workdir)
                if git_root:
                    r = subprocess.run(
                        ["git", "branch", "--show-current"],
                        capture_output=True, text=True, cwd=workdir,
                        timeout=3, encoding="utf-8", errors="replace",
                        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                    )
                    if r.returncode == 0:
                        branch = r.stdout.strip()
        except Exception:
            pass

        if branch:
            # 分支名过长时截断显示，悬浮显示全名
            display = branch if len(branch) <= 20 else branch[:8] + "…" + branch[-8:]
            self._branch_label.setText(display)
            self._branch_widget.setToolTip(f"分支: {branch}\n点击打开关键文档")

            # 容器样式：背景色 + 边框 + 圆角
            self._branch_widget.setStyleSheet(f"""
                #_branchWidget {{
                    background: {Colors.BRANCH_LABEL_BG};
                    border: 1px solid {Colors.BRANCH_LABEL_BORDER};
                    border-radius: 2px;
                }}
                #_branchWidget:hover {{
                    background: {Colors.HOVER_BG};
                }}
            """)
            # 文字标签：纯文字颜色+字体，无背景/边框
            self._branch_label.setStyleSheet(f"""
                color: {Colors.TEXT_SECONDARY};
                {get_font_family_css()}
                {font_size_css(10)};
                background: transparent;
                border: none;
            """)
            # 图标标签：透明
            self._branch_icon.setStyleSheet("background: transparent; border: none;")

            self._branch_widget.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
            self._branch_widget.setMaximumWidth(160)
            self._branch_widget.setVisible(True)
        else:
            self._branch_label.setText("")
            self._branch_widget.setVisible(False)
    def _on_branch_label_clicked(self, event):
        """分支标签点击 — 打开关键文档卡片"""
        event.accept()
        self._toggle_memory_card()
        # 确保切换到关键文档 Tab
        if hasattr(self, '_memory_card_popup') and self._memory_card_popup:
            from app.widgets.cards.settings.memory_card import TAB_KEY_DOCUMENTS
            self._memory_card_popup.switch_tab(TAB_KEY_DOCUMENTS)
            # 同步卡片 Tab 按钮
            if hasattr(self, '_memory_card') and self._memory_card:
                self._memory_card.set_current_tab(TAB_KEY_DOCUMENTS)

    def _show_project_selector_popup(self):
        """显示项目选择弹窗"""
        if hasattr(self, '_project_selector_popup') and self._project_selector_popup:
            self._project_selector_popup.close()
            self._project_selector_popup.deleteLater()

        projects = self.history_manager.get_projects() if self.history_manager else ["默认项目"]
        self._project_selector_popup = ProjectSelectorPopup(
            projects=projects,
            current_project=self._current_project,
            parent=self
        )
        self._project_selector_popup.projectSelected.connect(self._on_project_selected)
        self._project_selector_popup.newProjectCreated.connect(self._on_new_project_created)
        self._project_selector_popup.archiveProject.connect(self._on_archive_project)
        self._project_selector_popup.show_at(self._project_label)

    def _on_project_selected(self, project: str):
        """切换到选中的项目"""
        self._current_project = project
        self.backend._current_project = project
        self._project_label.setText(project)
        self._update_project_label_style()
        self._update_branch()
        self.cfg.current_project.value = project
        self.cfg.save()
        # 更新 tool_executor 的当前项目
        if self.backend and self.backend.tool_executor:
            self.backend.tool_executor.set_current_project(project)
        # 刷新记忆卡片的项目（项目笔记、关键文档会跟着刷新）
        if hasattr(self, '_memory_card_popup') and self._memory_card_popup:
            from loguru import logger
            logger.info(f"[MainWidget] Calling set_project({project}) on memory_card_popup")
            self._memory_card_popup.set_project(project)
            # 切换项目时自动同步工作目录
            self._sync_working_directory()
        # 刷新历史面板（切换项目过滤）
        self._current_history_project = project
        self._history_popup_card.set_current_project(project)
        self._refresh_history_toggle_panel()
        # 自动触发新建会话，避免原会话与切换后的项目不匹配
        self._create_new_session()

    def _on_new_project_created(self, project: str):
        """新建项目后"""
        self._current_project = project
        self.backend._current_project = project
        self._project_label.setText(project)
        self._update_project_label_style()
        self._update_branch()
        # 保存到配置
        self.cfg.current_project.value = project
        self.cfg.save()
        # 刷新记忆卡片的项目
        if hasattr(self, '_memory_card_popup') and self._memory_card_popup:
            self._memory_card_popup.set_project(project)
            # 自动切换到项目笔记tab（触发头部标签和内容同步切换）
            self._memory_card.set_current_tab(TAB_PROJECT_NOTES)
            # 切换项目时自动同步工作目录
            self._sync_working_directory()
        # 刷新历史面板
        self._history_popup_card.refreshRequested.emit()
        # 自动弹出长期记忆卡片
        if not self._memory_card.isVisible():
            self._toggle_memory_card()
        # 自动触发新建会话
        self._create_new_session()

    def _on_archive_project(self, project_name: str):
        """归档项目处理"""
        if not self.history_manager:
            return

        # 后端执行归档
        count = self.history_manager.archive_project(project_name)

        if count > 0:
            # 如果归档的是当前项目，切换到默认项目
            if project_name == self._current_project:
                default_project = "默认项目"
                self._current_project = default_project
                self.backend._current_project = default_project
                self._project_label.setText(default_project)
                self._update_project_label_style()
                self._update_branch()
                # 保存到配置
                self.cfg.current_project.value = default_project
                self.cfg.save()
                # 创建新会话
                self._create_new_session()
            else:
                # 更新当前项目过滤
                self._current_history_project = self._current_project
                self._refresh_history_toggle_panel()

            # 刷新历史面板
            self._history_popup_card.refreshRequested.emit()

            InfoBar.success(
                "归档成功",
                f"已归档项目「{project_name}」的 {count} 个会话",
                parent=self,
                duration=3000,
                position=InfoBarPosition.BOTTOM
            )
        else:
            InfoBar.warning(
                "归档失败",
                f"项目「{project_name}」没有可归档的会话",
                parent=self,
                duration=3000,
                position=InfoBarPosition.BOTTOM
            )

    def _on_memory_tab_changed(self, tab_id: str):
        """处理记忆管理标签切换"""
        # 清空搜索
        search_input = getattr(self._memory_card, '_search_input', None)
        if search_input:
            search_input.clear()
        # 更新占位文本
        placeholders = {"entries": "🔍 搜索条目记忆...", "notes": "🔍 搜索项目笔记...", "docs": "🔍 搜索关键文档..."}
        if search_input:
            search_input.setPlaceholderText(placeholders.get(tab_id, "🔍 搜索..."))
        # 切换内容
        self._memory_card_popup.switch_tab(tab_id)

    def _on_working_dir_changed(self, file_path: str):
        """工作目录变更 → 更新实例缓存 + 同步到工具执行器 + 刷新分支标签

        多窗口隔离：更新实例缓存（关键！），DB 写入在 memory_card 层已完成。
        """
        # 更新实例缓存（多窗口隔离：每个窗口独立持有自己的 workdir）
        if file_path:
            self._current_workdir[self._current_project] = file_path
        else:
            self._current_workdir.pop(self._current_project, None)
        # 同步到工具执行器
        if self.backend and self.backend.tool_executor:
            self.backend.tool_executor.set_workdir(file_path or None)
            from loguru import logger
            logger.info(f"[MainWidget] Working directory synced to tool executor: {file_path or 'default'}")
        # 工作目录变更后刷新分支标签
        self._update_branch()

    def _sync_working_directory(self):
        """切换项目时自动加载并同步工作目录

        多窗口隔离：实例缓存优先；首次启动时从 DB 读取（新窗口默认值回退）。
        """
        if getattr(self, '_is_destroyed', False):
            return
        if not self.backend or not self.backend.tool_executor:
            return
        project = self._current_project
        # 实例缓存优先（多窗口隔离的关键：保持自身选择，不受其他窗口 DB 写入影响）
        workdir = self._current_workdir.get(project)
        if workdir is None:
            # 首次启动或项目首次切换，从 DB 读取默认值（新窗口恢复用）
            if self.backend.memory_manager:
                workdir = self.backend.memory_manager.get_working_directory(project)
            if workdir:
                self._current_workdir[project] = workdir
        self.backend.tool_executor.set_workdir(workdir or None)
        from loguru import logger
        logger.info(f"[MainWidget] Synced working directory for project '{project}': {workdir or 'default'}")

    def _show_soul_memory(self):
        """切换记忆管理卡片的显示"""
        self._toggle_memory_card()

    def _toggle_memory_card(self):
        """切换记忆管理卡片的显示"""
        self._card_manager.toggle_card("memory")

    def _on_memory_card_saved(self, memories: list):
        """记忆卡片保存后的回调"""
        # 数据已经在 MemoryCardContent 中通过 backend 保存
        # 这里只显示提示信息
        InfoBar.success("已保存", "长期记忆已更新", parent=self, duration=1500, position=InfoBarPosition.BOTTOM)

    def _on_memory_updated(self, memories: list):
        self.backend.update_user_memories(memories)
        InfoBar.success("已保存", "长期记忆已更新", parent=self, duration=1500, position=InfoBarPosition.BOTTOM)

    def _on_title_double_click(self, event):
        from PyQt5.QtWidgets import QInputDialog, QLineEdit

        current_title = self.title_edit.text()
        new_title, ok = QInputDialog.getText(
            self, "编辑标题", "请输入新标题:", QLineEdit.Normal, current_title
        )
        if ok and new_title.strip():
            self._update_title(new_title.strip())

    def _update_title(self, new_title: str):
        self.title_edit.setText(new_title)
        if self._current_session_id is not None:
            idx = self.history_manager.find_index_by_session_id(
                self._current_session_id
            )
            if idx is not None:
                self.history_manager.update_session_title(idx, new_title)

    def _auto_save_current_session(self):
        session = self.session_manager.get_current_session()
        if not session or not session.messages:
            return
        
        # 跳过没有用户消息的会话（SessionStart hook 产生的空会话不应保存到历史）
        has_user_message = any(
            msg.get("role") == "user" 
            for msg in session.messages
        )
        if not has_user_message:
            return

        system_prompt = getattr(session, "system_prompt", "") or ""

        if self._current_session_id is not None:
            idx = self.history_manager.find_index_by_session_id(
                self._current_session_id
            )
            if idx is not None:
                self.history_manager.update_session(
                    idx,
                    session.messages,
                    compaction_state=getattr(session, "compaction_state", {}),
                    compaction_cache=getattr(session, "compaction_cache", {}),
                    system_prompt=system_prompt,
                )
            else:
                self.history_manager.save_session(
                    session.messages,
                    session_id=session.session_id,
                    compaction_state=getattr(session, "compaction_state", {}),
                    compaction_cache=getattr(session, "compaction_cache", {}),
                    system_prompt=system_prompt,
                )
                self._current_session_id = session.session_id
        else:
            self.history_manager.save_session(
                session.messages,
                session_id=session.session_id,
                compaction_state=getattr(session, "compaction_state", {}),
                compaction_cache=getattr(session, "compaction_cache", {}),
                system_prompt=system_prompt,
            )
            self._current_session_id = session.session_id

        if self._current_session_id:
            idx = self.history_manager.find_index_by_session_id(
                self._current_session_id
            )
            if idx is not None:
                return self.history_manager.get_current_title(idx)
        return None

    def closeEvent(self, event):
        # 标记窗口正在关闭，防止所有异步回调访问已销毁的 UI
        self._is_destroyed = True
        
        # 设置关闭标志，阻止 Settings.save() 写入磁盘（防止覆盖用户粘贴的配置）
        if hasattr(self, 'backend') and self.backend:
            try:
                self.backend.set_ui_valid(False)
                # 清除 ChatEngine 的所有回调，防止异步回调访问已销毁的 widget
                if self.backend.chat_engine:
                    self.backend.chat_engine.clear_callbacks()
                # 停止所有正在进行的流式输出
                self.backend.stop_streaming()
                self._topic_summary_cancelled = True  # 🛡️ 取消标题生成重试
                # 清理 worker
                self.backend.cleanup_worker()
            except Exception:
                pass
        
        # 标记初始化已完成（防止窗口在初始化期间关闭导致竞态条件）
        self._initialization_in_progress = False
        
        try:
            self._auto_save_current_session()
        except Exception:
            pass
        
        # 断开 aboutToQuit 信号，防止退出时访问已销毁的 widget
        try:
            app = QApplication.instance()
            if app is not None:
                app.aboutToQuit.disconnect(self._auto_save_current_session)
        except Exception:
            pass
        
        super().closeEvent(event)

    def _toggle_send_stop(self, is_sending: bool):
        if is_sending:
            self.history_btn.setDisabled(True)
            self.input_area.toggle_send_button(False)
        else:
            self.history_btn.setDisabled(False)
            self.input_area.toggle_send_button(True)

    def _on_stop_clicked(self):
        if getattr(self, '_is_destroyed', False):
            return
        self._tool_cancelled_by_user = False
        # 🛡️ 取消正在进行的标题生成任务，防止停止后仍继续重试
        self._topic_summary_cancelled = True
        interrupted_messages: List[Dict[str, Any]] = []

        if self.backend.chat_engine:
            interrupted_messages = self.backend.stop_streaming() or []

        self._is_streaming = False

        if self._tool_floating_widget:
            self._tool_floating_widget.clear()
            self._tool_floating_widget.setVisible(False)

        self._toggle_send_stop(False)
        if self._current_assistant_card:
            self._current_assistant_card.stop_streaming_anim()
            self._current_assistant_card.finish_streaming()

        if interrupted_messages:
            self._on_messages_updated(interrupted_messages)
            if self.history_manager:
                self._save_current_session_to_history()
        InfoBar.warning(
            title="已中止",
            content="",
            orient=Qt.Horizontal,
            isClosable=True,
            position=InfoBarPosition.BOTTOM,
            duration=2000,
            parent=self,
        )
        if self.input_area:
            self.input_area.setFocus()

    def _create_context_menu(self):
        self._context_menu_actions = {}

    def _show_context_menu(self, pos=None):
        from PyQt5.QtWidgets import QMenu

        menu = QMenu(self)
        export_action = menu.addAction("导出对话记录")
        export_action.triggered.connect(self._export_conversation)
        clear_action = menu.addAction("清空当前对话")
        clear_action.triggered.connect(self._clear_current_conversation)
        # 从项目标签位置弹出
        if hasattr(self, '_project_label') and self._project_label.isVisible():
            menu.exec_(self._project_label.mapToGlobal(self._project_label.rect().bottomLeft()))
        else:
            menu.exec_(pos)

    def _export_conversation(self):
        session = self.session_manager.get_current_session()
        if not session or not session.messages:
            InfoBar.warning("无法导出", "当前没有对话内容", parent=self, position=InfoBarPosition.BOTTOM)
            return
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "导出对话",
            f"对话_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md",
            "Markdown Files (*.md);;Text Files (*.txt)",
        )
        if not file_path:
            return
        try:
            content = export_messages_to_markdown(session.messages)
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)
            InfoBar.success("导出成功", f"已保存到: {file_path}", parent=self, position=InfoBarPosition.BOTTOM)
        except Exception as e:
            InfoBar.error("导出失败", str(e), parent=self, position=InfoBarPosition.BOTTOM)

    def _clear_current_conversation(self):
        self._create_new_session()
        InfoBar.success("已清空", "开始新的对话", parent=self, duration=1500, position=InfoBarPosition.BOTTOM)

    # ================================================================
    #  AutoLoop 相关方法
    # ================================================================

    def _show_auto_loop_config(self):
        """显示/隐藏 AutoLoop 配置卡（类似记忆卡片，点击切换）"""
        if self._is_auto_loop_running:
            return
        self._card_manager.toggle_card("auto_loop_config")

    def _on_auto_loop_start(self, config: 'AutoLoopConfig'):
        """开始 AutoLoop"""
        if self._is_auto_loop_running:
            return

        # 设置项目路径（工作目录）
        import os
        project_path = config.project_path.strip() if config.project_path else ""
        if not project_path:
            # 如果用户没有填写项目路径，默认使用当前工作目录
            project_path = os.getcwd()
        
        abs_path = os.path.abspath(project_path)
        if os.path.isdir(abs_path):
            if self.backend.tool_executor:
                # 通过 ToolExecutor.set_workdir 统一设置，同时更新 builtin_tools
                self.backend.tool_executor.set_workdir(abs_path)
            config.project_path = abs_path
            logger.info(f"[AutoLoop] Workdir set to: {abs_path}")
        else:
            logger.warning(f"[AutoLoop] Project path does not exist: {abs_path}")

        # 隐藏配置卡，显示运行卡
        self._auto_loop_config_card.hide()
        self._auto_loop_running_card.show()
        # 确保停止按钮可见（彻底修复完成后重新运行时停止按钮消失的问题）
        self._auto_loop_running_card.show_stop_button()
        self._auto_loop_running_card.start_animation()
        self._auto_loop_running_card.set_max_tokens(config.max_tokens)
        self._auto_loop_running_card.set_task(config.task_prompt)

        # 锁定 UI
        self._lock_ui_for_autoloop()

        # 获取 auto_loop agent 的工具权限，过滤掉 deny 的工具
        agent_manager = self.backend.agent_manager
        agent = agent_manager.get_agent("auto_loop") if agent_manager else None
        agent_perms = agent.permission if agent else {}
        denied_tools = {
            name for name, val in agent_perms.items()
            if val in ("deny", False)
        }

        from app.tools import get_builtin_tools_schema
        all_tools = get_builtin_tools_schema(
            agent_manager=agent_manager,
            builtin_tools=self.backend.tool_executor._builtin_tools if hasattr(self.backend, 'tool_executor') else None,
        )
        tools_schema = [
            t for t in all_tools
            if t.get("function", {}).get("name", "") not in denied_tools
        ]

        # 获取 compactor
        compactor = None
        if self.backend.chat_engine and hasattr(self.backend.chat_engine, '_compactor'):
            compactor = self.backend.chat_engine._compactor

        # 创建并启动 worker
        from app.core.workers.auto_loop_worker import AutoLoopWorker
        self._auto_loop_worker = AutoLoopWorker()
        self._auto_loop_worker.configure(
            config=config,
            model_config_getter=self._get_current_model_config,
            tool_executor=self.backend.tool_executor,
            tools_schema=tools_schema,
            agent_system_prompt_getter=lambda name: (
                self.backend.agent_manager.get_agent(name).prompt
                if self.backend.agent_manager and self.backend.agent_manager.get_agent(name)
                else ""
            ),
            permission_check_callback=(
                self.backend.chat_engine._check_tool_permission
                if self.backend.chat_engine else None
            ),
            permission_cache=(
                self.backend.chat_engine._permission_cache
                if self.backend.chat_engine else None
            ),
            compactor=compactor,
        )

        # 连接信号 - 注意：tokens_updated 必须用 DirectConnection 确保实时更新
        self._auto_loop_worker.iteration_started.connect(self._on_auto_loop_iteration_started, Qt.QueuedConnection)
        self._auto_loop_worker.iteration_completed.connect(self._on_auto_loop_iteration_completed, Qt.QueuedConnection)
        self._auto_loop_worker.progress_updated.connect(self._on_auto_loop_progress, Qt.QueuedConnection)
        self._auto_loop_worker.loop_completed.connect(self._on_auto_loop_completed, Qt.QueuedConnection)
        self._auto_loop_worker.loop_error.connect(self._on_auto_loop_error, Qt.QueuedConnection)
        self._auto_loop_worker.loop_stopped.connect(self._on_auto_loop_stopped, Qt.QueuedConnection)
        self._auto_loop_worker.log_signal.connect(self._on_auto_loop_log, Qt.QueuedConnection)
        self._auto_loop_worker.phase_changed.connect(self._on_auto_loop_phase_changed, Qt.QueuedConnection)
        # tokens_updated 使用 QueuedConnection 确保 UI 更新在主线程执行（避免 DirectConnection 在 worker 线程执行导致 UI 无法更新）
        self._auto_loop_worker.tokens_updated.connect(self._on_auto_loop_tokens_updated, Qt.QueuedConnection)

        self._is_auto_loop_running = True
        self._auto_loop_worker.start()

    def _on_auto_loop_stop(self):
        """停止 AutoLoop（用户主动停止）
        
        不再阻塞 UI 线程！通过 loop_stopped 信号异步处理清理。
        只在 looper 线程退出后(通过信号)才执行 _finish_auto_loop，
        避免 UI 卡死和二次清理导致的闪退。
        """
        if self._auto_loop_worker and self._auto_loop_worker.isRunning():
            # 1. 立即发送取消信号给 worker 线程
            self._auto_loop_worker.cancel()
            # 2. UI 立即反馈，不阻塞
            if self._auto_loop_running_card:
                self._auto_loop_running_card.set_status("⏹ 正在停止...")
            # 3. 安全兜底：5 秒后如果还没停，强制清理（避免永久卡住）
            from PyQt5.QtCore import QTimer
            QTimer.singleShot(5000, self._force_cleanup_autoloop)

    def _force_cleanup_autoloop(self):
        """兜底清理：如果 worker 线程未正常结束，强制清理"""
        if not self._is_auto_loop_running:
            return  # 已经通过信号正常清理了
        logger.warning("[AutoLoop] Force cleanup after timeout")
        if self._auto_loop_worker and self._auto_loop_worker.isRunning():
            self._auto_loop_worker.wait(2000)
        self._finish_auto_loop("⏹ 强制停止（超时）")

    def _on_auto_loop_phase_changed(self, phase: str):
        """AutoLoop 阶段变更"""
        if self._auto_loop_running_card:
            self._auto_loop_running_card.set_phase(phase)

    def _on_auto_loop_iteration_started(self, current: int, total: int):
        """迭代开始"""
        if self._auto_loop_running_card:
            progress = self._auto_loop_worker.get_current_progress()
            phase = progress.get("phase", "planning")
            current_step = progress.get("current_step", 0)
            total_steps = progress.get("total_steps", 0)

            if phase == "planning":
                # 规划阶段
                self._auto_loop_running_card.set_phase("planning")
                self._auto_loop_running_card.set_status(f"📋 第 {current} 轮: 规划中...")
            else:
                # 执行阶段
                self._auto_loop_running_card.set_phase("executing")
                # 显示当前步骤在状态文本中
                if total_steps > 0:
                    self._auto_loop_running_card.set_status(
                        f"▶ 第 {current} 轮 / 共 {total} 轮 | 步骤 {current_step}/{total_steps}"
                    )
                else:
                    self._auto_loop_running_card.set_status(
                        f"▶ 第 {current} 轮 / 共 {total} 轮"
                    )

    def _on_auto_loop_iteration_completed(self, iteration: int, summary: str):
        """迭代完成"""
        if self._auto_loop_running_card:
            self._auto_loop_running_card.append_log(f"第 {iteration} 轮完成: {summary[:40]}")

    def _on_auto_loop_log(self, text: str):
        """可视化日志更新"""
        if self._auto_loop_running_card:
            self._auto_loop_running_card.append_log(text)

    def _on_auto_loop_tokens_updated(self, total_tokens: int):
        """Token 实时更新 — 直接使用信号携带的值"""
        if self._auto_loop_running_card:
            self._auto_loop_running_card.update_tokens(total_tokens)

    def _on_auto_loop_progress(self, progress: dict):
        """更新运行卡进度（不更新 token，因为 update_tokens() 会专门处理）
        
        注意：token 更新由 update_tokens() 专门处理，避免与 progress_updated 信号的竞争条件
        导致 token 显示被覆盖的问题。
        """
        if self._auto_loop_running_card:
            self._auto_loop_running_card.update_progress_no_token(progress)

    def _on_auto_loop_completed(self, message: str):
        """AutoLoop 完成"""
        if self._auto_loop_running_card:
            self._auto_loop_running_card.set_phase("completed")
            self._auto_loop_running_card.show_completed(message)
        self._finish_auto_loop(message)

    def _on_auto_loop_error(self, message: str):
        """AutoLoop 出错"""
        if self._auto_loop_running_card:
            self._auto_loop_running_card.show_error(message)
            self._auto_loop_running_card.append_log(f"❌ {message[:50]}")
        self._finish_auto_loop(f"❌ {message}")

    def _on_auto_loop_stopped(self):
        """AutoLoop 已停止"""
        self._finish_auto_loop("⏹ 已停止")

    def _finish_auto_loop(self, message: str):
        """清理 AutoLoop 状态
        
        注意：可能被多个路径调用（loop_completed/loop_error/loop_stopped 信号 + 兜底定时器），
        必须在顶部做防重入保护。
        """
        if not self._is_auto_loop_running:
            # 防止二次清理导致 worker.deleteLater 冲突和闪退
            return
        self._is_auto_loop_running = False

        # 恢复为当前项目配置的工作目录
        self._sync_working_directory()

        # 停止动画（只调用一次，移除重复调用）
        if self._auto_loop_running_card:
            self._auto_loop_running_card.stop_animation()

        # 隐藏运行卡
        self._auto_loop_running_card.hide()
        self._restore_after_system_close()

        # 保存 AutoLoop 消息到会话历史
        if self._auto_loop_worker:
            try:
                messages = self._auto_loop_worker.get_all_messages()
                if messages:
                    self._save_auto_loop_messages_to_session(messages)
            except Exception as e:
                logger.warning(f"[AutoLoop] Failed to save messages to session: {e}")

        # 清理 worker
        if self._auto_loop_worker:
            try:
                self._auto_loop_worker.quit()
                self._auto_loop_worker.wait(1000)
            except Exception:
                pass
            self._auto_loop_worker.deleteLater()
            self._auto_loop_worker = None

        # 解锁 UI
        self._unlock_ui_after_autoloop()

        # 通知用户
        InfoBar.success("AutoLoop", message, parent=self, duration=5000, position=InfoBarPosition.BOTTOM)

    def _lock_ui_for_autoloop(self):
        """锁定 UI — 禁止发送消息和新建会话"""
        # 禁用输入框
        self.input_area.setDisabled(True)
        self.input_area.setPlaceholderText("AutoLoop 运行中... 点运行卡 [⏹ 停止] 恢复操作")

        # 禁用新建按钮
        self.new_session_btn.setDisabled(True)

        # 记录原有状态，用于解锁
        logger.info("[AutoLoop] UI locked")

    def _unlock_ui_after_autoloop(self):
        """解锁 UI"""
        self.input_area.setDisabled(False)
        self.input_area.setPlaceholderText("给 DriFox 发送消息，Enter 发送，Shift+Enter 换行")
        self.new_session_btn.setDisabled(False)

        # 重新聚焦输入框
        self.input_area.setFocus()
        logger.info("[AutoLoop] UI unlocked")

    def _save_auto_loop_messages_to_session(self, messages: List[Dict]):
        """将 AutoLoop 执行的消息保存到当前会话"""
        session = self.session_manager.get_current_session()
        if not session:
            return

        # messages 来自 AutoLoopWorker.get_all_messages()，
        # 包含 on_messages_updated 收集的完整消息（含 user + assistant + tool_calls）
        auto_loop_messages = list(messages or [])

        # 确保 user 消息存在（第一条 user 消息）
        has_user = any(msg.get("role") == "user" for msg in auto_loop_messages)
        if not has_user and self._auto_loop_worker:
            task_prompt = self._auto_loop_worker.get_task_prompt()
            if task_prompt:
                from datetime import datetime
                user_msg = {
                    "role": "user",
                    "content": task_prompt,
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                auto_loop_messages.insert(0, user_msg)

        # 更新会话（preserve_compaction=True 避免压缩状态被破坏）
        session.set_messages(auto_loop_messages, preserve_compaction=True)

        logger.info(f"[AutoLoop] 保存 {len(auto_loop_messages)} 条消息到会话: {self._current_project}")

        # 触发 topic_summary 生成标题（如果还没有标题）
        session = self.session_manager.get_current_session()
        if session and not session.topic_summary:
            self._maybe_generate_topic_summary()

        # 同步保存到历史记录
        self._save_current_session_to_history()
