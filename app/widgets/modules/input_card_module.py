# -*- coding: utf-8 -*-
"""input_card 模块 — 输入卡/附件区/命令三卡（源 main_widget.setup_ui L3273-3414）

Phase F：原 setup_ui 底部输入区域段（计划标注 3262-3410，实际 3273-3414）整体搬移到本模块。
系统默认装配逻辑在此实现；插件可 register_ui_module(module_id="input_card", priority>=100)
完全替换输入区装配。

属性契约（host.setattr）：
- _bottom_input_container _bottom_input_layout _input_card _input_card_wrapper
- _attach_container _attach_layout _attachments _history_working_attachments
- input_area _command_card _file_mention_card _undo_delete_card
- _undo_delete_cache _truncation_sentinel _pending_send_after_truncation _pending_send_user_text

契约集提取命令（搬运基线）：
    python -X utf8 -c "import re; lines=open('app/main_widget.py',encoding='utf-8').read().split(chr(10)); pat=re.compile(r'self\\.([\\w]+)\\s*[:=]'); attrs=[m.group(1) for l in lines[3272:3414] if (m:=pat.match(l.strip()))]; print(chr(10).join(attrs))"
"""

from app.plugins.contracts.ui_module import UIModule


class InputCardModule(UIModule):
    """输入卡模块：输入卡 + 附件区 + 命令/文件提及/撤销删除三张浮动卡"""

    module_id = "input_card"

    def build(self, host) -> None:
        from PySide6.QtCore import Qt, QTimer
        from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
        from qfluentwidgets import setFont

        from app.utils.config import Settings as _Cfg
        from app.utils.design_tokens import scale_font_size
        from app.widgets.bottom_input_area import SendableTextEdit
        from app.widgets.cards import ContainerType
        from app.widgets.cards.floating.command_card import CommandCard
        from app.widgets.cards.floating.file_mention_card import FileMentionCard
        from app.widgets.cards.floating.undo_delete_card import UndoDeleteCard

        # ===== 底部输入区域（输入卡 + 工具栏紧贴拼接）=====
        # 视觉目标：输入框 + toolbar 等宽，无间距，无外 padding，紧贴 chat 区。
        #          上半圆角（输入卡） + 下半圆角（toolbar），中间一条边框线作分隔。
        # 抖动修复：spacing 永久固定 0，不再随 collapsed 切换；toolbar y 位置
        #          只取决于 _input_card 高度的单调变化，无"先下后上"中间帧。
        host._bottom_input_container = QWidget(host)
        host._bottom_input_container.setStyleSheet("QWidget#bottomContainer { background: transparent; }")
        host._bottom_input_container.setObjectName("bottomContainer")
        bottom_layout = QVBoxLayout(host._bottom_input_container)
        host._bottom_input_layout = bottom_layout
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.setSpacing(0)

        # ===== 输入卡片（上方圆角 + 渐变 + 边框，border-bottom: none）=====
        host._input_card = QWidget(host._bottom_input_container)
        host._input_card.setObjectName("_input_card")
        host._input_card.setAcceptDrops(True)
        host._input_card.installEventFilter(host)
        card_layout = QVBoxLayout(host._input_card)
        card_layout.setContentsMargins(2, 2, 2, 2)
        card_layout.setSpacing(0)

        # 输入卡环境光晕容器（包裹 _input_card，承载宽柔的外层环境光）
        # 实现双层 halo：输入卡自身 = 紧致主光（primary），wrapper = 弥散环境光（ambient）
        host._input_card_wrapper = QWidget(host._bottom_input_container)
        host._input_card_wrapper.setObjectName("_input_card_wrapper")
        host._input_card_wrapper.setAttribute(Qt.WA_TranslucentBackground, True)
        wrapper_layout = QVBoxLayout(host._input_card_wrapper)
        wrapper_layout.setContentsMargins(0, 0, 0, 0)
        wrapper_layout.setSpacing(0)
        # 把 _input_card 移入 wrapper
        host._input_card.setParent(host._input_card_wrapper)
        wrapper_layout.addWidget(host._input_card)

        # 附件预览行（拖拽/粘贴文件时显示 AttachmentChip）
        host._attach_container = QWidget(host._input_card)
        host._attach_container.setVisible(False)
        host._attach_container.setAcceptDrops(True)
        host._attach_container.installEventFilter(host)
        host._attach_layout = QHBoxLayout(host._attach_container)
        host._attach_layout.setContentsMargins(6, 6, 6, 0)
        host._attach_layout.setSpacing(3)
        host._attach_layout.addStretch()
        host._attachments: list[str] = []
        host._history_working_attachments: list[str] = []  # 进入历史模式时保存的附件（退出时恢复）
        card_layout.addWidget(host._attach_container)

        # 输入框（融入卡片，无边框）
        host.input_area = SendableTextEdit(host._input_card)
        host.input_area._agent_combo.hide()
        host.input_area._initializing = False
        # host.input_area.setFixedHeight(52)
        setFont(host.input_area, scale_font_size(15))
        host.input_area.sendMessageRequested.connect(host._on_send_clicked)
        host.input_area.stopMessageRequested.connect(host._on_stop_clicked)
        host.input_area.clearRequested.connect(host._on_clear_shortcut)
        host.input_area.agentChanged.connect(host._on_agent_changed)
        # 注意：textChanged 已在 SendableTextEdit 内部连接 _on_text_changed
        # 并触发 _adjust_height_to_content；这里不重复连接，避免一次输入
        # 触发两次布局重算导致抖动。系统卡片开/关路径会显式调用
        # _on_input_area_height_changed。
        host.input_area.slashTriggered.connect(host._on_slash_triggered)
        host.input_area.slashDismissed.connect(host._on_slash_dismissed)
        host.input_area.slashShowHint.connect(host._on_slash_show_hint)
        host.input_area.atTriggered.connect(host._on_at_triggered)
        host.input_area.atDismissed.connect(host._on_at_dismissed)
        host.input_area.files_dropped.connect(host._on_files_dropped)
        host.input_area.enteringHistoryMode.connect(host._on_entering_history_mode)
        host.input_area.historyAttachmentsRestored.connect(host._on_history_attachments_restored)
        host.input_area.historyModeExited.connect(host._on_history_mode_exited)
        # ★ 用户输入时通知桌宠好奇看向输入框
        host.input_area.textChanged.connect(host._on_pet_typing)
        card_layout.addWidget(host.input_area)

        # 加载输入历史
        host._load_input_history()

        # 命令卡片（必须是输入框创建后）
        host._command_card = CommandCard(host._bottom_input_container)
        host._command_card.setVisible(False)
        host.input_area.set_command_card(host._command_card)
        mgr = host._card_manager
        # 命令卡片压制 tool、sub_agent 和 sub_agent_compact
        mgr.register_card(
            host._window_id,
            ContainerType.BOTTOM,
            "command",
            host._command_card,
            suppress_others=["tool", "sub_agent", "sub_agent_compact"],
        )
        host._bottom_card_container.add_card("command", host._command_card)

        # 文件提及卡片（输入 @ 时显示文件列表）
        host._file_mention_card = FileMentionCard(host._bottom_input_container)
        host._file_mention_card.setVisible(False)
        host.input_area.set_file_mention_card(host._file_mention_card)
        host._file_mention_card.fileSelected.connect(host._on_file_mention_selected)
        mgr.register_card(
            host._window_id,
            ContainerType.BOTTOM,
            "file_mention",
            host._file_mention_card,
        )
        host._bottom_card_container.add_card("file_mention", host._file_mention_card)

        # 预缓存文件列表：延迟到事件循环空闲后执行，不阻塞 UI 初始化
        QTimer.singleShot(200, host._ensure_file_mention_cache)

        # 撤销删除卡片
        host._undo_delete_card = UndoDeleteCard(host._bottom_input_container)
        host._undo_delete_card.setVisible(False)
        host._undo_delete_card.restoreRequested.connect(host._restore_deleted_message)
        host._undo_delete_card.dismissed.connect(host._on_undo_delete_dismissed)
        mgr.register_card(host._window_id, ContainerType.BOTTOM, "undo_delete", host._undo_delete_card)
        host._bottom_card_container.add_card("undo_delete", host._undo_delete_card)

        # 初始化撤销删除缓存（只缓存一步）
        host._undo_delete_cache = {}

        # 🛡️ Bug 修复：截断哨兵 — 记录最近一次 session 截断的关键信息，
        # 用于在异步 finalize_stop / messages_updated 回调到达时识别"是否发生了截断"
        # 结构：{"session_id": str, "messages_len": int, "set_at": float} 或 None
        # 时机：撤销/删除消息触发的 _persist_session_after_mutation 末尾设置；
        #       _on_finalize_complete 在覆盖 session.messages 前检查；
        #       若 worker 返回的消息序列比截断后的当前序列长，且不是其前缀，则丢弃覆盖。
        host._truncation_sentinel = None

        # 🛡️ 截断后发送标志：用户撤销消息后又快速发送新消息时置 True，
        # 用于 _on_finalize_complete / _on_messages_updated 识别并丢弃旧 worker 的过期回调。
        host._pending_send_after_truncation = False
        host._pending_send_user_text = None  # 截断后发送的用户消息文本（用于指纹比对）

        # （内置命令已在上方注册）更新命令 --model= 参数描述
        host._update_subagents_param_description()
        host._update_title_gen_param_description()

        # 监听配置变更，配置同步时自动刷新命令卡参数描述和 UI
        _cfg = _Cfg.get_instance()
        _cfg.llm_subagent_default_model.valueChanged.connect(host._on_subagent_model_config_changed)
        _cfg.llm_title_gen_default_model.valueChanged.connect(host._on_title_gen_model_config_changed)
