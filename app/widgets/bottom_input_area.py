# 大模型输入框
import math
import os
import random
import tempfile
import uuid
from pathlib import Path
from typing import Optional

from PyQt5.QtCore import QMimeData, QRectF, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import (
    QColor,
    QFont,
    QImage,
    QInputMethodEvent,
    QKeyEvent,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPen,
    QSyntaxHighlighter,
    QTextCharFormat,
    QTextCursor,
)
from PyQt5.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QShortcut,
    QWidget,
)
from qfluentwidgets import ComboBox, FluentIcon, IconWidget, TextEdit, TransparentToolButton

from app.widgets.stop_button import SendStopButton

from app.utils.design_tokens import Colors, font_size_css
from app.utils.utils import get_font_family_css
from app.widgets.simple_hover_tooltip import install_hover_tooltip

# ======== 输入框 placeholder 定时轮播 tips ========
PLACEHOLDER_TIPS = [
    # ════ 基本输入 ════
    "拖拽文件到输入框即可快速分析",
    "Shift+Enter 换行，Enter 发送",
    "输入框为空时按 ↑/↓ 切换历史输入",
    "输入 @ 快速引用项目文件",
    "输入 / 查看内建指令、技能与智能体",
    "Ctrl+Z 撤销 / Ctrl+Shift+Z 重做",
    # ════ 快捷键 ════
    "Ctrl+N 新建对话，Ctrl+L 清空会话",
    "Ctrl+Shift+G 重排分组窗口",
    "Shift+esc 拆散所有分组",
    "Shift+点击窗口头添加分组",
    # ════ 项目 ════
    "点击顶部项目名切换/新建/归档项目",
    "项目笔记自动关联，切换项目即切换笔记",
    "文档中添加文件夹作为工具工作目录",
    "/project-note 快速新建/优化项目笔记",
    "/worktree 管理 Git 工作树并行开发",
    # ════ 模型与参数 ════
    "点击顶部模型名快速切换模型",
    "温度/最大Token影响回复风格",
    "/quota-setting 查看/设置模型额度与用量",
    # ════ 智能体系统 ════
    "/plan 分析需求制定编码计划",
    "/build 执行编码实现与测试验证",
    "/explore 探索分析代码库结构",
    "/code-reviewer 审查代码修改并提改进建议",
    "/review 代码审查（简版）",
    "/auto_loop 自动化规划→执行→归档循环",
    "/leader 统筹子智能体团队协作",
    "/compaction 手动触发上下文压缩",
    "/task-executor 执行批量预设任务",
    "子智能体协作处理复杂任务，支持并行 DAG 工作流",
    # ════ 技能系统 ════
    "/brainstorming 集思广益探索需求方案",
    "/tdd 测试驱动开发：红→绿→重构",
    "/caveman 极简模式节省 Token 提升效率",
    "/diagnose 系统诊断 Bug 与性能回归",
    "/drifox-dev DriFox 专属开发技能",
    "/skill-creator 创建自定义技能",
    "/git-commit 生成规范提交信息",
    "/minimax-image-understanding 理解分析图片内容",
    "/ui-plugin-creator 创建自定义 UI 插件扩展界面",
    "/find-skills 搜索发现可用技能",
    "/github-ops GitHub 操作自动化",
    "/grill-me 基于代码库深度提问",
    "/grill-with-docs 结合文档深度分析",
    "/improve-codebase-architecture 分析优化架构",
    "/session-summary 生成会话摘要",
    "/writing-plans 编写实施计划",
    "/zoom-out 宏观视角审视项目",
    "/triage 问题分类与优先级评估",
    "/to-issues 转 GitHub Issues",
    "/to-prd 生成产品需求文档",
    "/subagent-driven-development 子智能体驱动开发",
    "/agent-canvas-designer 设计智能体协作蓝图",
    "/using-superpowers 使用全部高级工具能力",
    "/dispatching-parallel-agents 并行分派子智能体",
    "/executing-plans 执行编码实施计划",
    # ════ 代码与工具 ════
    "代码块右上角可复制或保存文件",
    "工具结果点击「查看差异」对比修改",
    "工具悬浮框显示执行详情与日志",
    "撤销按钮可单独撤销编辑操作",
    "文件操作历史追溯，误改一键恢复",
    # ════ 窗口与布局 ════
    "右上角「新建窗口」并发处理多任务",
    "「分支」按钮复制会话到新窗口",
    "右下角展开历史会话卡片继续对话",
    "记忆管理让 AI 记住你的偏好",
    "像素宠物陪伴开发，点击互动",
    # ════ 消息卡片页脚 ════
    "页脚：差异 | 审查 | Token | 耗时 | 模型",
    "点击消息页脚差异对比统计查看文件修改对比",
    "点击 🔍 用 code-reviewer 审查修改",
    "点击 Token 查看上下文详情与预算",
    "点击模型名快速切换对应服务商和模型",
    # ════ 高级功能 ════
    "点击上下文指示器查看 Token 趋势与消息量",
    "子智能体 DAG 工作流编排复杂多步骤任务",
    "子智能体对话框实时查看任务日志与执行摘要",
    "长对话自动启用上下文压缩省 Token",
    "历史会话自动保存，关闭不丢失",
    "来源项目追踪：切换项目不串会话",
    "/debug 调试模式查看内部状态",
    "/release 打包发布当前版本",
    "/verify 验证项目配置完整性",
    "/todos 管理待办事项清单",
    "/remember 让 AI 记住重要信息",
    "/theme 一键生成主题配色",
    # ════ 动态主题 ════
    "设置中实时切换主题配色，全局即时生效",
    "UI 插件自动适配主题色，无需手动配置",
    # ════ UI 插件系统 ════
    "UI 插件提供可热加载的组件，支持自定义按钮、面板、卡片等",
    "已安装插件：插件市场 / 文件树 / 系统清理 / Token 统计",
    # ════ 插件与市场 ════
    "/plugin-market 浏览安装社区插件，即装即用",
    "/plugin-manager 管理已安装插件，启用/禁用/卸载",
    "/system-cleaner 清理系统缓存和临时文件",
    "/context-usage-stats Token 趋势/消息量图表",
    "/file-tree 浏览/搜索/实时监听文件变更"
    # ════ Hook 预设 ════
    "系统 Hook 链：会话注入→安全检查→自动压缩→智能增强",
    "安全守卫在写/改文件前自动审查指令安全性",
    "长期记忆 Hook 让 AI 跨会话记住你的偏好",
    "项目笔记自动注入对话上下文，无需手动引用",
    "命令提示词自动注入，/ 命令即用即知",
    # ════ MCP 系统 ════
    "系统设置中配置 MCP Server 扩展 AI 能力",
    "MCP 工具连接后自动可用，无需额外配置",
    "npx server-filesystem 让 AI 读写文件系统",
    "npx @playwright/mcp 让 AI 操作浏览器",
    "npx server-github 让 AI 访问 GitHub API",
    "npx server-sqlite 让 AI 查询数据库",
    "/lsp-install 安装语言服务器协议支持",
    # ════ 内建指令 ════
    "/new 新建会话 /branch 创建分支",
    "/init 笔记 /theme 主题色 /compact 压缩",
    "/subagents 启动子智能体任务",
    "/subagent_dag 编排多步骤 DAG 工作流",
    "/team 团队协作模式启动",
    "/title-gen 自动生成会话标题",
    "/receive-review 接收外部审查意见",
    "/finish-branch 完成分支并合并",
    "/webresearch 联网搜索研究",
    "/wordcloud 生成词云分析",
    "/ 命令支持 #skill #agent #ui #prompt 类别过滤",
    "/ 搜索支持 | 和 & 组合关键字",
    # ════ 文件提及 ════
    "@ 搜索支持 | 和 & 组合筛选",
    "@ 模糊匹配：rqrmnts 也能找到 requirements.txt",
]

# 轮播间隔（毫秒）
_PLACEHOLDER_ROTATE_INTERVAL_MS = 15000


class SendableTextEdit(TextEdit):
    sendMessageRequested = pyqtSignal()
    stopMessageRequested = pyqtSignal()
    clearRequested = pyqtSignal()
    newSessionRequested = pyqtSignal()
    historyUpRequested = pyqtSignal()
    historyDownRequested = pyqtSignal()
    agentChanged = pyqtSignal(str)
    slashTriggered = pyqtSignal(str)  # 检测到 / 触发，携带查询文本
    slashDismissed = pyqtSignal()  # / 触发结束
    slashShowHint = pyqtSignal(str, str)  # cmd_name, selected_display_type
    atTriggered = pyqtSignal(str)  # 检测到 @ 触发，携带查询文本
    atDismissed = pyqtSignal()  # @ 触发结束
    files_dropped = pyqtSignal(list)  # list[str] 拖入/粘贴的文件路径
    enteringHistoryMode = pyqtSignal()  # 即将进入历史浏览模式（main_widget 需保存当前附件）
    historyAttachmentsRestored = pyqtSignal(list)  # 恢复附件路径列表
    historyModeExited = pyqtSignal()  # 退出历史浏览模式（main_widget 从备份恢复附件）

    def __init__(self, parent=None):
        super().__init__(parent)
        self._initializing = True
        self._glow_effect = None

        self._setup_glow_effect()
        self._apply_input_style()
        # placeholder 仅用 tips 轮播，不用通用提示语
        self.setPlaceholderText(random.choice(PLACEHOLDER_TIPS))
        self.setAcceptRichText(False)
        self.setLineWrapMode(TextEdit.WidgetWidth)
        self.setAcceptDrops(True)
        self.setMinimumHeight(42)
        self.setMaximumHeight(300)
        self.setFixedHeight(42)

        self._agent_combo = ComboBox(self)
        self._agent_combo.setFixedSize(75, 28)
        self._agent_combo.setStyleSheet(self._build_combo_style())
        self._agent_combo.currentTextChanged.connect(self._on_agent_changed)

        self.send_btn = SendStopButton(self)
        self.send_btn.setToolTip("发送（Enter）")
        install_hover_tooltip(self.send_btn)
        self.send_btn.clicked.connect(self._on_send_click)
        self.send_btn.set_send_enabled(False)

        self.textChanged.connect(self._on_text_changed)
        self.textChanged.connect(self._on_slash_trigger_check)
        self.textChanged.connect(self._on_at_trigger_check)

        # 关闭 qfluentwidgets TextEdit 焦点时的底部高亮
        if hasattr(self, "layer"):
            self.layer.hide()

        self._setup_keyboard_shortcuts()

        # [[filename]] 占位符高亮
        self._placeholder_highlighter = PlaceholderHighlighter(self.document())

        # 命令卡片引用（由 main_widget 注入）
        self._command_card_ref = None
        self._slash_trigger_pos = -1  # / 触发位置

        # 文件提及卡片引用（由 main_widget 注入）
        self._file_mention_card_ref = None
        self._at_trigger_pos = -1  # @ 触发位置
        self._ime_composing = False  # IME 输入法组合状态

        # 卡片选中项：供 execute() 按选中类型执行
        self._card_selected_name: Optional[str] = None
        self._card_selected_type: Optional[str] = None  # display_type: command/prompt/agent/skill

        # 节流相关：/ 命令触发
        self._slash_throttle_timer = QTimer(self)
        self._slash_throttle_timer.setSingleShot(True)
        self._slash_throttle_timer.timeout.connect(self._on_slash_throttle_timeout)
        self._pending_slash_query = None  # None 表示无待发射 query
        self._last_slash_trigger_time = 0  # 上次触发时间（毫秒）
        self._slash_trigger_count = 0  # 快速触发计数

        # 节流相关：@ 文件提及触发（与 / 共用逻辑，分开状态独立追踪）
        self._at_throttle_timer = QTimer(self)
        self._at_throttle_timer.setSingleShot(True)
        self._at_throttle_timer.timeout.connect(self._on_at_throttle_timeout)
        self._pending_at_query = None  # None 表示无待发射 query（空字符串也是合法 query）
        self._last_at_trigger_time = 0  # 上次 @ 触发时间（毫秒）
        self._at_trigger_count = 0  # @ 快速触发计数（保留用于兼容）

        # 输入历史浏览
        self._history_list: list = []  # 最近输入历史（最新在前）
        self._history_index: int = -1  # -1 = 不在浏览模式
        self._history_working_line: str = ""  # 进入历史模式时保存的当前输入（退出时恢复）
        self._setting_history_text: bool = False  # 正在 _set_history_text 中，阻止 _on_text_changed 误触发 reset
        self._suppress_slash_trigger: bool = False  # 切换历史时临时阻止 / 触发

        # placeholder 定时随机切换 tips
        self._placeholder_tip_timer = QTimer(self)
        self._placeholder_tip_timer.setInterval(_PLACEHOLDER_ROTATE_INTERVAL_MS)
        self._placeholder_tip_timer.timeout.connect(self._rotate_placeholder_tip)
        self._placeholder_tip_timer.start()

        # 使用 QTimer.singleShot(0, ...) 在事件循环启动后重置初始化标志
        QTimer.singleShot(0, self._finish_initialization)

    def _apply_send_btn_style(self):
        """从 Colors 应用发送按钮样式"""
        Colors.refresh()
        radius = Colors.SEND_BTN_RADIUS
        self.send_btn.setStyleSheet(f"""
            TransparentToolButton {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 {Colors.SEND_BTN_START}, stop:1 {Colors.SEND_BTN_END});
                border: none;
                border-radius: {radius}px;
                color: white;
            }}
            TransparentToolButton:hover {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 {Colors.SEND_BTN_HOVER_START}, stop:1 {Colors.SEND_BTN_HOVER_END});
            }}
            TransparentToolButton:disabled {{
                background: {Colors.TOOLBAR_BG};
                color: {Colors.TEXT_SECONDARY};
            }}
        """)

    def _finish_initialization(self):
        """初始化完成后重置标志，允许高度调整"""

    def _rotate_placeholder_tip(self):
        """定时随机切换 placeholder tips (QTimer 15s 触发 random.choice)"""
        if not self.toPlainText():
            self.setPlaceholderText(random.choice(PLACEHOLDER_TIPS))

    def set_command_card(self, card):
        """注入命令卡片引用（由 main_widget 创建并注册）"""
        self._command_card_ref = card
        card.commandSelected.connect(self._on_command_selected)
        card.parameterSelected.connect(self._on_parameter_selected)
        card.parameterDeselected.connect(self._on_parameter_deselected)
        card.parameterValueSelected.connect(self._on_param_value_selected)
        card.dismissed.connect(self._on_card_dismissed)

    def _get_card(self):
        """获取命令卡片引用"""
        return self._command_card_ref

    def set_file_mention_card(self, card):
        """注入文件提及卡片引用（由 main_widget 创建并注册）"""
        self._file_mention_card_ref = card
        card.dismissed.connect(self._on_card_dismissed)

    def _get_file_mention_card(self):
        """获取文件提及卡片引用"""
        return self._file_mention_card_ref

    def _on_slash_trigger_check(self):
        """检测 / 触发——仅在开头（位置0）的 / 触发命令卡片，支持节流

        扩展逻辑：
        - `/cmd`（无空格）→ 列表模式（原行为）
        - `/cmd `（有空格，且 cmd 是已知命令）→ detail 模式（显示参数提示）
        - `/xxx `（有空格，但 xxx 不是已知命令）→ 关闭卡片
        """
        # IME 组合输入中跳过检测，避免打断中文输入法
        if self._ime_composing:
            return

        # 历史浏览模式下，如果当前历史项以 / 开头，阻止命令卡片触发
        if self._suppress_slash_trigger:
            self._suppress_slash_trigger = False
            card = self._get_card()
            if card and card.is_card_visible:
                card.dismiss()
                self.slashDismissed.emit()
            return

        # 无论什么分支，先同步 detail 模式参数显隐（删除/修改参数时恢复列表项）
        self._sync_detail_params()

        card = self._get_card()
        try:
            cursor = self.textCursor()
            text = self.toPlainText()
            cursor_pos = cursor.position()

            if cursor_pos < 0 or cursor_pos > len(text):
                return

            # 仅当 / 在文本开头（位置0）时触发
            text_before_cursor = text[:cursor_pos]

            if not text.startswith("/"):
                # 没有在开头
                self._cancel_slash_throttle()
                if card and card.is_card_visible:
                    card.dismiss()
                    self.slashDismissed.emit()
                self._slash_trigger_pos = -1
                return

            query = text[1:cursor_pos] if cursor_pos > 1 else ""

            # 换行符 → 关闭
            if "\n" in query:
                self._cancel_slash_throttle()
                if card and card.is_card_visible:
                    card.dismiss()
                    self.slashDismissed.emit()
                self._slash_trigger_pos = -1
                return

            # 空格 → 检查是否是已知命令或技能后跟参数
            if " " in query:
                self._cancel_slash_throttle()
                cmd_name = query.split(" ", 1)[0]
                # 检测类别过滤器（#agent, #skill, #prompt, #cmd, #ui 等）
                # 如果是类别过滤器 + 空格 + 搜索文本 → 保持列表模式，不尝试进入 detail 模式
                _type_tag_map = {"cmd": "command", "skill": "skill", "agent": "agent", "prompt": "prompt", "ui": "ui"}
                if cmd_name.startswith("#") and cmd_name[1:] in _type_tag_map:
                    self._slash_trigger_pos = 0
                    self._apply_slash_throttle(query)
                    return

                # 🚀 性能优化：已处于同一命令的 detail 模式时跳过
                # 避免每次敲键都触发 get_skill_by_name（扫描文件系统）和 signal 发射
                card = self._get_card()
                if card and card.is_detail_mode and card.detail_cmd_name == cmd_name:
                    # 同步参数显隐：追踪输入框中的参数变化
                    self._sync_detail_params()
                    return

                from app.core.command_manager import CommandManager
                from app.utils.utils import get_skill_by_name

                # 解析后缀：如 "tdd-skill" → base="tdd", type="skill"
                raw_cmd, suffix_type = CommandManager.parse_suffixed_name(cmd_name)

                # ── 确定实际匹配的「真名」：按实际匹配来源选择 ──
                # 不能用 raw_cmd or cmd_name 一刀切——当技能名恰好以 -skill/-prompt/-cmd/-agent
                # 结尾时，raw_cmd 会把真名"my-skill"截断为"my"，导致后续 show_command_detail
                # 用错误的名字查不到技能，参数卡片不弹出。
                cmd_match = CommandManager.get_instance().is_known_command_name(cmd_name) or get_skill_by_name(cmd_name)
                raw_match = bool(raw_cmd) and (
                    CommandManager.get_instance().is_known_command_name(raw_cmd) or get_skill_by_name(raw_cmd)
                )

                if cmd_match or raw_match:
                    # 已知命令/技能 + 参数 → 切换到 detail 模式
                    self._slash_trigger_pos = 0
                    # check_name：以实际匹配来源为准——优先用精确的 cmd_name（不截断），
                    # 只有 raw_cmd 才匹配时才用 raw_cmd（处理 display_name 后缀如"tdd-skill"→"tdd"）
                    check_name = cmd_name if cmd_match else raw_cmd

                    # 传入当前选中项的 display_type（供 show_command_detail 显示对应类型的 hint）
                    # 优先使用 _on_command_selected 中由卡片传来的精确类型（_card_selected_type），
                    # 避免 card._current_selected_type 在未导航时仍是首个命令条目的类型而误判。
                    # 同时用 _card_selected_name 做名称匹配校验，防止上次选择残留的类型泄漏。
                    if self._card_selected_name == check_name and self._card_selected_type:
                        selected_type = self._card_selected_type
                    else:
                        selected_type = card._current_selected_type if card else ""
                    # 从后缀推断类型：当卡片未提供选中类型时（如用户手动输入 /tdd-skill），
                    # 确保 detail 模式能正确显示技能参数而非同名命令参数
                    if suffix_type and not selected_type:
                        selected_type = suffix_type
                    self.slashShowHint.emit(check_name, selected_type)
                else:
                    # 未知命令/技能 + 参数 → 关闭
                    if card and card.is_card_visible:
                        card.dismiss()
                        self.slashDismissed.emit()
                    self._slash_trigger_pos = -1
                return

            # 无空格 → 列表模式（使用节流）
            self._slash_trigger_pos = 0
            self._apply_slash_throttle(query)
        except Exception:
            pass

    def _apply_slash_throttle(self, query: str):
        """/ 触发节流：统一防抖合并快速敲键，减少过滤+渲染次数

        与 _apply_at_throttle 实现一致：始终走 100ms 防抖，
        去除旧版「正常速度立即发射」路径，避免每次敲键触发完整命令卡片渲染。
        """
        # 连续输入相同 query → 跳过
        if self._pending_slash_query is not None and query == self._pending_slash_query:
            return
        self._pending_slash_query = query
        self._slash_throttle_timer.stop()
        self._slash_throttle_timer.start(100)

    def _on_slash_throttle_timeout(self):
        """节流定时器超时：发射最终的 query"""
        if self._slash_trigger_pos >= 0:
            self.slashTriggered.emit(self._pending_slash_query)

    def _cancel_slash_throttle(self):
        """取消节流定时器"""
        self._slash_throttle_timer.stop()
        self._pending_slash_query = None
        self._slash_trigger_count = 0

    # ==================== @ 文件提及节流 ====================

    def _apply_at_throttle(self, query: str):
        """@ 触发节流：统一防抖合并快速敲键，减少过滤+渲染次数

        关键优化：无论键入多快都不立即发射，始终走防抖。
        去除了旧的「正常速度立即发射」路径——即使 ~200ms 的普通打字速度，
        每次键盘事件 emit → show_card → 2000 项评分 → widget 重建的链路
        依然昂贵。统一 100ms 防抖让快速打字期间只执行最后一次过滤/渲染。
        """
        # 连续输入相同 query（如按方向键/退格到原位置）→ 跳过
        # 注意：初始 _pending_at_query = None，此时空字符串 query = "" 是合法首次触发
        if self._pending_at_query is not None and query == self._pending_at_query:
            return

        self._pending_at_query = query
        self._at_throttle_timer.stop()
        self._at_throttle_timer.start(100)

    def _on_at_throttle_timeout(self):
        """@ 节流定时器超时：发射最终的 query"""
        if self._at_trigger_pos >= 0:
            self.atTriggered.emit(self._pending_at_query)

    def _cancel_at_throttle(self):
        """取消 @ 节流定时器"""
        self._at_throttle_timer.stop()
        self._pending_at_query = None

    # ==================== @ 文件提及触发检测 ====================

    def _on_at_trigger_check(self):
        """检测 @ 触发——在文本中任意位置的 @ 触发文件提及卡片

        规则：
        - @ 必须处于单词边界（前面是空格/换行/制表符/文本开头）
        - query = @ 到光标之间的文本（不含换行）
        - 若 query 含换行 → 关闭卡片
        - 若 @ 前后无合法 query 区间 → 关闭卡片
        - IME 组合输入中跳过检测，避免打断中文输入法
        """
        if self._ime_composing:
            return

        try:
            text = self.toPlainText()
            cursor = self.textCursor()
            cursor_pos = cursor.position()

            if cursor_pos < 0 or cursor_pos > len(text):
                return

            text_before_cursor = text[:cursor_pos]

            # 从光标向前找最后一个合法 @
            at_pos = -1
            for i in range(cursor_pos - 1, -1, -1):
                ch = text_before_cursor[i]
                if ch == "@":
                    # 检查是否为单词边界
                    if i == 0 or text_before_cursor[i - 1] in (" ", "\n", "\t", "\r"):
                        at_pos = i
                        break
                    else:
                        # 非单词边界（如 email@domain），不触发
                        break
                elif ch in ("\n", "\r"):
                    # 遇到换行则停止向前搜索
                    break

            file_card = self._get_file_mention_card()

            if at_pos < 0:
                # 没有找到合法 @ → 关闭卡片
                self._cancel_at_throttle()
                self._at_trigger_pos = -1
                if file_card and file_card.is_card_visible:
                    file_card.dismiss()
                    self.atDismissed.emit()
                return

            query = text_before_cursor[at_pos + 1 :]

            # 换行 → 关闭
            if "\n" in query:
                self._cancel_at_throttle()
                self._at_trigger_pos = -1
                if file_card and file_card.is_card_visible:
                    file_card.dismiss()
                    self.atDismissed.emit()
                return

            self._at_trigger_pos = at_pos
            # 使用节流发射（合并快速敲键，只发最后一次）
            self._apply_at_throttle(query)

        except Exception:
            pass

    def insert_file_mention(self, file_path: str):
        """将 @ 提及文本替换为 [[basename]] 占位符（选中文件后由 main_widget 调用）

        用户选中文件后，移除输入框中的 @query 文本并插入 [[basename]] 占位符。
        main_widget 随后会创建 AttachmentChip。
        发送时 _build_user_text_with_attachments 会将 [[basename]] 替换为完整路径。
        """
        cursor = self.textCursor()
        cursor_pos = cursor.position()
        trigger_pos = self._at_trigger_pos

        if trigger_pos >= 0:
            cursor.setPosition(trigger_pos)
            cursor.setPosition(cursor_pos, QTextCursor.KeepAnchor)
            basename = os.path.basename(file_path)
            cursor.insertText(f"[[{basename}]] ")

        self._cancel_at_throttle()
        self._at_trigger_pos = -1
        self.setFocus(Qt.OtherFocusReason)

    # ==================== 命令文本插入 ====================

    def insert_command_text(self, item_name: str):
        """将选中的命令/技能文本插入输入框（由 main_widget 调用）"""
        cursor = self.textCursor()
        text = self.toPlainText()
        cursor_pos = cursor.position()

        trigger_pos = self._slash_trigger_pos

        if trigger_pos >= 0:
            cursor.setPosition(trigger_pos)
            cursor.setPosition(cursor_pos, QTextCursor.KeepAnchor)

            # 统一使用 / 前缀（命令、技能、智能体都用 /）
            insert_text = f"/{item_name} "
            cursor.insertText(insert_text)

            cursor.setPosition(trigger_pos + len(insert_text))
            self.setTextCursor(cursor)

        self._slash_trigger_pos = -1
        self.setFocus(Qt.OtherFocusReason)

    def _on_command_selected(self, item_name: str, item_type: str = ""):
        """命令/技能被选中（由 CommandCard.commandSelected 触发）"""
        # 记录卡片选中的名称和类型，供 execute() 按选中类型执行
        self._card_selected_name = item_name if item_type else None
        self._card_selected_type = item_type or None

        card = self._get_card()
        self.insert_command_text(item_name)
        if card:
            # insert_command_text 可能触发 textChanged → detail 模式，
            # 此时卡片应保持可见，不 dismiss
            if not card.is_detail_mode:
                card.dismiss()
        if not (card and card.is_detail_mode):
            self.slashDismissed.emit()

    def pop_card_selected_type(self, cmd_name: str) -> Optional[str]:
        """弹出卡片选中项的类型（供 main_widget 调用 execute() 前使用）

        调用本方法会同时清除存储，避免二次消费。

        Args:
            cmd_name: 命令名（不含 /）

        Returns:
            显示类型字符串 "command"/"prompt"/"agent"，或 None
        """
        if self._card_selected_name == cmd_name and self._card_selected_type:
            result = self._card_selected_type
            self._card_selected_name = None
            self._card_selected_type = None
            return result
        return None

    def _on_card_dismissed(self):
        """卡片被关闭时的清理"""
        self._slash_trigger_pos = -1

    # ==================== Detail 模式参数交互 ====================

    def _on_parameter_selected(self, param_name: str, param_type: str):
        """参数项被选中（来自 CommandCard.parameterSelected）"""
        self.insert_parameter_text(param_name, param_type)
        # 插入文本后显式同步参数显隐，确保互斥规则立即生效
        self._sync_detail_params()

    def _on_parameter_deselected(self, param_name: str, param_type: str):
        """已激活参数被再次点击（来自 CommandCard.parameterDeselected）

        从输入框中移除该参数并同步卡片激活态。
        """
        self.remove_parameter_text(param_name, param_type)

    def _on_param_value_selected(self, value: str):
        """值选择完成（来自 CommandCard.parameterValueSelected）

        自动补全 --model= 的值。
        如果值包含空格（如 "Azure OpenAI:gpt-4o"），自动加双引号。
        防御：如果文本在当前光标前已包含该值，跳过插入避免重复。
        替换：用户在 --model= 后已输入部分过滤词（如 "gpt"），选中项应替换该部分，
        而不是追加在末尾。
        """
        text = self.toPlainText()
        cursor_pos = self.textCursor().position()
        before_cursor = text[:cursor_pos]

        # 确定要插入的值（含空格时自动加双引号）
        inserted_value = f'"{value}"' if " " in value else value

        # 替换模式：查找光标前最近的 --xxx= 模式，把 = 后的部分输入替换为选中值
        # 这样用户在 --model=gpt 后选中 "Azure OpenAI:gpt-4o" 时，
        # 结果是 --model="Azure OpenAI:gpt-4o" 而不是 --model=gpt"Azure OpenAI:gpt-4o"
        import re

        eq_matches = list(re.finditer(r"--[\w-]+=", before_cursor))
        if eq_matches:
            last_eq = eq_matches[-1]
            eq_end = last_eq.end()
            partial_input = before_cursor[eq_end:]  # = 到光标之间的部分输入

            if partial_input:
                cursor = self.textCursor()
                # 选中 partial_input 并替换为完整值
                cursor.setPosition(eq_end)
                cursor.setPosition(cursor_pos, QTextCursor.KeepAnchor)
                cursor.insertText(inserted_value)
                cursor.insertText(" ")
                self.setTextCursor(cursor)
                self.setFocus(Qt.OtherFocusReason)
                # 值插入后同步参数显隐（如 --model= 选择完成后）
                self._sync_detail_params()
                return

        # 检查光标前是否已有 --key=value（用户手动输入后按 Tab 确认）
        # 同时检查原始值和带引号版本
        if value in before_cursor or inserted_value in before_cursor:
            # 已存在值，只确保有空格
            if not text.endswith(" ") and not text.endswith("\n"):
                self.textCursor().insertText(" ")
            return

        cursor = self.textCursor()
        cursor.insertText(inserted_value)
        cursor.insertText(" ")
        self.setTextCursor(cursor)
        self.setFocus(Qt.OtherFocusReason)
        # 值插入后同步参数显隐（如 --model= 选择完成后）
        self._sync_detail_params()

    def _find_partial_param(self, text: str, param_name: str, cursor_pos: int = None):
        """在输入文本中查找参数名的部分匹配（优先光标附近）

        用于智能补全：文本中已有 --subag，点击 --subagent 参数时
        原地替换为 --subagent，避免变成 --subag --subagent

        Args:
            text: 输入框全文
            param_name: 参数名，如 "--subagent", "--model="
            cursor_pos: 光标位置（可选），存在时优先匹配光标附近的参数

        Returns:
            (start, end) 部分匹配范围，或 None
        """
        import re

        clean_name = param_name.rstrip("=")

        def _is_partial_match(token: str, m_end: int) -> bool:
            """判断 token 是否为 param_name 的部分匹配

            额外条件：
            - token 不能与 param_name 完全相等（已完整无需替换）
            - token 后不能有 =（属于已完成 value 参数，如 --model=GPT-4o）
            """
            if m_end < len(text) and text[m_end] == "=":
                return False
            return clean_name.startswith(token) and token != param_name

        # 如果有光标位置，优先找光标附近一定范围内的匹配
        if cursor_pos is not None:
            nearby_match = None
            for m in re.finditer(r"--[\w-]+", text):
                token = m.group()
                if _is_partial_match(token, m.end()):
                    # 匹配在光标附近（前后 30 字符范围内）
                    if abs(m.start() - cursor_pos) <= 30:
                        if nearby_match is None or abs(m.start() - cursor_pos) < abs(nearby_match.start() - cursor_pos):
                            nearby_match = m
            if nearby_match:
                return (nearby_match.start(), nearby_match.end())

        # 无光标位置或附近无匹配 → 返回第一个匹配（向后兼容）
        for m in re.finditer(r"--[\w-]+", text):
            token = m.group()
            if _is_partial_match(token, m.end()):
                return (m.start(), m.end())
        return None

    def insert_parameter_text(self, param_name: str, param_type: str):
        """在光标处插入参数文本（detail 模式参数补全）

        智能补全：如果输入框中已有部分匹配（如 --subag），
        则原地替换为完整参数名（--subagent），而非追加。

        - flag: 插入 " --param-name "
        - value: 插入 " --param="（等待值选择）
        - positional: 不插入（提示用户自行输入）
        """
        if param_type == "positional":
            return

        cursor = self.textCursor()
        text = self.toPlainText()

        # 智能补全：部分匹配则原地替换（优先光标附近的匹配）
        cursor_pos = cursor.position()
        partial = self._find_partial_param(text, param_name, cursor_pos)
        if partial:
            cursor.setPosition(partial[0])
            cursor.setPosition(partial[1], QTextCursor.KeepAnchor)
            if param_type == "flag":
                cursor.insertText(f"{param_name} ")
            elif param_type == "value":
                cursor.insertText(f"{param_name}")
            self.setTextCursor(cursor)
            self.setFocus(Qt.OtherFocusReason)
            # 参数插入后立即同步卡片显隐（含互斥逻辑）
            self._sync_detail_params()
            return

        # 无部分匹配 → 在光标处追加
        pos = cursor.position()
        if pos < 0:
            pos = len(text)
        cursor.setPosition(pos)
        # 智能判断是否需要前导空格：光标前是空格 / -- / 文本开头 → 不加
        need_space = pos > 0 and text[pos - 1] not in (" ", "\t", "\n")
        if pos >= 2 and text[pos - 2 : pos] == "--":
            # 光标前是用户手动输入的 --，替换这 2 个字符为完整参数名，
            # 避免追加到 -- 后面变成 ----parameter
            cursor.setPosition(pos - 2)
            cursor.setPosition(pos, QTextCursor.KeepAnchor)
            if param_type == "flag":
                cursor.insertText(f"{param_name} ")
            elif param_type == "value":
                cursor.insertText(f"{param_name}")
            self.setTextCursor(cursor)
            self.setFocus(Qt.OtherFocusReason)
            self._sync_detail_params()
            return
        prefix = " " if need_space else ""
        if param_type == "flag":
            cursor.insertText(f"{prefix}{param_name} ")
        elif param_type == "value":
            cursor.insertText(f"{prefix}{param_name}")
        self.setTextCursor(cursor)
        self.setFocus(Qt.OtherFocusReason)
        # 参数插入后立即同步卡片显隐（含互斥逻辑）
        self._sync_detail_params()

    def remove_parameter_text(self, param_name: str, param_type: str):
        """从输入框文本中移除指定参数（点击已固化参数时调用）

        与 insert_parameter_text 对称：点击已激活参数时反向删除。
        - flag: 匹配 `--param-name` 整段并删除
        - value: 匹配 `--param-name=value` 或 `--param-name="value"` 整段并删除
          （值含空格时会被引号包裹，按需识别）
        - positional: 无操作

        删除范围包含该参数段前的一个空格（若有），保留参数位置；
        光标位置按其在删除段前/中/后智能调整。
        删除后调用 _sync_detail_params 触发卡片激活态同步。
        """
        import re

        if param_type == "positional":
            return

        text = self.toPlainText()
        if not text:
            return

        clean_name = param_name.rstrip("=")
        # value 类型：参数名 + = + 值（带引号整段 OR 无空格的非引号值）
        # flag 类型：仅参数名
        # 末尾用 (?=\s|$) 防止误吞 --with-contexts 这类更长前缀
        if param_type == "value":
            pattern = r"\s*" + re.escape(clean_name) + r"""=(?:"[^"]*"|[^\s"]*)(?=\s|$)"""
        else:
            pattern = r"\s*" + re.escape(clean_name) + r"(?=\s|$)"

        m = re.search(pattern, text)
        if not m:
            return

        start, end = m.start(), m.end()
        new_text = text[:start] + text[end:]

        # 保留参数后一个空格，避免删除参数后重新进入命令补全列表状态
        # 如 "/cmd --model=xxx" → "/cmd "（而非 "/cmd"）
        if new_text and not new_text.endswith(" "):
            new_text += " "

        # 光标位置智能调整：前/中/后 三段
        old_pos = self.textCursor().position()
        if old_pos <= start:
            new_pos = old_pos
        elif old_pos >= end:
            new_pos = old_pos - (end - start)
        else:
            new_pos = start

        self.setPlainText(new_text)
        # setPlainText 重置光标到位置 0，导致 textChanged→_on_slash_trigger_check
        # 误判为"无空格→列表模式"并启动节流定时器，100ms 后发射 slashTriggered("")
        # 进而 show_card 调用 _reset_detail_mode 破坏 detail 模式。
        # 需要立即取消这个过期的节流。
        self._cancel_slash_throttle()
        cursor = self.textCursor()
        cursor.setPosition(max(0, new_pos))
        self.setTextCursor(cursor)
        self.setFocus(Qt.OtherFocusReason)
        # 用正确的光标位置重新评估 / 状态
        self._on_slash_trigger_check()
        # 删除后同步卡片显隐（set_active(False) 即取消固化打勾）
        self._sync_detail_params()

    def _sync_detail_params(self):
        """同步 detail 模式的参数显隐：从输入文本提取已存在参数 → 更新卡片

        同时透传完整文本和光标位置，供卡片做：
        - 自动检测 --model 前缀并弹出模型列表
        - 模型列表的实时搜索过滤
        """
        from app.core.command_manager import CommandManager

        card = self._get_card()
        if not card or not card.is_detail_mode:
            return
        text = self.toPlainText()
        cursor_pos = self.textCursor().position()
        active = CommandManager.parse_active_params(text) if text else set()
        card.update_active_params(active, full_text=text, cursor_pos=cursor_pos)

    # ==================== 输入历史浏览 ====================

    def load_history(self, history_list: list):
        """从外部加载输入历史列表（支持 list[dict] 和 list[str]）"""
        processed = []
        for item in history_list:
            if isinstance(item, dict):
                processed.append(item)
            else:
                # 兼容旧数据：纯字符串转为 dict
                processed.append({"text": str(item), "attachments": []})
        self._history_list = processed
        self._history_index = -1

    def _enter_history_mode(self):
        """进入历史浏览模式：保存当前文本和附件，加载最新一条"""
        if not self._history_list:
            return
        # 保存当前输入为 working line，退出时恢复
        self._history_working_line = self.toPlainText()
        # 发出信号让 main_widget 保存当前附件到 _history_working_attachments
        self.enteringHistoryMode.emit()
        # 进入历史模式时，隐藏命令卡片
        card = self._get_card()
        if card and card.is_card_visible:
            card.dismiss()
            self.slashDismissed.emit()
        self._suppress_slash_trigger = False
        self._history_index = 0
        self._set_history_text()

    def _set_history_text(self):
        """根据当前 history_index 设置输入框文本和附件

        - index >= 0: 显示对应历史条目（含附件）
        - index == -1: 恢复 working line（退出历史模式，main_widget 从备份恢复附件）
        """
        self._setting_history_text = True
        try:
            if self._history_index < 0:
                # 退出历史模式，恢复进入时保存的文本和附件
                self._suppress_slash_trigger = self._history_working_line.strip().startswith("/")
                self.setPlainText(self._history_working_line)
                self.historyModeExited.emit()
                cursor = self.textCursor()
                cursor.movePosition(QTextCursor.End)
                self.setTextCursor(cursor)
                return
            if self._history_index < len(self._history_list):
                entry = self._history_list[self._history_index]
                text = entry["text"]
                self._suppress_slash_trigger = text.strip().startswith("/")
                self.setPlainText(text)
                self.historyAttachmentsRestored.emit(entry.get("attachments", []))
                # 选中全部文本，方便继续编辑
                cursor = self.textCursor()
                cursor.movePosition(QTextCursor.End)
                cursor.movePosition(QTextCursor.Start, QTextCursor.KeepAnchor)
                self.setTextCursor(cursor)
        finally:
            self._setting_history_text = False

    def _navigate_history(self, direction: int):
        """方向导航：1 = 更旧（Up），-1 = 更新（Down）"""
        if not self._history_list:
            return

        if self._history_index < 0:
            # 不在浏览模式
            if direction == 1:  # Up → 进入模式
                self._enter_history_mode()
            return

        new_index = self._history_index + direction

        if new_index >= len(self._history_list):
            # 超过最旧条目，停留在最旧
            return

        if new_index < 0:
            # 超过最新条目 → 退出浏览模式，恢复 working line
            self._history_index = -1
            self._set_history_text()
            return

        self._history_index = new_index
        self._set_history_text()

    def _reset_history_mode(self, clear_attachments: bool = False):
        """退出历史浏览模式

        Args:
            clear_attachments: 是否同时清空当前恢复的附件。
                               鼠标点击退出时不清（chip 保持可见），
                               编辑文本时不清（chip 转为工作附件保持可见），
                               仅外部主动清空时传 True。
        """
        self._history_index = -1
        if clear_attachments:
            self.historyAttachmentsRestored.emit([])

    def _tab_complete_if_card_visible(self):
        """Tab 补全：卡片可见时选中当前项"""
        card = self._get_card()
        if card and card.is_card_visible:
            card.select_current()
        self._slash_trigger_pos = -1

    def _on_agent_changed(self, text: str):
        self.agentChanged.emit(text)

    def _setup_keyboard_shortcuts(self):
        self._shortcut_clear = QShortcut(QKeySequence("Ctrl+L"), self)
        self._shortcut_clear.activated.connect(self._on_clear_shortcut)

    def _on_clear_shortcut(self):
        self.clearRequested.emit()

    def _on_text_changed(self):
        has_text = bool(self.toPlainText().strip())
        # 在停止模式下，按钮应该始终可用（用于停止正在进行的请求）
        # 只在发送模式下才根据文本内容决定是否启用
        if not getattr(self, "_is_stop_mode", False):
            self.send_btn.set_send_enabled(has_text)
        # 文本变化时总是需要调整高度，不管是否在停止模式
        if not getattr(self, "_initializing", False):
            self._adjust_height_to_content()
        # 历史模式：用户修改了当前显示的文本 → 退出历史模式，↑↓ 不再切历史
        # 注意：_setting_history_text 为 True 时跳过，避免 setPlainText 期间误触发
        if self._history_index >= 0 and not self._setting_history_text:
            idx = self._history_index
            if idx < len(self._history_list) and self._history_list[idx].get("text", "") != self.toPlainText():
                self._reset_history_mode(clear_attachments=False)
        # detail 模式参数同步
        self._sync_detail_params()

    def _adjust_height_to_content(self):
        """根据内容自动调整高度

        setFixedHeight + resize + updateGeometry 三步保证：
        1. setFixedHeight(new_h) 固定约束（min = max = new_h）
        2. resize(w, new_h) 立即应用新高度，不等布局 defer → 消除发送超高内容时
           空输入框滞留高位的"回弹"卡顿感（旧行为只用 setFixedHeight，实际高度要等
           下一轮布局传递才生效，clear 后输入框短暂悬在 300px 再回落 44px）
        3. updateGeometry() 通知父布局尺寸变更，级联调整父卡片与工具栏位置

        防重入：_adjusting_height 标识在 resize 全程保持 True，嵌套的 resizeEvent
        再入 _adjust_height_to_content 时直接返回。
        """
        if getattr(self, "_initializing", False):
            return
        if getattr(self, "_adjusting_height", False):
            return  # 防重入：级联 resize 不要再进入

        # 窗口拖拽过程中跳过高度调整，防止布局重算干扰窗口管理
        try:
            from app.tool_popup import ToolPopupDialog

            if ToolPopupDialog._any_window_dragging:
                return
        except ImportError:
            pass

        doc = self.document()
        content_height = int(doc.size().height()) + 8
        new_height = max(44, min(300, content_height))

        if self.height() != new_height:
            self._adjusting_height = True
            try:
                self.setFixedHeight(new_height)
                # ⭐ 立即 apply 新高度，不等布局 defer → clear 后输入框不回弹
                self.resize(self.width(), new_height)
                self.updateGeometry()
                # 发送按钮位置由 resizeEvent → _position_send_button 同步到位
            finally:
                self._adjusting_height = False

    def toggle_send_button(self, enable: bool):
        """切换发送/停止模式（enable=True=发送模式, enable=False=停止模式）"""
        if enable:
            self._is_stop_mode = False
            self.send_btn.set_send_mode()
            self.send_btn.setToolTip("发送（Enter）")
            self._on_text_changed()
        else:
            self._is_stop_mode = True
            self.send_btn.set_stop_mode()
            self.send_btn.setToolTip("停止")

        self._sync_external_send_btn()

    def _sync_external_send_btn(self):
        """不再需要外部同步，发送按钮在输入框内部"""
        pass

    def _on_send_click(self):
        """发送/停止按钮点击事件（点击按钮触发）"""
        if self.send_btn.is_stop_mode():
            # 停止模式 → 停止当前请求
            self.toggle_send_button(True)
            self.stopMessageRequested.emit()
        else:
            # 发送模式 → 发送消息
            if not self.toPlainText().strip():
                return
            self.toggle_send_button(False)
            self.sendMessageRequested.emit()

    def _on_enter_send(self):
        """Enter 键发送：始终触发发送流程

        与按钮点击不同，Enter 键不检查停止模式，直接发射 sendMessageRequested。
        main_widget 的 _on_send_clicked 内部会处理：
        - 命令（/xxx）→ 不打断流式直接执行
        - 非命令 + 流式中 → 先停止再发送新消息
        """
        if not self.toPlainText().strip():
            return
        # 如果当前在发送模式（非流式），切换到停止模式表示正在请求
        if not self.send_btn.is_stop_mode():
            self.toggle_send_button(False)
        # 直接发送请求，由 main_widget 内部逻辑处理命令/停止
        self.sendMessageRequested.emit()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_send_button()
        # 窗口 resize → 宽度变化 → 文本换行量变化 → 文档高度变化
        # 必须触发高度重算，否则滚动条会在远未达到 maxHeight 时出现
        self._adjust_height_to_content()

    def showEvent(self, event):
        super().showEvent(event)
        # 首次显示时同步定位发送按钮——showEvent 在 paintEvent 之前
        # 同步执行，且此时 width()/height() 已由父布局确定。
        # 否则：__init__ 阶段 width/height 都是 0，send_btn 落在 (0, 0)
        # （输入框内的"左边"），要等 resizeEvent → debounce timer(0ms)
        # 异步跑一轮才到右下角——视觉上就是"刚进去按钮在左边，过一会
        # 才到右边"。后续 resize 仍走 debounce timer 路径。
        self._position_send_button()

    def _position_send_button(self):
        """定位发送按钮到输入框右下角"""
        if self.send_btn:
            btn_size = self.send_btn.size()
            send_btn_x = self.width() - btn_size.width() - 10
            send_btn_y = self.height() - btn_size.height() - 4
            self.send_btn.move(max(0, send_btn_x), max(0, send_btn_y))

    def keyPressEvent(self, event: QKeyEvent):
        # 强制 / 键直接输入 /，不受中文输入法影响（防止变成、）
        # 仅在光标在输入框第一个字符位置时生效，中间位置仍交给输入法处理
        if event.key() == Qt.Key_Slash and not event.modifiers():
            cursor = self.textCursor()
            if cursor.position() == 0:
                cursor.insertText("/")
                event.accept()
                return

        # 历史浏览模式下，↑↓ 始终导航历史，不受命令卡片影响
        in_history_mode = self._history_index >= 0

        card = self._get_card()
        # 先检查命令卡片是否可见（但历史浏览模式时跳过）
        if card and card.is_card_visible and not in_history_mode:
            if event.key() == Qt.Key_Down:
                if card.select_next():
                    event.accept()
                    return
            elif event.key() == Qt.Key_Up:
                if card.select_prev():
                    event.accept()
                    return
            elif event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Tab):
                if card.is_detail_mode and event.key() == Qt.Key_Tab:
                    # 文件提及卡片可见时，Tab 优先用于文件补全
                    # （回车已自然穿透到文件卡片处理，只有 Tab 被 detail 模式拦截）
                    file_card = self._get_file_mention_card()
                    if file_card and file_card.is_card_visible and not in_history_mode:
                        file_card.select_current()
                        event.accept()
                        return
                    card.select_current()
                    event.accept()
                    return
                if not card.is_detail_mode:
                    card.select_current()
                    event.accept()
                    return
            elif event.key() == Qt.Key_Escape:
                card.dismiss()
                self.slashDismissed.emit()
                event.accept()
                return

        # 文件提及卡片可见时，优先处理导航
        file_card = self._get_file_mention_card()
        if file_card and file_card.is_card_visible and not in_history_mode:
            if event.key() == Qt.Key_Down:
                if file_card.select_next():
                    event.accept()
                    return
            elif event.key() == Qt.Key_Up:
                if file_card.select_prev():
                    event.accept()
                    return
            elif event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Tab):
                file_card.select_current()
                event.accept()
                return
            elif event.key() == Qt.Key_Escape:
                file_card.dismiss()
                self.atDismissed.emit()
                event.accept()
                return

        # Tab 键：开头有 / 时触发补全（detail 模式不触发）
        if event.key() == Qt.Key_Tab:
            text = self.toPlainText()
            if text.startswith("/") and not (card and card.is_detail_mode):
                # 模拟 / 触发，然后选择当前项
                self._slash_trigger_pos = 0
                self.slashTriggered.emit(text[1:] if len(text) > 1 else "")
                # 延迟选中（等待卡片加载）
                QTimer.singleShot(10, lambda: self._tab_complete_if_card_visible())
                event.accept()
                return

        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            if event.modifiers() & Qt.ShiftModifier:
                super().keyPressEvent(event)  # 换行
            else:
                self._on_enter_send()
                event.accept()
        elif event.key() == Qt.Key_Up:
            if self._history_index >= 0 or not self.toPlainText():
                # 历史浏览模式，或在空输入框按↑
                self._navigate_history(1)
                event.accept()
            elif event.modifiers() & Qt.ControlModifier:
                self.historyUpRequested.emit()
                event.accept()
            else:
                super().keyPressEvent(event)
        elif event.key() == Qt.Key_Down:
            if self._history_index >= 0:
                # 历史浏览模式
                self._navigate_history(-1)
                event.accept()
            elif event.modifiers() & Qt.ControlModifier:
                self.historyDownRequested.emit()
                event.accept()
            else:
                super().keyPressEvent(event)
        else:
            super().keyPressEvent(event)

    def inputMethodEvent(self, event: QInputMethodEvent):
        """拦截输入法事件：光标在开头时输入法提交、→ 替换为/

        中文输入法在输入 / 时会提交 、，这绕过了 keyPressEvent 的拦截。
        通过重写 inputMethodEvent 在 IME 提交阶段拦截、并替换为 /。

        同时追踪 IME 组合状态（preedit），组合进行中时跳过 @ 检测，
        避免每次按键触发卡片刷新打断输入法。
        """
        # 追踪 IME 组合状态
        if event.preeditString():
            self._ime_composing = True
        else:
            self._ime_composing = False

        if self.textCursor().position() == 0 and event.commitString() == "、":
            cursor = self.textCursor()
            cursor.insertText("/")
            return  # 不调用 super，阻止 IME 提交 、
        super().inputMethodEvent(event)

    def canInsertFromMimeData(self, source: QMimeData) -> bool:
        """允许拖放/粘贴图片和文件"""
        if source.hasImage() or source.hasUrls():
            return True
        return super().canInsertFromMimeData(source)

    def insertFromMimeData(self, source):
        """重写以处理拖放和粘贴 —— 文件/图片走附件芯片，纯文本走默认"""
        try:
            file_paths = []

            # 拖放/粘贴本地文件
            if source.hasUrls():
                for url in source.urls():
                    local_path = url.toLocalFile()
                    if local_path and os.path.exists(local_path):
                        file_paths.append(local_path)

            # 粘贴剪贴板图片 → 保存到临时文件
            if source.hasImage() and not file_paths:
                img = source.imageData()
                if isinstance(img, QImage) and not img.isNull():
                    tmp_dir = Path(tempfile.gettempdir()) / "drifox_paste"
                    tmp_dir.mkdir(parents=True, exist_ok=True)
                    name = f"paste_{uuid.uuid4().hex[:8]}.png"
                    path = str(tmp_dir / name)
                    img.save(path)
                    file_paths.append(path)

            if file_paths:
                self.files_dropped.emit(file_paths)
                # 在光标位置插入 [[basename]] 占位符（发送时替换为完整路径）
                cursor = self.textCursor()
                for fp in file_paths:
                    basename = os.path.basename(fp)
                    cursor.insertText(f"[[{basename}]] ")
                return

            # 纯文本 → 默认处理
            super().insertFromMimeData(source)

        except Exception:
            try:
                super().insertFromMimeData(source)
            except Exception:
                pass

    def _setup_glow_effect(self):
        """设置输入卡片发光效果 — 挂载到父卡片而非输入框自身"""
        self._glow_effect = QGraphicsDropShadowEffect(self)
        self._glow_effect.setBlurRadius(0)
        self._glow_effect.setColor(QColor(201, 168, 92, 0))
        self._glow_effect.setOffset(0, 0)
        # 延迟挂载：等 input_area 加入 _input_card 后再设置
        self._glow_target = None

    def _apply_input_style(self):
        """应用输入框样式 - 融入卡片，无边框"""
        Colors.refresh()
        self.setStyleSheet(f"""
            QTextEdit {{
                background: transparent;
                color: {Colors.INPUT_TEXT};
                border: none;
                border-radius: 16px 16px 0 0;
                padding: 8px 52px 0px 20px;
                selection-background-color: {Colors.SELECTED_BG};
                {get_font_family_css()} {font_size_css(15)};
            }}
            QTextEdit:focus {{
                border: none;
                color: {Colors.INPUT_FOCUS_TEXT};
            }}
            QTextEdit QScrollBar:vertical {{
                background: transparent;
                width: 0px;
                margin: 0;
            }}
            QTextEdit QScrollBar::handle:vertical {{
                background: {Colors.SCROLLBAR_HANDLE_BG};
                border-radius: 3px;
                min-height: 20px;
            }}
            QTextEdit QScrollBar::handle:vertical:hover {{
                background: {Colors.SCROLLBAR_HANDLE_HOVER_BG};
            }}
            QTextEdit QScrollBar::add-line:vertical,
            QTextEdit QScrollBar::sub-line:vertical {{
                height: 0px;
            }}
            QTextEdit QScrollBar::add-page:vertical,
            QTextEdit QScrollBar::sub-page:vertical {{
                background: none;
            }}
        """)

    def _build_combo_style(self) -> str:
        """构建智能体下拉框样式"""
        Colors.refresh()
        return f"""
            ComboBox {{
                background-color: {Colors.TOOLBAR_BG};
                color: {Colors.INPUT_TEXT};
                border: 1px solid {Colors.INPUT_BORDER};
                border-radius: 10px;
                padding: 3px 10px;
                {get_font_family_css()} {font_size_css(12)};
            }}
            ComboBox:hover {{
                background-color: {Colors.HOVER_BG};
                border-color: {Colors.INPUT_FOCUS_BORDER};
            }}
            ComboBox::drop-down {{
                border: none;
                width: 16px;
            }}
            ComboBox::down-arrow {{
                border-left: 4px solid transparent;
                border-right: 4px solid transparent;
                border-top: 5px solid {Colors.INPUT_TEXT};
                margin-right: 2px;
            }}
            ComboBox AbstractItemView {{
                background-color: {Colors.CONTENT_BG};
                color: {Colors.INPUT_TEXT};
                selection-background-color: {Colors.TEXT_ACCENT};
                border: 1px solid {Colors.INPUT_BORDER};
                border-radius: 10px;
                padding: 4px;
            }}
        """

    def refresh_style(self):
        """刷新样式（响应主题切换）"""
        self._apply_input_style()
        if hasattr(self, "_agent_combo") and self._agent_combo:
            self._agent_combo.setStyleSheet(self._build_combo_style())

    def _animate_glow(self, target_blur, target_alpha, duration=300):
        try:
            host = self.parent()
            while host and not hasattr(host, "_apply_bottom_input_stack_style"):
                host = host.parent()
            if host:
                host._apply_bottom_input_stack_style(target_alpha > 0)
                return
        except Exception:
            pass
        """后备：刷新输入卡样式（仅样式表，双层 glow 由 host._apply_bottom_input_stack_style 管理）"""
        if not self._glow_effect:
            return
        try:
            Colors.refresh()
            # 延迟定位父卡片
            if self._glow_target is None:
                card = self.parent()
                while card and not hasattr(card, "_input_card"):
                    card = card.parent()
                if card and hasattr(card, "_input_card"):
                    self._glow_target = card._input_card
            if self._glow_target:
                # 后备样式：与 main_widget._apply_bottom_input_stack_style 保持一致
                # 注意：不再 setGraphicsEffect（_input_card 已有 _input_card_primary_shadow 管理主光）
                if target_alpha > 0:
                    self._glow_target.setStyleSheet(f"""
                        QWidget {{
                            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                stop:0 {Colors.INPUT_FOCUS_BG_START},
                                stop:1 {Colors.INPUT_FOCUS_BG_END});
                            border: 2px solid {Colors.INPUT_FOCUS_BORDER};
                            border-bottom: none;
                            border-top-left-radius: 16px;
                            border-top-right-radius: 16px;
                            border-bottom-left-radius: 0px;
                            border-bottom-right-radius: 0px;
                        }}
                    """)
                else:
                    self._glow_target.setStyleSheet(f"""
                        QWidget {{
                            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                stop:0 {Colors.INPUT_BG_START},
                                stop:1 {Colors.INPUT_BG_END});
                            border: 1px solid {Colors.INPUT_BORDER};
                            border-bottom: none;
                            border-top-left-radius: 16px;
                            border-top-right-radius: 16px;
                            border-bottom-left-radius: 0px;
                            border-bottom-right-radius: 0px;
                        }}
                    """)
        except Exception:
            pass

    def focusInEvent(self, event):
        try:
            super().focusInEvent(event)
            self._animate_glow(25, 180, 250)
            self._ime_composing = False  # 重新获得焦点时重置 IME 组合状态
            QTimer.singleShot(0, self._ensure_cursor_visible)
        except Exception:
            pass

    def focusOutEvent(self, event):
        try:
            super().focusOutEvent(event)
            self._animate_glow(0, 0, 200)
            # 延迟检查失焦后的焦点去向：点击 CommandCard 项时焦点可能短暂转移，
            # 这里用 0ms 延迟等焦点稳定后再判断焦点是否在命令卡片子树中。
            # 若焦点在卡片内 → 保持卡片可见；若焦点在外（真正失焦）→ 关闭卡片。
            QTimer.singleShot(0, self._deferred_focus_check_dismiss)
        except Exception:
            pass

    def _deferred_focus_check_dismiss(self):
        """失焦延迟检查：若焦点仍在输入框或在卡片内，不关闭卡片"""
        focused = QApplication.focusWidget()
        if focused is self:
            return

        # 检查命令卡片
        card = self._get_card()
        if card and card.is_card_visible:
            if focused:
                p = focused
                while p:
                    if p is card:
                        return
                    p = p.parent()
            card.dismiss()
            self.slashDismissed.emit()

        # 检查文件提及卡片
        file_card = self._get_file_mention_card()
        if file_card and file_card.is_card_visible:
            if focused:
                p = focused
                while p:
                    if p is file_card:
                        return
                    p = p.parent()
            file_card.dismiss()
            self.atDismissed.emit()

    def _ensure_cursor_visible(self):
        cursor = self.textCursor()
        if cursor.position() > 0:
            self.ensureCursorVisible()

    def mousePressEvent(self, event):
        # 点击时退出历史浏览模式
        # 注意：点击输入框内不主动 dismiss 命令卡片 —— 卡片跟随输入框失焦关闭
        # （见 focusOutEvent），这样点击卡片项或在输入框内继续编辑时卡片仍可见。
        if self._history_index >= 0:
            self._reset_history_mode()
        super().mousePressEvent(event)

    def wheelEvent(self, event):
        # 输入框内滚轮不主动 dismiss 命令卡片 —— 同 mousePressEvent
        super().wheelEvent(event)

    def clear(self):
        """重写 clear 方法，清空输入时退出历史浏览模式"""
        self._reset_history_mode()
        super().clear()


class InputGlowUnderlay(QWidget):
    """统一胶囊向内发光层。

    输入卡（上圆角 + border-bottom:none）和工具栏条（上方下圆）原本是两个
    独立 widget，各自挂 QGraphicsDropShadowEffect 时，光晕只跟自己的局部
    轮廓走，接缝处又互相遮挡 —— 看起来就像"只有上半弧形发光"。

    本控件作为主窗口的子控件，绝对定位覆盖整个胶囊（含 margin），通过
    paintEvent 一次性绘制连贯的胶囊形 **向内** 发光：边缘最亮、向胶囊中心
    平滑衰减，类似 lit-up 霓虹边框效果。鼠标事件全部穿透，不影响输入 / 按钮。

    使用方式：
      ``set_pill_geometry`` 同步胶囊在 underlay 内部坐标中的位置与圆角；
      ``set_glow`` 切换主光 / 环境光的强度（聚焦 / 失焦）；
      ``set_color`` 切换发光色（主题切换）。
    """

    DEFAULT_RADIUS = 16

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._color = QColor(201, 168, 92)
        self._primary_alpha = 0
        self._primary_blur = 0
        self._ambient_alpha = 0
        self._ambient_blur = 0
        self._pill_x = 0
        self._pill_y = 0
        self._pill_w = 0
        self._pill_h = 0
        self._radius = self.DEFAULT_RADIUS

    def set_color(self, color: QColor):
        c = QColor(color)
        if c.rgb() == self._color.rgb():
            return
        self._color = c
        self.update()

    def set_glow(
        self,
        primary_alpha: int,
        primary_blur: int,
        ambient_alpha: int,
        ambient_blur: int,
    ):
        if (
            primary_alpha == self._primary_alpha
            and primary_blur == self._primary_blur
            and ambient_alpha == self._ambient_alpha
            and ambient_blur == self._ambient_blur
        ):
            return
        self._primary_alpha = max(0, int(primary_alpha))
        self._primary_blur = max(0, int(primary_blur))
        self._ambient_alpha = max(0, int(ambient_alpha))
        self._ambient_blur = max(0, int(ambient_blur))
        self.update()

    def set_pill_geometry(
        self,
        pill_x: int,
        pill_y: int,
        pill_w: int,
        pill_h: int,
        radius: int = DEFAULT_RADIUS,
    ):
        if (
            pill_x == self._pill_x
            and pill_y == self._pill_y
            and pill_w == self._pill_w
            and pill_h == self._pill_h
            and radius == self._radius
        ):
            return
        self._pill_x = int(pill_x)
        self._pill_y = int(pill_y)
        self._pill_w = max(0, int(pill_w))
        self._pill_h = max(0, int(pill_h))
        self._radius = max(0, int(radius))
        self.update()

    def has_visible_glow(self) -> bool:
        return (
            self._pill_w > 0
            and self._pill_h > 0
            and (
                (self._primary_alpha > 0 and self._primary_blur > 0)
                or (self._ambient_alpha > 0 and self._ambient_blur > 0)
            )
        )

    def paintEvent(self, event):
        if not self.has_visible_glow():
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(Qt.NoBrush)

        # 正向裁剪：只在胶囊 **内部** 绘制 —— 这样发光从边缘向中心扩散，
        # 不会溢出胶囊轮廓外（外面的窗口背景保持纯净）。
        inner = QPainterPath()
        inner.addRoundedRect(
            QRectF(self._pill_x, self._pill_y, self._pill_w, self._pill_h),
            self._radius,
            self._radius,
        )
        painter.setClipPath(inner)

        # 先画环境光（弥散底层，更深更柔），再画主光（紧致核心，更亮更窄）
        # 两层叠加形成"边缘核心亮 → 向心柔光晕开"的层次
        if self._ambient_blur > 0 and self._ambient_alpha > 0:
            self._paint_inner_halo(painter, self._ambient_blur, self._ambient_alpha, falloff=2.0)
        if self._primary_blur > 0 and self._primary_alpha > 0:
            self._paint_inner_halo(painter, self._primary_blur, self._primary_alpha, falloff=2.4)

    def _paint_inner_halo(self, painter: QPainter, blur: int, alpha: int, falloff: float):
        """从胶囊边缘向内堆叠 N 道单像素描边圆角矩形，模拟向心高斯衰减。

        第 i 层位于离边缘 i 像素处（向胶囊中心方向），alpha 按
        ``exp(-(t*falloff)^2)`` 递减 ─→ 边缘最亮、深处趋近透明。
        因为 paintEvent 之前已 clip 到胶囊内部，stroke 多出来的部分不会
        画到胶囊外面，每一道描边都是闭合的圆角矩形轮廓。
        """
        steps = max(blur, 12)
        for i in range(steps):
            t = i / steps  # 0 边缘 → 1 深处
            falloff_factor = math.exp(-((t * falloff) ** 2))
            layer_alpha = int(alpha * falloff_factor)
            if layer_alpha < 1:
                continue
            layer_alpha = min(255, layer_alpha)
            c = QColor(self._color)
            c.setAlpha(layer_alpha)

            pen = QPen(c, 1)
            pen.setCosmetic(True)
            pen.setJoinStyle(Qt.RoundJoin)
            painter.setPen(pen)

            # i + 0.5 偏移：把 1px 描边正好画在像素中心，抗锯齿更平滑
            offset = i + 0.5
            w = self._pill_w - 2 * offset
            h = self._pill_h - 2 * offset
            if w <= 0 or h <= 0:
                break
            r = max(0.0, self._radius - offset)
            painter.drawRoundedRect(
                QRectF(self._pill_x + offset, self._pill_y + offset, w, h),
                r,
                r,
            )


class PlaceholderHighlighter(QSyntaxHighlighter):
    """[[filename]] 占位符语法高亮：输入框中的 [[...]] 标记高亮显示"""

    def __init__(self, document):
        super().__init__(document)
        self._fmt = QTextCharFormat()
        self._fmt.setForeground(QColor(201, 168, 92))  # 金色，与主题色一致
        self._fmt.setFontWeight(QFont.Bold)

    def highlightBlock(self, text: str):
        import re

        for match in re.finditer(r"\[\[[^\]]*\]\]", text):
            self.setFormat(match.start(), match.end() - match.start(), self._fmt)


class AttachmentChip(QFrame):
    """附件标签块：显示文件类型图标 + 文件名 + 删除按钮，响应式圆角矩形"""

    removed = pyqtSignal(str)  # file path

    # 文件扩展名 → FluentIcon 映射
    _FILE_ICON_MAP: dict[tuple[str, ...], FluentIcon] = {
        # 代码
        (".py", ".pyw", ".pyx"): FluentIcon.CODE,
        (".js", ".jsx", ".mjs", ".cjs"): FluentIcon.CODE,
        (".ts", ".tsx"): FluentIcon.CODE,
        (".html", ".htm", ".css", ".scss", ".less"): FluentIcon.CODE,
        (".java", ".kt", ".kts"): FluentIcon.CODE,
        (".cpp", ".c", ".h", ".hpp", ".hxx", ".cxx", ".cc"): FluentIcon.CODE,
        (".cs"): FluentIcon.CODE,
        (".go", ".rs", ".rb", ".php"): FluentIcon.CODE,
        (".swift", ".m", ".mm"): FluentIcon.CODE,
        (".sql"): FluentIcon.CODE,
        (".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd"): FluentIcon.COMMAND_PROMPT,
        # 图片
        (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg", ".ico"): FluentIcon.IMAGE_EXPORT,
        # 视频
        (".mp4", ".avi", ".mov", ".mkv", ".wmv", ".flv", ".webm", ".m4v"): FluentIcon.VIDEO,
        # 音频
        (".mp3", ".wav", ".flac", ".aac", ".ogg", ".wma", ".m4a"): FluentIcon.MUSIC,
        # 压缩包
        (".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".zst"): FluentIcon.ZIP_FOLDER,
        # 文档/数据
        (".pdf"): FluentIcon.DOCUMENT,
        (".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"): FluentIcon.DOCUMENT,
        (".txt", ".md", ".rst", ".log"): FluentIcon.DOCUMENT,
        (".json", ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf"): FluentIcon.DOCUMENT,
        (".csv", ".tsv"): FluentIcon.DOCUMENT,
    }

    def __init__(self, filepath: str, parent=None):
        super().__init__(parent)
        self.filepath = filepath
        self._setup_ui()

    @staticmethod
    def _get_file_icon(filepath: str) -> FluentIcon:
        """根据文件扩展名返回对应的 FluentIcon"""
        if os.path.isdir(filepath):
            return FluentIcon.FOLDER
        ext = os.path.splitext(filepath)[1].lower()
        for exts, icon in AttachmentChip._FILE_ICON_MAP.items():
            if ext in exts:
                return icon
        return FluentIcon.DOCUMENT

    def _setup_ui(self):
        Colors.refresh()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 2, 4, 2)
        layout.setSpacing(5)

        # 文件类型图标
        self._icon_widget = IconWidget(self)
        self._icon_widget.setIcon(self._get_file_icon(self.filepath))
        self._icon_widget.setFixedSize(14, 14)
        layout.addWidget(self._icon_widget)

        # 文件名
        name = os.path.basename(self.filepath)
        if len(name) > 22:
            name = name[:19] + "..."

        self._label = QLabel(name, self)
        self._label.setStyleSheet(
            f"color: {Colors.INPUT_TEXT}; {get_font_family_css()} {font_size_css(12)} background: transparent; border: none; padding: 0;"
        )
        layout.addWidget(self._label)

        # 删除按钮
        close_btn = TransparentToolButton(FluentIcon.CLOSE, self)
        close_btn.setFixedSize(10, 10)
        close_btn.clicked.connect(lambda: self.removed.emit(self.filepath))
        layout.addWidget(close_btn)

        # 整体样式：QFrame 的 border-radius 渲染更可靠，:hover 伪态支持更好
        border_color = Colors.INPUT_BORDER
        self.setFixedHeight(28)
        self.setStyleSheet(
            f"""
            AttachmentChip {{
                background: rgba(255, 255, 255, 0.06);
                border: 1px solid {border_color};
                border-radius: 14px;
            }}
            AttachmentChip:hover {{
                background: rgba(255, 255, 255, 0.12);
                border: 1px solid rgba(255, 255, 255, 0.25);
            }}
            """
        )

        # 悬浮 tooltip 显示完整路径
        self.setToolTip(self.filepath)
