# -*- coding: utf-8 -*-
"""system_cards 模块 — 六张系统卡懒创建 + 项目选择卡（源 main_widget.setup_ui L2979-3112）

搬运时基线：app/main_widget.py 的 setup_ui 中「六张系统卡片框架懒创建（P0-1 性能优化）」
整段（2979-3112）原样搬移；self → host，赋值显式挂回 host；import 提升到 build 内。

契约属性集（grep `self.[a-z_]+ *=` over 2979-3112，逐项 host.setattr）：
- _history_card _history_popup_card _share_card _share_card_content
- _history_questions_card _history_questions_card_content
- _memory_card _memory_card_popup _model_config_card _model_config_popup
- _model_selector_card _model_selector_card_content
- _tool_control_card _project_selector_card _project_selector_card_content
- _project_new_edit _project_new_btn _project_open_folder_btn _project_import_btn
- _question_floating_widget
host 方法依赖（build 内调用/连接，插件 override 时应保持同名）：
- _card_manager(_bottom_card_container) / _register_cards_to_manager / _system_card_ids
- _on_project_selected 等信号回调 / _restore_after_system_close / _refresh_tool_toggle_btn
- _init_builtin_commands（经 QTimer.singleShot 延迟注册）
"""

from app.plugins.contracts.ui_module import UIModule


class SystemCardsModule(UIModule):
    """系统卡模块：六张系统卡片框架懒创建 + 项目选择卡 + 问答浮动卡"""

    module_id = "system_cards"

    def build(self, host) -> None:
        from PyQt5.QtCore import QTimer
        from PyQt5.QtWidgets import QLineEdit
        from qfluentwidgets import FluentIcon, TransparentToolButton

        from app.utils.design_tokens import Colors, font_size_css
        from app.utils.utils import get_font_family_css, get_icon
        from app.widgets.cards.floating.question_floating_widget import (
            QuestionFloatingWidget,
        )
        from app.widgets.cards.settings.base_settings_card import BaseSettingsCard
        from app.widgets.cards.settings.project_selector_card import (
            ProjectSelectorCardContent,
        )
        from app.widgets.cards.settings.tool_control_card import ToolControlCardFrame

        # ── 六张系统卡片框架懒创建（P0-1 性能优化）──
        # 原 setup_ui 同步段直接创建 6 张 BaseSettingsCard 框架（~160ms），
        # 改为 _ensure_xxx_card() 惰性创建：deferred 链预构建 + 打开入口兜底。
        # 属性名保持稳定（None 占位），引用点已有 hasattr/getattr/if 保护。
        host._history_card = None
        host._history_popup_card = None
        host._share_card = None
        host._share_card_content = None
        host._history_questions_card = None
        host._history_questions_card_content = None
        host._memory_card = None
        host._memory_card_popup = None
        host._model_config_card = None
        host._model_config_popup = None
        host._model_selector_card = None
        host._model_selector_card_content = None

        # 工具控制卡片（controller 由 _tool_permission_controller 在后续 set_controller 注入）
        host._tool_control_card = ToolControlCardFrame(host)
        # 🛡️ 如果 controller 已存在（__init__ 中在 super 之前创建时），立即绑定
        if hasattr(host, "_tool_permission_controller") and host._tool_permission_controller is not None:
            host._tool_control_card.set_controller(host._tool_permission_controller)
        host._tool_control_card.setObjectName("toolControlCard")
        host._tool_control_card.setMinimumHeight(250)
        host._tool_control_card.setVisible(False)
        host._tool_control_card.closed.connect(
            lambda: (
                host._card_manager.hide_card("tool_control", host._window_id),
                host._restore_after_system_close(),
            )
        )
        host._tool_control_card.togglesChanged.connect(lambda _: host._refresh_tool_toggle_btn())
        host._bottom_card_container.add_card("tool_control", host._tool_control_card)

        # 模型选择卡片框架懒创建（P0-1）：见上方 _ensure_model_selector_card() 说明

        # 项目选择卡片（Top 卡片，与 settings 同容器）
        host._project_selector_card = BaseSettingsCard("", "", host)
        host._project_selector_card.setMinimumHeight(200)  # 自适应窗口高度
        host._project_selector_card_content = ProjectSelectorCardContent()
        host._project_selector_card_content.projectSelected.connect(host._on_project_selected)
        host._project_selector_card_content.newProjectCreated.connect(host._on_new_project_created)
        host._project_selector_card_content.archiveProject.connect(host._on_archive_project)
        host._project_selector_card_content.exportProject.connect(host._on_export_project)
        host._project_selector_card_content.importProjectRequested.connect(host._on_import_project)
        host._project_selector_card_content.projectFileDropped.connect(host._on_project_file_dropped)
        host._project_selector_card_content.openFolderRequested.connect(host._on_open_project_folder)
        host._project_selector_card_content.folderDropped.connect(host._on_project_folder_dropped)

        host._project_selector_card.content_layout.addWidget(host._project_selector_card_content)
        # ── 新建项目输入放到标题栏 ──
        Colors.refresh()
        host._project_new_edit = QLineEdit(host._project_selector_card)
        host._project_new_edit.setPlaceholderText("新建/搜索项目...")
        host._project_new_edit.setMaximumWidth(220)
        host._project_new_edit.setMinimumWidth(130)
        host._project_new_edit.setFixedHeight(26)
        host._project_new_edit.setStyleSheet(f"""
            QLineEdit {{
                background: {Colors.HOVER_BG};
                border: 1px solid {Colors.BORDER};
                border-radius: 4px;
                color: {Colors.TEXT_PRIMARY};
                padding: 2px 6px;
                {font_size_css(11)}
                {get_font_family_css()}
            }}
            QLineEdit:focus {{
                border: 1px solid {Colors.TEXT_ACCENT};
            }}
            QLineEdit::placeholder {{
                color: {Colors.INPUT_PLACEHOLDER};
            }}
        """)
        host._project_new_edit.returnPressed.connect(host._on_header_new_project)
        host._project_new_edit.textChanged.connect(host._on_project_filter_changed)

        host._project_new_btn = TransparentToolButton(FluentIcon.ADD, host._project_selector_card)
        host._project_new_btn.setFixedSize(24, 24)
        host._project_new_btn.setToolTip("创建项目")
        host._project_new_btn.clicked.connect(host._on_header_new_project)

        # 选择文件夹按钮（+号右侧）
        host._project_open_folder_btn = TransparentToolButton(FluentIcon.FOLDER, host._project_selector_card)
        host._project_open_folder_btn.setFixedSize(24, 24)
        host._project_open_folder_btn.setToolTip("选择文件夹作为项目根目录")
        host._project_open_folder_btn.clicked.connect(host._on_project_open_folder_btn)

        # 导入项目按钮（从 .drifox_project 压缩包导入）
        host._project_import_btn = TransparentToolButton(get_icon("导入"), host._project_selector_card)
        host._project_import_btn.setFixedSize(24, 24)
        host._project_import_btn.setToolTip("导入项目（从 .drifox_project 压缩包）")
        host._project_import_btn.clicked.connect(host._on_import_project)

        # 插入到标题栏的额外按钮区（关闭按钮之前）
        host._project_selector_card._extra_buttons_container.insertWidget(0, host._project_new_edit)
        host._project_selector_card._extra_buttons_container.insertWidget(1, host._project_new_btn)
        host._project_selector_card._extra_buttons_container.insertWidget(2, host._project_open_folder_btn)
        host._project_selector_card._extra_buttons_container.insertWidget(3, host._project_import_btn)

        host._project_selector_card.setVisible(False)
        host._project_selector_card.closed.connect(
            lambda: (
                host._card_manager.hide_card("project_selector", host._window_id),
                host._restore_after_system_close(),
            )
        )

        host._question_floating_widget = QuestionFloatingWidget(host)
        host._question_floating_widget.setVisible(False)
        host._question_floating_widget.answered.connect(host._on_question_answered)
        host._question_floating_widget.cancelled.connect(host._on_question_cancelled)
        host._question_floating_widget.previewRequested.connect(host._on_question_preview_requested)
        host._bottom_card_container.add_card("question", host._question_floating_widget)

        # 注册卡片到 CardManager（优先级：数值越小权限越高）
        host._register_cards_to_manager()

        # 系统卡片打开时隐藏文本输入框（保留按钮栏），关闭时恢复
        # _system_card_ids 在 __init__ 顶部初始化为 _BASE_SYSTEM_CARD_IDS，
        # UI 插件注册浮动卡片后通过 register_system_card() 扩展该集合。
        for _cid in host._system_card_ids:
            host._card_manager.on_card_shown(host._window_id, _cid, lambda cid: host._on_system_card_opened(cid))
            host._card_manager.on_card_hidden(host._window_id, _cid, lambda cid: host._on_system_card_closed(cid))

        # ===== 内置命令先注册（UI 插件命令依赖 CommandManager） =====
        # [PERF] 延迟 100ms 到首帧之后注册，节省 ~200ms 关键路径时间。
        # 为什么是 100ms 而非 singleShot(0)：Qt QTimer 按到期时间排序，
        # singleShot(0) 到期时间 ≈ 创建时间，早于 main.py 中 _show_popup 的
        # singleShot(0)（创建更晚），导致 BuiltinCommands 仍在窗口显示前执行。
        # 100ms 延迟确保到期时间晚于所有 singleShot(0)，在窗口第一次绘制后注册。
        QTimer.singleShot(100, host._init_builtin_commands)
