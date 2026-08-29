# 大模型输入框
import logging
import math
import os
import random
import re
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from PyQt5.QtCore import (
    QMimeData,
    QObject,
    QRectF,
    QSize,
    QSizeF,
    Qt,
    QTimer,
    pyqtSignal,
)
from PyQt5.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
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
    QTextFormat,
    QTextObjectInterface,
)
from PyQt5.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QShortcut,
    QSizePolicy,
    QWidget,
)
from qfluentwidgets import ComboBox, FluentIcon, IconWidget, TextEdit, TransparentToolButton

from app.widgets.stop_button import SendStopButton

from app.utils.design_tokens import Colors, font_size_css, qcolor_from_token
from app.utils.utils import get_font_family_css
from app.widgets.simple_hover_tooltip import install_hover_tooltip

logger = logging.getLogger(__name__)

# 正文中的附件引用占位符：[[basename]]
# 这是附件的 **文本表示**，服务于所有纯文本通道（toPlainText、输入历史、
# 发送文本构建、反向同步扫描）。屏幕上的呈现由下面的 inline object 胶囊负责，
# 二者由 SendableTextEdit.toPlainText() 双向对齐。
_PLACEHOLDER_RE = re.compile(r"\[\[([^\]]*)\]\]")

# ── inline 文件引用胶囊（QTextDocument 自定义对象）──────────────────
# Qt 用 U+FFFC（object replacement character）在文档里代表一个 inline object。
_FILE_MENTION_TYPE = QTextFormat.UserObject + 1
_OBJECT_REPLACEMENT = "\ufffc"
# 文件路径存在 charFormat 的自定义属性里（int key，见 QTextFormat.UserProperty）
_FILE_MENTION_PATH_PROP = QTextFormat.UserProperty + 1

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
    "/autoloop:config 自动循环（插件：规划→执行→归档）",
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
    "/plugin-market 浏览安装社区插件，即装即用（含启用/禁用/卸载管理）",
    "/system-cleaner 清理系统缓存和临时文件",
    "/context-usage-stats Token 趋势/消息量图表",
    "/file-tree 浏览/搜索/实时监听文件变更",
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
    attachmentsRemoved = pyqtSignal(list)  # list[str] 正文中被用户删除的 [[basename]] 引用名

    def __init__(self, parent=None):
        super().__init__(parent)
        self._initializing = True
        self._glow_effect = None

        # ⚠️ 状态属性前置区 —— 必须在 textChanged 连接（下方）之前初始化
        #
        # 以下属性全部会被 textChanged 的槽函数（_on_text_changed /
        # _on_slash_trigger_check / _on_at_trigger_check）读取。而 textChanged
        # 在信号连接完成的那一刻起就可能被触发：PlaceholderHighlighter 构造时的
        # rehighlight、样式应用、文档初始化等都会让 Qt 发出内容变更信号。
        # 一旦属性晚于连接初始化，就会在构造期间炸 AttributeError
        # （'_SendableTextEdit' object has no attribute 'xxx'）。
        # 因此这里集中声明，后续各功能区只保留注释、不再重复赋值。
        self._ime_composing = False  # IME 输入法组合状态
        self._slash_trigger_pos = -1  # / 触发位置
        self._at_trigger_pos = -1  # @ 触发位置
        self._setting_history_text = False  # 正在 _set_history_text 中，阻止 _on_text_changed 误触发 reset
        self._suppress_slash_trigger = False  # 切换历史时临时阻止 / 触发
        # 文本 → 附件 反向同步（详见 _sync_placeholder_removals）
        self._last_placeholder_names: list[str] = []  # 上一次文本里的 [[...]] 快照
        self._syncing_attachments = False  # 程序化改文本中：只校准快照，不上报删除
        self._sync_attachments_enabled = True  # 总开关：批量流程整体暂停反向同步

        # 🛡️ R1：粘贴图片异步保存的进行中集合（threading.Event，发送前等待就绪）
        self._pending_image_saves: list = []
        self._pending_saves_lock = threading.Lock()
        # 防重入：嵌套 QEventLoop 等待期间用户再次触发发送（递归进入 wait）
        self._waiting_image_saves: bool = False
        # 并发信号量：限制 PNG 编码线程数（大图 64MB 驻留 × N 线程，R1-R2）
        self._paste_save_semaphore = threading.Semaphore(2)

        # ⚠️ 必须先于 textChanged.connect 初始化：
        # _on_text_changed → _schedule_detail_sync() 会读 _detail_sync_timer；
        # _on_text_changed → _reset_history_mode 分支会读 _history_index/_history_list。
        # QTimer 占位为 None，下方原位置再创建实例（依赖 self 的 QObject 父对象）。
        self._detail_sync_timer: Optional[QTimer] = None
        self._history_list: list = []  # 最近输入历史（最新在前）
        self._history_index: int = -1  # -1 = 不在浏览模式
        self._history_working_line: str = ""  # 进入历史模式时保存的当前输入（退出时恢复）
        # 卡片引用前置：_on_slash_trigger_check → _get_card() 会读 _command_card_ref
        self._command_card_ref = None
        self._file_mention_card_ref = None

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

        # [[filename]] 占位符高亮（仅对纯文本残留的 [[...]] 生效，胶囊不走这里）
        self._placeholder_highlighter = PlaceholderHighlighter(self.document())

        # inline 文件引用胶囊：注册自定义对象处理器
        # ⚠️ 必须持有强引用（self._file_mention_object），否则被 GC 后
        #    文档布局拿到悬空指针，绘制时直接崩溃。
        self._file_mention_object = FileMentionObject()
        self.document().documentLayout().registerHandler(_FILE_MENTION_TYPE, self._file_mention_object)

        # detail 参数同步防抖（参考 / 命令触发节流：合并快速敲键 + IME 保护）
        # 值选择模式（枚举列表）每次 textChanged 都会触发 _sync_detail_params →
        # update_active_params → _refresh_value_list 重建全部 widget。打拼音时
        # 每敲一个字母 textChanged 就触发一次重建，打断输入法且浪费性能。
        # 统一 100ms 防抖：快速敲键期间只执行最后一次过滤/渲染。
        # 前置属性区已声明 self._detail_sync_timer = None 占位；此处创建实例。
        self._detail_sync_timer = QTimer(self)
        self._detail_sync_timer.setSingleShot(True)
        self._detail_sync_timer.timeout.connect(self._on_detail_sync_timeout)

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

        # _history_list / _history_index / _history_working_line 见顶部状态属性前置区
        #
        # 反向同步说明：常规路径下正文不再出现 [[...]]（附件栏是唯一真相源），但两类
        # 文本仍会带占位符 —— 历史输入记录恢复的文本、用户手动键入的引用。
        # 这里保存「上一次文本里的占位符」快照，只在占位符数量减少时判定为用户删除。

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
        # 走防抖：textChanged 每次敲键都会触发本方法，直接同步会每键重建
        # 值列表 widget（打断输入法）。参考命令卡片列表 100ms 防抖合并。
        self._schedule_detail_sync()

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
                    # 同步参数显隐：追踪输入框中的参数变化（走防抖合并快速敲键）
                    self._schedule_detail_sync()
                    return

                from app.core.command_manager import CommandManager
                from app.utils.utils import get_skill_by_name

                # 解析后缀：如 "tdd-skill" → base="tdd", type="skill"
                raw_cmd, suffix_type = CommandManager.parse_suffixed_name(cmd_name)

                # ── 确定实际匹配的「真名」：按实际匹配来源选择 ──
                # 不能用 raw_cmd or cmd_name 一刀切——当技能名恰好以 -skill/-prompt/-cmd/-agent
                # 结尾时，raw_cmd 会把真名"my-skill"截断为"my"，导致后续 show_command_detail
                # 用错误的名字查不到技能，参数卡片不弹出。
                #
                # ★ 关键修复：用 has_command（精确匹配，不剥后缀）代替 is_known_command_name
                # （它会内部剥后缀），避免 display_name 后缀导致 check_name 传入带后缀的假名，
                # 使 show_command_detail 在命令字典中找不到条目而直接 return 不显示参数。
                cmd_mgr = CommandManager.get_instance()
                exact_cmd_match = cmd_mgr.has_command(cmd_name)  # 精确匹配（不剥后缀）
                exact_skill_match = bool(get_skill_by_name(cmd_name))  # 精确技能匹配
                exact_match = exact_cmd_match or exact_skill_match
                raw_match = bool(raw_cmd) and (cmd_mgr.has_command(raw_cmd) or bool(get_skill_by_name(raw_cmd)))

                if exact_match or raw_match:
                    # 已知命令/技能 + 参数 → 切换到 detail 模式
                    self._slash_trigger_pos = 0
                    # check_name：精确匹配用原 cmd_name（防止真名含后缀被截断），
                    # 只有 raw_cmd 才匹配时用 raw_cmd（处理 display_name 后缀如"tdd-prompt"→"tdd"）
                    check_name = cmd_name if exact_match else raw_cmd

                    # 传入当前选中项的 display_type（供 show_command_detail 显示对应类型的 hint）
                    # 优先使用 _on_command_selected 中由卡片传来的精确类型（_card_selected_type），
                    # 避免 card._current_selected_type 在未导航时仍是首个命令条目的类型而误判。
                    # 同时用 _card_selected_name 做名称匹配校验，防止上次选择残留的类型泄漏。
                    if suffix_type:
                        # 用户手工输入了后缀（如 -prompt），类型由后缀精确决定
                        selected_type = suffix_type
                    elif self._card_selected_name == check_name and self._card_selected_type:
                        selected_type = self._card_selected_type
                    else:
                        selected_type = card._current_selected_type if card else ""
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
        """@ 触发节流：首次弹出立即响应，卡片展开后的连续输入走 100ms 防抖

        [PERF] 旧实现对每一次 @ 触发都统一加 100ms 防抖，于是「按下 @ → 卡片出现」
               永远有至少 100ms 的固定等待。实测热路径端到端 130ms，其中 100ms
               纯属等待，用户体感就是"按了 @ 要愣一下"。
               首次弹出时列表还没显示，一次过滤+渲染的实测成本仅 ~2ms（远低于
               100ms 防抖窗口），立即发射可以让卡片真正"跟手"出现；
               等卡片展开后再敲键，才需要防抖来避免每敲一个字符都重建整份列表。
        """
        # 连续输入相同 query（如按方向键/退格到原位置）→ 跳过
        # 注意：初始 _pending_at_query = None，此时空字符串 query = "" 是合法首次触发
        if self._pending_at_query is not None and query == self._pending_at_query:
            return

        if self._at_trigger_pos < 0:
            return

        self._pending_at_query = query
        self._at_throttle_timer.stop()

        # 卡片尚未显示 = 用户正在等列表出现 → 立即发射，不进防抖窗口
        file_card = self._get_file_mention_card()
        if file_card is None or not file_card.is_card_visible:
            self.atTriggered.emit(query)
            return

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

    def toPlainText(self) -> str:
        """返回纯文本，inline 文件胶囊展开为 ``[[basename]]`` 形式

        QTextDocument 用 U+FFFC 表示 inline object，``super().toPlainText()``
        会原样吐出 ``\\ufffc``，文件名信息就丢了。这里遍历 document 的 fragment
        把它还原成 ``[[basename]]``，从而 **对上层完全透明** —— 命令检测、@ 检测、
        输入历史、发送文本构建、附件反向同步等所有既有的 toPlainText() 调用点
        拿到的字符串与旧的「字面占位符」实现完全一致，无需任何改动。
        """
        raw = super().toPlainText()
        if _OBJECT_REPLACEMENT not in raw:
            return raw
        return self._expand_mention_objects()

    def _expand_mention_objects(self) -> str:
        """把文档中的 inline 文件胶囊展开为 [[basename]] 文本"""
        parts: list[str] = []
        block = self.document().begin()
        while block.isValid():
            it = block.begin()
            while not it.atEnd():
                frag = it.fragment()
                if frag.isValid():
                    fmt = frag.charFormat()
                    if fmt.objectType() == _FILE_MENTION_TYPE:
                        path = fmt.stringProperty(_FILE_MENTION_PATH_PROP) or ""
                        parts.append(f"[[{os.path.basename(path)}]]")
                    else:
                        parts.append(frag.text())
                it += 1
            parts.append("\n")
            block = block.next()
        text = "".join(parts)
        # 末位换行是块分隔符，不是文档内容
        return text[:-1] if text.endswith("\n") else text

    @staticmethod
    def _make_mention_format(file_path: str) -> QTextCharFormat:
        """构造 inline 文件胶囊的字符格式（objectType + 路径属性 + tooltip）

        路径存在自定义 property 里，读取侧统一用 ``fmt.stringProperty(key)``。
        ⚠️ 写入只能用 ``setProperty``：Qt 只有只读的 ``stringProperty(int)``，
        没有配对的 setter（``setStringProperty`` 并不存在）。
        """
        fmt = QTextCharFormat()
        fmt.setObjectType(_FILE_MENTION_TYPE)
        fmt.setProperty(_FILE_MENTION_PATH_PROP, file_path)
        fmt.setToolTip(file_path)
        return fmt

    def insert_file_mention(self, file_path: str):
        """@ 提及选中文件 → 把已键入的 @query 替换为一枚 inline 文件胶囊

        与拖放/粘贴走同一个通道（见 insertFromMimeData），因此 @ 选中的文件
        在正文里的呈现也是圆角胶囊，而非字面 ``[[basename]]``。
        """
        cursor = self.textCursor()
        cursor_pos = cursor.position()
        trigger_pos = self._at_trigger_pos

        if trigger_pos >= 0:
            cursor.setPosition(trigger_pos)
            cursor.setPosition(cursor_pos, QTextCursor.KeepAnchor)

        # 先插入 U+FFFC（带 object 格式），再插一个普通空格 —— 两步必须分开：
        # insertText(text, fmt) 会把 fmt 应用到整段文本，若空格也带上 objectType，
        # 空格会被当成 inline object 渲染成第二个胶囊。
        cursor.insertText(_OBJECT_REPLACEMENT, self._make_mention_format(file_path))
        cursor.insertText(" ")
        self.setTextCursor(cursor)

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
        if pos > 0 and text[pos - 1] == "-":
            # 光标前是用户手动输入的参数前缀（-、--），替换前缀为完整参数名，
            # 避免追加到前缀后面变成 `- --parameter` 或 `----parameter`。
            prefix_start = pos - 1
            while prefix_start > 0 and text[prefix_start - 1] == "-":
                prefix_start -= 1
            # 仅处理独立的参数前缀，避免修改普通文本末尾的连字符。
            if prefix_start == 0 or text[prefix_start - 1] in (" ", "\t", "\n"):
                cursor.setPosition(prefix_start)
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
        # 文本 → 附件 反向同步：正文里的 [[basename]] 被删掉时通知附件栏
        self._sync_placeholder_removals()
        # detail 模式参数同步（防抖：合并快速敲键，参考 / 命令触发节流）
        # IME 组合进行中（打拼音）跳过同步，避免每次敲键重建值列表打断输入法；
        # 提交后（preedit 清空）textChanged 再次触发，走防抖后正常同步。
        self._schedule_detail_sync()

    # ==================== 文本 → 附件 反向同步 ====================

    def _sync_placeholder_removals(self):
        """扫描正文中的 [[basename]]，与上一次快照做差集并上报被删除的引用

        这是「删除文字里的附件引用 → 附件栏同步删除」的实现。判定规则：

        - **多重集差，不是集合差**：两个同名文件各自占一个占位符，删掉其中一个
          不应把另一个也带走。因此逐个消耗式做差，而非简单 set 相减。
        - **只在减少时上报**：``[[a]] [[b]]`` 变成 ``[[a]]`` 才上报 ``b``；
          新增占位符不上报（附件只能由附件栏或拖放产生，正文打字不会凭空造附件）。
        - **基线校准**：程序化设置文本（历史浏览切换、附件→文本同步）时只更新
          快照不上报，否则「恢复历史文本」会被误判成「用户删光了引用」→ 附件被清空。
        """
        if not self._sync_attachments_enabled:
            return

        current = _PLACEHOLDER_RE.findall(self.toPlainText())
        last = self._last_placeholder_names

        # 程序化改文本：只校准基线
        if self._setting_history_text or self._syncing_attachments:
            self._last_placeholder_names = current
            return
        # IME 组合期间 preedit 未提交，文本处于不稳定中间态，等提交后再判
        if self._ime_composing:
            return
        if current == last:
            return

        remaining = list(current)
        removed: list[str] = []
        for name in last:
            if name in remaining:
                remaining.remove(name)
            else:
                removed.append(name)

        self._last_placeholder_names = current
        if removed:
            self.attachmentsRemoved.emit(removed)

    def remove_placeholder(self, basename: str) -> bool:
        """删除正文中第一个 [[basename]] 占位符（附件 → 文本 方向的同步）

        与 :meth:`_sync_placeholder_removals` 互补：点 chip 的 × 删除附件时，
        正文里对应的引用也应一并消失。

        只删第一个匹配 —— 旧实现用 ``str.replace(placeholder, "")`` 会把同名
        占位符一次删光，两个同名文件删一个会连带删掉另一个的引用。

        Returns:
            是否实际发生了改动
        """
        pattern = re.compile(r"\[\[" + re.escape(basename) + r"\]\][ \u3000]?")
        current = self.toPlainText()
        new_text = pattern.sub("", current, count=1)
        if new_text == current:
            return False

        cursor_pos = self.textCursor().position()
        # 守卫：setPlainText 会同步触发 textChanged → _sync_placeholder_removals。
        # 不加守卫的话，这次程序化删除会被误判成「用户删了引用」，反过来再删一遍附件。
        self._syncing_attachments = True
        try:
            self.setPlainText(new_text)
        finally:
            self._syncing_attachments = False

        # setPlainText 会把光标重置到开头，恢复原位置（截断到新文本长度内）
        cursor = self.textCursor()
        cursor.setPosition(max(0, min(cursor_pos, len(new_text))))
        self.setTextCursor(cursor)
        return True

    def remove_mention_objects(self, path: str) -> bool:
        """删除正文中指向 path 的 inline 文件胶囊（附件 → 正文 方向的同步）

        与 :meth:`_sync_placeholder_removals` 互补：点附件栏 chip 的 × 删除附件时，
        正文里对应的胶囊也应一并消失。

        Returns:
            是否实际删除了
        """
        doc = self.document()
        spans: list[tuple[int, int]] = []
        block = doc.begin()
        while block.isValid():
            it = block.begin()
            while not it.atEnd():
                frag = it.fragment()
                if frag.isValid():
                    fmt = frag.charFormat()
                    if (
                        fmt.objectType() == _FILE_MENTION_TYPE
                        and (fmt.stringProperty(_FILE_MENTION_PATH_PROP) or "") == path
                    ):
                        spans.append((frag.position(), frag.position() + frag.length()))
                it += 1
            block = block.next()

        if not spans:
            return False

        last = doc.characterCount() - 1
        # 守卫：删除会同步触发 textChanged → _sync_placeholder_removals，
        # 不抑制的话这次程序化删除会被误判成「用户删了引用」，反过来再删一遍附件。
        self._syncing_attachments = True
        try:
            # 从后往前删：前面的 span 位置不会因删除而偏移
            for start, end in reversed(spans):
                cursor = QTextCursor(doc)
                cursor.setPosition(start)
                stop = end
                # 连胶囊后紧跟的一个空格一起删，避免正文里留下一串空格
                probe = QTextCursor(doc)
                probe.setPosition(end)
                probe.setPosition(min(end + 1, last), QTextCursor.KeepAnchor)
                if probe.selectedText() == " ":
                    stop = end + 1
                cursor.setPosition(stop, QTextCursor.KeepAnchor)
                cursor.removeSelectedText()
        finally:
            self._syncing_attachments = False
        return True

    def set_attachment_sync_enabled(self, enabled: bool):
        """暂停 / 恢复「文本 → 附件」反向同步

        发送、清空输入框等批量流程会连续改动文本与附件列表，中间态不应触发
        反向同步（否则正文占位符随文本一起消失时，会被误判成用户删除了附件）。
        由调用方显式决定附件去留更安全。

        恢复时会把快照校准到当前文本，暂停期间的改动被忽略。
        """
        self._sync_attachments_enabled = bool(enabled)
        if enabled:
            self._last_placeholder_names = _PLACEHOLDER_RE.findall(self.toPlainText())

    def _schedule_detail_sync(self):
        """detail 参数同步防抖调度（参考命令卡片列表刷新方式）

        - IME 组合中（打拼音）直接跳过：每敲一个拼音字母 textChanged 都会
          触发，若每次重建值列表 widget 会打断输入法。提交后 preedit 清空，
          textChanged 再次触发，此时正常走防抖同步。
        - 非组合时统一 100ms 防抖：快速敲键期间合并为最后一次过滤/渲染。
        """
        if self._ime_composing:
            return
        # 防 __init__ 期间被提前触发的 textChanged 命中：定时器尚未创建
        timer = self._detail_sync_timer
        if timer is None:
            return
        timer.stop()
        timer.start(100)

    def _on_detail_sync_timeout(self):
        """detail 参数同步防抖超时：执行真正的同步"""
        # 防抖窗口内若进入 IME 组合（如拼音刚敲下），跳过本次同步，
        # 等提交后的 textChanged 再触发一轮防抖。
        if self._ime_composing:
            return
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
            from app.utils.window_drag_state import any_window_dragging

            if any_window_dragging:
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
            # 🛡️ R1：发送前等待粘贴图片异步保存完成（附件路径就绪）；
            # 超时未就绪 → 阻止发送并提示（避免正常环境静默丢图）
            if not self._wait_pending_image_saves():
                self._warn_image_save_pending()
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
        # 🛡️ R1：发送前等待粘贴图片异步保存完成（附件路径就绪）；
        # 超时未就绪 → 阻止发送并提示（避免正常环境静默丢图）
        if not self._wait_pending_image_saves():
            self._warn_image_save_pending()
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
                    # 可读命名：原为 paste_<uuid8>.png，显示为附件名时是一串无意义
                    # 的十六进制。改为「截图_月日_时分秒.png」，同秒内追加序号防覆盖。
                    stamp = datetime.now().strftime("%m%d_%H%M%S")
                    path = tmp_dir / f"截图_{stamp}.png"
                    seq = 1
                    while path.exists() and seq < 100:
                        path = tmp_dir / f"截图_{stamp}_{seq}.png"
                        seq += 1
                    # 🛡️ R1：PNG 编码+写盘移出主线程（大截图同步 save 100-500ms
                    # 冻结 UI）。UI 立即返回：附件芯片照常创建；
                    # 发送前 _wait_pending_image_saves 保证文件就绪。
                    self._save_paste_image_async(img, str(path))
                    file_paths.append(str(path))

            if file_paths:
                # 附件栏芯片由 main_widget 创建；正文里同步插入 inline 胶囊，
                # 让「这句话引用的是哪个文件」在正文里可见。
                self.files_dropped.emit(file_paths)
                cursor = self.textCursor()
                for fp in file_paths:
                    # U+FFFC 与尾随空格分开插入，理由见 insert_file_mention
                    cursor.insertText(_OBJECT_REPLACEMENT, self._make_mention_format(fp))
                    cursor.insertText(" ")
                self.setTextCursor(cursor)
                return

            # 纯文本 → 默认处理
            # 复制带胶囊的文本再粘贴回来时，剪贴板里会带着 U+FFFC 裸字符。
            # 它已失去 charFormat（不含路径属性），留着只会渲染成一个空白块。
            if source.hasText() and _OBJECT_REPLACEMENT in source.text():
                cleaned = QMimeData()
                cleaned.setText(source.text().replace(_OBJECT_REPLACEMENT, ""))
                super().insertFromMimeData(cleaned)
                return

            super().insertFromMimeData(source)

        except Exception:
            try:
                super().insertFromMimeData(source)
            except Exception:
                pass

    def _save_paste_image_async(self, img: QImage, path: str) -> None:
        """粘贴图片后台保存（PNG 编码+写盘移出主线程，避免大图粘贴冻结 UI）

        - QImage 隐式共享（implicit sharing）跨线程安全：主线程不再使用 img
        - 保存失败静默降级并记日志，与原先同步 img.save 失败行为一致
          （原代码不检查 save 返回值，失败时路径仍加入附件、文件缺失）
        - 保存完成后 set() 对应 Event，供发送前 _wait_pending_image_saves 等待
        """
        ev = threading.Event()
        with self._pending_saves_lock:
            self._pending_image_saves.append(ev)

        def _do_save():
            try:
                # 并发信号量：限制 PNG 编码线程数（大图内存驻留 × 线程数上限）
                with self._paste_save_semaphore:
                    img.save(path)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[InputArea] 粘贴图片保存失败: {path}: {e}")
            finally:
                ev.set()
                with self._pending_saves_lock:
                    try:
                        self._pending_image_saves.remove(ev)
                    except ValueError:
                        pass

        threading.Thread(target=_do_save, daemon=True, name="drifox-paste-image-save").start()

    def _wait_pending_image_saves(self, timeout: float = 5.0) -> bool:
        """发送前等待粘贴图片保存完成（保证附件路径就绪）

        QEventLoop + QTimer 驱动等待：等待期间 UI 事件循环正常运转（不冻结），
        用户仍可交互。正常情况（保存早已完成）立即返回 True；极端情况
        （粘贴后立即发送）最多等待 timeout 秒。

        Returns:
            True = 全部就绪；False = 超时（附件可能未就绪，调用方应阻止发送）。

        🛡️ 防重入：嵌套 QEventLoop 中用户再次触发发送会递归进入本方法，
        `_waiting_image_saves` 标志阻止递归（外层 wait 已覆盖同一批附件）。
        """
        if getattr(self, "_waiting_image_saves", False):
            return True
        with self._pending_saves_lock:
            pending = [ev for ev in self._pending_image_saves if not ev.is_set()]
        if not pending:
            return True

        self._waiting_image_saves = True
        try:
            from PyQt5.QtCore import QEventLoop, QTimer as _QTimer

            deadline = time.monotonic() + timeout
            while True:
                with self._pending_saves_lock:
                    pending = [ev for ev in self._pending_image_saves if not ev.is_set()]
                if not pending:
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                loop = QEventLoop()
                _QTimer.singleShot(int(min(0.05, remaining) * 1000), loop.quit)
                loop.exec_()
        finally:
            self._waiting_image_saves = False

    def _warn_image_save_pending(self) -> None:
        """粘贴图片保存超时未就绪 → 阻止发送并提示（R1-C2：避免正常环境静默丢图）"""
        try:
            from qfluentwidgets import InfoBar, InfoBarPosition
            from app.widgets.tab_manager_window import TabManagerWindow

            InfoBar.warning(
                "图片仍在保存",
                "粘贴的大图正在后台保存，请稍候再发送",
                parent=TabManagerWindow.get_instance() or self.window() or self,
                duration=3000,
                position=InfoBarPosition.BOTTOM,
            )
        except Exception:  # noqa: BLE001
            logger.debug("[InputArea] 图片保存等待超时（提示失败，静默跳过发送）")

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

        # 同步文档默认字体：inline 文件胶囊（FileMentionObject）用
        # document().defaultFont() 计算尺寸并绘制文件名。QSS 的 font 只作用于
        # widget 自身，不会同步到 QTextDocument —— 不同步的话胶囊会比正文小一号，
        # 宽度也按错误字号计算，出现文字截断/胶囊过窄。
        doc = self.document()
        if doc is not None and doc.defaultFont() != self.font():
            doc.setDefaultFont(self.font())

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

    def contextMenuEvent(self, event):
        """Phase E：接管输入框右键菜单——保留基础 cut/copy/paste + 追加插件项"""
        from PyQt5.QtWidgets import QMenu

        menu = QMenu(self)
        cut_act = menu.addAction("剪切")
        cut_act.triggered.connect(lambda: self.cut())
        copy_act = menu.addAction("复制")
        copy_act.triggered.connect(lambda: self.copy())
        paste_act = menu.addAction("粘贴")
        paste_act.triggered.connect(lambda: self.paste())
        # 插件菜单项注入（main_widget 提供方法）
        win = self.window()
        builder = getattr(win, "_build_plugin_input_menu", None)
        if callable(builder):
            try:
                builder(menu)
            except Exception:
                pass
        menu.exec_(event.globalPos())


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
    """[[filename]] 占位符语法高亮

    样式刻意做得克制（主题强调色 + 淡底，不加粗）：正文里出现 [[...]] 现在只是
    「这是一个附件引用标记」的提示，而不是附件的主体呈现 —— 主体在附件栏的 chip 上。
    旧实现的「金色加粗」过于抢眼，满屏方括号正是「附件显示很简陋」的直接观感来源。
    """

    def __init__(self, document):
        super().__init__(document)
        self._fmt = QTextCharFormat()
        self.refresh_theme()

    def refresh_theme(self):
        """主题切换后重取颜色

        必须走 qcolor_from_token：主题 YAML 里的色值是 rgba(r,g,b,a) 写法，
        QColor(str) 不认这种格式（实测 isValid() == False），直接传会静默失效。
        """
        Colors.refresh()
        self._fmt.setForeground(qcolor_from_token(Colors.INPUT_FOCUS_BORDER))
        self._fmt.setBackground(qcolor_from_token(Colors.TOOLBAR_BG))
        self._fmt.setFontWeight(QFont.Normal)
        self.rehighlight()

    def highlightBlock(self, text: str):
        for match in _PLACEHOLDER_RE.finditer(text):
            self.setFormat(match.start(), match.end() - match.start(), self._fmt)


class AttachmentChip(QFrame):
    """附件标签块：文件类型图标 + 文件名 + 删除按钮

    尺寸约定（直接影响外层 FlowLayout 的 minimumWidth，进而影响 QSplitter 布局）：

    - 高度固定 26px，圆角 13px（半高胶囊）。
    - 文件名中间省略，像素上限 :data:`_MAX_NAME_WIDTH`；
      chip 整体宽度上限 :data:`_MAX_CHIP_WIDTH` 兜底。
      两者共同保证单个 chip 不会宽到把父布局顶开。

    配色约定：**禁止硬编码 rgba(255,255,255,x)**。该写法在浅色主题下是「白叠白」，
    完全不可见（本项目反复出现的缺陷模式）。一律取 :class:`Colors` 的大写属性，
    它们由主题 YAML 自动填充，是主题感知的安全值。
    """

    removed = pyqtSignal(str)  # file path

    #: 文件名最大像素宽度，超出部分中间省略
    _MAX_NAME_WIDTH = 148
    #: chip 整体宽度硬上限（兜底，防止极端长名顶开布局）
    _MAX_CHIP_WIDTH = 210
    #: chip 固定高度
    _CHIP_HEIGHT = 26

    # 文件扩展名 → FluentIcon 映射
    # 注意：单元素元组必须写尾随逗号，否则 (".cs") 是 str，
    # ``ext in exts`` 会退化成子串判断（历史 bug，曾漏掉三处逗号）。
    _FILE_ICON_MAP: dict[tuple[str, ...], FluentIcon] = {
        # 代码
        (".py", ".pyw", ".pyx"): FluentIcon.CODE,
        (".js", ".jsx", ".mjs", ".cjs"): FluentIcon.CODE,
        (".ts", ".tsx"): FluentIcon.CODE,
        (".html", ".htm", ".css", ".scss", ".less"): FluentIcon.CODE,
        (".java", ".kt", ".kts"): FluentIcon.CODE,
        (".cpp", ".c", ".h", ".hpp", ".hxx", ".cxx", ".cc"): FluentIcon.CODE,
        (".cs",): FluentIcon.CODE,
        (".go", ".rs", ".rb", ".php"): FluentIcon.CODE,
        (".swift", ".m", ".mm"): FluentIcon.CODE,
        (".sql",): FluentIcon.CODE,
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
        (".pdf",): FluentIcon.DOCUMENT,
        (".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"): FluentIcon.DOCUMENT,
        (".txt", ".md", ".rst", ".log"): FluentIcon.DOCUMENT,
        (".json", ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf"): FluentIcon.DOCUMENT,
        (".csv", ".tsv"): FluentIcon.DOCUMENT,
    }

    def __init__(self, filepath: str, parent=None):
        super().__init__(parent)
        self.filepath = filepath
        self._missing = not os.path.exists(filepath)
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

    @staticmethod
    def _human_size(path: str) -> str:
        """人类可读的文件大小；目录或读取失败返回空串"""
        try:
            if os.path.isdir(path):
                return "文件夹"
            size = os.path.getsize(path)
        except OSError:
            return ""
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024 or unit == "GB":
                return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
            size /= 1024
        return ""

    def _setup_ui(self):
        Colors.refresh()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(7, 0, 3, 0)
        layout.setSpacing(4)

        name_color = Colors.REALTIME_ERROR if self._missing else Colors.INPUT_TEXT

        # 文件类型图标
        self._icon_widget = IconWidget(self)
        self._icon_widget.setIcon(self._get_file_icon(self.filepath))
        self._icon_widget.setFixedSize(14, 14)
        layout.addWidget(self._icon_widget)

        # 文件名：中间省略（原实现是硬编码截断 22 字符，长名一律 "xxx..."）
        name = os.path.basename(self.filepath.rstrip(os.sep)) or self.filepath
        self._label = QLabel(self)
        self._label.setStyleSheet(
            f"color: {name_color}; {get_font_family_css()} {font_size_css(12)}"
            " background: transparent; border: none; padding: 0;"
        )
        fm = QFontMetrics(self._label.font())
        self._label.setText(fm.elidedText(name, Qt.ElideMiddle, self._MAX_NAME_WIDTH))
        layout.addWidget(self._label)

        # 删除按钮：16x16 命中区，图标 9x9 保持视觉轻盈
        # （原实现是 10x10 按钮，图标几乎看不见且难点中）
        self._close_btn = TransparentToolButton(FluentIcon.CLOSE, self)
        self._close_btn.setFixedSize(16, 16)
        self._close_btn.setIconSize(QSize(9, 9))
        self._close_btn.clicked.connect(lambda: self.removed.emit(self.filepath))
        layout.addWidget(self._close_btn)

        # 整体样式：QFrame 的 border-radius 渲染更可靠，:hover 伪态支持更好
        self.setFixedHeight(self._CHIP_HEIGHT)
        self.setMaximumWidth(self._MAX_CHIP_WIDTH)
        self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        self._apply_style()

        # tooltip：完整路径 + 大小；文件已失效时额外提示
        size_text = self._human_size(self.filepath)
        tip = self.filepath + (f"\n{size_text}" if size_text else "")
        if self._missing:
            tip += "\n⚠ 文件已不存在，发送时将被忽略"
        self.setToolTip(tip)

    def _apply_style(self):
        """按当前主题与文件状态生成样式表

        所有颜色取自 Colors（主题感知）。hover 时边框切换为主题强调色，
        比单纯加深背景更容易被察觉。
        """
        Colors.refresh()
        if self._missing:
            border = Colors.REALTIME_ERROR
            border_hover = Colors.REALTIME_ERROR
        else:
            border = Colors.BORDER
            border_hover = Colors.INPUT_FOCUS_BORDER
        radius = self._CHIP_HEIGHT // 2
        self.setStyleSheet(
            f"""
            AttachmentChip {{
                background: {Colors.TOOLBAR_BG};
                border: 1px solid {border};
                border-radius: {radius}px;
            }}
            AttachmentChip:hover {{
                background: {Colors.HOVER_BG};
                border: 1px solid {border_hover};
            }}
            """
        )

    def refresh_theme(self):
        """主题切换后刷新配色（由 main_widget 统一调用）"""
        self._apply_style()


class FileMentionObject(QObject, QTextObjectInterface):
    """输入框正文中的 inline 文件引用胶囊（圆角背景 + 类型图标 + 文件名）

    ⚠️ 必须同时继承 QObject：``QTextDocument.documentLayout().registerHandler()``
    的签名要求 component 是 QObject，纯 QTextObjectInterface 会被拒绝
    （TypeError: argument 2 has unexpected type）。
    继承顺序必须是 (QObject, QTextObjectInterface)，反了会导致 MRO 冲突。


    为什么不用字面 ``[[basename]]``（旧实现）:

    - 观感就是「一对方括号」，加粗高亮后更显眼，这正是「附件显示很简陋」的来源；
    - 它可以被任意部分编辑 —— 删掉半个括号、在中间插入字符，引用就破损了，
      随之而来的是各种占位符匹配不上的降级分支。

    用 QTextObjectInterface 的好处:

    - 文档里是真正的 inline object，外观完全自绘：圆角胶囊 + 文件类型图标 + 文件名；
    - 底层只占 **一个字符**（U+FFFC），Backspace 整体删除、光标不会进入内部，
      引用在结构上不可能被拆坏；
    - :meth:`SendableTextEdit.toPlainText` 会把它展开回 ``[[basename]]``，
      因此对上层（命令检测、@ 检测、输入历史、发送文本构建、附件反向同步）完全透明。
    """

    _PAD_LEFT = 6
    _PAD_RIGHT = 6
    _ICON_SIZE = 13
    _GAP = 4
    _HEIGHT = 20
    _RADIUS = 6
    #: 文件名最大像素宽度，超出中间省略（保证胶囊不会宽到撑坏换行）
    _MAX_TEXT_WIDTH = 148

    # FluentIcon → QIcon 缓存（构造 QIcon 涉及 SVG 解析，绘制期反复调用太贵）
    _icon_cache: dict[str, object] = {}

    def intrinsicSize(self, doc, posInDocument, format) -> QSizeF:  # noqa: A002
        """胶囊尺寸（由文档布局在排版时查询）"""
        name, fm = self._name_and_metrics(doc, format)
        text_w = fm.horizontalAdvance(fm.elidedText(name, Qt.ElideMiddle, self._MAX_TEXT_WIDTH))
        width = self._PAD_LEFT + self._ICON_SIZE + self._GAP + text_w + self._PAD_RIGHT
        return QSizeF(width, self._HEIGHT)

    def drawObject(self, painter, rect, doc, posInDocument, format):  # noqa: A002
        """绘制胶囊（由文档布局在重绘时调用）"""
        Colors.refresh()
        name, fm = self._name_and_metrics(doc, format)
        path = (format.stringProperty(_FILE_MENTION_PATH_PROP) or "") if format else ""

        painter.save()
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setRenderHint(QPainter.TextAntialiasing, True)

            # 胶囊背景：在 rect 内垂直居中（rect 高度 = 行高，通常大于胶囊高度）
            h = min(self._HEIGHT, rect.height())
            top = rect.top() + (rect.height() - h) / 2
            pill = QRectF(rect.left(), top, rect.width(), h)

            painter.setPen(QPen(qcolor_from_token(Colors.BORDER), 1))
            painter.setBrush(QBrush(qcolor_from_token(Colors.TOOLBAR_BG)))
            painter.drawRoundedRect(pill, self._RADIUS, self._RADIUS)

            # 文件类型图标
            x = pill.left() + self._PAD_LEFT
            icon = self._icon_for(path)
            if icon is not None:
                pm = icon.pixmap(QSize(self._ICON_SIZE, self._ICON_SIZE))
                if not pm.isNull():
                    painter.drawPixmap(int(x), int(top + (h - self._ICON_SIZE) / 2), pm)
            x += self._ICON_SIZE + self._GAP

            # 文件名（中间省略）
            text = fm.elidedText(name, Qt.ElideMiddle, self._MAX_TEXT_WIDTH)
            text_rect = QRectF(x, pill.top(), max(0.0, pill.right() - self._PAD_RIGHT - x), h)
            painter.setFont(doc.defaultFont() if doc else QFont())
            painter.setPen(QPen(qcolor_from_token(Colors.INPUT_TEXT)))
            painter.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, text)
        finally:
            painter.restore()

    # ── 内部辅助 ──────────────────────────────

    @staticmethod
    def _name_and_metrics(doc, format) -> tuple[str, QFontMetrics]:
        """(文件名, 字体度量) —— 字体跟随文档，保证与正文一致"""
        path = (format.stringProperty(_FILE_MENTION_PATH_PROP) or "") if format else ""
        name = os.path.basename(path) or path or "?"
        font = doc.defaultFont() if doc else QFont()
        return name, QFontMetrics(font)

    @classmethod
    def _icon_for(cls, path: str):
        """按扩展名取图标（复用 AttachmentChip 的映射），失败返回 None"""
        try:
            icon_enum = AttachmentChip._get_file_icon(path)
        except Exception:  # noqa: BLE001
            return None
        key = str(icon_enum)
        if key not in cls._icon_cache:
            try:
                cls._icon_cache[key] = icon_enum.icon()
            except Exception:  # noqa: BLE001
                cls._icon_cache[key] = None
        return cls._icon_cache[key]


class AttachmentOverflowChip(QLabel):
    """附件数量溢出提示：附件过多时显示「+N」，不可删除

    附件栏没有滚动条，靠限制渲染数量控制高度。超出上限的附件仍然参与发送，
    只是不再单独渲染 chip。
    """

    _CHIP_HEIGHT = AttachmentChip._CHIP_HEIGHT

    def __init__(self, count: int, total: int, parent=None):
        super().__init__(parent)
        Colors.refresh()
        self.setFixedHeight(self._CHIP_HEIGHT)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet(
            f"color: {Colors.TEXT_SECONDARY}; {get_font_family_css()} {font_size_css(12)}"
            f" background: transparent; border: 1px dashed {Colors.BORDER};"
            f" border-radius: {self._CHIP_HEIGHT // 2}px; padding: 0 8px;"
        )
        self.setText(f"+{count}")
        self.setToolTip(f"还有 {count} 个附件未显示（共 {total} 个）\n全部附件都会随消息一起发送")
