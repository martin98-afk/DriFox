# -*- coding: utf-8 -*-
"""
MessageCard - 消息卡片组件

负责渲染和显示对话消息，支持：
- Markdown 内容渲染（使用 WebEngineView）
- 代码高亮（使用 Pygments）
- 工具调用结果显示
- 流式内容追加
- 用户/助手消息区分

消息结构：
- role: "user" | "assistant" | "system" | "tool"
- content: str | List[Dict]  # 支持多内容块
- tool_calls: List[Dict]     # 工具调用
- tool_call_id: str         # 工具结果关联 ID
"""

import base64
import concurrent.futures
import contextlib
import hashlib
import math
import os
import random
import time
import re
import sys
import threading
import urllib.parse
import weakref
from collections import OrderedDict
from datetime import datetime
from functools import lru_cache
from html import escape, unescape
from typing import Any, Dict, List, Optional

import orjson as json
import sip
from loguru import logger
from markdown import Markdown
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import TextLexer, get_lexer_by_name
from PyQt5.QtCore import (
    QByteArray,
    QEasingCurve,
    QElapsedTimer,
    QObject,
    QRect,
    QSize,
    QThread,
    Qt,
    QTimer,
    QTimerEvent,
    QUrl,
    QVariantAnimation,
    pyqtSignal,
)
from PyQt5.QtGui import (
    QBrush,
    QColor,
    QFontMetrics,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QTextDocument,
    QWheelEvent,
)
from PyQt5.QtSvg import QSvgRenderer
from PyQt5.QtWebEngineWidgets import QWebEnginePage, QWebEngineSettings, QWebEngineView
from PyQt5.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    MaskDialogBase,
    TransparentToolButton,
)
from qfluentwidgets.components.widgets.card_widget import (
    CardSeparator,
    SimpleCardWidget,
)
from qfluentwidgets.components.widgets.scroll_bar import SmoothScrollDelegate

from app.core import (
    append_text_block,
    content_to_markdown,
    content_to_text,
    ensure_content_blocks,
)
from app.core.conversation.message_content import make_tool_result_block
from app.core.infra.webengine_profile import get_shared_web_profile
from app.utils.design_tokens import (
    Animations,
    BorderRadius,
    Colors,
    _get_global_font,
    current_theme,
    fade_in_widget,
    font_size_css,
    get_unified_scrollbar_style,
    scale_font_size,
    scale_icon_size,
)

from app.utils.utils import get_font_family_css, get_icon
from app.widgets.custom_title_bar import CustomTabButton, TabHoverSyncHost, TabIndicatorController
from app.widgets.flow_layout import FlowLayout
from app.widgets.modules.message_bubble import MessageBubble, ensure_bubble_contrast
from app.widgets.render_helpers import (
    _format_natural_preview,
    _get_tool_cn_name,
    _get_tool_icon,
    _get_tool_icon_html,
    _reg_metadata_flag,
    get_tool_qrc_prefix,
    render_tool_block,
)
from app.utils.session_preview import format_relative_time
from app.widgets.simple_hover_tooltip import install_hover_tooltip

# 渲染管线与查看器已拆分至独立模块；此处 re-export 保持既有 import 路径兼容。
from app.widgets.card_render_core import (
    AUTO_SCROLL_THRESHOLD,
    CustomTabButton,
    FINISH_HEIGHT_ANIM_ENABLED,
    FINISH_HEIGHT_ANIM_MAX_USES,
    FINISH_HEIGHT_ANIM_MIN_DELTA,
    FINISH_HEIGHT_ANIM_MS,
    FINISH_HEIGHT_ANIM_WINDOW_S,
    FINISH_HEIGHT_TRACK_FACTOR,
    FlowLayout,
    MessageBubble,
    TabHoverSyncHost,
    TabIndicatorController,
    _CODE_FONT_SIZE,
    _FILE_EDIT_TOOLS_FALLBACK_TEXT,
    _MAX_CHART_PAYLOAD_B64,
    _MarkdownBlockViewerCls,
    _PLACEHOLDER_QSS,
    _QWIDGETSIZE_MAX,
    _RESIZE_GHOST_QSS,
    _STREAM_BAND_BOTTOM,
    _STREAM_BAND_H,
    _STREAM_BAND_MAX_DT_MS,
    _STREAM_BAND_REPAINT_PAD,
    _STREAM_BAND_SWEEP_MS,
    _STREAM_TINT_RETRY,
    STREAM_HEIGHT_ANIM_ENABLED,
    STREAM_HEIGHT_ANIM_MIN_DELTA,
    STREAM_HEIGHT_TICK_MS,
    STREAM_HEIGHT_TRACK_EPSILON,
    STREAM_HEIGHT_TRACK_FACTOR,
    _THINK_SNAKE_SVG,
    _classify_think_tag,
    _count_think_tool_prefix,
    _edit_tools,
    _extract_closed_segments,
    _extract_fenced_code,
    _extract_formulas,
    _format_elapsed,
    _format_natural_preview,
    _format_tool_progress_badge,
    _get_formatter_cached,
    _get_lexer_cached,
    _get_markdown_block_viewer_cls,
    _get_plugin_fence_renderer,
    _get_think_icon_html,
    _get_think_preview,
    _has_unclosed_chart_fence,
    _has_unclosed_registered_tag,
    _has_unclosed_think,
    _has_unclosed_think_or_tool,
    _inject_context_links,
    _inject_tag_cards,
    _inject_think_cards,
    _inject_tool_blocks,
    _iter_think_segments,
    _last_para_break_outside_fence,
    _plugin_fence_placeholder,
    _protect_inline_svg_blocks,
    _qt_renderer_enabled,
    _render_inline_tail,
    _render_markdown_to_html_cached_impl,
    _render_plugin_fence,
    _render_stable_segment,
    _render_think_block,
    _render_think_block_lightweight,
    _render_tool_block_content,
    _render_tool_streaming_block,
    _restore_fenced_code,
    _sanitize_incomplete_markdown,
    _tail_before_unclosed_block,
    _unwrap_code_blocks_with_context_links,
    _update_icon_prefix,
    _wrap_code_blocks_with_copy_button_web,
    clear_global_render_cache,
    ensure_bubble_contrast,
    format_relative_time,
    get_font_family_css,
    get_icon,
    get_markdown_instance,
    get_random_greeting,
    install_hover_tooltip,
    prewarm_markdown_block_viewer,
    render_tool_block,
    set_pygments_style,
)
from app.widgets.card_viewers import (
    CodeWebViewer,
    ConsoleMonitorPage,
    PlainTextViewer,
    _HEIGHT_CACHE_MAX,
    _ImagePreviewDialog,
    _PHYSICAL_TEXTURE_LIMIT,
    _decode_image_url_to_pixmap,
    _download_and_preview,
    _fence_assets_for_skeleton,
    _logical_height_cap,
    _show_image_preview,
    extract_image_data_uris,
    plan_image_attachment_sources,
)


class MessageCard(SimpleCardWidget):
    heightChanged = pyqtSignal(int)
    deleteRequested = pyqtSignal()
    undoRequested = pyqtSignal()
    actionRequested = pyqtSignal(str, str)
    contextActionRequested = pyqtSignal(str, str)
    optionSelected = pyqtSignal(dict)
    interventionRequested = pyqtSignal(dict)
    toolDiffRequested = pyqtSignal(str)  # tool_call_id
    subAgentLogRequested = pyqtSignal(str)  # task_ids (comma-separated)
    cardDiffRequested = pyqtSignal(int, int)  # round_index, message_index（消息在 _message_batch 中的索引）
    reviewRequested = pyqtSignal(int, int)  # round_index, message_index — 用户点击页脚 Review 按钮时触发
    branchRequested = pyqtSignal(int, int)  # round_index, message_index — 用户点击页脚「分支」按钮时触发
    saveFileRequested = pyqtSignal(str, str)  # code, lang
    lazyRenderCompleted = pyqtSignal()  # 懒渲染完成信号，用于通知滚动保持
    modelLabelClicked = pyqtSignal(str, str)  # model_name, config_id — 用户点击页脚模型标签时触发
    welcomeModeChanged = pyqtSignal(str)  # 欢迎卡片模式切换（sessions / projects / 插件注册 tab）
    saveChartPngRequested = pyqtSignal(str, str)  # (name_b64, png_b64) — 图表 PNG 导出（内部处理保存，不透传）

    # 流式实时吞吐采样：单段「出字间隔」计入上限（秒）。间隔超过它视为工具执行
    # / 请求等待，不计入生成秒 —— 工具循环里卡片不重建（start_elapsed_tracking
    # 不重置），若用「首字至今」当分母，10s 工具会把实时 tps 稀释到 1/10。
    _LIVE_GAP_CAP_S = 2.0

    def __init__(
        self,
        role: str,
        timestamp: str = None,
        parent=None,
        error: bool = False,
        reasoning_content: str = "",
        model_name: str = None,
        provider_name: str = None,
        config_id: str = None,
        identity: Optional[Any] = None,
        source_message: Optional[dict] = None,
    ):
        super().__init__(parent)
        self._parent = parent
        self.role = role
        self.model_name = model_name
        self.provider_name = provider_name
        self._provider_config_id = config_id  # UUID key in _valid_configs, for precise provider lookup
        # 消息发送者身份（MessageIdentity 实例；None = 由 _ensure_identity 按 role 解析）
        self._identity = identity
        self._identity_header = None  # IdentityHeader（懒建；开关关闭时保持 None）
        # 消息源数据（可选）：历史加载 / TeamMail 等场景由调用方注入，用于解析
        # 消息级身份（如从邮件内容取发送者名）。必须早于 _setup_ui 赋值——
        # 身份行在 __init__ 内构建，晚赋值会拿到未注入的源。
        self._source_message = source_message
        self.timestamp = timestamp or datetime.now().strftime("%m-%d %H:%M")
        # 历史数据 timestamp 格式为 %Y-%m-%d %H:%M:%S，转为 %m-%d %H:%M
        if self.timestamp and len(self.timestamp) >= 19:
            try:
                dt = datetime.strptime(self.timestamp[:19], "%Y-%m-%d %H:%M:%S")
                self.timestamp = dt.strftime("%m-%d %H:%M")
            except ValueError:
                self.timestamp = self.timestamp[:14]
        self.error = error
        self._interactive_options: List[dict] = []
        self._content_data: Any = [] if role == "assistant" else ""
        # 将 reasoning_content 转为 _content_data 的 reasoning block
        if role == "assistant" and reasoning_content:
            self._content_data.append({"type": "reasoning", "content": reasoning_content})
        self._streaming = False
        # 本轮对话是否已完成流式输出。用于区分"本轮已结束的消息"与
        # "从磁盘加载的历史会话"——两者 _streaming 都是 False，但产品诉求
        # 是前者工具区保持展开，后者折叠（避免长会话加载时信息过载）。
        # 缺失此标志时，本轮消息在虚拟滚动回收重建后会被误判为历史而突然折叠。
        self._streaming_finished = False
        self._retrying = False  # 重试模式标志
        # 任务列表快照（卡片底部内嵌 todo 区数据）：viewer 未创建/JS 未就绪时
        # 暂存，viewer 就绪后补推；骨架重载后据此恢复。
        self._todos_snapshot: Optional[list] = None
        self._retry_error_type = ""  # 重试错误类型
        self._retry_attempt = 0  # 当前重试次数
        self._retry_max = 15  # 最大重试次数
        self._retry_wait_time = 0.0  # 等待时间
        self._round_index: Optional[int] = None  # 用于卡片差异功能
        self._message_index: Optional[int] = None  # 用于卡片差异和撤销功能：消息在 session.messages 中的索引
        # 底部元信息栏（助手卡片）
        self._footer_bar: Optional[QWidget] = None
        self._footer_model_label: Optional[QLabel] = None
        self._footer_elapsed_label: Optional[QLabel] = None
        self._footer_diff_stats_label: Optional[QLabel] = None  # 差异胶囊内文本
        self._footer_diff_pill: Optional[QWidget] = None  # 差异胶囊容器（文本 + 🔍 同舱）
        # 左区成员表 [(分隔点或None, label)]：耗时 + 插件 stat，· 分隔动态管理
        self._footer_left_items: List[Tuple[Optional[QLabel], QLabel]] = []
        # 插件注册的页脚信息项（footer_stat 槽位）：{stat_id: QLabel}
        self._footer_stat_labels: Dict[str, QLabel] = {}
        # 耗时实时计时器
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.timeout.connect(self._update_elapsed_display)
        self._elapsed_start_time: Optional[float] = None
        self._anim_timer = QTimer(self)
        self._anim_timer.timeout.connect(self._update_anim)
        # 动画累积时间(ms)：光块位置与重试 spinner 都由它推导，帧率抖动不改变速度
        self._anim_t_ms = 0.0
        self._anim_clock = QElapsedTimer()
        self._anim_clock.start()
        self._grad_main, self._grad_inner = (QLinearGradient(0, 0, 1, 1) for _ in range(2))
        self._clip_inner = self._clip_border = QPainterPath()
        self._clip_inner_border = self._clip_border_region = QPainterPath()
        # 流式视觉绘制区域缓存（x/y/w/h）：assistant 跟随气泡矩形，其余整卡
        self._clip_x = self._clip_y = self._clip_w = self._clip_h = -1
        self._height_anim = QVariantAnimation(self)
        # 插值时长占位（见下：随即被 setDuration(0) 覆盖，实际长度由每轮动画
        # 自行设置 —— 结束态收敛用 FINISH_HEIGHT_ANIM_MS，其余为 0 禁用插值）
        self._height_anim.setDuration(Animations.EXIT_MS)
        self._height_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._height_anim.valueChanged.connect(self._apply_viewer_height)
        self._height_anim.stateChanged.connect(self._on_height_anim_state_changed)
        self._is_height_animating = False  # 动画期间抑制重复报告
        # 禁用 Python 端的动画，依赖 JS 动画控制高度
        self._height_anim.setDuration(0)  # 设置为0相当于禁用插值
        self._target_viewer_height = 40
        self._last_applied_viewer_height = 40
        # 结束态高度缓动窗口（monotonic 截止时刻；0 = 无窗口）+ 剩余可用次数。
        # 由 MessageCard.finish_streaming 打开，_update_height 用尽预算即失效。
        self._finish_height_anim_until = 0.0
        self._finish_height_anim_left = 0
        self._finish_height_anim_active = False
        # [T29] 流式高度追踪进行中标记。追踪期间 Chromium 侧 ResizeObserver 会
        # 不断送来"当前中间高度"，若照单全收会把追踪打断成锯齿（stop → 防抖 →
        # 重启）。故追踪期间一律吞掉上报，落定时由 tick 主动校正。
        self._stream_height_anim_active = False
        # 追踪节拍器：流式高度增长的连续化。目标值（_target_viewer_height）可
        # 随时被新上报更新，tick 每拍朝它按比例逼近 —— 天然 retarget，无动画
        # stop/start 抖动。parent=self：随卡片销毁。
        self._stream_height_tick = QTimer(self)
        self._stream_height_tick.setInterval(STREAM_HEIGHT_TICK_MS)
        self._stream_height_tick.timeout.connect(self._stream_height_tick_step)
        # [T30] 每拍逼近比例：流式 0.45；结束态 FINISH 窗口换成更缓的 0.28
        # （丝绸尾音，与页面内 CSS 过渡/FLIP 的 200~220ms 同量级收尾）。
        self._height_track_factor = STREAM_HEIGHT_TRACK_FACTOR
        # 结束态打点起点（0 = 无待结算的结束拍）
        self._finish_t0 = 0.0
        # 最近一次 viewer 高度增量（新值 - 旧值），供外层列表滚动锚定补偿读取
        self._last_height_delta = 0
        # 🆕 流式高度防抖：减少频繁 height report 导致的 viewer resize 抖动
        # [T29] 80 → 32ms：目标值进入追踪的频率。追踪 tick（30ms）已把"应用"
        # 侧的节拍连续化，防抖只负责合并同一窗口内的上报，不再需要独自扛
        # 削峰 —— 拉长只会平白增加"文字已出、目标未到"的滞后。
        self._stream_height_timer = QTimer(self)
        self._stream_height_timer.setSingleShot(True)
        self._stream_height_timer.setInterval(32)
        self._stream_height_timer.timeout.connect(self._apply_debounced_height)
        self._debounced_target_height = 40
        self._theme = self._build_theme(role, error)
        self._base_bg = self._theme["bg"]
        self._base_border = self._theme["border"]
        # 性能优化：缓存上次宽度值，避免不必要的更新
        self._last_synced_width = 0
        # [L3] 宽度 → 内容高度缓存：resize 命中历史宽度时同步套用已知高度，
        # 省掉一次 JS 异步往返（窗口拖拽时宽度来回往返，命中率很高）。
        # 它只是**预测**：命中后仍会发起一次异步上报校正，不会锁死错误高度。
        self._height_cache: Dict[int, int] = {}
        self._resize_preview_mode = False
        self._resize_preview_height = 0
        # T42 占位守恒标记：批次重建时 _apply_placeholder_height 会临时把卡片
        # 钉死在占位高度（min=max=H）；viewer 真实高度到达时解除（见
        # _unpin_layout_height）。False = 高度由内容自适应。
        self._layout_height_pinned = False
        self._options_were_visible_before_resize = False
        # [PERF] preview 期间 viewer height 目标值累积。_apply_viewer_height 在
        # preview 模式只写此字段，不真正 setFixedHeight（避免 Chromium 级联
        # relayout）。set_resize_preview_mode(False) 退出时一次性应用。
        self._pending_viewer_height: Optional[int] = None
        # ── 空白守卫（blank-guard）──
        # 背景：简洁模式流式中「偶尔」工具/折叠框下方出现大段空白 = viewer 高度
        # （Qt 落地值）大于 JS 侧实际内容高。根因：_apply_debounced_height 收拢
        # 方向 <40px 直接丢弃（防抖动设计），多工具依次完成时每次"运行框→折叠行"
        # 缩 ~24px 全被吞 → 累积虚高，正文静默期（纯工具执行阶段）暴露为空白。
        # 修复：小收缩改延迟落地（_shrink_timer 500ms 稳定窗）。
        # _blank_guard_* 为二道防线（gap 探针取证），默认不启用单独开关。
        self._shrink_timer: Optional[QTimer] = None
        self._pending_shrink_height: Optional[int] = None
        self._blank_guard_timer: Optional[QTimer] = None
        self._blank_guard_rounds = 0
        # WebEngine 上下文恢复标志
        self._webengine_needs_restore = False
        # 懒渲染标志：未进入可视区域前不创建QWebEngine
        self._lazy_rendered = False
        # 标记：内容刚加载到viewer，首次heightChanged后滚动并清除
        self._content_just_loaded = False
        self._finished_streaming_ids: set = set()  # 防止 streaming 状态回退
        # 欢迎卡片模式数据（set_welcome_content 时填充；切换 mode 不重建 QWebEngineView）
        self._welcome_mode: str = ""
        # 首次渲染时固定的问候语，软刷新（其他标签页会话变更广播）复用，
        # 避免欢迎卡片内容无谓跳变（仅会话列表应静默更新）。
        self._welcome_greeting: str = ""
        self._welcome_recent: list = []
        self._welcome_top: list = []
        # 欢迎 tab 条：宿主容器 + 按钮列表 + 滑动指示器控制器（见 _build_welcome_mode_tabs）
        self._welcome_tab_host: Optional[QWidget] = None
        self._welcome_tab_buttons: list = []
        self._welcome_tab_ids: list = []
        self._welcome_indicator_ctl: Optional[TabIndicatorController] = None
        self._welcome_tabs_bar_layout: Optional["FlowLayout"] = None
        self._pending_welcome_md: Optional[str] = None  # viewer 懒渲染前的等待内容
        # 异步刷新事件回调引用（destroyed 退订时按 is 匹配）
        self._welcome_refresh_cb: Optional[Callable[[Dict[str, Any]], None]] = None
        # 窗口上下文提供者（多窗口隔离）：欢迎卡片渲染插件 tab 时调用，注入
        # 当前窗口的 project_root / project_name / window_id，避免插件回读全局
        # 状态导致多标签页内容串项目（create_welcome_card 传入 window._build_ui_context）
        self._welcome_ctx_provider: Optional[Callable[[], Dict[str, Any]]] = None
        # 工具参数首次到达跟踪：每个 tool_call_id 第一次 update_tool_streaming 时
        # 触发"标记当前思考块为完成"，避免 reasoning→tool_call 切换时思考块残留"思考中"
        self._tool_args_first_seen_ids: set = set()
        # 🆕 Bug B（顺序错乱修复）：工具结果插入锚点 + 启动序号。
        # 同一卡片内"思考/工具/正文"必须按实际流式到达顺序交错，
        # 而不是思考恒顶部、工具恒底部。锚点 = 工具调用发生时 _content_data 的长度，
        # append_tool_result 时 insert(锚点) 而非 append，工具结果插回调用发生的位置。
        self._tool_insert_anchors: Dict[str, int] = {}  # tool_call_id → 工具调用时流末尾位置
        # 🆕 Bug B 方案 F（数据层稳定锚点）：_tool_insert_anchors 的 int 索引在
        # 其他工具结果插入 / 思考块追加后**失效**（列表偏移），导致 finish 完整重渲染
        # 时 _content_data 顺序错乱（"思考在前、工具在后"）。改为记录**块引用**：
        # 引用在列表增删中保持稳定，index(ref)+1 恒等于"工具调用时刻的逻辑末尾"。
        self._tool_anchor_refs: Dict[str, Any] = {}  # tool_call_id → 调用时 _content_data[-1] 块引用
        self._tool_call_order: Dict[str, int] = {}  # tool_call_id → 递增启动序号（同锚点工具按调用序）
        # 🆕 Bug B：当前活动思考块。append_reasoning 只追加到它（避免合并进已完成的
        # 旧思考块导致多轮思考堆积）；工具调用/新块开始时置 None / 覆盖。
        self._active_thinking_block: Optional[dict] = None
        self._pending_content: Optional[str] = None
        self._reasoning_total_len = 0  # reasoning 内容总长度计数器，避免每次遍历
        self._viewer_container = QWidget(self)
        self._viewer_layout = QVBoxLayout(self._viewer_container)
        self._viewer_layout.setContentsMargins(0, 0, 0, 0)
        self._setup_ui()
        # 订阅欢迎卡片插件 tab 异步刷新事件（通用机制，与具体 mode_key 解耦）；
        # fetcher / 数据源完成时插件通过 UIEventBus 通知，订阅者对当前 mode 重渲染。
        # 仅 welcome 角色启用订阅，其他角色不必白订阅。
        if role == "welcome":
            self._subscribe_welcome_tab_refresh()

    def _build_theme(self, role: str, error: bool = False) -> Dict[str, str]:
        Colors.refresh()

        # 获取当前窗口透明度（OpacitySlider 控制），用于调整卡片背景色 alpha
        try:
            win = self.window()
            if win is not None:
                _win_opacity = win.windowOpacity()
            else:
                _win_opacity = 1.0
        except Exception:
            _win_opacity = 1.0

        themes = {
            "assistant": {
                "avatar": "AI",
                "title": "Drifox",
                "subtitle": "Assistant",
                "bg": Colors.ASSISTANT_CARD_BG,
                "border": "none",
                "accent": Colors.ASSISTANT_CARD_ACCENT,
                "text": Colors.ASSISTANT_CARD_TEXT,
                "muted": Colors.ASSISTANT_CARD_MUTED,
            },
            "welcome": {
                "avatar": "DX",
                "title": "Drifox",
                "subtitle": "AI Copilot",
                "bg": Colors.ASSISTANT_CARD_BG,
                "border": "none",
                "accent": Colors.ASSISTANT_CARD_ACCENT,
                "text": Colors.ASSISTANT_CARD_TEXT,
                "muted": Colors.ASSISTANT_CARD_MUTED,
            },
            "user": {
                "avatar": "User",
                "title": "User",
                "subtitle": "Prompt",
                "bg": Colors.USER_CARD_BG,
                "border": "none",
                "accent": Colors.USER_CARD_ACCENT,
                "text": Colors.USER_CARD_TEXT,
                "muted": Colors.USER_CARD_MUTED,
            },
        }
        theme = dict(themes.get(role, themes["assistant"]))

        # 按窗口透明度调整背景色 alpha
        if _win_opacity < 1.0 and theme["bg"].startswith("rgba("):
            import re

            m = re.match(r"rgba\((\d+),\s*(\d+),\s*(\d+),\s*(\d+)\)", theme["bg"])
            if m:
                r, g, b, a = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
                new_a = max(0, min(255, int(a * _win_opacity)))
                theme["bg"] = f"rgba({r}, {g}, {b}, {new_a})"

        # 气泡可读性保障（双策略）：assistant 贴背景极简（低饱和微偏移），
        # user 保留主题色相、明度过近时拉开。welcome 不参与。
        if role in ("user", "assistant"):
            theme["bg"] = ensure_bubble_contrast(theme["bg"], Colors.CONTENT_BG, role)

        if error:
            # 检测深浅色模式，选择合适的错误配色
            try:
                from app.utils.theme_manager import theme_manager

                _is_light = theme_manager.is_light_theme()
            except Exception:
                _is_light = False
            if _is_light:
                theme["bg"] = "#FFF5F5"  # 浅粉底
                theme["border"] = "#FCA5A5"  # 浅红边框
                theme["accent"] = "#DC2626"  # 深红强调
            else:
                theme["bg"] = "#2A1F1F"  # 暗红褐底
                theme["border"] = "#A94444"  # 暗红边框
                theme["accent"] = "#FF7B7B"  # 亮红强调
        return theme

    def refresh_theme(self):
        """刷新主题颜色，响应全局主题切换"""
        # 🐛 清空 LRU 渲染缓存 + 骨架 HTML 缓存，强制下次渲染使用新主题颜色。
        # 否则 _render_markdown_to_html_cached 的 @lru_cache 会返回旧主题的 HTML
        # （旧 pygments 代码高亮 + 旧图标路径），导致代码块颜色与背景混淆而"消失"。
        clear_global_render_cache()
        # 同步全局性能缓存（图标前缀和字号），确保下次渲染使用新主题
        _update_icon_prefix()
        global _CODE_FONT_SIZE
        _CODE_FONT_SIZE = scale_font_size(13)
        # 刷新主题颜色
        self._theme = self._build_theme(self.role, self.error)
        self._base_bg = self._theme["bg"]
        self._base_border = self._theme["border"]
        self._apply_card_style()
        # 更新头像
        if hasattr(self, "_av_label"):
            self._av_label.setStyleSheet(self._build_avatar_style())
        # 更新标题
        if hasattr(self, "_name_label"):
            font_css = get_font_family_css()
            self._name_label.setStyleSheet(
                f"{font_css} font-size:{scale_font_size(14)}px;color:{self._theme['text']};font-weight:700;"
            )
        # 更新副标题
        if hasattr(self, "_subtitle_label"):
            font_css = get_font_family_css()
            self._subtitle_label.setStyleSheet(
                f"{font_css} font-size:{scale_font_size(11)}px;color:{self._theme['muted']};font-weight:500;letter-spacing:0.02em;"
            )
        # 更新时间戳
        if hasattr(self, "_ts_label"):
            if self.role == "user":
                # 简洁气泡：无胶囊背景的弱化小字
                self._ts_label.setStyleSheet(
                    f"{get_font_family_css()} font-size: {scale_font_size(11)}px; color: {self._theme['muted']};"
                )
            else:
                self._ts_label.setStyleSheet(
                    f"""
                    QLabel {{
                        {get_font_family_css()} font-size: {scale_font_size(11)}px;
                        color: {self._theme["muted"]};
                        background: {Colors.CONTENT_BG};
                        border: 1px solid {Colors.BORDER};
                        border-radius: 9px;
                        padding: 2px 8px;
                    }}
                    """
                )
        # 刷新身份行名称颜色（跟随新主题）
        if getattr(self, "_identity_header", None) is not None:
            try:
                self._identity_header.apply_text_color(self._theme["muted"])
            except RuntimeError:
                self._identity_header = None
        # 刷新 viewer 主题（注入 CSS 变量 + 失效实例渲染缓存）
        # ⚠️ 顺序必须在 _refresh_viewer_font() 之前：主题变化时先让
        # refresh_theme 清掉 _cached_streaming_html 等实例缓存并注入新 CSS
        # 变量，随后 _refresh_viewer_font 触发的重渲染才会使用新主题 HTML。
        if hasattr(self, "viewer") and self.viewer and hasattr(self.viewer, "refresh_theme"):
            self.viewer.refresh_theme()
        # 刷新富文本视图字体并触发重渲染（缓存已在 refresh_theme 中失效）
        if hasattr(self, "viewer") and self.viewer and hasattr(self.viewer, "_refresh_viewer_font"):
            self.viewer._refresh_viewer_font()
        # 欢迎 tab 条：文字/hover/选中底色都由 CustomTabButton 实时取 Colors token，
        # refresh_style 重算 QSS 即可；胶囊（_TabIndicator）配色每次 paint 实时读，
        # 补一次 update 触发重绘。
        for _btn in self._welcome_tab_buttons:
            try:
                _btn.refresh_style()
            except RuntimeError:
                continue
        if self._welcome_indicator_ctl is not None:
            try:
                self._welcome_indicator_ctl.indicator.update()
            except RuntimeError:
                self._welcome_indicator_ctl = None

    # ── 卡片背景色覆盖（替代 qfluentwidgets CardWidget 的固定白色覆盖层）──
    # 背景色完全由 _apply_card_style() 通过 CSS 控制，无需动态解析

    def _normalBackgroundColor(self):
        """返回透明色，让 CSS background-color 透出"""
        from PyQt5.QtGui import QColor

        return QColor(0, 0, 0, 0)

    def _hoverBackgroundColor(self):
        """返回透明色，让 CSS background-color 透出"""
        return QColor(0, 0, 0, 0)

    def _pressedBackgroundColor(self):
        """返回透明色，让 CSS background-color 透出"""
        return QColor(0, 0, 0, 0)

    def _get_footer_model_text(self) -> str:
        """根据 model_name 生成页脚显示文本（服务商名已隐藏，仅显示模型名）"""
        return self.model_name or ""

    def set_model_name(self, model_name: str, provider_name: str = None, config_id: str = None):
        """设置模型名称显示（用于助手卡片）

        Args:
            model_name: 模型名称
            provider_name: 服务商显示名（可选）
            config_id: 服务商配置 UUID（可选，用于精确导航到对应配置）
        """
        if self.role != "assistant":
            return
        if not model_name:
            return
        self.model_name = model_name
        if provider_name is not None:
            self.provider_name = provider_name
        if config_id is not None:
            self._provider_config_id = config_id
        footer_text = self._get_footer_model_text()
        if hasattr(self, "_ts_label"):
            self._ts_label.setText(model_name)
            self._ts_label.setVisible(True)
            self._ts_label.setStyleSheet(
                f"""
                QLabel {{
                    {get_font_family_css()} font-size: {scale_font_size(11)}px;
                    color: {self._theme["muted"]};
                    background: {Colors.CONTENT_BG};
                    border: 1px solid {Colors.BORDER};
                    border-radius: 9px;
                    padding: 2px 8px;
                }}
                """
            )
        # 同步到底部元信息栏
        if self._footer_model_label:
            self._footer_model_label.setText(footer_text)
            self._footer_model_label.setVisible(True)
            self._refresh_footer_separators()

    def _build_footer_bar(self, main: QVBoxLayout):
        """构建助手卡片底部极简元信息栏

        布局：左侧纯文本（耗时 + 插件注册信息项），右侧全可点击
        （模型胶囊 / 差异胶囊含内嵌 Review，分支+复制 hover 浮现 + 插件按钮）。
        token 总量与吞吐量的展示已移除，由插件经 footer_stat 槽位注入。
        """
        bar = QWidget(self)
        self._footer_bar = bar
        bar.setStyleSheet("background: transparent;")
        layout = QHBoxLayout(bar)
        # 水平 8 让两端对称，垂直 0 配合统一字号后整体更紧凑
        layout.setContentsMargins(8, 0, 8, 0)
        layout.setSpacing(0)

        font_css = get_font_family_css()
        # 统一所有 footer 元素字号为 10px；信息区 muted 降噪（accent 留给正文强调）
        label_style = (
            f"{font_css} font-size: {scale_font_size(10)}px; "
            f"color: {self._theme['muted']}; font-weight: 400; padding: 0px; margin: 0px;"
        )
        # 插件 stat 的基准样式：配色时只在它后面追加 color，保证与耗时 label
        # 同字号、同内边距（两者并排，样式来源必须一致才不会错行）
        self._footer_stat_base_style = label_style

        # 耗时
        elapsed_l = QLabel("", self)
        elapsed_l.setStyleSheet(label_style)
        elapsed_l.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        elapsed_l.setVisible(False)
        self._footer_elapsed_label = elapsed_l
        layout.addWidget(elapsed_l)
        self._footer_left_items.append((None, elapsed_l))

        # 插件注册信息项（footer_stat 槽位）：耗时右侧依次排布，· 分隔动态管理
        footer_stats = []
        try:
            from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

            footer_stats = UIPluginRegistry.get_instance().get_footer_stats()
        except Exception:
            footer_stats = []
        for info in footer_stats:
            sep = QLabel("·", self)
            sep.setStyleSheet(label_style)
            sep.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
            sep.setVisible(False)
            stat_l = QLabel("", self)
            stat_l.setStyleSheet(label_style)
            stat_l.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
            stat_l.setVisible(False)
            self._footer_stat_labels[info.stat_id] = stat_l
            layout.addWidget(sep)
            layout.addWidget(stat_l)
            self._footer_left_items.append((sep, stat_l))

        # Review 图标（内嵌差异胶囊右端；点击触发 code-reviewer 子智能体）
        # ★ 胶囊存在时常显，不与 hover 组一起隐现。
        icon_size = scale_font_size(10)
        review_icon = QLabel(self)
        review_icon.setObjectName("footer_review_icon")
        review_icon.setPixmap(get_icon("Search").pixmap(icon_size, icon_size))
        review_icon.setFixedSize(icon_size + 4, icon_size + 4)
        review_icon.setScaledContents(True)
        review_icon.setStyleSheet(
            "QLabel {"
            " background: transparent; padding: 1px; margin: 0px;"
            # 父级差异胶囊用 QWidget 选择器设了 1px 实线边框，类型选择器会级联到
            # 子 QLabel；不显式清掉就会在放大镜外露出一圈方框
            " border: none; border-radius: 3px;"
            " }"
            "QLabel:hover { background: rgba(128,128,128,0.18); border: none; }"
        )
        review_icon.setAlignment(Qt.AlignCenter)
        review_icon.setCursor(Qt.PointingHandCursor)
        review_icon.mousePressEvent = lambda e: self._emit_review_requested()
        install_hover_tooltip(review_icon, "用 code-reviewer 子智能体快速审查本次修改")
        self._footer_review_icon = review_icon

        # 弹性分隔：左侧纯文本信息区 | 右侧可点击区（胶囊/按钮）
        layout.addStretch()

        # 模型胶囊（可点击跳目标配置；仅显示模型名，服务商名隐藏但保留用于跳转）
        footer_text = self._get_footer_model_text()
        model_l = QLabel(footer_text, self)
        model_l.setStyleSheet(
            f"{font_css} font-size: {scale_font_size(10)}px; color: {self._theme['muted']};"
            f" border: 1px solid {Colors.BORDER}; border-radius: 8px; padding: 1px 8px;"
        )
        model_l.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        model_l.setVisible(bool(footer_text))
        model_l.setCursor(Qt.PointingHandCursor)
        model_l.mousePressEvent = lambda e: self._on_footer_model_clicked(e)
        install_hover_tooltip(model_l, "点击切换到目标模型配置")
        self._footer_model_label = model_l
        layout.addWidget(model_l)

        # 差异胶囊：文本（点击弹差异弹窗）+ 🔍（点击触发 Review）同舱，有 diff 才显示
        diff_pill = QWidget(self)
        diff_pill.setAttribute(Qt.WA_StyledBackground, True)  # 纯 QWidget 让 QSS border 生效
        diff_pill.setStyleSheet(
            f"QWidget {{ background: transparent; border: 1px solid {Colors.BORDER};"
            f" border-radius: 8px; margin-left: 4px; }}"
        )
        dp = QHBoxLayout(diff_pill)
        dp.setContentsMargins(8, 0, 2, 0)
        dp.setSpacing(2)
        diff_l = QLabel("", diff_pill)
        diff_l.setStyleSheet(
            f"{font_css} font-size: {scale_font_size(10)}px; color: {self._theme['muted']};"
            f" background: transparent; border: none; padding: 1px 0px; margin: 0px;"
        )
        diff_l.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        diff_l.setVisible(False)
        diff_l.setCursor(Qt.PointingHandCursor)
        diff_l.mousePressEvent = lambda e: self._emit_card_diff_requested()
        install_hover_tooltip(diff_l, "点击查看当条消息的文件差异详情")
        self._footer_diff_stats_label = diff_l
        dp.addWidget(diff_l)
        dp.addWidget(review_icon, 0, Qt.AlignVCenter)
        diff_pill.setVisible(False)
        self._footer_diff_pill = diff_pill
        layout.addWidget(diff_pill)

        # 右侧操作区：hover 浮现组（复制 / 分支 / 插件按钮）。
        # 固定尺寸占位：按钮显隐切换时 footer 尺寸不变，卡片不跳动、不重排。
        hover_btns = QWidget(self)
        self._assistant_action_btns = hover_btns
        hb = QHBoxLayout(hover_btns)
        hb.setContentsMargins(0, 0, 0, 0)
        hb.setSpacing(2)
        for ic, tp, cb in [
            (get_icon("分支"), "从此条分支新对话", lambda: self._emit_branch_requested()),
            (get_icon("复制"), "复制", lambda: self.actionRequested.emit(self.get_plain_text(), "copy")),
        ]:
            b = TransparentToolButton(ic, self)
            b.setToolTip(tp)
            b.clicked.connect(cb)
            b.setFixedSize(20, 20)  # 弱化处理：比原顶部按钮 32px 更小
            install_hover_tooltip(b, delay_ms=200)
            hb.addWidget(b)
        # 插件注册按钮（footer_action 槽位）：与内置按钮同排同风格。
        # 只渲染 assistant/both 角色（user 角色走用户气泡底部操作行）。
        footer_actions = []
        try:
            from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

            footer_actions = [
                a
                for a in UIPluginRegistry.get_instance().get_footer_actions()
                if getattr(a, "role", "assistant") in ("assistant", "both")
            ]
        except Exception:
            footer_actions = []
        for info in footer_actions:
            try:
                from PyQt5.QtGui import QIcon

                from app.utils.theme_manager import theme_manager

                try:
                    is_light = theme_manager.is_light_theme()
                except Exception:
                    is_light = False
                path = info.icon_light_path if (is_light and info.icon_light_path) else info.icon_path
                b = TransparentToolButton(QIcon(str(path)) if path else QIcon(), self)
                if info.tooltip:
                    b.setToolTip(info.tooltip)
                    install_hover_tooltip(b, delay_ms=200)
                b.setFixedSize(20, 20)
                b.clicked.connect(lambda _c=False, _info=info: self._on_footer_plugin_action(_info))
                hb.addWidget(b)
            except Exception as e:
                logger.warning(f"[MessageCard] 页脚插件按钮 {getattr(info, 'action_id', '?')} 构建失败: {e}")
        # 容器整体显隐（assistant 卡片为定宽布局，显隐只引起 footer 内部横移，
        # 不会撑宽卡片）。非 hover 时容器退出布局 = 不占宽度；行高恒定改由
        # bar.setFixedHeight 保证（见下），否则 Qt 布局跳过隐藏控件的 sizeHint，
        # 行高在文本高度（≈14px）与按钮高度（20px）间跳变 → 卡片高度抖动。
        hover_btns.setVisible(False)
        layout.addWidget(hover_btns)

        # 行高下界 = 按钮高度：非 hover 时按钮容器退出布局，行高会由文本元素
        # （耗时 15px / 模型胶囊 19px）决定 → hover 时被 20px 按钮抬高，卡片轻微
        # 抖动。约束 bar 最小高度为按钮高度，两种状态下行高恒定。
        # 用 setMinimumHeight 而非 setFixedHeight：内容更高时仍可撑开，不裁剪。
        bar.setMinimumHeight(hb.sizeHint().height())

        main.addWidget(bar)

    def set_meta_info(self, elapsed: float = None, token_usage: dict = None):
        """设置助手卡片的元信息（耗时；token 总量与速度展示已移除，由插件经 footer_stat 注入）

        Args:
            elapsed: 响应耗时（秒），如 3.2。传入后停止实时计时。
            token_usage: 如 {"input": 1234, "output": 567, "total": 1801}，透传给 provider 自行取舍
        """
        if self.role != "assistant":
            return
        # 耗时
        if elapsed is not None and self._footer_elapsed_label:
            self._elapsed_timer.stop()
            self._elapsed_start_time = None
            try:
                self._footer_elapsed_label.setText(f"{_format_elapsed(elapsed)}")
                self._footer_elapsed_label.setVisible(True)
            except RuntimeError:
                # 🛡️ 防御：footer label 可能已被 C++ 侧销毁（deleteLater 排队中），
                # 访问已删除 QLabel 会抛 wrapped C/C++ object ... has been deleted。
                # 静默忽略（项目既有风格参考 _safe_report_height / L2465 先例）。
                pass
        # Token 总量与速度展示已移除（footer_stat 槽位由插件注入）
        # ⚠️ elapsed=None 是中间态调用（流式期间 _refresh_context_usage_indicator
        # 每 500ms 只带 token_usage 刷圆环）：不得清 live 累计、不得刷 stat ——
        # 否则平均分支拿不到落定值会把 stat 藏掉，与 1s tick 的实时值交替 → 闪烁。
        if elapsed is not None:
            # 轮次结束，清掉流式采样缓冲
            self._stream_text_acc = ""
            self._stream_gen_s = 0.0
            self._stream_last_text_t = None
            # 插件信息项按落定态刷新（token_usage 透传给 provider 自行取舍）
            self._refresh_footer_stats(streaming=False, elapsed=elapsed, token_usage=token_usage)
            # 单次补刷：历史会话加载 / 投影晚到的兜底（旧版 1s+2.5s 二连发是为等
            # collector 投影；现在会话均值由插件累加表即时算出，1s 兜底足够）
            self._schedule_stat_refresh(1000, elapsed, token_usage)

    def _schedule_stat_refresh(self, delay_ms: int, elapsed, token_usage) -> None:
        """延迟补刷页脚插件 stat（绑定卡片生命周期）。

        ⚠️ 用绑定卡片生命周期的 QTimer 而非裸 singleShot（L14086 P050 同因：
        延迟窗口内卡片销毁后回调访问已释放控件）。
        """
        if not self._footer_stat_labels:
            return
        timer = QTimer(self)
        timer.setSingleShot(True)

        def _run() -> None:
            try:
                self._refresh_footer_stats(streaming=False, elapsed=elapsed, token_usage=token_usage)
            finally:
                timer.deleteLater()

        timer.timeout.connect(_run)
        timer.start(delay_ms)

    def set_diff_stats(self, files_count: int = 0, additions: int = 0, deletions: int = 0):
        """设置差异徽章：N 文件 +N/-N（点击弹出差异弹窗）

        Args:
            files_count: 修改的文件数
            additions: 新增行数
            deletions: 删除行数
        """
        if self.role != "assistant":
            return
        if not self._footer_diff_stats_label:
            return
        if files_count == 0 and additions == 0 and deletions == 0:
            # 无 diff：整舱隐藏（文本 + 内嵌 Review 同舱联动）
            if self._footer_diff_pill:
                self._footer_diff_pill.setVisible(False)
            return

        muted = self._theme.get("muted", "#888888")
        html = f'<span style="color:{muted};">{files_count} 文件</span>'

        add_del = []
        if additions > 0:
            add_del.append('<span style="color:#2ea043;">+{}</span>'.format(additions))
        if deletions > 0:
            add_del.append('<span style="color:#f85149;">-{}</span>'.format(deletions))
        if add_del:
            html += "&nbsp;" + "/".join(add_del)

        self._footer_diff_stats_label.setText(html)
        self._footer_diff_stats_label.setTextFormat(Qt.RichText)
        self._footer_diff_stats_label.setVisible(True)

        # 整舱显示（含内嵌 Review 图标）
        if self._footer_diff_pill:
            self._footer_diff_pill.setVisible(True)

    def add_diff_stats(self, files_count: int = 0, additions: int = 0, deletions: int = 0, seen_files: set = None):
        """增量累加差异统计（工具执行时实时调用，文件级去重避免多次编辑同一文件重复计数）

        Args:
            files_count: 本次新增的文件数
            additions: 本次新增的行数
            deletions: 本次删除的行数
            seen_files: 本次操作涉及的文件路径集合（用于去重）
        """
        if self.role != "assistant":
            return
        if not self._footer_diff_stats_label:
            return

        # 懒初始化累积计数器
        if not hasattr(self, "_diff_seen_files"):
            self._diff_seen_files = set()
        if not hasattr(self, "_diff_files_total"):
            self._diff_files_total = 0
        if not hasattr(self, "_diff_additions_total"):
            self._diff_additions_total = 0
        if not hasattr(self, "_diff_deletions_total"):
            self._diff_deletions_total = 0

        if seen_files:
            new_files = seen_files - self._diff_seen_files
            self._diff_seen_files.update(seen_files)
        else:
            new_files = set()

        self._diff_files_total += len(new_files) if seen_files else files_count
        self._diff_additions_total += additions
        self._diff_deletions_total += deletions

        self.set_diff_stats(
            files_count=self._diff_files_total,
            additions=self._diff_additions_total,
            deletions=self._diff_deletions_total,
        )

    def _on_footer_model_clicked(self, event):
        """用户点击页脚模型标签时，发出 modelLabelClicked(model_name, config_id)"""
        if self.model_name:
            self.modelLabelClicked.emit(
                self.model_name,
                getattr(self, "_provider_config_id", "") or "",
            )

    def _refresh_footer_separators(self):
        """左区 · 分隔点可见性：前段有可见成员且当前成员非空才显示（比 isVisible 更可靠）"""
        try:
            seen_visible = False
            for sep, label in getattr(self, "_footer_left_items", []):
                label_ok = bool(label and label.text())
                if sep is not None:
                    sep.setVisible(seen_visible and label_ok)
                seen_visible = seen_visible or label_ok
        except RuntimeError:
            # 🛡️ 防御：footer label / separator 可能已被 C++ 侧销毁（deleteLater 排队中），
            # 访问已删除 QLabel 会抛 wrapped C/C++ object ... has been deleted。静默忽略。
            pass

    def _resolve_footer_host(self):
        """沿父链查找宿主窗口（带 _window_id 的祖先），插件回调 context 用"""
        p = self.parent()
        while p is not None:
            if getattr(p, "_window_id", None):
                return p
            p = p.parent()
        return None

    def _footer_stat_context(
        self,
        streaming: bool = False,
        elapsed: float = None,
        token_usage: dict = None,
    ) -> Dict[str, Any]:
        """组装 footer_stat / footer_action 回调 context（口径见 FooterStatInfo）"""
        host = self._resolve_footer_host()
        ctx: Dict[str, Any] = {
            "window_id": getattr(host, "_window_id", "") if host is not None else "",
            "main_widget": host,
            "card": self,
            "role": self.role,
            "model_name": self.model_name,
            "round_index": self._round_index,
            "message_index": self._message_index,
            "elapsed": elapsed,
            "token_usage": token_usage,
            "streaming": streaming,
        }
        if streaming:
            # 流式实时采样：只给「本流累计原文」+「首字至今秒数」，token 估算与
            # 吞吐量口径交给 provider（插件侧走 tiktoken/cl100k，中文约 1.2
            # token/字）。主程序不再用 chars÷4 粗估（中文低估约 4~5 倍）。
            ctx["live_text"] = getattr(self, "_stream_text_acc", "") or ""
            # 生成秒 = 累计出字时间（已排除工具执行 / 长等待段）
            ctx["live_gen_s"] = max(0.0, float(getattr(self, "_stream_gen_s", 0.0) or 0.0))
        return ctx

    def _refresh_footer_stats(
        self, streaming: bool = False, elapsed: float = None, token_usage: dict = None
    ):
        """回调全部 footer_stat provider 并刷新对应 label（主线程节拍调用）"""
        if self.role != "assistant" or not self._footer_stat_labels:
            return
        try:
            from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

            infos = {i.stat_id: i for i in UIPluginRegistry.get_instance().get_footer_stats()}
        except Exception:
            return
        ctx = self._footer_stat_context(streaming=streaming, elapsed=elapsed, token_usage=token_usage)
        for stat_id, label in self._footer_stat_labels.items():
            try:
                info = infos.get(stat_id)
                text = ""
                if info is not None:
                    val = None
                    try:
                        val = info.provider(ctx)
                    except Exception as e:
                        logger.warning(f"[MessageCard] footer_stat {stat_id} provider 失败: {e}")
                    if val:
                        text = str(val.get("text") or "")
                        color = val.get("color")
                        tip = val.get("tooltip")
                        # ⚠️ 纯文本 + QSS 上色，不用 RichText：富文本 QLabel 的基线
                        # 与 sizeHint 和耗时 label（纯文本）不同，两者并排会垂直错位；
                        # 顺带避免插件文本里的 < & 被当标记解析。
                        label.setTextFormat(Qt.PlainText)
                        if label.text() != text:
                            label.setText(text)
                        if getattr(label, "_stat_color", None) != color:
                            label._stat_color = color
                            base = getattr(self, "_footer_stat_base_style", "")
                            label.setStyleSheet(f"{base} color: {color};" if color else base)
                        if tip:
                            install_hover_tooltip(label, str(tip))
                if not text:
                    # provider 返回 None / 插件已注销 → 隐藏（分隔点联动收敛）
                    if label.text():
                        label.setText("")
                        label.setVisible(False)
                    continue
                label.setVisible(True)
            except RuntimeError:
                # 🛡️ label 可能已被 C++ 侧销毁（deleteLater 排队中），跳过该成员
                continue
        self._refresh_footer_separators()

    def _on_footer_plugin_action(self, info):
        """页脚插件按钮点击 → 派发 on_click(context)"""
        cb = getattr(info, "on_click", None)
        if cb is None:
            return
        try:
            cb(self._footer_stat_context())
        except Exception as e:
            logger.warning(f"[MessageCard] footer_action {getattr(info, 'action_id', '?')} 回调失败: {e}")

    def start_elapsed_tracking(self):
        """开始实时计时（流式输出时调用）"""
        if self.role != "assistant":
            return
        if not self._footer_elapsed_label:
            return
        self._elapsed_start_time = time.time()
        # 流式采样状态：update_content 累计原文与出字时间，首个内容 chunk 记时刻
        self._stream_text_acc = ""
        self._stream_gen_s = 0.0
        self._stream_last_text_t = None
        self._stream_first_text_t = None
        self._footer_elapsed_label.setText(f"{_format_elapsed(0)}")
        self._footer_elapsed_label.setVisible(True)
        self._refresh_footer_stats(streaming=True)
        self._elapsed_timer.start(1000)  # 每秒更新

    def _update_elapsed_display(self):
        """实时更新耗时显示 + 插件页脚信息项（流式跟随刷新）"""
        if self._elapsed_start_time is None:
            self._elapsed_timer.stop()
            return
        # [V1] 可见性门控：隐藏 tab 跳过 label setText（空转），
        # elapsed 基于 _elapsed_start_time 绝对时间戳计算，恢复后数值依然准确。
        if not self.isVisible():
            return
        elapsed = time.time() - self._elapsed_start_time
        self._footer_elapsed_label.setText(f"{_format_elapsed(elapsed)}")
        # 插件信息项流式跟随（live 估算数据在 context 里，口径由 provider 定）
        self._refresh_footer_stats(streaming=True, elapsed=elapsed)

    def _build_avatar_style(self):
        font_css = get_font_family_css()
        if self.role in ("welcome", "assistant"):
            return ""
        # 头像直径 30px：首字母字号取 16px（直径的 ~53%），
        # 与项目卡片 _SquareAvatar(14/24≈58%) 比例接近，避免字符过小看不清。
        return f"""
            QLabel {{
                {font_css} font-size: {scale_font_size(16)}px;
                color: #FFFFFF;
                font-weight: 700;
                background: {self._theme["accent"]};
                border: 1px solid rgba(255,255,255,0.12);
                border-radius: 15px;
                padding: 0px;
            }}
        """

    # ========== 欢迎卡片 mode 切换（自绘胶囊 tabs）==========
    # 内置项仅保留消息卡片核心（会话列表）。其余 tab 由插件通过
    # ``UIPluginRegistry.register_welcome_tab`` 动态注入（如 更新），
    # 卸载/禁用对应插件后该 tab 自动消失，无需主程序介入。
    # 标签文字统一剥离前导 emoji（见 _strip_label_emoji），与顶栏 / 工作台页签
    # 共用「纯文字胶囊」视觉语言；插件 label 字面量无需改动。
    _WELCOME_MODE_ITEMS = [
        ("sessions", "会话"),
    ]

    #: 卡片内 tab 文字基准字号：比顶栏（13）略小，与卡片内分区标题（12）同档
    _WELCOME_TAB_FONT = 12

    @staticmethod
    def _strip_label_emoji(label: str) -> str:
        """剥离标签前导 emoji / 符号及分隔空白

        插件 label 常见形如 ``"🤖 助手"`` / ``"📜 更新"``。彩色 emoji 与卡片内
        线性图标体系混排显脏，这里统一在渲染层清洗：插件契约与字面量不动，
        主程序单方面决定呈现方式，后续新增 tab 自动受益。
        """
        s = (label or "").strip()
        stripped = re.sub(r"^[^\w\u4e00-\u9fff]+", "", s, flags=re.UNICODE).strip()
        return stripped or s

    def _welcome_tab_specs(self) -> list:
        """当前欢迎 tab 规格：[(mode_key, 显示文本), ...]

        内置项在前，插件注册项按注册序追加（与旧 SegmentedWidget 顺序一致）。
        """
        specs = [(key, self._strip_label_emoji(label)) for key, label in self._WELCOME_MODE_ITEMS]
        try:
            from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

            for key, info in UIPluginRegistry.get_instance().get_welcome_tabs().items():
                specs.append((key, self._strip_label_emoji(info.label)))
        except Exception:
            pass
        return specs

    def _build_welcome_mode_tabs(self, parent_layout):
        """构建欢迎 tab 条（welcome 角色专属）：卡片底部独立一行

        与顶栏 / 工作台页签共用同一套组件（``CustomTabButton`` +
        ``TabIndicatorController`` 滑动胶囊）：未选中透明底、hover 前景色 6%、
        选中前景色 14% 底 + 文字提亮加粗。FlowLayout 负责自动折行，其
        minimumWidth 只取最宽单个子项，不会把卡片撑宽。
        """
        host = TabHoverSyncHost(self)
        self._welcome_tab_host = host
        host.setStyleSheet("background: transparent;")
        # 高度策略：FlowLayout 的 heightForWidth 已是真实折行高度，但 Qt5 在
        # 「子布局带 heightForWidth」的子 widget 上会用 **minimumWidth** 估高
        # （本项目 P010 记录过 PyQt5 不派发 Python 侧 sizeHint override）——
        # 6 个 tab 会被当成 3 行 = 90px，实际 1 行只需 30px，多出的就是卡片
        # 底部空白。这里不依赖 Qt 估高：布局跑完后按实测宽度主动设高
        # （见 _sync_welcome_tab_host_height）。
        _sp = QSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        host.setSizePolicy(_sp)
        # margins：上留白 4，左右 4（居中时对称即可）
        bar = FlowLayout(host, spacing=2, alignment=Qt.AlignHCenter, margins=(4, 4, 4, 0))
        self._welcome_tabs_bar_layout = bar
        parent_layout.addWidget(host)

        # 滑动指示器：构造必须早于任何按钮加入，天然垫在按钮之下
        self._welcome_indicator_ctl = TabIndicatorController(
            host,
            self,
            self._welcome_tab_active_geometry,
        )
        self._rebuild_welcome_tab_buttons(current_mode=self._welcome_mode or None)

    def _sync_welcome_tab_host_height(self) -> None:
        """按 FlowLayout 在**当前实测宽度**下的折行结果给宿主设高

        窗口 resize 导致 tab 重新折行时必须重调，否则高度停在旧行数
        （多一行会被裁、少一行留空白）。
        """
        host = self._welcome_tab_host
        bar = self._welcome_tabs_bar_layout
        if host is None or bar is None:
            return
        width = host.width()
        if width <= 0:
            return
        try:
            h = bar.heightForWidth(width)
            if h > 0 and host.height() != h:
                host.setFixedHeight(h)
        except RuntimeError:
            self._welcome_tab_host = None
            self._welcome_tabs_bar_layout = None

    def _welcome_tab_active_geometry(self):
        """当前激活 tab 按钮的几何（无激活项 / 控件已销毁返回 None）"""
        try:
            idx = self._welcome_tab_ids.index(self._welcome_mode)
        except ValueError:
            return None
        if not (0 <= idx < len(self._welcome_tab_buttons)):
            return None
        try:
            return self._welcome_tab_buttons[idx].geometry()
        except RuntimeError:
            return None

    def _rebuild_welcome_tab_buttons(self, current_mode: Optional[str] = None) -> None:
        """按当前注册表重建 tab 按钮（重建后同步高亮 + 胶囊钉位）"""
        bar = self._welcome_tabs_bar_layout
        if self._welcome_tab_host is None or bar is None:
            return
        while bar.count():
            item = bar.takeAt(0)
            w = item.widget() if item is not None else None
            if w is not None:
                # 先断父子再预约删除：仅 deleteLater 在事件循环繁忙期会留残影
                # （对齐工作台页签重建的同款教训）
                w.setParent(None)
                w.hide()
                w.deleteLater()
        self._welcome_tab_buttons = []
        self._welcome_tab_ids = []

        for mode_key, label in self._welcome_tab_specs():
            btn = CustomTabButton(
                mode_key,
                label,
                self._welcome_tab_host,
                indicator_managed=True,
                font_size=self._WELCOME_TAB_FONT,
            )
            btn.clicked.connect(self._on_welcome_mode_tab_clicked)
            bar.addWidget(btn)
            self._welcome_tab_buttons.append(btn)
            self._welcome_tab_ids.append(mode_key)

        target = current_mode if current_mode in self._welcome_tab_ids else None
        if target is None and self._welcome_tab_ids:
            # 仅在尚未确定 mode（首次构建）时回落首项；已有 mode 但对应 tab 消失
            # （插件卸载）时不改写，交给上层失效重建决定去向
            if not self._welcome_mode:
                self._welcome_mode = self._welcome_tab_ids[0]
            target = self._welcome_mode if self._welcome_mode in self._welcome_tab_ids else None
        for i, btn in enumerate(self._welcome_tab_buttons):
            btn.set_active(self._welcome_tab_ids[i] == target)
        self._apply_welcome_tabs_font()
        self._sync_welcome_tab_host_height()
        self._schedule_welcome_indicator_snap()

    def _schedule_welcome_indicator_snap(self) -> None:
        """延迟一拍把指示器钉到激活 tab（本帧布局尚未收敛，读到的几何是旧值）

        ⚠️ 用绑定 card 生命周期的 QTimer 而非裸 ``QTimer.singleShot(0, ...)``：
        延迟窗口内卡片被销毁（会话切换 / 标签页关闭）时 singleShot 仍会回调，
        对已释放控件取几何 → ACCESS_VIOLATION（同 _defer_emit 的 P050 根因）。
        """
        ctl = self._welcome_indicator_ctl
        if ctl is None:
            return
        timer = QTimer(self)
        timer.setSingleShot(True)

        def _run() -> None:
            try:
                self._snap_welcome_indicator()
            finally:
                timer.deleteLater()

        timer.timeout.connect(_run)
        timer.start(0)

    def _snap_welcome_indicator(self) -> None:
        ctl = self._welcome_indicator_ctl
        if ctl is None:
            return
        try:
            ctl.snap_to_active()
        except RuntimeError:
            self._welcome_indicator_ctl = None

    def _apply_welcome_tabs_font(self):
        """欢迎 tabs 适配系统字号

        CustomTabButton 的文字样式由 ``_apply_label_color`` 写死 font-size，
        走 ``apply_font_size_to_widget`` 的 setFont 覆盖不到；这里显式按当前
        delta 重设（首次构建 + _apply_runtime_ui_settings 字体块都会调用）。
        """
        fs = scale_font_size(self._WELCOME_TAB_FONT)
        for btn in self._welcome_tab_buttons:
            try:
                btn.set_font_size(fs)
            except RuntimeError:
                continue

    def _sync_welcome_tab_active(self, mode: str, animate: bool = False) -> None:
        """把 tab 高亮 + 滑动胶囊对齐到 mode（控件已销毁时静默跳过）

        ``animate=True`` 仅用于用户点击（胶囊滑过去）；其余路径用 False
        （布局平移 / 静态刷新场景下瞬移，避免动画被自己的副作用打断）。
        """
        if mode not in self._welcome_tab_ids:
            return
        for i, btn in enumerate(self._welcome_tab_buttons):
            try:
                btn.set_active(self._welcome_tab_ids[i] == mode)
            except RuntimeError:
                continue
        ctl = self._welcome_indicator_ctl
        if ctl is None:
            return
        try:
            geom = self._welcome_tab_active_geometry()
            if geom is not None:
                ctl.move_to(geom, animate=animate)
        except RuntimeError:
            self._welcome_indicator_ctl = None

    def _on_welcome_mode_tab_clicked(self, mode: str):
        """tab 点击：滑动胶囊 + 切 mode + 重渲染 body（不重建 QWebEngineView）"""
        if mode == self._welcome_mode:
            return
        # 先落 mode：``_sync_welcome_tab_active`` 经 ``_welcome_tab_active_geometry``
        # 按 self._welcome_mode 定位目标按钮，顺序反了会读到旧项几何 → 胶囊不动。
        self._welcome_mode = mode
        self._sync_welcome_tab_active(mode, animate=True)
        # sync_tab=False：重渲染不能把刚起的滑动动画瞬移掉
        self.set_welcome_mode(mode, sync_tab=False)
        self.welcomeModeChanged.emit(mode)

    def _get_welcome_window_context(self) -> dict:
        """获取当前窗口的 UI 上下文（注入插件 render_func 用）

        多窗口隔离：每张欢迎卡片持有自己窗口的 context provider（创建时由
        create_welcome_card 传入 window._build_ui_context），渲染插件 tab 时
        读到的 project_root / project_name 属于**本窗口**，不会因标签页切换
        或全局配置变更而串成其他窗口的项目。
        """
        if self._welcome_ctx_provider is not None:
            try:
                ctx = self._welcome_ctx_provider()
                if isinstance(ctx, dict):
                    return ctx
            except Exception:
                pass
        return {}

    def set_welcome_mode(self, mode: str, *, sync_tab: bool = True):
        """切换欢迎卡片模式（同步 active tab + 重渲染 body）

        所有 mode 统一走 ``_render_welcome_body`` 分发（内置 sessions /
        插件注册 tab），插件 fetcher 完成后通过 UIEventBus 通知本卡片
        再次调 ``set_welcome_mode`` 强制重渲染当前 mode（见 _subscribe_welcome_refresh）。

        Args:
            sync_tab: False 时不动 tab 高亮（点击路径已自行同步，且需要保留
                滑动动画，不能在这里用 animate=False 把它盖掉）。
        """
        self._welcome_mode = mode
        # 静态刷新路径（插件数据到达 / 外部改 mode）不经点击处理，高亮与胶囊
        # 必须在这里收敛，否则 tab 会停在旧项上。
        if sync_tab:
            self._sync_welcome_tab_active(mode, animate=False)
        body_html = _render_welcome_body(
            mode,
            self._welcome_recent,
            self._welcome_top,
            self._get_welcome_window_context(),
        )
        self._render_welcome_with_body(body_html)

    # ── 欢迎 tab 异步刷新事件订阅（通用机制，零业务字面量）──────────────
    def _subscribe_welcome_tab_refresh(self) -> None:
        """订阅 ``EV_WELCOME_TAB_REFRESHED``：插件 fetcher / 数据源完成时通知卡片重渲。

        仅匹配当前 ``self._welcome_mode`` 的 mode_key（payload 由插件声明），
        不匹配则忽略。widget 销毁时自动退订，防止悬挂 callback。
        """
        from app.core.infra.ui_event_bus import EV_WELCOME_TAB_REFRESHED, UIEventBus

        def _on_refresh(payload):
            mode_key = payload.get("mode_key")
            if not mode_key or mode_key != self._welcome_mode:
                return
            # 绕过 _on_welcome_mode_tab_clicked 的等值短路，强制重渲染当前 mode
            self.set_welcome_mode(self._welcome_mode)

        self._welcome_refresh_cb = _on_refresh
        UIEventBus.get_instance().subscribe(EV_WELCOME_TAB_REFRESHED, _on_refresh)
        # destroyed signal 在 widget 销毁时 emit，释放 UIEventBus 中的悬挂 callback
        self.destroyed.connect(lambda: UIEventBus.get_instance().unsubscribe(EV_WELCOME_TAB_REFRESHED, _on_refresh))

    def _render_welcome_with_body(self, body_html: str):
        """统一的 body 渲染入口：拼接 greeting + 写入 viewer（markdown 路径）

        软刷新（refresh_welcome_data）也经此入口，复用首次渲染的固定问候语，
        避免其他标签页会话变更广播到本窗口时欢迎卡片问候语无谓跳变。
        """
        greeting = self._welcome_greeting or get_random_greeting()
        welcome_md = f"### 👋 {greeting}\n\n{body_html}\n"
        if self.viewer is not None and self._lazy_rendered:
            self.set_content(welcome_md)
        else:
            self._pending_welcome_md = welcome_md

    def set_welcome_content(
        self,
        recent_sessions: list,
        top_by_count: list,
        mode: str = "sessions",
        context_provider: Optional[Callable[[], Dict[str, Any]]] = None,
    ) -> None:
        """一次性设置欢迎卡片数据 + 初始 mode（被 create_welcome_card 调用）

        Args:
            recent_sessions: 最近会话列表
            top_by_count: 最活跃会话列表
            mode: 初始欢迎模式（sessions / 插件注册 tab）
            context_provider: 窗口上下文提供者（无参回调 → dict）。多窗口隔离：
                渲染插件 tab 时注入当前窗口的 project_root / project_name /
                window_id，避免插件回读全局状态导致跨标签页内容串项目。
        """
        self._welcome_ctx_provider = context_provider
        self._welcome_recent = list(recent_sessions or [])
        self._welcome_top = list(top_by_count or [])
        self._welcome_mode = mode
        # 初始 mode 由 resolve_initial_welcome_mode 解析（可能是插件 tab），
        # 与已建好的按钮集合对齐高亮；mode 不在集合内时安装点已回落首项
        self._sync_welcome_tab_active(mode, animate=False)
        self._schedule_welcome_indicator_snap()
        body_html = _render_welcome_body(
            mode,
            self._welcome_recent,
            self._welcome_top,
            self._get_welcome_window_context(),
        )
        greeting = get_random_greeting()
        self._welcome_greeting = greeting
        self._pending_welcome_md = f"### 👋 {greeting}\n\n{body_html}\n"

    def refresh_welcome_data(self, recent_sessions: list, top_by_count: list) -> None:
        """会话数据变更后的轻量刷新：更新列表数据并重渲染 body（保留卡片实例）。

        与 set_welcome_content 的区别：set_welcome_content 只写
        _pending_welcome_md（懒渲染消费），已渲染的卡片调用后 UI 不更新；
        本方法在卡片已渲染时直接重渲染 DOM，避免调用方走「销毁缓存卡片 +
        重建 QWebEngineView」路径（100-500ms 主线程占用 + 视觉闪烁）。

        仅 sessions 类 body 展示会话列表，插件 tab 不依赖该数据，
        跳过重渲染（插件 tab 的 render_func 也不应因会话变更被反复调用）。
        """
        old_recent, old_top = self._welcome_recent, self._welcome_top
        new_recent = list(recent_sessions or [])
        new_top = list(top_by_count or [])
        # 数据无变化时跳过重渲染：其他标签页对话完成广播到本窗口时，
        # 若新会话不在本窗口当前项目下（按项目过滤），recent/top 完全不变，
        # 重渲染会白播一遍 stagger fade-in 动画。
        if new_recent == old_recent and new_top == old_top:
            return
        self._welcome_recent = new_recent
        self._welcome_top = new_top
        if self._welcome_mode != "sessions":
            return
        # 软刷新：数据更新导致的重渲染，抑制 session-item 进入动画（仅首次
        # 进入播放），避免其他标签页对话完成广播到本窗口时所有列表项重播
        # stagger fade-in（见 _render_sessions_body / _render_item 的 suppress_anim）。
        body_html = _render_welcome_body(
            self._welcome_mode,
            self._welcome_recent,
            self._welcome_top,
            self._get_welcome_window_context(),
            suppress_anim=True,
        )
        # 增量替换优先：可见窗口只换 #welcome-sessions-root 的 innerHTML，
        # 问候语/欢迎 tab 栏原地保留，零整页重排闪动；viewer 未就绪或窗口
        # 不可见回退整页渲染（后台窗口由 viewer 门控 deferred，切回补渲）。
        if not self._refresh_welcome_body_incremental(body_html):
            self._render_welcome_with_body(body_html)

    def _refresh_welcome_body_incremental(self, body_html: str) -> bool:
        """增量替换欢迎卡片 sessions body DOM（不整页重渲染）。

        背景：软刷新（refresh_welcome_data）旧实现走 set_content 整页替换
        #content-placeholder 的 innerHTML，问候语/欢迎 tab 栏连带重建，重排
        闪动可见（其他标签页会话结束广播到本窗口场景）。本方法只替换
        #welcome-sessions-root（_render_sessions_body 包根输出）的 innerHTML，
        greeting 与 tab 栏原地保留。

        Returns:
            True = 已增量替换，或窗口不可见无需立即替换（数据已更新到
            _welcome_recent/_welcome_top，下次整页渲染自然生效）；
            False = viewer 未就绪，调用方应回退整页渲染。
        """
        try:
            viewer = getattr(self, "viewer", None)
        except RuntimeError:
            # stub（__new__ 绕过 __init__）实例：sip 未初始化，getattr 即抛错
            return False
        if viewer is None or not getattr(self, "_lazy_rendered", False):
            return False
        if not viewer.isVisible():
            # 后台 tab：与 viewer 可见性门控同语义，切回时整页补渲拿新数据
            return False
        try:
            page = viewer.page()
            if page is None or not getattr(viewer, "_is_js_ready", False):
                return False
            payload = json.dumps(body_html).decode("utf-8")
            page.runJavaScript(
                f"var _r=document.getElementById('welcome-sessions-root');if(_r){{_r.innerHTML={payload};}}"
            )
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[_refresh_welcome_body_incremental] failed: {e}")
            return False

    def _ensure_identity(self):
        """解析并缓存本条消息的身份（消息自带快照优先，否则走解析链）。

        会话上下文从当前窗口取（session_id / 团队成员角色名 / window_id），
        取不到时回落默认身份——渲染路径绝不因身份解析失败而中断。
        """
        if getattr(self, "_identity", None) is not None:
            return self._identity
        try:
            from app.core.infra.message_identity import resolve_for_message

            session_id = ""
            team_agent = ""
            window_id = ""
            host = self._parent
            if host is not None:
                window_id = getattr(host, "_window_id", "") or ""
                team_agent = getattr(host, "_team_agent_name", "") or ""
                session_mgr = getattr(host, "session_manager", None)
                if session_mgr is not None:
                    session = session_mgr.get_current_session()
                    session_id = getattr(session, "session_id", "") or ""
            role = "user" if self.role == "user" else "assistant"
            self._identity = resolve_for_message(
                self._source_message,
                role,
                session_id=session_id,
                team_agent=team_agent,
                window_id=window_id,
            )
        except Exception:
            from app.core.infra.message_identity import MessageIdentity

            self._identity = MessageIdentity(name="Drifox" if self.role != "user" else "")
        return self._identity

    def refresh_identity(self) -> None:
        """强制重解析身份并刷新已构建的身份行（无变化时零成本跳过）。

        背景：assistant 占位卡在发送同步段创建（早于后台 PreSendWorker 的
        PreUserMessage hook），身份行此时解析会把「@助手切换前」的旧身份
        固化在卡片上——表现为回答内容/工具档位已是新助手、身份行仍是主
        助手（2026-09-22 输入框历史回填发送场景）。流式开始时 hook 必已
        完成（build_messages 后才有首 chunk），此时重解析即拿到最新身份。
        """
        try:
            from app.core.infra.message_identity import resolve_for_message

            session_id = ""
            team_agent = ""
            window_id = ""
            host = self._parent
            if host is not None:
                window_id = getattr(host, "_window_id", "") or ""
                team_agent = getattr(host, "_team_agent_name", "") or ""
                session_mgr = getattr(host, "session_manager", None)
                if session_mgr is not None:
                    session = session_mgr.get_current_session()
                    session_id = getattr(session, "session_id", "") or ""
            role = "user" if self.role == "user" else "assistant"
            identity = resolve_for_message(
                self._source_message,
                role,
                session_id=session_id,
                team_agent=team_agent,
                window_id=window_id,
            )
            changed = identity != self._identity
            self._identity = identity
            if changed and self._identity_header is not None:
                self._identity_header.set_identity(identity)
        except Exception:
            pass

    def _identity_enabled(self) -> bool:
        """身份行显示开关（设置项，默认开；取配置失败时视为开启）"""
        try:
            from app.utils.config import Settings

            return bool(Settings.get_instance().ui_message_identity.value)
        except Exception:
            return True

    def _build_identity_header(self, parent, align_right: bool):
        """构建身份行控件（两行：名称 + 时间）；开关关闭或不适用时返回 None。"""
        if not self._identity_enabled():
            return None
        try:
            from app.widgets.modules.identity_header import IdentityHeader

            header = IdentityHeader(
                self._ensure_identity(),
                align_right=align_right,
                parent=parent,
                timestamp=self.timestamp or "",
            )
            header.apply_text_color(self._theme["muted"])
            self._identity_header = header
            return header
        except Exception:
            return None

    def _build_card_header(self, main: QVBoxLayout):
        """头部：头像 + 名称/副标题 + 时间戳/模型名 + 顶部操作按钮 + 分隔线

        仅 welcome 卡片使用；assistant 全减模式无头部（见 _setup_ui）；
        user 卡片为简洁气泡（见 _setup_user_bubble）。
        """
        if self.role == "assistant":
            # 身份行：头像 + 显示名（助手在左）。开关关闭时保持原有「全减模式」。
            header = self._build_identity_header(parent=self, align_right=False)
            if header is not None:
                main.addWidget(header)
            return
        if self.role == "welcome":
            # 欢迎卡片：无头部（不画头像行 / 标题 / 分隔线），首屏直接从问候语开始。
            # 仍建 label 引用占位以兼容 hasattr 守卫（refresh_theme 等），但不显示。
            nm_l = QLabel(self._theme["title"], self)
            self._name_label = nm_l
            nm_l.setVisible(False)
            sub_l = QLabel(self._theme["subtitle"], self)
            self._subtitle_label = sub_l
            sub_l.setVisible(False)
            return
        top = QHBoxLayout()
        top.setContentsMargins(4, 0, 4, 0)
        top.setSpacing(6)

        av = QLabel(self)
        self._av_label = av
        if self.role in ("welcome", "assistant"):
            # 品牌图标头像
            av_icon = get_icon("drifox")
            pixmap = av_icon.pixmap(28, 28)
            av.setPixmap(pixmap)
            av.setFixedSize(30, 30)
            av.setAlignment(Qt.AlignCenter)
        else:
            av_icon = get_icon("用户")
            pixmap = av_icon.pixmap(28, 28)
            av.setPixmap(pixmap)
            av.setFixedSize(30, 30)
            av.setAlignment(Qt.AlignCenter)

        font_css = get_font_family_css()
        top.addWidget(av)
        # assistant / user 路径：title_wrap + 模型名/时间戳 + 顶部操作按钮
        title_wrap = QWidget(self)
        title_layout = QVBoxLayout(title_wrap)
        title_layout.setContentsMargins(0, 0, 0, 0)
        title_layout.setSpacing(1)

        nm_l = QLabel(self._theme["title"], self)
        self._name_label = nm_l
        nm_l.setStyleSheet(
            f"{font_css} font-size:{scale_font_size(14)}px;color:{self._theme['text']};font-weight:700;"
        )
        sub_l = QLabel(self._theme["subtitle"], self)
        self._subtitle_label = sub_l
        sub_l.setStyleSheet(
            f"{font_css} font-size:{scale_font_size(11)}px;color:{self._theme['muted']};font-weight:500;letter-spacing:0.02em;"
        )
        title_layout.addWidget(nm_l)
        title_layout.addWidget(sub_l)
        top.addWidget(title_wrap)
        # 助手卡片显示模型名称
        label_text = self.model_name if (self.role == "assistant" and self.model_name) else self.timestamp
        ts = QLabel(label_text, self)
        self._ts_label = ts
        ts.setVisible(bool(label_text))
        ts.setStyleSheet(
            f"""
                QLabel {{
                    {get_font_family_css()} font-size: {scale_font_size(11)}px;
                    color: {self._theme["muted"]};
                    background: {Colors.CONTENT_BG};
                    border: 1px solid {Colors.BORDER};
                    border-radius: 9px;
                    padding: 2px 8px;
                }}
                """
        )
        top.addWidget(ts)
        top.addStretch()

        # 顶部操作按钮
        btns = QWidget(self)
        bl = QHBoxLayout(btns)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(4)
        if self.role == "assistant":
            specs = [
                (
                    get_icon("复制"),
                    "复制",
                    lambda: self.actionRequested.emit(self.get_plain_text(), "copy"),
                ),
            ]
        else:
            specs = []
        for ic, tp, cb in specs:
            b = TransparentToolButton(ic, self)
            b.setToolTip(tp)
            b.clicked.connect(cb)
            b.setFixedSize(32, 32)
            install_hover_tooltip(b, delay_ms=200)
            bl.addWidget(b)
        if specs:
            top.addWidget(btns)
        main.addLayout(top)
        main.addWidget(CardSeparator(self))

    def _setup_user_bubble(self, main: QVBoxLayout):
        """用户消息简洁气泡：纯文本 + 底部 hover 操作行（主流大模型式）

        - 无头像 / "User·Prompt" 标题 / 分隔线
        - 复制/撤销/删除按钮 hover 浮现（见 enterEvent/leaveEvent），时间戳常显弱化
        - 宽度自适应见 PlainTextViewer._update_height（idealWidth 收缩，不占满整行）
        """
        # 修 #2：用户气泡 PlainTextViewer 改为懒加载——首次 set_content / showEvent 时才
        # 创建，避免每张卡片 __init__ 即构造 QTextEdit+QTextDocument+QTimer+QWidget 子树
        # （参考 assistant/welcome 卡片的懒渲染模式，复用 _lazy_rendered 守卫）。
        self.viewer = None
        self._viewer_pending_text = None

        # 身份行（气泡**外**上方，右对齐）：头像 + 名称 + 时间
        # 与气泡同宽同侧：加进 bubble_lay 会随气泡收缩，视觉上贴合气泡右缘。
        _header = self._build_identity_header(parent=self, align_right=True)
        self._identity_owner = self
        if _header is not None:
            main.addWidget(_header, 0, Qt.AlignRight)

        # 气泡容器：背景色/圆角只在这一层（身份行与底部操作行在容器外，
        # 不随气泡底色渲染）。视图与图片条在内。
        #
        # ⚠️ 垂直策略必须是 Maximum：默认 Preferred 会被父级布局拉伸，
        # 而中间层（viewer_container → PlainTextViewer）的 sizeHint 与实测尺寸
        # 不一致时，多出的空间全落在气泡上 → 气泡与下方按钮栏脱节、
        # 图片条看起来"漏出"气泡（2026-09-16 用户反馈的三连问题）。
        # Maximum = 取 sizeHint 上限，不额外膨胀。
        # 自绘气泡（圆角 + 右上引脚指向头像）：背景由 paintEvent 画，不走样式表，
        # 避免样式表 QWidget 选择器污染后代控件（viewer/正文视图）
        self._user_bubble = MessageBubble("right", self)
        self._user_bubble.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Maximum)
        bubble_lay = QVBoxLayout(self._user_bubble)
        bubble_lay.setContentsMargins(0, 0, 0, 0)
        bubble_lay.setSpacing(0)
        # 右对齐：卡片宽度由「气泡」与「footer」中的较大者决定。窄气泡时卡片会
        # 比气泡宽（多出的部分透明），footer 的时间戳与按钮才有地方放，而不必
        # 反过来把气泡撑宽。
        main.addWidget(self._user_bubble, 0, Qt.AlignRight)
        _bubble_alive = True

        # 正文视图容器挂到气泡内（原 _viewer_container 直接挂卡片）
        self._viewer_container.setParent(self._user_bubble)
        bubble_lay.addWidget(self._viewer_container)
        self._lazy_rendered = True

        # 图片附件预览条：挂 main 布局，位于气泡与底部按钮行**之间**（气泡外），
        # set_image_attachments 时才显示（懒占位）
        self._image_strip = QWidget(self)
        self._image_strip_lay = QHBoxLayout(self._image_strip)
        self._image_strip_lay.setContentsMargins(2, 4, 2, 2)
        self._image_strip_lay.setSpacing(6)
        self._image_strip.setVisible(False)
        main.addWidget(self._image_strip, 0, Qt.AlignRight)

        # 底部操作行（纯按钮）：时间戳已移到身份行第二行（见 IdentityHeader）
        # 外层 wrap 固定高度：按钮显隐切换时 footer 占位不变，卡片不跳动
        footer_wrap = QWidget(self)
        footer_wrap.setStyleSheet("background: transparent;")
        footer_wrap.setFixedHeight(28)  # 26px 按钮 + 垂直余量，紧凑
        footer = QHBoxLayout(footer_wrap)
        footer.setContentsMargins(6, 0, 6, 0)
        footer.setSpacing(6)
        footer.addStretch()

        # 按钮 hover 浮现在右端（卡片右缘对齐，与身份行头像侧一致）
        btns = QWidget(self)
        self._user_action_btns = btns
        bl = QHBoxLayout(btns)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(2)
        # 插件注册按钮（footer_action role=user/both）：位于内置按钮之前（左侧），
        # 同排同风格，hover 随容器整体浮现；点击复用 _on_footer_plugin_action。
        plugin_actions = []
        try:
            from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

            plugin_actions = [
                a
                for a in UIPluginRegistry.get_instance().get_footer_actions()
                if getattr(a, "role", "assistant") in ("user", "both")
            ]
        except Exception:
            plugin_actions = []
        for info in plugin_actions:
            try:
                from PyQt5.QtGui import QIcon

                from app.utils.theme_manager import theme_manager

                try:
                    is_light = theme_manager.is_light_theme()
                except Exception:
                    is_light = False
                path = (
                    info.icon_light_path if (is_light and info.icon_light_path) else info.icon_path
                )
                b = TransparentToolButton(QIcon(str(path)) if path else QIcon(), self)
                if info.tooltip:
                    b.setToolTip(info.tooltip)
                    install_hover_tooltip(b, delay_ms=200)
                b.setFixedSize(26, 26)  # 与内置按钮同尺寸（弱化处理）
                b.clicked.connect(lambda _c=False, _info=info: self._on_footer_plugin_action(_info))
                bl.addWidget(b)
            except Exception as e:
                logger.warning(
                    f"[MessageCard] 用户按钮栏插件按钮 {getattr(info, 'action_id', '?')} 构建失败: {e}"
                )
        for ic, tp, cb in [
            (get_icon("复制"), "复制", lambda: self._copy_user_message()),
            (get_icon("撤销"), "撤销到这里", self.undoRequested.emit),
            (get_icon("删除"), "删除", self.deleteRequested.emit),
        ]:
            b = TransparentToolButton(ic, self)
            b.setToolTip(tp)
            b.clicked.connect(cb)
            b.setFixedSize(26, 26)  # 弱化处理：比助手卡 32px 更小
            install_hover_tooltip(b, delay_ms=200)
            bl.addWidget(b)

        # 固定尺寸 = 全部按钮都显示时的尺寸。这样 hover 显隐子按钮时容器尺寸
        # 恒定、布局不重排（否则宽度 0↔82 来回变，表现为「hover 撑大气泡」）。
        btns.setFixedSize(bl.sizeHint())
        footer.addWidget(btns)
        self._footer_wrap = footer_wrap
        # 初始隐藏：容器常驻布局占位，仅切换子按钮显隐（不用 effect，见 _set_actions_visible）
        MessageCard._set_actions_visible(btns, False)
        main.addWidget(footer_wrap)

    def set_image_attachments(self, paths, fallback_content=None):
        """设置图片附件预览（用户气泡下方、底部按钮行上方缩略图条）

        Args:
            paths: 附件图片本地路径列表。发送时来自输入区附件；恢复会话时
                   来自 session 消息的 ``_image_attachments`` 标记。
            fallback_content: 恢复会话时的原始消息 content（multimodal list），
                   传给 plan_image_attachment_sources 做路径失效兑底。
        """
        # stretch 放头部：缩略图右对齐（与用户气泡 AlignRight 同侧）
        thumb_count = 0
        for source, data_uri, path in plan_image_attachment_sources(paths, fallback_content):
            thumb = self._build_image_thumb(source, data_uri, path)
            if thumb is not None:
                self._image_strip_lay.addWidget(thumb)
                thumb_count += 1
        if thumb_count:
            self._image_strip_lay.insertStretch(0)
            self._image_strip.setVisible(True)

    def _build_image_thumb(self, source, data_uri, path):
        """构建单张缩略图 QLabel（等比 80px 高）；加载失败返回 None

        记录原始 QPixmap 引用，点击弹出大图查看。
        """
        pixmap = QPixmap()
        if source:
            pixmap.load(source)
        elif data_uri:
            b64 = data_uri.split("base64,", 1)[-1]
            pixmap.loadFromData(QByteArray.fromBase64(b64.encode("ascii")))
        if pixmap.isNull():
            return None
        thumb = QLabel(self._image_strip)
        dpr = thumb.devicePixelRatioF() or 1.0
        scaled = pixmap.scaledToHeight(int(80 * dpr), Qt.SmoothTransformation)
        scaled.setDevicePixelRatio(dpr)
        thumb.setPixmap(scaled)
        thumb.setFixedHeight(80)
        thumb.setToolTip(os.path.basename(path))
        thumb.setCursor(Qt.PointingHandCursor)
        # [方案 2] 闭包只捕获 (source, data_uri) 源引用，点击时现解码全尺寸图——
        # 原始 pixmap 随本函数返回出作用域释放，不再被闭包长期持有
        # （3840×2160 解码后 ≈33MB RGBA/张，多张图片会话即数百 MB 常驻）。
        thumb.mousePressEvent = lambda e, src=source, uri=data_uri: self._show_image_dialog(src, uri)
        return thumb

    def _show_image_dialog(self, source, data_uri):
        """点击缩略图放大查看（Mask 遮罩弹窗，完整等比显示、无滚动、点遮罩关闭）

        Args:
            source: 图片本地路径（可能已失效，None/空串跳过）。
            data_uri: base64 data URI（source 无效时的兜底来源）。
        """
        pixmap = QPixmap()
        if source:
            pixmap.load(source)
        elif data_uri:
            pixmap.loadFromData(QByteArray.fromBase64(data_uri.split("base64,", 1)[-1].encode("ascii")))
        if pixmap.isNull():
            return
        _ImagePreviewDialog(pixmap, parent=self.window()).exec_()

    def _ensure_user_viewer(self) -> None:
        """懒创建用户气泡 PlainTextViewer（修 #2）。

        首次 set_content / showEvent / append_text 时调用；创建后连接 contentHeightChanged
        并应用暂存的待显示文本。幂等。避免 __init__ 即建 QTextEdit 子树造成的内存占用。
        """
        if self.viewer is not None:
            return
        self.viewer = PlainTextViewer(self)
        self.viewer.contentHeightChanged.connect(self._update_height)
        self._viewer_layout.addWidget(self.viewer)
        if getattr(self, "_viewer_pending_text", None):
            self.viewer.set_text(self._viewer_pending_text)
            self._viewer_pending_text = None

    def _setup_ui(self):
        main = QVBoxLayout(self)
        # 上下 2（2026-09-23 收敛消息间距：原 4 配合卡片间 spacing 8 视觉空隙过大）
        main.setContentsMargins(4, 2, 4, 2)
        main.setSpacing(4 if self.role != "user" else 0)  # user：正文与时间行零间隙

        if self.role == "user":
            # 用户消息：ChatGPT 式简洁气泡（无头像/标题/分隔线），
            # 右对齐由 chat_layout 的 AlignRight 控制，宽度自适应见 PlainTextViewer
            self._setup_user_bubble(main)
        else:
            self._build_card_header(main)

        # ── 内容区（welcome/assistant 走懒渲染，user 已在气泡方法内创建）──
        if self.role == "welcome":
            # 欢迎卡片使用懒渲染：占位符，不立即创建 QWebEngine
            # 避免首帧 Chromium 进程创建阻塞主线程（优化前首帧卡顿 200-500ms 的根因）
            placeholder = QLabel("加载中...", self)
            placeholder.setStyleSheet(_PLACEHOLDER_QSS)
            placeholder.setAlignment(Qt.AlignCenter)
            self._viewer_layout.addWidget(placeholder)
            main.addWidget(self._viewer_container)
            self._lazy_rendered = False
            self.viewer = None  # 懒加载，延后创建
            self.resize_placeholder = QFrame(self)
            self.resize_placeholder.setVisible(False)
            self.resize_placeholder.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            self.resize_placeholder.setStyleSheet(_RESIZE_GHOST_QSS)
            main.addWidget(self.resize_placeholder)
        elif self.role != "user":  # user 已在 _setup_user_bubble 创建，不再进入懒渲染
            # 懒渲染：占位符，不立即创建QWebEngine，进入可视区域再创建
            placeholder = QLabel("加载中...", self)
            placeholder.setStyleSheet(_PLACEHOLDER_QSS)
            placeholder.setAlignment(Qt.AlignCenter)
            self._viewer_layout.addWidget(placeholder)
            if self.role == "assistant":
                # 助手气泡：全宽底色块 + 左上引脚指向头像（与用户气泡镜像，2026-09-23）。
                # 只包 _viewer_container（正文/工具区都在 WebEngine 内），
                # 身份行与 footer 留在气泡外。
                self._assistant_bubble = MessageBubble("left", self)
                _b_lay = QVBoxLayout(self._assistant_bubble)
                _b_lay.setContentsMargins(0, 0, 0, 0)
                _b_lay.setSpacing(0)
                self._viewer_container.setParent(self._assistant_bubble)
                _b_lay.addWidget(self._viewer_container)
                main.addWidget(self._assistant_bubble)
            else:
                main.addWidget(self._viewer_container)
            self._lazy_rendered = False
            self.viewer = None  # 懒加载，延后创建
            self.resize_placeholder = QFrame(self)
            self.resize_placeholder.setVisible(False)
            self.resize_placeholder.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            self.resize_placeholder.setStyleSheet(_RESIZE_GHOST_QSS)
            main.addWidget(self.resize_placeholder)

        self.options_widget = QWidget(self)
        self.options_layout = QVBoxLayout(self.options_widget)
        self.options_layout.setContentsMargins(0, 4, 0, 0)
        self.options_layout.setSpacing(4)
        self.options_widget.setVisible(False)
        main.addWidget(self.options_widget)

        # 重试状态栏（默认隐藏）
        self._retry_status_widget = QWidget(self)
        self._retry_status_widget.setVisible(False)
        retry_layout = QHBoxLayout(self._retry_status_widget)
        retry_layout.setContentsMargins(12, 6, 12, 6)
        retry_layout.setSpacing(8)
        self._retry_status_widget.setStyleSheet(
            """
            QWidget {
                background: rgba(255, 40, 40, 0.08);
                border-top: 1px solid rgba(255, 60, 60, 0.2);
                border-radius: 0px;
            }
            """
        )
        # 旋转图标（CSS动画模拟）
        self._retry_spinner = QLabel("⟳", self)
        self._retry_spinner.setStyleSheet(
            f"""
            QLabel {{
                color: rgba(255, 80, 80, 0.8);
                font-size: {scale_font_size(14)}px;
                font-weight: bold;
            }}
            """
        )
        retry_layout.addWidget(self._retry_spinner)
        # 错误类型
        self._retry_type_label = QLabel("", self)
        self._retry_type_label.setStyleSheet(
            f"""
            QLabel {{
                color: #ff6b6b;
                font-size: {scale_font_size(12)}px;
                font-weight: 600;
            }}
            """
        )
        retry_layout.addWidget(self._retry_type_label)
        # 重试次数
        self._retry_attempt_label = QLabel("", self)
        self._retry_attempt_label.setStyleSheet(
            f"""
            QLabel {{
                color: #ffaa44;
                font-size: {scale_font_size(12)}px;
            }}
            """
        )
        retry_layout.addWidget(self._retry_attempt_label)
        retry_layout.addStretch()
        # 等待倒计时
        self._retry_wait_label = QLabel("", self)
        self._retry_wait_label.setStyleSheet(
            f"""
            QLabel {{
                color: #888;
                font-size: {scale_font_size(11)}px;
            }}
            """
        )
        retry_layout.addWidget(self._retry_wait_label)
        main.addWidget(self._retry_status_widget)

        if self.role == "welcome":  # 全减：assistant 无底部装饰线；user 简洁气泡本就不带
            # 欢迎卡片：tab 条落在卡片**底部**（内容下方），切换区不占用
            # 头部首位；顶部头部与底部区分隔线均已去掉（2026-09-17 用户要求）。
            self._build_welcome_mode_tabs(main)

        # ===== 助手卡片底部元信息栏（分割线下方） =====
        if self.role == "assistant":
            self._build_footer_bar(main)
        # 卡片背景/圆角：user 简洁气泡 12px 圆角无边框，其余 10px
        self._apply_card_style()

        # 淡入动画：新消息微妙出现（200ms，仅透明度）
        fade_in_widget(self, 200)

    def start_streaming_anim(self):
        if self._streaming:
            return
        self._streaming = True
        # 新一轮开始：清除上一轮的"已完成流式"标记，使本轮重新按流式语义
        # 走展开路径（同 _is_history 的修正，见下方）。
        self._streaming_finished = False
        # 🐛 修复"工具结果冒出又消失"：新轮流式开始时恢复 viewer 流式模式，
        # 避免 finish_streaming 后 viewer._streaming=False 导致工具结果到达时
        # append_tool_result 跳过 callback 更新，被后续 _perform_update 覆盖。
        if self.viewer and hasattr(self.viewer, "_streaming") and not self.viewer._streaming:
            self.viewer._streaming = True
        # 修正 viewer 初始化时的 _is_history 标记（可能因 viewer 创建早于
        # start_streaming_anim 而为 True，导致 _on_js_ready 误折叠工具区）
        if self.viewer and hasattr(self.viewer, "_is_history") and self.viewer._is_history:
            self.viewer._is_history = False
        # 新轮流式开始：恢复简洁模式坞态（工具区沉底跟随最新活动）
        if self.viewer and hasattr(self.viewer, "_sync_streaming_dock"):
            self.viewer._sync_streaming_dock(True)
        self._anim_t_ms = 0.0
        self._anim_clock.restart()
        try:
            self._anim_timer.start(50)  # 80→50ms，帧率从12.5fps提升到20fps
        except RuntimeError:
            return
        self.update()

    def _update_anim(self):
        # [V1] 可见性门控：隐藏 tab 不执行动画帧（避免隐藏页每 50ms 空转 update()）。
        # 暂停期间重启时钟：动画位置由累积时间 _anim_t_ms 决定，不推进即停在原位置，
        # 恢复后从原位置继续，无视觉跳变；下一拍定时器自动续跑，无需显式重启。
        if not self.isVisible():
            self._anim_clock.restart()
            return
        # 系统「减少动态效果」：底部光块是纯装饰动画，直接不重绘。
        # 这是流式热路径上的逐帧绘制，关掉即省下这段开销。
        if not Animations.motion_enabled():
            self._anim_clock.restart()
            return
        # 拖拽期间暂停重绘：原生拖拽时主线程在 DefWindowProc 模态循环里，
        # 每 50ms 触发一次 update() 会强制 DWM 对整窗重新合成 → 拖拽卡顿。
        # 直接跳过 update() 让窗口保持静止，DWM 仅平移已有纹理，拖拽顺滑；
        # 松手后 _any_window_dragging 复位，下一拍定时器自然恢复动画。
        from app.utils.window_drag_state import any_window_dragging

        if any_window_dragging:
            self._anim_clock.restart()
            return
        # 时间驱动：按真实经过时间推进，掉帧只丢中间帧、不改变运动速度（旧实现
        # 每拍固定 +0.035 相位，主线程一忙整条动画就变慢，恢复后又连续补帧猛冲）。
        # 单拍钳制 _STREAM_BAND_MAX_DT_MS：被内容渲染占满 500ms 后也只前进一帧的量，
        # 观感是「慢了一下」，而不是瞬移或猛冲。
        dt = self._anim_clock.restart()
        if dt <= 0.0:  # 同一拍内重复进入（理论上不会）：不推进
            dt = 0.0
        elif dt > _STREAM_BAND_MAX_DT_MS:
            dt = _STREAM_BAND_MAX_DT_MS
        self._anim_t_ms += dt
        # 重试状态栏降频更新（每200ms一次，避免和paintEvent双重刷新导致卡顿）
        if self._retrying:
            if not hasattr(self, "_retry_status_tick"):
                self._retry_status_tick = 0
            self._retry_status_tick += 1
            if self._retry_status_tick >= 4:  # 50ms * 4 = 200ms
                self._retry_status_tick = 0
                self._update_retry_status_bar()
        # 局部重绘：动画帧只标脏底部光带这一条窄带（不碰描边所在的卡片边缘），
        # 免得每帧都把整卡交给 Qt 重绘、和流式内容渲染抢主线程。重试中状态栏
        # 位置不确定，退回整卡重绘。
        bubble = self._assistant_bubble if (self.role == "assistant" and getattr(self, "_assistant_bubble", None) is not None) else None
        if bubble is not None:
            # 气泡自绘流式帧：推送三角波相位与 tint，气泡内部幂等判定 + 标脏重绘
            phase = (self._anim_t_ms / _STREAM_BAND_SWEEP_MS) % 2.0
            tri = phase if phase < 1.0 else 2.0 - phase
            tint_s = _STREAM_TINT_RETRY if (self._retrying or self.error) else self._theme["accent"]
            bubble.set_stream_frame(tri, QColor(tint_s))
            return
        if self._retrying:
            self.update()
        else:
            band_top = self.height() - _STREAM_BAND_BOTTOM - _STREAM_BAND_H - _STREAM_BAND_REPAINT_PAD
            self.update(0, band_top, self.width(), _STREAM_BAND_H + 2 * _STREAM_BAND_REPAINT_PAD)

    def _apply_card_style(self, border: str = None, bg: str = None):
        # [PERF] 幂等短路：setStyleSheet 会触发 Qt 样式重新 polish + 子控件 relayout，
        # 而流式结束时 stop_streaming_anim() 会再调一次 —— 与最终全量渲染撞在
        # 同一拍，是结束态卡顿的一分子。参数未变时直接跳过。
        _style_key = (self.role, self.error, border, bg, self._base_bg, self._base_border, Colors.BORDER)
        if getattr(self, "_applied_card_style_key", None) == _style_key:
            return
        self._applied_card_style_key = _style_key
        # user 简洁气泡：底色/圆角/引脚由 MessageBubble 自绘（错误态仍显示红色边框）
        # 背景只画在气泡容器上：身份行与底部操作行在容器外，不受气泡底色影响
        if self.role == "user" and not self.error:
            self.setStyleSheet(
                """
                CardWidget {
                    background-color: transparent;
                    border: none;
                }
                """
            )
            bubble = getattr(self, "_user_bubble", None)
            if bubble is not None:
                bubble.set_bubble_color(bg or self._base_bg)
                bubble.set_border_color(Colors.BORDER)
            return
        if self.role == "assistant" and not self.error:
            # 助手气泡：全宽底色块 + 左上引脚（2026-09-23，替代原「全减」透明样式）；
            # 卡片自身保持透明（身份行/footer 不吃底色），
            # 错误/重试/上下文丢失态仍走下方原逻辑（红框提示）
            self.setStyleSheet(
                """
                CardWidget {
                    background-color: transparent;
                    border: none;
                }
                """
            )
            bubble = getattr(self, "_assistant_bubble", None)
            if bubble is not None:
                bubble.set_bubble_color(bg or self._base_bg)
                bubble.set_border_color(Colors.BORDER)
            return
        self.setStyleSheet(
            f"""
            CardWidget {{
                background-color: {bg or self._base_bg};
                border: 1px solid {border or self._base_border};
                border-radius: 10px;
            }}
            """
        )
        # 错误/重试态：气泡底色同步为主题色（红系），避免旧底色与红框冲突
        for attr in ("_user_bubble", "_assistant_bubble"):
            b = getattr(self, attr, None)
            if b is not None:
                b.set_bubble_color(bg or self._base_bg)
                b.set_border_color(Colors.BORDER)

    def stop_streaming_anim(self):
        self._streaming = False
        bubble = getattr(self, "_assistant_bubble", None)
        if bubble is not None:
            bubble.set_stream_frame(None, None)  # 气泡退出流式态（清描边/光带）
        # 标记本轮已走过流式（含用户中断/出错中断）：viewer 创建或虚拟滚动
        # 回收重建时据此保持工具区展开，不再被判为"历史"而折叠。
        self._streaming_finished = True
        self._retrying = False
        self.error = False  # 重试成功后清除错误状态
        try:
            self._anim_timer.stop()
        except RuntimeError:
            return
        self._apply_card_style()
        self._retry_status_widget.setVisible(False)
        # [PERF] 去掉 repaint()：同步强制重绘会把整卡 paint 塞进当前这一拍，
        # 而结束态这一拍还要承载最终全量渲染与高度变化；异步 update() 足够，
        # 由 Qt 在下一个合成周期统一绘制。
        self.update()

    def start_retry_anim(self, error_type: str, attempt: int, max_retries: int, wait_time: float):
        """切换到重试边框模式（红色流动+白光点）"""
        self._retrying = True
        self._retry_error_type = error_type
        self._retry_attempt = attempt
        self._retry_max = max_retries
        self._retry_wait_time = wait_time
        # 确保动画定时器运行
        if not self._streaming:
            self._streaming = True
            self._anim_t_ms = 0.0
            self._anim_clock.restart()
            try:
                self._anim_timer.start(50)
            except RuntimeError:
                return
        # 更新状态栏
        self._update_retry_status_bar()
        self._retry_status_widget.setVisible(True)
        self.update()

    def update_retry_status(self, error_type: str, attempt: int, max_retries: int, wait_time: float):
        """更新重试状态信息"""
        self._retry_error_type = error_type
        self._retry_attempt = attempt
        self._retry_max = max_retries
        self._retry_wait_time = wait_time
        self._update_retry_status_bar()
        self.update()

    def stop_retry_anim(self):
        """停止重试动画，恢复正常边框"""
        self._retrying = False
        self.error = False
        self._retry_status_widget.setVisible(False)
        self._apply_card_style()
        if not self._streaming:
            return
        # 继续正常的流式动画（彩虹边框）
        self.update()
        self.repaint()

    def _update_retry_status_bar(self):
        """更新重试状态栏的文本内容"""
        # 重试时恢复标准重试样式
        self._retry_status_widget.setStyleSheet(
            """
            QWidget {
                background: rgba(255, 40, 40, 0.08);
                border-top: 1px solid rgba(255, 60, 60, 0.2);
                border-radius: 0px;
            }
            """
        )
        # 旋转图标动画
        spin_chars = ["◜", "◝", "◞", "◟"]
        idx = int(self._anim_t_ms / 180.0) % 4  # 每 180ms 转一格
        self._retry_spinner.setText(spin_chars[idx])
        # 错误类型
        self._retry_type_label.setStyleSheet(
            f"""
            QLabel {{
                color: #ff6b6b;
                font-size: {scale_font_size(12)}px;
                font-weight: 600;
            }}
            """
        )
        self._retry_type_label.setText(self._retry_error_type)
        # 重试次数
        self._retry_attempt_label.setStyleSheet(
            f"""
            QLabel {{
                color: #ffaa44;
                font-size: {scale_font_size(12)}px;
            }}
            """
        )
        self._retry_attempt_label.setText(f"第 {self._retry_attempt}/{self._retry_max} 次重试")
        # 等待时间
        self._retry_wait_label.setStyleSheet(
            f"""
            QLabel {{
                color: #888;
                font-size: {scale_font_size(11)}px;
            }}
            """
        )
        self._retry_wait_label.setText(f"等待 {self._retry_wait_time:.0f}s")

    def _on_preview_image(self, image_url: str):
        """正文图片点击 → 内置预览（可滚轮缩放）。

        原先直接 QDesktopServices.openUrl 交给系统默认程序：跳出应用、体验割裂，
        且 data:/qrc: 的 src 交给 openUrl 后实际无响应。改为：
          file:// / 绝对本地路径 / data:image → 同步解码后立即预览
          http(s)://                          → 异步下载后预览（P1 已启用磁盘
                                                HTTP 缓存，命中即几乎无等待）
          qrc:/ 等                            → 回退原行为
        任一步失败都回退 openUrl，保证不比改动前差。
        """
        if not image_url or image_url.startswith("qrc:/"):
            self._open_url_external(image_url)
            return
        pixmap = _decode_image_url_to_pixmap(image_url)
        if pixmap is not None and not pixmap.isNull():
            _show_image_preview(pixmap, parent=self.window())
            return
        if image_url.startswith(("http://", "https://")):
            _download_and_preview(image_url, self.window())
            return
        self._open_url_external(image_url)

    def _open_url_external(self, url_str: str) -> None:
        """回退路径：交给系统默认程序打开（与改动前行为一致）。"""
        if not url_str:
            return
        try:
            from PyQt5.QtCore import QUrl
            from PyQt5.QtGui import QDesktopServices

            QDesktopServices.openUrl(QUrl(url_str))
        except Exception:
            pass

    def _on_chart_expand(self, chart_type: str, payload_b64: str):
        """图表放大查看 → 打开覆盖右侧对话区域的 chart_viewer 全局卡

        内部直接处理（不走 main_widget 回调），assistant/welcome/历史卡统一生效；
        ui_helpers 顶部反向 import MessageCard，必须延迟导入避免循环依赖。
        """
        try:
            from app.widgets.ui_helpers import show_chart_viewer

            show_chart_viewer(self, chart_type, payload_b64)
        except Exception as e:
            logger.error(f"[MessageCard] 图表放大失败: {e}")

    def _on_save_chart_png(self, name_b64: str, png_b64: str):
        """图表 PNG 导出保存（消息卡小图导出与放大视图共用通道）"""
        try:
            name = base64.b64decode(name_b64).decode("utf-8") if name_b64 else "图表"
        except Exception:
            name = "图表"
        from app.widgets.ui_helpers import save_png_from_b64

        path = save_png_from_b64(self, png_b64, name or "图表")
        if path:
            logger.info(f"[MessageCard] 图表 PNG 已导出: {path}")

    def _on_save_widget_file(self, wtype: str, content_b64: str):
        """Widget 源码保存（svg 存矢量源码补 xmlns；html 存净化产物 .html）"""
        try:
            if wtype == "html":
                from app.widgets.ui_helpers import save_html_source

                path = save_html_source(self, content_b64)
                if path:
                    logger.info(f"[MessageCard] HTML 源文件已导出: {path}")
            else:
                from app.widgets.ui_helpers import save_svg_source

                path = save_svg_source(self, content_b64)
                if path:
                    logger.info(f"[MessageCard] SVG 源文件已导出: {path}")
        except Exception as e:
            logger.error(f"[MessageCard] Widget 保存失败: {e}")

    def _on_webengine_context_lost(self):
        """WebEngine 上下文丢失时显示恢复提示"""
        # 设置卡片为错误状态样式（根据深浅模式选择边框色）
        try:
            from app.utils.theme_manager import theme_manager

            _is_light = theme_manager.is_light_theme()
        except Exception:
            _is_light = False
        _border = "#FCA5A5" if _is_light else "#A94444"
        self._apply_card_style(border=_border)
        # 标记需要恢复
        self._webengine_needs_restore = True

    def _on_webengine_context_restored(self):
        """WebEngine 上下文恢复后恢复正常样式"""
        self._apply_card_style()
        self._webengine_needs_restore = False
        # 重新同步宽度
        self.sync_width(force=True)

    def _on_webengine_need_recreate(self):
        """需要完全重建 WebEngine 视图（GPU上下文丢失无法恢复时）"""
        if not self._lazy_rendered or self.viewer is None:
            return

        # 保存当前内容
        markdown_text = None
        if hasattr(self.viewer, "_markdown_text"):
            markdown_text = self.viewer._markdown_text

        # 销毁旧viewer
        self.viewer.deleteLater()

        # 重新创建viewer
        for i in reversed(range(self._viewer_layout.count())):
            item = self._viewer_layout.itemAt(i)
            if item and item.widget():
                item.widget().deleteLater()

        # 灰度：Qt 渲染器无 context lost，不应进入此方法；防御性回退到 WebEngine
        self.viewer = CodeWebViewer(self)
        self.viewer._lazy_markdown_cb = self._build_incremental_md
        self.viewer.codeActionRequested.connect(self.actionRequested)
        self.viewer.contextActionRequested.connect(self.contextActionRequested)
        self.viewer.contentHeightChanged.connect(self._update_height)
        self.viewer.toolDiffRequested.connect(self.toolDiffRequested)
        self.viewer.subAgentLogRequested.connect(self.subAgentLogRequested)
        self.viewer.saveFileRequested.connect(self.saveFileRequested)
        self.viewer.chartExpandRequested.connect(self._on_chart_expand)
        self.viewer.saveChartPngRequested.connect(self._on_save_chart_png)
        self.viewer.saveWidgetFileRequested.connect(self._on_save_widget_file)
        self.viewer.previewImageRequested.connect(self._on_preview_image)
        self.viewer.contextLost.connect(self._on_webengine_context_lost)
        self.viewer.contextRestored.connect(self._on_webengine_context_restored)
        self.viewer.needRecreate.connect(self._on_webengine_need_recreate)
        self.viewer._install_dialog_filter()

        self._viewer_layout.addWidget(self.viewer)

        # 恢复内容
        if markdown_text:
            self.viewer._markdown_text = markdown_text
            self.viewer._schedule_render(immediate=True)

        # 任务列表随 viewer 重建补推（_pending_todos 由 viewer._on_js_ready 消费）
        if self._todos_snapshot is not None:
            self._push_todo_list()

        # 恢复正常样式
        self._apply_card_style()
        self._webengine_needs_restore = False

        # 同步宽度
        self.sync_width(force=True)

    def _stream_region(self) -> QRect:
        """流式视觉（漫射/描边/光带）的绘制区域。

        assistant 气泡化（2026-09-23）后视觉主体是气泡，流式效果必须跟随气泡
        矩形；继续画整卡会在气泡外浮出一圈与气泡无关的「外边框」。
        其余角色仍为整卡。
        """
        if self.role == "assistant":
            bubble = getattr(self, "_assistant_bubble", None)
            if bubble is not None:
                return bubble.geometry()
        return QRect(0, 0, self.width(), self.height())

    def paintEvent(self, event):
        # ⚠️ SimpleCardWidget.paintEvent 会无条件画一圈描边：
        #   painter.setPen(QColor(0,0,0,12 或 48)) + drawRoundedRect(...)
        # CSS 的 `border: none` 管不到它（这是 QPainter 直接画的，不走样式表），
        # 于是 user 气泡（背景已移交给 _user_bubble）与 assistant 全减模式卡片
        # 上下会残留一条淡边框。这两个角色由自身样式/子容器负责外观，
        # 跳过父类绘制；welcome 与错误态仍需原来的卡片底与描边。
        if self.role in ("user", "assistant") and not self.error:
            pass
        else:
            super().paintEvent(event)

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        w, h = self.width(), self.height()
        radius = 16

        # 侧边竖条已整体移除：welcome 卡片的 accent 竖条（左缘那条线）由用户
        # 2026-09-17 明确要求去掉，保持卡片四边纯净。

        if not self._streaming:
            painter.end()
            return

        if self.role == "assistant" and getattr(self, "_assistant_bubble", None) is not None:
            # 流式视觉（描边/光带）改由气泡自绘（画在其底色之上）：卡片层画会被
            # 气泡不透明底色盖住，且视觉主体应是气泡而非卡片外缘（2026-09-23）。
            painter.end()
            return

        # ══════════════════════════════════════════════════════
        #  流式态视觉：静态单色细描边 + 底部往返光块
        # ══════════════════════════════════════════════════════
        # 旧实现：10 色高饱和彩虹绕整卡循环 + 7px 霓虹外发光 + 3px 白色流光带，
        # 动区覆盖整卡周长、色相跳变（青→紫→粉→橙→绿），阅读时过于抢眼。
        # 现把「动」收敛到底部一条光带：
        #   · 描边：1.5px 单色（accent / 重试红），完全静态（alpha 不再随呼吸调制，
        #     否则每帧都要整卡重绘内壁渐变 + 描边，与流式内容渲染抢主线程）
        #   · 光块：底部内侧 3px 高、约 40% 卡宽，两端淡出，左右往返（单程 1.6s）

        # 单色 tint：重试/错误态用警示红，其余用主题 accent（不再循环变色）
        if self._retrying or self.error:
            tint = QColor(_STREAM_TINT_RETRY)
        else:
            tint = QColor(self._theme["accent"])

        # ── 层1：内壁漫射（极柔和的边缘渗光）──
        # M1：裁剪路径按几何缓存，仅尺寸变化时重建，不再每帧 new QPainterPath。
        # 区域 = _stream_region()（assistant 为气泡矩形），气泡高度随流式上报变化，
        # 四元组任一变化即重建。
        region = self._stream_region()
        rx, ry, rw, rh = region.x(), region.y(), region.width(), region.height()
        if (self._clip_x, self._clip_y, self._clip_w, self._clip_h) != (rx, ry, rw, rh):
            self._clip_x, self._clip_y, self._clip_w, self._clip_h = rx, ry, rw, rh
            self._clip_inner = QPainterPath()
            self._clip_inner.addRoundedRect(rx + 3, ry + 3, rw - 6, rh - 6, radius - 2, radius - 2)
            self._clip_border = QPainterPath()
            self._clip_border.addRoundedRect(rx, ry, rw, rh, radius + 1, radius + 1)
            self._clip_inner_border = QPainterPath()
            self._clip_inner_border.addRoundedRect(rx + 2, ry + 2, rw - 4, rh - 4, radius - 1, radius - 1)
            self._clip_border_region = self._clip_border - self._clip_inner_border
        painter.setClipPath(self._clip_inner)
        inner_gradient = self._grad_inner
        inner_gradient.setStart(rx, ry)
        inner_gradient.setFinalStop(rx + rw, ry + rh)
        _c0 = QColor(tint)
        _c0.setAlpha(11)
        _c1 = QColor(tint)
        _c1.setAlpha(4)
        inner_gradient.setColorAt(0.0, _c0)
        inner_gradient.setColorAt(1.0, _c1)
        painter.fillRect(rx, ry, rw, rh, inner_gradient)

        # ── 层2：静态细描边（1.5px 单色，替代原 4px 彩虹循环 + 7px 外发光）──
        # 静态层每帧照画，不做「局部重绘就跳过」的优化：动画帧的脏区是底部一条
        # 窄带（见 _update_anim），Qt 按脏区裁剪光栅化，整卡 fillRect 的实际填充
        # 仍被限制在窄带内；若按脏区跳过静态层，脏区覆盖到的描边会被擦掉却不
        # 重画（下边框随动画帧一闪一闪）。
        painter.setClipPath(self._clip_border_region)
        _bc = QColor(tint)
        _bc.setAlpha(92)
        border_pen = QPen(_bc)
        border_pen.setWidthF(1.5)
        painter.setPen(border_pen)
        painter.setBrush(QBrush(Qt.NoBrush))
        painter.drawRoundedRect(rx, ry, rw, rh, radius + 1, radius + 1)

        # ── 层3：底部往返光块（唯一的运动元素）──
        painter.setClipPath(self._clip_inner_border)
        band_h = _STREAM_BAND_H
        band_ratio = 0.4  # 光块宽度占卡宽比例
        travel_ratio = 1.0 - band_ratio
        # 三角波 0→1→0：直接对累积时间取模 2，周期严格闭合。
        # 旧写法 _t = (相位/2π) * 3.0 % 2.0 每个相位圈走 3 个半程，回绕处
        # 从「最右」瞬跳「最左」（约每 9s 一次跳变），是跳变感的直接来源。
        _t = (self._anim_t_ms / _STREAM_BAND_SWEEP_MS) % 2.0
        _tri = _t if _t < 1.0 else 2.0 - _t
        band_cx = (0.5 * band_ratio + travel_ratio * _tri) * rw
        band_w = band_ratio * rw
        band_x = rx + int(band_cx - 0.5 * band_w)
        band_w = int(band_w)
        band_y = ry + rh - _STREAM_BAND_BOTTOM - band_h
        # 复用模板渐变：stop 位置固定（0/0.5/1），仅改坐标与颜色，不每帧 new
        band_gradient = self._grad_main
        band_gradient.setStart(band_x, 0)
        band_gradient.setFinalStop(band_x + band_w, 0)
        _b0 = QColor(tint)
        _b0.setAlpha(0)
        _b1 = QColor(tint)
        _b1.setAlpha(170)
        band_gradient.setColorAt(0.0, _b0)
        band_gradient.setColorAt(0.5, _b1)
        band_gradient.setColorAt(1.0, _b0)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(band_gradient))
        painter.drawRoundedRect(band_x, band_y, band_w, band_h, 1.5, 1.5)
        painter.end()

    def set_error_state(self, is_error: bool, error_message: str = ""):
        """设置错误状态

        Args:
            is_error: 是否为错误状态
            error_message: 错误信息（错误状态时显示在状态栏）
        """
        self.error = is_error
        if is_error:
            self._retrying = False
            # 显示错误状态栏（而不是隐藏）
            self._show_error_status(error_message)
            # 检测深浅色模式，选择合适背景
            try:
                from app.utils.theme_manager import theme_manager

                _is_light = theme_manager.is_light_theme()
            except Exception:
                _is_light = False
            bd = "#ff4d4d"
            bg = "#FFF5F5" if _is_light else "#2a1f1f"
        else:
            self._retry_status_widget.setVisible(False)
            bd, bg = self._base_border, self._base_bg
        self._apply_card_style(border=bd, bg=bg)

    def _show_error_status(self, error_message: str):
        """显示错误状态信息（复用重试状态栏UI，但显示错误信息）"""
        self._retry_error_type = "错误"
        self._retry_type_label.setText("❌")
        self._retry_attempt_label.setText(error_message if error_message else "请求失败")
        self._retry_wait_label.setText("")
        self._retry_spinner.setText("⚠")
        # 检测深浅色模式，选择合适的错误文字颜色
        try:
            from app.utils.theme_manager import theme_manager

            _is_light = theme_manager.is_light_theme()
        except Exception:
            _is_light = False
        _err_text_color = "#DC2626" if _is_light else "#ff6b6b"
        _err_sub_color = "#B91C1C" if _is_light else "#ff9999"
        # 改变状态栏样式为错误风格
        self._retry_status_widget.setStyleSheet(
            """
            QWidget {
                background: rgba(255, 40, 40, 0.08);
                border-top: 1px solid rgba(255, 60, 60, 0.2);
                border-radius: 0px;
            }
            """
        )
        self._retry_type_label.setStyleSheet(
            f"""
            QLabel {{
                color: {_err_text_color};
                font-size: {scale_font_size(14)}px;
                font-weight: bold;
            }}
            """
        )
        self._retry_attempt_label.setStyleSheet(
            f"""
            QLabel {{
                color: {_err_sub_color};
                font-size: {scale_font_size(12)}px;
            }}
            """
        )
        self._retry_status_widget.setVisible(True)

    def is_user_reading_inside(self) -> bool:
        """用户是否正在卡片内部（WebEngine 侧）滚动阅读。

        body / #content-placeholder / #tool-content / #todo-content 任一容器被
        用户上滚即为 True。WebEngine 内滚动不会移动 Qt 滚动条，宿主的
        ``_user_intentionally_away_from_bottom`` 对卡内阅读完全失明；外层滚底
        判定必须显式查询此状态让位，否则流式中每个高度变化都会把卡片拉回
        「底部对齐」固定姿态（正文阅读位置反复被推走的根因）。
        """
        return bool(getattr(self.viewer, "_user_reading_inside", False))

    def _emit_card_diff_requested(self):
        """发射卡片差异请求信号

        Signal:
            cardDiffRequested(int round_index, int message_index)
        """
        round_idx = self._round_index if self._round_index is not None else -1
        msg_idx = self._message_index if self._message_index is not None else -1
        self.cardDiffRequested.emit(round_idx, msg_idx)

    def _emit_review_requested(self):
        """发射页脚 Review 按钮点击信号（触发 code-reviewer 子智能体快速审查）

        Signal:
            reviewRequested(int round_index, int message_index)
        """
        round_idx = self._round_index if self._round_index is not None else -1
        msg_idx = self._message_index if self._message_index is not None else -1
        self.reviewRequested.emit(round_idx, msg_idx)

    def _emit_branch_requested(self):
        """发射页脚「分支」按钮点击信号（以本条消息为界开新会话）

        Signal:
            branchRequested(int round_index, int message_index)
        """
        round_idx = self._round_index if self._round_index is not None else -1
        msg_idx = self._message_index if self._message_index is not None else -1
        self.branchRequested.emit(round_idx, msg_idx)

    def _remember_height_for_width(self, height: int) -> None:
        """[L3] 记录「最近一次同步宽度 → 内容高度」，供后续 resize 预测命中。

        只在**非流式、非占位**状态记录：流式中间态和占位高度都不是稳定高度，
        写进缓存会让后续预测把卡片撑错（虽然有异步校正兜底，但会多抖一帧）。
        """
        width = self._last_synced_width
        if width <= 0:
            return
        cache = self._height_cache
        cache[width] = height
        if len(cache) > _HEIGHT_CACHE_MAX:
            # dict 保持插入序：丢弃最早写入的键
            for key in list(cache)[: len(cache) - _HEIGHT_CACHE_MAX]:
                cache.pop(key, None)

    def _update_height(self, h):
        target_height = max(40, h)
        current_height = self.viewer.height() or self.viewer.minimumHeight() or 40
        self._target_viewer_height = target_height
        # [L3] 稳定状态下的高度才写入宽度→高度缓存
        if not self._streaming and not self._resize_preview_mode:
            self._remember_height_for_width(target_height)

        # [T29] 流式高度补间进行中：一律吞掉上报。
        # 补间的每一帧 setFixedHeight 都会让 Chromium 视口变化 → ResizeObserver
        # → reportHeight → 回到本函数，送来的正是"补间途中的中间高度"。若不隔离，
        # 中间值与动画终值的差轻易超过 24px（一次跳 200px 的补间，途中的任意
        # 采样都能差上百），会被下面的新目标判定误认成「内容又变了」→ stop +
        # 重启补间 → 永远走不到终点、退化成锯齿。隔离后补间独占这条通道，
        # 收尾由 _on_height_anim_state_changed 主动校正一次。
        if self._stream_height_anim_active:
            # 只更新已知的目标值，不打断进行中的动画
            self._target_viewer_height = target_height
            return

        # 🆕 结束态高度动画进行中：reportHeight 回环（setFixedHeight → 视口变化 →
        # ResizeObserver → reportHeight）会不断送来"当前中间高度"，若照单全收会
        # 把补间打断成锯齿。判定为同一收敛值（差异 <24px）时忽略。
        if self._is_height_animating:
            try:
                _end = int(self._height_anim.endValue())
            except Exception:
                _end = target_height
            if abs(target_height - _end) < 24:
                return
            # 明显不同的新目标（内容又变了）：停机，交回常规路径
            self._height_anim.stop()

        # 🆕 流式中防抖：累积高度变化，定时器到期才应用 viewer 高度。
        # 流式期间每个 text chunk 都会触发 height report（~60fps），
        # 若每次立即 resize viewer 会导致卡片高度持续跳动、主滚动区不稳定。
        # 防抖后只有最后一次高度在 80ms 窗口到期后被应用，大幅减少 resize 频率。
        if self._streaming:
            if self._height_anim.state() == QVariantAnimation.Running:
                self._height_anim.stop()
            self._debounced_target_height = target_height
            if not self._stream_height_timer.isActive():
                self._stream_height_timer.start()
            return

        # 非流式小变化（<10px）→ 立即跳转避免闪烁
        if abs(target_height - current_height) < 10:
            if self._height_anim.state() == QVariantAnimation.Running:
                self._height_anim.stop()
            self._apply_viewer_height(target_height)
            return

        # 🆕 结束态高度过渡：坞态归位 + 最终重排会让卡片高度一次跳几百 px，
        # snap 会让外层滚动区瞬移（页面内已由 FLIP 补间，唯独 Qt 侧没有）。
        # 仅在"流式结束后第一次收敛"的窗口内启用，且变化足够大才值得动画。
        if self._in_finish_height_window() and abs(target_height - current_height) >= FINISH_HEIGHT_ANIM_MIN_DELTA:
            self._start_finish_height_anim(current_height, target_height)
            return

        # 非流式大高度变化（折叠/展开）：一次性设 viewer 最终高度，
        # 但跳过容器的 200ms maximumHeight 动画，避免 AlignBottom 布局中
        # 卡片位置因容器动画与 viewer 高度变化不同步而"闪现"。
        # card_container 检查 NO_ANIMATION_PROP → 直接 snap 到目标高度。
        self.setProperty("noContainerAnimation", True)
        self._apply_viewer_height(target_height)
        # 延迟清除标志，等容器 snap 完成
        QTimer.singleShot(50, lambda: self.setProperty("noContainerAnimation", False))

    # ── 结束态高度过渡（可关：FINISH_HEIGHT_ANIM_ENABLED）──

    def _in_finish_height_window(self) -> bool:
        """是否处于"流式结束后的高度收敛窗口"内。

        只在 ``MessageCard.finish_streaming`` 打开的一个短窗口内为真，
        保证动画只服务结束态这一次收敛，不污染折叠/展开/主题刷新等日常路径。
        """
        global FINISH_HEIGHT_ANIM_ENABLED
        if not FINISH_HEIGHT_ANIM_ENABLED:
            return False
        # 系统「减少动态效果」：不走缓动，退回调用方的 snap（内容照样到位）
        if not Animations.motion_enabled():
            return False
        if getattr(self, "_finish_height_anim_until", 0.0) <= 0.0:
            return False
        if time.monotonic() > self._finish_height_anim_until:
            self._finish_height_anim_until = 0.0
            self._finish_height_anim_left = 0
            return False
        # 次数预算：结束态通常有两次收敛（坞态归位+重排、随后自动折叠），
        # 用完即回到 snap，避免把后续日常高度变化也动画化。
        return int(getattr(self, "_finish_height_anim_left", 0)) > 0

    def _start_finish_height_anim(self, start_h: int, end_h: int) -> None:
        """结束态高度收敛：走追踪 tick（[T30] 替代一次性 QVariantAnimation）。

        为什么换掉 ``_height_anim``：
        1. 它没有上报隔离——归位/折叠的 200ms CSS 过渡期间 ResizeObserver 会
           送来中间高度，与动画终值的差轻易超过 ``_update_height`` 守卫的
           24px 阈值 → stop + 重启，补间被打断成锯齿（"剧烈抖动"的 Qt 侧成分）。
        2. QVariantAnimation 不支持运行中改终值，新目标只能 stop+start，同样
           丢进度。追踪 tick 天然 retarget + 隔离，与流式同一套机制。
        追踪比例换更缓的 ``FINISH_HEIGHT_TRACK_FACTOR``（0.28，约 300ms 收敛），
        与页面内 CSS 过渡（200ms）/ FLIP（220ms）同量级收尾，丝绸不抢拍。
        """
        # 消费一次预算；预算用尽则关闭窗口
        self._finish_height_anim_left = max(0, int(getattr(self, "_finish_height_anim_left", 0)) - 1)
        if self._finish_height_anim_left <= 0:
            self._finish_height_anim_until = 0.0
        try:
            self._finish_height_anim_active = True
            self._height_track_factor = FINISH_HEIGHT_TRACK_FACTOR
            self._target_viewer_height = int(end_h)
            self._begin_stream_height_track(int(end_h))
        except Exception:
            # 追踪不可用（对象已销毁等）：退回原有 snap 行为
            self._finish_height_anim_active = False
            self.setProperty("noContainerAnimation", True)
            self._apply_viewer_height(end_h)
            QTimer.singleShot(50, lambda: self.setProperty("noContainerAnimation", False))

    def _on_height_anim_state_changed(self, state):
        self._is_height_animating = state == QVariantAnimation.Running
        # 动画结束时触发一次高度变化信号，让父容器更新
        if state == QVariantAnimation.Stopped:
            # 重发的是**已应用过**的高度，没有新的高度增量。必须清零，否则
            # 外层列表会拿上一次的残值再补偿一次 → 视口被重复拖拽。
            self._last_height_delta = 0
            # 🆕 结束态高度缓动收尾：缓动过程中只做增量补偿（不触发整段滚底），
            # 收尾时补一次 _content_just_loaded，让 main_widget 把视口真正钉到底
            # （否则结束态会停在"补偿后的位置"而不是底部）。
            if self._finish_height_anim_active:
                self._finish_height_anim_active = False
                self._content_just_loaded = True
            self.heightChanged.emit(self._last_applied_viewer_height)
            layout = self.layout()
            if layout:
                layout.invalidate()

    def _begin_stream_height_track(self, target_h: int) -> None:
        """启动/续跑流式高度追踪：viewer 朝 ``target_h`` 按拍逼近。

        流式期间高度上报是「延迟后一次性落地」的台阶：文字按帧连续出现，卡片
        高度却每 ~160ms 蹦一格 —— 这就是用户感知的顿挫。追踪 tick 用固定节拍
        把台阶摊成连续生长；目标值可随时被新上报刷新（_target_viewer_height），
        无需重启任何动画，天然 retarget。
        """
        self._stream_height_anim_active = True
        if not self._stream_height_tick.isActive():
            self._stream_height_tick.start()

    def _stream_height_tick_step(self):
        """追踪 tick：朝目标值按比例逼近，收敛（±2px）即落定停拍。

        ⚠️ 不得以 ``_streaming`` 作为停止条件：结束态（FINISH 窗口）也走本
        追踪，而彼时 ``_streaming`` 已是 False——若在此自杀，tick 首拍即停、
        一次 setFixedHeight 都不会执行，viewer 高度冻结在流式末值（坞态正文
        限高的小值），归位后内容全高大于它 → 卡片底部被裁（文字显示不全）。
        停止只由「收敛落定 / 对象失效」驱动，收敛保证不空转。
        """
        target = int(getattr(self, "_target_viewer_height", 0) or 0)
        if target <= 0:
            self._stop_stream_height_track()
            return
        try:
            current_height = self.viewer.height() or self.viewer.minimumHeight() or 40
            diff = target - current_height
            if abs(diff) <= STREAM_HEIGHT_TRACK_EPSILON:
                # 落定：精确对齐 + 解除上报隔离 + 通知宿主布局
                self._apply_viewer_height(target)
                self._stop_stream_height_track()
                return
            self._apply_viewer_height(int(current_height + diff * self._height_track_factor))
        except RuntimeError, AttributeError:
            # viewer 已被虚拟滚动池化摘走（detach 置 None）/ 对象析构：
            # 停拍防泄漏。内容重挂后会自行上报高度，走常规路径校正。
            self._stop_stream_height_track()

    def _stop_stream_height_track(self):
        """停追踪拍 + 解除上报隔离 + 通知宿主。"""
        self._stream_height_anim_active = False
        self._height_track_factor = STREAM_HEIGHT_TRACK_FACTOR
        if self._stream_height_tick.isActive():
            self._stream_height_tick.stop()
        # 重发的是**已应用过**的高度，没有新的高度增量。必须清零，否则
        # 外层列表会拿上一次的残值再补偿一次 → 视口被重复拖拽。
        self._last_height_delta = 0
        # 结束态（FINISH 窗口）追踪收尾：补一次 _content_just_loaded，让
        # main_widget 把视口真正钉到底（追踪过程中只做增量补偿，不触发整段滚底；
        # 结束态会停在「补偿后的位置」而不是底部）。
        if self._finish_height_anim_active:
            self._finish_height_anim_active = False
            self._content_just_loaded = True
        self.heightChanged.emit(self._last_applied_viewer_height)
        layout = self.layout()
        if layout:
            layout.invalidate()

    def _apply_debounced_height(self):
        """应用防抖后的流式高度（_stream_height_timer 到期回调）"""
        h = self._debounced_target_height
        # 流式已结束则跳过（由 finish_streaming 后的非流式 _update_height 接管）
        if not self._streaming:
            return
        current_height = self.viewer.height() or self.viewer.minimumHeight() or 40
        if h >= current_height:
            # 增长方向：小阈值立即应用，保证流式输出滚底跟随。
            # 🐛 新内容填平了此前的收拢需求 → 取消挂起的延迟收缩。
            self._cancel_pending_shrink()
            if h - current_height > 2:
                # [T29] 增量足够大 → 启动追踪，消除「憋一下再整块蹦高」的
                # 台阶感；量级不足（流式尾巴的小抖动）直接 snap，避免 tick 常转。
                if (
                    STREAM_HEIGHT_ANIM_ENABLED
                    and Animations.motion_enabled()
                    and (h - current_height) >= STREAM_HEIGHT_ANIM_MIN_DELTA
                ):
                    self._target_viewer_height = h
                    self._begin_stream_height_track(h)
                else:
                    self._apply_viewer_height(h)
        else:
            # 收拢方向：小步回弹（<40px）不立即应用（流式块→完成块的 DOM
            # 替换、滚动条出现/消失的重排噪声，立即应用会"长一下又缩回去"抖动）。
            # 🐛 但旧实现直接丢弃会累积虚高：12 个工具依次完成，每次
            # "运行框→折叠行"缩 ~24px 全被吞 → 累积 250px+ 底部空白；
            # 正文增长会暂时填平看不出，纯工具执行阶段（正文静默）即暴露。
            # 修复：改为**延迟落地**——500ms 稳定窗合并连续抖动，窗口期被
            # 增长取消（内容填平），静止的收缩最终落地。大幅收拢（≥40px）
            # 仍立即应用。
            if current_height - h >= 40:
                self._cancel_pending_shrink()
                # [T29] 大幅收拢同样走追踪：多个工具框同时到期会一次性回落
                # 数百 px，snap 是「掉下去」，追踪是「滑下去」。
                if STREAM_HEIGHT_ANIM_ENABLED and Animations.motion_enabled():
                    self._target_viewer_height = h
                    self._begin_stream_height_track(h)
                else:
                    self._apply_viewer_height(h)
            else:
                self._schedule_pending_shrink(h)

    def _schedule_pending_shrink(self, target: int):
        """挂起一次小步收拢：500ms 稳定窗后落地，窗口内被增长/finish 取消。"""
        self._pending_shrink_height = int(target)
        if self._shrink_timer is None:
            self._shrink_timer = QTimer(self)
            self._shrink_timer.setSingleShot(True)
            self._shrink_timer.setInterval(500)
            self._shrink_timer.timeout.connect(self._apply_pending_shrink)
        self._shrink_timer.start()

    def _cancel_pending_shrink(self):
        """取消挂起的延迟收缩（增长方向到来 / finish 收敛接管）。"""
        self._pending_shrink_height = None
        if self._shrink_timer is not None:
            self._shrink_timer.stop()

    def _apply_pending_shrink(self):
        """延迟收缩落地：仅当流式中且当前落地值仍大于挂起目标。"""
        target = self._pending_shrink_height
        self._pending_shrink_height = None
        if target is None or not self._streaming:
            return
        current_height = self.viewer.height() or self.viewer.minimumHeight() or 40
        # 目标仍小于当前值才收拢；期间已被增长覆盖（>= 目标）则放弃
        if current_height > target:
            # [T29] 延迟收缩落地同样走追踪：工具密集时多个收缩会在同一拍落地，
            # snap 是「掉下去」。量级小时不值得动画（这里是 <40px 的小步收拢，
            # 阈值用 FINISH 的 60 太大，直接判是否 >= 12px）。
            if STREAM_HEIGHT_ANIM_ENABLED and Animations.motion_enabled() and (current_height - target) >= 12:
                self._target_viewer_height = target
                self._begin_stream_height_track(target)
            else:
                self._apply_viewer_height(target)

    def _on_qt_viewer_height(self, h: int) -> None:
        """灰度：纯 Qt viewer 高度自治（layout 自适应，不 setFixedHeight），
        仅转发高度变化给父容器（滚底跟随依赖 heightChanged 链路）。"""
        # 灰度路径无增量语义：清零防止外层列表用残留 delta 做错误锚定补偿
        self._last_height_delta = 0
        self.heightChanged.emit(max(40, int(h)))

    def _resolve_height_batch(self):
        """沿 Qt 父链上溯，找到本卡所属聊天页的高度批量提交器。

        不用 ``self._parent``（其语义随调用点变化），直接走 Qt 父链更稳：
        ``MessageCard → chat_container → scroll_area(viewport) → … → MainWidget``。
        """
        widget = self.parentWidget()
        for _ in range(6):
            if widget is None:
                return None
            batch = getattr(widget, "_height_batch", None)
            if batch is not None:
                return batch
            widget = widget.parentWidget()
        return None

    def pin_layout_height(self, height: int) -> None:
        """T42 占位守恒：把卡片钉死在起步高度（min=max），重建瞬间容器总高不变。

        只作起步高度，不锁死：viewer 首个真实高度上报到达时由
        ``_unpin_layout_height`` 解除。方法形式（而非调用方直接 setFixedHeight）
        是为了让「钉死」带上标记，解除路径才有据可依。
        """
        try:
            self.setFixedHeight(max(1, int(height)))
            self._layout_height_pinned = True
        except RuntimeError, AttributeError:
            pass

    def _unpin_layout_height(self) -> None:
        """解除 T42 起步高度钉死，恢复高度由内容自适应。

        为什么必须解除：钉死后 viewer 高度上报只改 viewer 自身的固定高度，
        卡片的 min=max=H 不再跟随内容；H 与内容实际高度的差会被卡片布局压给
        仅有的两个可伸缩项（气泡容器 + 页脚栏）——页脚模型胶囊带边框，拉伸后
        成竖长条（真机畸变截图）；H 偏小时则裁掉内容底部。viewer 真实高度到达
        的此刻解除，内容自然高度 ≈ H，解除与 viewer 落高在同一次布局内收敛，
        无可见跳动（T42 防重建骤降的目的在此前窗口期已经达成）。
        """
        if not getattr(self, "_layout_height_pinned", False):
            return
        self._layout_height_pinned = False
        try:
            self.setMinimumHeight(0)
            self.setMaximumHeight(_QWIDGETSIZE_MAX)
        except RuntimeError:
            pass

    def _commit_viewer_height(self, height: int) -> None:
        """高度应用的统一出口。

        批量提交器激活时（resize 恢复期）交由它统一 flush —— 由它做
        「一次布局 + 一次锚点修正」，与高度到达顺序无关；否则按原行为直接
        应用。流式卡片始终走直接路径，保证跟底不受批处理延迟影响。
        """
        # viewer 真实高度到达 → T42 起步钉死完成使命，解除（幂等，未钉死零开销）
        self._unpin_layout_height()
        batch = getattr(self, "_height_batch", None)
        if batch is None:
            batch = self._resolve_height_batch()
        if batch is not None and batch.active and not self._streaming:
            batch.submit(self, height)
            return
        self.viewer.setFixedHeight(height)
        self.heightChanged.emit(height)

    def _apply_viewer_height(self, value):
        height = max(40, int(value))
        if height == self._last_applied_viewer_height:
            return
        # 🐛 记录本次高度增量：外层聊天列表（main_widget）据此做滚动锚定补偿
        # （Qt 滚动区无 scroll anchoring，卡片高度变化时视口内容会被推走）。
        self._last_height_delta = height - self.viewer.height()
        self._last_applied_viewer_height = height
        # [PERF] resize preview 期间 viewer 已 hide + setUpdatesEnabled(False)，
        # 此时 setFixedHeight 仍会触发 Qt 布局链 → QWebEngineView Chromium
        # 视口大小变化 → 整页 relayout → ResizeObserver → reportHeight →
        # _stream_height_timer 80ms 防抖 → 又一轮 setFixedHeight 的循环。
        # 是流式 + resize 卡顿的根因之一。仅记录目标高度，preview 退出时
        # set_resize_preview_mode(False) 一次性应用 + 上报，避免级联重排。
        if self._resize_preview_mode:
            self._pending_viewer_height = height
            return
        self._commit_viewer_height(height)
        # viewer 高度变化后 body 视口可能改变，仅在用户已处于底部时重新滚动到底部
        # 🐛 修复：当 MAX_HEIGHT 限制导致 body 首次出现溢出时，scrollTop=0，
        # wasAtBottom 永远为 false，auto-scroll 不触发。跟踪用户主动滚动行为，
        # 未滚动时强制 auto-scroll 到底部。
        if self._streaming and hasattr(self.viewer, "page") and self.viewer.page():
            try:
                # 🐛 修复：同步 auto-scroll 取代 setTimeout(0)，避免渲染间隙置顶闪烁
                # 🐛 修复：auto-scroll 成功后复位 _userScrolledWithin，
                # 防止用户一次滚轮操作后永久丧失粘性滚底能力。
                # 🐛 修复 race condition：打 auto-scroll 时间戳，防止异步派发的
                # scroll 事件在 _suppressScrollEvent=false 后被误判为用户主动滚动，
                # 导致后续流式输出卡顶部。
                self.viewer.page().runJavaScript(
                    "(function(){"
                    "  window._suppressScrollEvent = true;"
                    # 区域独立 II：高度回调由任何内容变化（含工具/思考区高度）触发，
                    # 不代表正文更新 → bodyOnly 不碰正文容器滚动位置
                    "  if (!window._userScrolledWithin) {"
                    "    _autoScrollStreamingBody(true);"
                    "  } else {"
                    "    var wasAtBottom = Math.abs(document.body.scrollHeight - document.body.scrollTop - document.body.clientHeight) < "
                    + str(AUTO_SCROLL_THRESHOLD)
                    + ";"
                    "    if (wasAtBottom) {"
                    "      _autoScrollStreamingBody(true);"
                    "      window._userScrolledWithin = false;"
                    "    }"
                    "  }"
                    "  window._prevScrollTop = document.body.scrollTop;"
                    "  window._autoScrollTime = performance.now();"
                    "  window._suppressScrollEvent = false;"
                    "})();"
                )
            except RuntimeError:
                pass

    def sync_width(self, force: bool = False, target_width: int | None = None):
        """同步卡片宽度

        Args:
            force: 是否强制更新，即使宽度没变化
            target_width: 显式指定目标宽度（用于 resize 期间绕过循环依赖）。
                          传入时直接使用此宽度，不再从 parent 推算。
        """
        if target_width is None:
            parent = self.parentWidget()
            if not parent:
                return
            parent_width = parent.width()
            if self.role == "user":
                # 与 main_widget 的比例 margin 保持一致（约容器 94%，留少量对齐余量）
                horizontal_margin = max(150, int(parent_width * 0.15))
            else:
                horizontal_margin = 20
            target_width = max(320, parent_width - horizontal_margin)

        # 性能优化：只有宽度真正变化时才更新（user/非 user 统一守卫）
        if not force and target_width == self._last_synced_width:
            return
        self._last_synced_width = target_width

        if self.role == "user":
            # 简洁气泡：释放最小宽，只设上限，
            # 实际宽度由 PlainTextViewer 按内容最长行自适应收缩
            self.setMinimumWidth(60)
            self.setMaximumWidth(target_width)
            # 🛡️ 卡片宽度下限抬到 footer 需求宽（时间戳 + hover 按钮）：
            # 否则窄气泡（如「你好」）的卡片会比 footer 窄，Qt 压缩布局时
            # 按钮会盖到时间戳上（2026-09-16 用户截图反馈）。
            # 气泡自身仍按内容收缩（_user_bubble 右对齐 + Maximum 策略），
            # 卡片多出的部分是透明留白，不影响气泡视觉宽度。
            footer = getattr(self, "_footer_wrap", None)
            if footer is not None:
                need = footer.minimumSizeHint().width()
                if need > 60:
                    self.setMinimumWidth(need)
            # 上限同步给 viewer（卡内边距 4*2 + viewer 布局边距 8*2），
            # cap 未变化时 set_width_cap 内部为 no-op。
            # 🐛 不受 _resize_preview_mode 拦截：preview 守卫是为 CodeWebViewer
            # （WebEngine 重排昂贵）设计的，PlainTextViewer 轻量无需保护；
            # 若在 resize 期间拦截，而退出 preview 时 user 卡片直接 return
            # 不补同步，气泡宽度/高度将永远停留在 resize 前的旧值，
            # 窗口缩小后固定尺寸的气泡超出可视区（文字跑到显示范围之外）。
            if self.viewer is not None:
                self.viewer.set_width_cap(target_width - 24)
            self._sync_identity_header_width()
            return

        # 非 user（assistant/welcome）：固定宽度（min=max）
        if self.minimumWidth() != target_width or self.maximumWidth() != target_width:
            self.blockSignals(True)
            self.setMinimumWidth(target_width)
            self.setMaximumWidth(target_width)
            self.blockSignals(False)

        # 宽度同步后触发 viewer 高度重算（CodeWebViewer 内容重排）
        if not self._resize_preview_mode and hasattr(self.viewer, "update_height"):
            self.viewer.update_height()

    def set_resize_preview_mode(self, enabled: bool):
        """在窗口 resize 期间切换到轻量占位模式，减少复杂子控件重绘。

        只有使用 CodeWebViewer 的卡片需要 placeholder 优化，
        PlainTextViewer（user 卡片）weight 很轻，不需要。
        """
        if enabled == self._resize_preview_mode:
            return

        # user 卡片使用 PlainTextViewer，weight 很轻，不需要 placeholder
        if self.role == "user":
            return

        # 懒渲染还没创建viewer，跳过（welcome 卡已创建 viewer 时同样走占位逻辑）
        # 🐛 D1：赋值必须在所有守卫之后。旧实现先置标志再判 viewer，未懒渲染的卡片
        # 会在 resize 周期里被标成「已占位」，而它什么都没隐藏。随后 ensure_rendered
        # 创建 viewer，_apply_viewer_height 命中该标志把真实高度写进
        # _pending_viewer_height 就 return → 卡片永久停在 40px 空白。恢复链队列是
        # _begin_restore_chain 时刻的快照，不会再回头看这张卡，只能等下一次 resize。
        if self.viewer is None:
            return

        self._resize_preview_mode = enabled

        if enabled:
            viewer_height = max(self.viewer.height(), self.viewer.minimumHeight(), 40)
            options_height = self.options_widget.sizeHint().height() if self.options_widget.isVisible() else 0
            self._resize_preview_height = max(40, viewer_height + options_height)
            self.resize_placeholder.setFixedHeight(self._resize_preview_height)
            self.resize_placeholder.show()
            self.viewer.setUpdatesEnabled(False)
            self.viewer.hide()
            self._options_were_visible_before_resize = self.options_widget.isVisible()
            if self._options_were_visible_before_resize:
                self.options_widget.setUpdatesEnabled(False)
                self.options_widget.hide()
            return

        self.viewer.show()
        self.viewer.setUpdatesEnabled(True)
        if self._options_were_visible_before_resize:
            self.options_widget.show()
            self.options_widget.setUpdatesEnabled(True)
        self.resize_placeholder.hide()
        self.resize_placeholder.setFixedHeight(0)
        self._resize_preview_height = 0
        self._options_were_visible_before_resize = False

        # [L3] 宽度→高度缓存命中：把已知高度作为本次恢复的目标，省掉等待
        # JS 异步回传的那一帧（窗口拖拽宽度往返时命中率很高）。
        # 只是预测：下方仍会发起异步上报，若真实高度不同会自动校正。
        if not self._streaming and self.role != "user":
            cached = self._height_cache.get(self._last_synced_width)
            if cached is not None:
                self._pending_viewer_height = cached

        if hasattr(self.viewer, "update_height"):
            self.viewer.update_height()

        # [PERF] 流式卡片：preview 期间累积的高度变化一次性应用。
        # _apply_viewer_height 在 preview 模式下只记录 _pending_viewer_height，
        # 此处 setFixedHeight 把 viewer 设到正确高度，触发一次 ResizeObserver →
        # reportHeight → Python 拿到真实高度，结束 preview 期间累积的高度死循环。
        # 只对 CodeWebViewer（非 PlainTextViewer）有效：PlainTextViewer 的
        # update_height() 已在上方调用且自身无 _pending_viewer_height 字段。
        pending_h = getattr(self, "_pending_viewer_height", None)
        if pending_h is not None and self.role != "user" and self.viewer is not None:
            try:
                # 🐛 从占位高度恢复到真实内容高度**存在真实增量**，不能清零：
                # 占位高度 = resize 前的旧 viewer 高度（+options），真实高度是新宽度
                # 下的重排高度，两者差值必须由外层做锚定补偿。旧实现写死 `= 0`
                # → 外层 `if delta and ...` 直接跳过 → 整个 resize 恢复期零补偿
                # → 视口被 N 张卡逐张推走（「一 resize 画面就很乱」的直接来源）。
                # 批量提交（HeightCommitBatch）激活时由 batch 统一接管并改用锚点修正。
                self._last_height_delta = pending_h - self.viewer.height()
                self._commit_viewer_height(pending_h)
            except RuntimeError:
                pass
            self._pending_viewer_height = None
            # 强制一次高度上报：让 JS 侧 ResizeObserver 也跟上 viewer 新高度，
            # 防止 Chromium 内部仍按旧高度布局（_apply_viewer_height 没真正
            # 改高度时 Chromium 视口尺寸未变）→ 首帧 paint 仍按旧宽排版。
            if hasattr(self.viewer, "page") and self.viewer.page():
                try:
                    self.viewer.page().runJavaScript("reportHeight();")
                except RuntimeError:
                    pass

    def _sync_identity_header_width(self) -> None:
        """身份行右缘与气泡对齐（宽度取内容需求与气泡宽的较大值）。

        身份行内容宽（头像 + 名称/时间列）常大于窄气泡宽（实测短消息气泡
        100px vs 身份行需 ~135px）。强行压到气泡宽会把时间文本截断成
        「09-16 23…」（2026-09-16 用户反馈的排布问题）。故取
        ``max(内容需求, 气泡宽)``：右缘与气泡严格对齐，宽出部分向左延伸
        （右对齐天然如此），视觉上仍贴着气泡。
        """
        header = getattr(self, "_identity_header", None)
        if header is None:
            return
        try:
            # 先解绑固定宽，才能拿到「不被压缩时」的真实内容宽
            header.setMinimumWidth(0)
            header.setMaximumWidth(16777215)
            need = header.sizeHint().width()
            bubble = getattr(self, "_user_bubble", None)
            target = need
            if bubble is not None and bubble.width() > 0:
                target = max(need, bubble.width())
            elif self.width() > 0:
                target = max(need, self.width() - 8)
            header.setFixedWidth(target)
        except RuntimeError:
            pass

    def enterEvent(self, event):
        # 用户气泡 / assistant 全减：hover 浮现操作按钮，保持静态简洁
        if self.role == "user" and getattr(self, "_user_action_btns", None) is not None:
            self._set_actions_visible(self._user_action_btns, True)
        elif self.role == "assistant" and getattr(self, "_assistant_action_btns", None) is not None:
            self._assistant_action_btns.setVisible(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        if self.role == "user" and getattr(self, "_user_action_btns", None) is not None:
            self._set_actions_visible(self._user_action_btns, False)
        elif self.role == "assistant" and getattr(self, "_assistant_action_btns", None) is not None:
            self._assistant_action_btns.setVisible(False)
        super().leaveEvent(event)

    @staticmethod
    def _set_actions_visible(container, visible: bool) -> None:
        """用**子按钮显隐**控显隐（容器常驻布局，不用 graphicsEffect）。

        两条踩过的经验：
        1. `container.setVisible(False)` 会让容器退出布局计算 → 父级 sizeHint
           变小 → 卡片宽度重排，hover 瞬间气泡被“撑长/变形”。
        2. 容器上用 `QGraphicsOpacityEffect` 控透明度同样不可取：卡片自身有
           fade_in 的 effect（见 fade_in_widget），**Qt 在父级已有 effect 时对
           子级 effect 的合成不可靠** —— 表现为控件「先显示一瞬间随后消失」。

        故改为：容器保持可见且尺寸固定（占位不变），只切换其内部按钮的
        setVisible，既不改布局尺寸，也不引入任何 effect。
        """
        try:
            for child in container.findChildren(QWidget):
                # 只切换直接承载内容的按钮（有 sizeHint 的子控件）
                if child.parent() is container:
                    child.setVisible(visible)
            container.setAttribute(Qt.WA_TransparentForMouseEvents, not visible)
        except RuntimeError:
            pass

    def wheelEvent(self, event: QWheelEvent):
        # MessageCard 的 wheelEvent 仅在子 widget（viewer）未消费事件时被调用。
        # 此时说明内部没有可滚动内容，或内部已达边界 → 转发外层滚动区，
        # 走 qfluentwidgets SmoothScroll，与卡片间隙滚动同款平滑手感。
        try:
            scroll_area = self._parent.chat_scroll_area
            if scroll_area:
                vbar = scroll_area.verticalScrollBar()
                if vbar and vbar.minimum() != vbar.maximum() and event.angleDelta().y() != 0:
                    scroll_area.wheelEvent(event)
                    event.accept()
                    return
        except Exception:
            pass
        super().wheelEvent(event)

    def update_content(self, txt):
        if self.role == "assistant" and not self._streaming:
            self.start_streaming_anim()
        if isinstance(txt, list):
            self.set_content(txt)
            return
        # 流式吞吐采样：累计输出原文，并**逐段累加出字时间**（不是首字至今）
        if self.role == "assistant" and isinstance(txt, str) and txt:
            now = time.time()
            self._stream_text_acc = (getattr(self, "_stream_text_acc", "") or "") + txt
            prev = getattr(self, "_stream_last_text_t", None)
            if prev is None:
                # 首个非空 chunk：仅记时刻（供 TTFT 语义），生成秒从 0 起算
                self._stream_first_text_t = now
            elif 0 < now - prev <= self._LIVE_GAP_CAP_S:
                self._stream_gen_s = getattr(self, "_stream_gen_s", 0.0) + (now - prev)
            self._stream_last_text_t = now
            # 按 chunk 节流刷新插件 stat（200ms）：1s tick 的采样窗口会整段漏掉
            # 快模型的短流式，导致流式期间始终无实时值、落定才闪现
            if now - getattr(self, "_last_live_stat_refresh", 0.0) >= 0.2:
                self._last_live_stat_refresh = now
                self._refresh_footer_stats(streaming=True)
        self.append_text(txt)

    def showEvent(self, event):
        """可见性恢复：窗口切回可见时补渲此前因不可见而推迟的欢迎卡片 QWebEngineView。

        批量建标签页时，非 current 标签页的欢迎卡片在 200ms 懒渲染队列触发
        ensure_rendered 时窗口不可见，被 _do_ensure_rendered 的可见性守卫推迟
        （设 _render_deferred=True 并 return，避免弹出幽灵窗口）。切回该标签页
        时本事件触发，此时父 HWND 已就绪，可安全创建 QWebEngineView。
        """
        super().showEvent(event)
        # 修 #2：用户气泡 viewer 懒加载——若已暂存待显示文本且 viewer 尚未创建，补建
        if self.role == "user" and self.viewer is None and getattr(self, "_viewer_pending_text", None):
            self._ensure_user_viewer()
        if getattr(self, "_render_deferred", False) and not getattr(self, "_lazy_rendered", False):
            self.ensure_rendered()

    def _is_effectively_visible(self) -> bool:
        """判断本卡片是否真正显示在屏幕上（有有效父 native window 供 QWebEngineView 附着）。

        仅用 isVisible() 不可靠：批量建标签页时，被 QStackedWidget 挤出 current 的
        隐藏页在某些时序下 isVisible() 仍返回 True，导致 QWebEngineView（Windows 上
        创建原生 HWND 子窗口）在缺少有效父句柄时弹出独立原生窗口（幽灵窗口/白窗一闪而过）。
        故直接检查本卡片是否在其所在 QStackedWidget 的当前页子树中。

        注意：必须遍历**所有**父链上的 QStackedWidget，而不是只检查第一个。
        Tab 管理器有嵌套两层 QStackedWidget——窗口级 _content_area 与外层覆盖级
        _content_stack（index 0 对话区 / index 1 系统卡片覆盖层）。若只检查第一层，
        覆盖层打开时（如项目选择卡片）对话区实际隐藏，但窗口级 currentWidget 仍是
        当前窗口 → 误判可见 → 创建 QWebEngineView 弹出幽灵窗口。
        """
        top = self.window()
        if top is None or not top.isVisible():
            return False
        # 沿父链查找 QStackedWidget（duck-typing，避免强依赖导入），逐层检查全部层级
        p = self.parentWidget()
        while p is not None:
            cur = getattr(p, "currentWidget", None)
            if cur is not None and callable(cur):
                current = cur()
                if current is None or not current.isAncestorOf(self):
                    return False
                # 本层通过，继续向上检查外层 QStackedWidget（覆盖层级）
            p = p.parentWidget()
        return True

    def _connect_viewer_signals(self):
        """连接 viewer → 卡片的全部信号。

        与 :meth:`_disconnect_viewer_signals` **成对维护**，两处写在一起是为了
        支持 WebView 池化：viewer 换卡片时必须先断开旧连接再连到新卡片。

        ⚠️ 转发一律 signal-to-signal 直连（``.connect(self.actionRequested)``），
        禁止 ``.connect(self.actionRequested.emit)``：bound ``.emit`` 对 PyQt
        是普通 Python callable，不绑定 receiver（卡片）生命周期——卡片被虚拟
        滚动回收销毁后连接残留，viewer 复用时信号触发即调用已析构对象
        → ``Qt5Core!QObject::signalsBlocked`` AV READ 0x0（2026-09-18 代码框
        按钮闪退根因）；且 ``disconnect(bound.emit)`` 永远抛 TypeError
        （每次访问 ``.emit`` 都是新对象，匹配不上），导致
        :meth:`_disconnect_viewer_signals` 静默失效。直连由 Qt 记录 receiver
        QObject，销毁自动断连，``disconnect(信号对象)`` 也可正常断开。
        """
        v = self.viewer
        if v is None:
            return
        v.codeActionRequested.connect(self.actionRequested)
        v.contextActionRequested.connect(self.contextActionRequested)
        v.contentHeightChanged.connect(self._update_height)
        v.toolDiffRequested.connect(self.toolDiffRequested)
        v.subAgentLogRequested.connect(self.subAgentLogRequested)
        v.saveFileRequested.connect(self.saveFileRequested)
        v.chartExpandRequested.connect(self._on_chart_expand)
        v.saveChartPngRequested.connect(self._on_save_chart_png)
        v.saveWidgetFileRequested.connect(self._on_save_widget_file)
        # 图片预览（T9-2）：池化路径漏连，非 user 卡片预览失灵存量 bug
        v.previewImageRequested.connect(self._on_preview_image)
        # WebEngine 上下文丢失处理
        v.contextLost.connect(self._on_webengine_context_lost)
        v.contextRestored.connect(self._on_webengine_context_restored)
        v.needRecreate.connect(self._on_webengine_need_recreate)
        # 安装对话框过滤
        v._install_dialog_filter()

    def _disconnect_viewer_signals(self):
        """断开 viewer → 卡片的全部信号（池化复用 / 摘除 viewer 前调用）。"""
        v = self.viewer
        if v is None:
            return
        pairs = (
            (v.codeActionRequested, self.actionRequested),
            (v.contextActionRequested, self.contextActionRequested),
            (v.contentHeightChanged, self._update_height),
            (v.toolDiffRequested, self.toolDiffRequested),
            (v.subAgentLogRequested, self.subAgentLogRequested),
            (v.saveFileRequested, self.saveFileRequested),
            (v.chartExpandRequested, self._on_chart_expand),
            (v.saveChartPngRequested, self._on_save_chart_png),
            (v.saveWidgetFileRequested, self._on_save_widget_file),
            (v.previewImageRequested, self._on_preview_image),
            (v.contextLost, self._on_webengine_context_lost),
            (v.contextRestored, self._on_webengine_context_restored),
            (v.needRecreate, self._on_webengine_need_recreate),
        )
        for signal, slot in pairs:
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass

    def detach_viewer(self) -> bool:
        """把 ``CodeWebViewer`` 摘下并归还复用池（卡片随后被卸载/销毁）。

        与「直接销毁」的区别：renderer 进程与已初始化的 WebContents 保留下来，
        下次有卡片需要 viewer 时直接复用实例，省掉一次 Chromium 初始化。

        Returns:
            是否成功归还到池。``False`` 表示未池化，调用方照旧销毁整张卡片。
        """
        viewer = self.viewer
        if viewer is None:
            return False
        # user 卡片走 PlainTextViewer；灰度中的纯 Qt 渲染器不参与池化
        if self.role == "user" or not isinstance(viewer, CodeWebViewer):
            return False
        # 流式输出中的卡片不可摘：摘掉会中断正在进行的渲染与高度回传
        if self._streaming:
            return False

        from app.widgets.webview_pool import WebViewPool

        light = bool(getattr(viewer, "_light_skeleton", False))
        try:
            self._disconnect_viewer_signals()
            self._viewer_layout.removeWidget(viewer)
            # 🛡️ 必须先 hide() 再 setParent(None)：CodeWebViewer 持有原生 HWND，
            # 可见状态下脱离父窗口树会让 Chromium 弹出独立原生窗口（白窗一闪），
            # 与 main_widget 里其它 detach 点（_clear_chat_area / ui_helpers）同一护栏。
            viewer.hide()
            viewer.setParent(None)
        except RuntimeError:
            return False
        self.viewer = None
        # 回到「未渲染」态。消息数据仍留在 _message_batch / _content_data，
        # 批次重建时由上层重新灌入，这里不需要暂存。
        self._lazy_rendered = False
        if WebViewPool.get_instance().release(viewer, light=light):
            return True
        # 入池失败：照旧销毁，绝不让 viewer 实例泄漏
        try:
            viewer.deleteLater()
        except RuntimeError:
            pass
        return False

    def ensure_rendered(self, delay_ms: int = 0):
        """如果还没渲染，懒加载创建QWebViewer并渲染内容

        Args:
            delay_ms: 延迟加载毫秒数。默认0立即加载，>0则延迟加载并发送信号
        """
        if self._lazy_rendered or self.role == "user":
            return

        def _do_ensure_rendered():
            # 🛡️ 防幽灵窗口：与 _schedule_render 对称，不可见时不创建 QWebEngineView。
            # QWebEngineView 在 Windows 上创建原生 HWND 子窗口（见 _hide_for_dialog 注释）。
            # 当 widget 所在窗口不可见（Tab 管理器中非 current 标签页）时，父链无有效
            # native window 句柄，Chromium 会弹出独立原生窗口（幽灵窗口）。
            # 快速批量建标签页时，只有最后一个标签页可见，前 N-1 个在 200ms 懒渲染队列
            # 触发 ensure_rendered 时已不可见 → 弹出幽灵窗口。
            # 修复：非当前可见标签页时标记 _render_deferred 并 return，等 showEvent（窗口切回可见）补渲。
            if not self._is_effectively_visible():
                self._render_deferred = True
                return
            # 移除占位符，创建真正的viewer
            for i in reversed(range(self._viewer_layout.count())):
                item = self._viewer_layout.itemAt(i)
                if item and item.widget():
                    item.widget().deleteLater()

            # welcome 卡片使用轻量骨架（无 echarts CDN）
            is_welcome = self.role == "welcome"
            if not is_welcome and _qt_renderer_enabled():
                # 灰度：纯 Qt 块级渲染器（无 Chromium/JS 层）
                self.viewer = _get_markdown_block_viewer_cls()(self)
                self.viewer.contentHeightChanged.connect(self._on_qt_viewer_height)
                # 直连（非 .emit 转发）：Qt 绑定 receiver 生命周期，viewer 销毁自动断连
                self.viewer.saveFileRequested.connect(self.saveFileRequested)
                # 仅"从磁盘加载的历史会话"折叠；本轮对话（流式进行中或已完成）
                # 保持展开 —— 后者若按 _streaming=False 判为历史，会在虚拟滚动
                # 回收重建后突然折叠，与首次渲染的展开态不一致。
                self.viewer._is_history = not (self._streaming or self._streaming_finished)
                # 🐛 流式态同步必须覆盖「已结束的本轮对话」：_is_history=False 只代表
                # 保持展开，不代表还在流式。Qt 渲染器同规则保持一致。
                if not self._streaming:
                    self.viewer._streaming = False
                self._viewer_layout.addWidget(self.viewer)
                self._lazy_rendered = True
                self._render_deferred = False
                if self._todos_snapshot is not None:
                    self._push_todo_list()
                if self._pending_content is not None:
                    self.set_content(self._pending_content)
                    self._pending_content = None
                elif self._pending_welcome_md is not None:
                    self.set_content(self._pending_welcome_md)
                    self._pending_welcome_md = None
                self.lazyRenderCompleted.emit()
                return
            # [池化] 优先复用池中实例：省掉一次 Chromium renderer/上下文初始化
            # （既有注释记录的量级是 100–500ms 主线程占用 + 视觉闪烁）。
            # 取不到（池空 / 池已停用）时回退到新建，行为与改造前一致。
            from app.widgets.webview_pool import WebViewPool

            pooled = WebViewPool.get_instance().acquire(light=is_welcome)
            if pooled is not None:
                try:
                    pooled.setParent(self)
                    pooled.setUpdatesEnabled(True)
                    # 🛡️ 与 detach_viewer 的 hide() 成对：显式隐藏过的 widget 不会
                    # 随父控件 show() 自动恢复可见，复用时必须显式 show()，
                    # 否则卡片区域是一片空白（viewer 存在但不可见）。
                    pooled.show()
                    # 高度兜底复位：丢弃上一张卡片钉死的高度（长消息可达数千 px），
                    # 避免骨架就绪前的窗口期显示成"巨高空白卡片"。
                    pooled.setMinimumHeight(40)
                    pooled.setFixedHeight(40)
                    # 🐛 复用卡片空白防护：正常路径下 reset_for_reuse 会**保留骨架**
                    # （只清空内容），JS 仍是就绪的，这里无需任何重载 —— 复用成本
                    # 因此只剩一次轻量 DOM 清理（大会话加载时的内存/CPU 关键）。
                    # 仅当骨架根本没加载成功（_is_js_ready=False，如首次加载被打断）
                    # 才补一次 _load_skeleton，避免把 runJavaScript 打在空页上
                    # （updateContent 不存在 → 静默失败 → 卡片永久空白）。
                    if not getattr(pooled, "_is_js_ready", False):
                        pooled._load_skeleton()
                    self.viewer = pooled
                except Exception:
                    # 骨架重载失败（C++ 对象已删除等）：弃用该实例，回退新建
                    try:
                        pooled.deleteLater()
                    except Exception:
                        pass
                    self.viewer = None
            if self.viewer is None:
                self.viewer = CodeWebViewer(self, light=is_welcome)
            self.viewer._lazy_markdown_cb = self._build_incremental_md
            if not is_welcome:
                # 标记是否为历史会话：非流式加载的历史消息自动折叠工具区
                # 仅"从磁盘加载的历史会话"折叠；本轮对话（流式进行中或已完成）
                # 保持展开 —— 后者若按 _streaming=False 判为历史，会在虚拟滚动
                # 回收重建后突然折叠，与首次渲染的展开态不一致。
                self.viewer._is_history = not (self._streaming or self._streaming_finished)
                # 🐛 流式态同步必须覆盖「已结束的本轮对话」：_is_history=False 只代表
                # 保持展开，不代表还在流式。新建 viewer 的 _streaming 初始 True（流式
                # 设计），池化实例经 reset_for_reuse 置 False——两条路径初始值不一致，
                # 若只处理历史分支，新建重建的已结束卡片会残留 _streaming=True →
                # 渲染走流式分支（流式形态/字数统计），叠加 _on_js_ready 坞态兑底
                # 误开 → 卡片重现流式结构（多 tab 并行时配额收缩+池被争抢，回退
                # 新建概率大增，后台标签页切回即触发）。
                if not self._streaming:
                    self.viewer._streaming = False
                # 让 viewer 的 restore 逻辑知道哪些工具结果已到达，
                # 避免全量重渲染时把已完成的运行框以“运行中”状态复活。
                self.viewer._restore_finished_ids = self._finished_streaming_ids
            self._connect_viewer_signals()

            self._viewer_layout.addWidget(self.viewer)
            self._lazy_rendered = True
            # 创建 viewer 完成（不可见门控已放行），清除"推迟渲染"标记；
            # 若下方 set_content 因 JS 未就绪再次 deferred，由 _on_js_ready 兜底补渲。
            self._render_deferred = False

            # 任务列表随 viewer 创建补推（JS 未就绪时由 _on_js_ready 兜底）
            if self._todos_snapshot is not None:
                self._push_todo_list()

            # 如果有等待渲染的内容，现在渲染
            if self._pending_content is not None:
                self.set_content(self._pending_content)
                self._pending_content = None
            elif self._pending_welcome_md is not None:
                # 欢迎卡片懒渲染：set_welcome_content 在 viewer 创建前存的内容
                self.set_content(self._pending_welcome_md)
                self._pending_welcome_md = None

            # 通知懒渲染完成，让父组件可以修正滚动位置
            self.lazyRenderCompleted.emit()

        if delay_ms > 0:
            # 延迟加载，批量处理减少卡顿
            QTimer.singleShot(delay_ms, _do_ensure_rendered)
        else:
            _do_ensure_rendered()

    def set_content(self, content: Any):
        # [L3] 内容整体替换：此前的「宽度→高度」预测全部失效
        self._height_cache.clear()
        if self.role == "assistant":
            self._content_data = ensure_content_blocks(content)
            rendered = content_to_markdown(self._content_data)
            # [PERF] 內容已整體替換，失效舊工具塊 markdown 緩存
            if self.viewer and hasattr(self.viewer, "_tool_md_cache"):
                self.viewer._tool_md_cache.clear()
        else:
            # 用户消息支持 multimodal 内容（含图片块的列表）
            if isinstance(content, list):
                # 使用 content_to_text 正确提取文本，图片块转为 [图片] 占位符
                self._content_data = content
                rendered = content_to_text(content)
            else:
                self._content_data = str(content or "")
                rendered = self._content_data

        if not self._lazy_rendered:
            # 懒渲染阶段，保存内容等待进入可视区域
            self._pending_content = content
            return

        # 修 #2：用户气泡 viewer 懒加载，首次 set_content 时创建（set_text 前确保存在）
        if self.role == "user" and self.viewer is None:
            self._ensure_user_viewer()
        if hasattr(self.viewer, "_markdown_text"):
            self.viewer._markdown_text = rendered
            # [B1] 内容整体替换：使差量渲染缓存失效（_stable_md_len 指向旧内容偏移，
            # 继续差量会重复渲染旧段），强制下次全量渲染建立新基线。
            self.viewer._needs_full_render = True
            self.viewer._stable_html = ""
            self.viewer._stable_md_len = 0
            self.viewer._schedule_render(immediate=True)
        elif hasattr(self.viewer, "set_text"):
            self.viewer.set_text(rendered)
        self._content_just_loaded = True

    def rerender_custom_blocks(self, plugin_name: str = "") -> bool:
        """插件热重载后重绘该插件渲染的自定义内容块（custom block）

        已渲染消息的 HTML 是加载时刻的快照：content renderer 的 render_func
        在 content_to_markdown 时执行一次，热重载不会自动重绘。本方法
        检测本卡片是否包含属于该插件的 custom 块（plugin_name 为空 = 全部），
        命中则用最新 render_func 重新生成 markdown 并刷新视图；未命中零开销。

        Returns:
            True 表示已重绘；False 表示本卡片无该插件的 custom 块（无需处理）。
        """
        blocks = getattr(self, "_content_data", None)
        if self.role != "assistant" or not blocks:
            return False
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

        registry = UIPluginRegistry.get_instance()
        hit = False
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "custom":
                continue
            custom_type = block.get("custom_type", "")
            if not custom_type:
                continue
            info = registry.get_content_renderer(custom_type)
            if info is not None and (not plugin_name or info.plugin_name == plugin_name):
                hit = True
                break
        if not hit:
            return False
        rendered = content_to_markdown(blocks)
        if not self._lazy_rendered:
            # 懒渲染尚未执行：无需主动重绘，下次 ensure_rendered 自然用新 render_func
            return True
        if hasattr(self.viewer, "_markdown_text"):
            self.viewer._markdown_text = rendered
            # 内容整体替换：失效差量渲染缓存，强制全量渲染建立新基线（同 set_content）
            self.viewer._needs_full_render = True
            self.viewer._stable_html = ""
            self.viewer._stable_md_len = 0
            if hasattr(self.viewer, "_tool_md_cache"):
                self.viewer._tool_md_cache.clear()
            self.viewer._schedule_render(immediate=True)
        elif hasattr(self.viewer, "set_text"):
            self.viewer.set_text(rendered)
        return True

    # ── 增量 markdown 構建（性能優化）───────────────
    def _build_incremental_md(self) -> str:
        """增量構建 markdown：已完成的 tool_result 塊從緩存讀取，跳過昂貴的全量重建

        Profile 實測：
        - 純文本 10 block: 1.7 μs（極快，不緩存）
        - 5 個工具結果: 319 μs → 首次 ~64 μs/塊，之後每次渲染 0 μs
        - 30 個工具結果: 2,428 μs → 首次 ~81 μs/塊，之後每次渲染 0 μs

        對 tool_streaming / custom 等未知類型自動回退 content_to_markdown。
        """
        content = self._content_data
        if isinstance(content, str) or not isinstance(content, list):
            return content_to_markdown(content)

        cache = getattr(self.viewer, "_tool_md_cache", {}) if self.viewer else {}
        parts: List[str] = []

        for block in content:
            if not isinstance(block, dict):
                parts.append(str(block))
                continue

            bt = block.get("type")

            if bt == "tool_result":
                tid = block.get("tool_call_id", "")
                cached = cache.get(tid)
                if cached is not None:
                    parts.append(cached)
                    continue
                # 懶緩存：首次遇到未緩存的工具塊，轉換後快取
                single_md = content_to_markdown([block])
                if tid:
                    cache[tid] = single_md
                parts.append(single_md)

            elif bt == "text":
                text = str(block.get("text", ""))
                if text:
                    parts.append(text)

            elif bt == "reasoning":
                reasoning_content = str(block.get("content", "") or "")
                if reasoning_content:
                    parts.append(f"<think>{reasoning_content}</think>")

            elif bt in ("image_url", "input_image", "image"):
                image_url = ""
                if bt == "image_url":
                    image_data = block.get("image_url", {}) or {}
                    image_url = str(image_data.get("url", ""))
                if image_url and image_url.startswith("data:image"):
                    parts.append("![image](uploaded_image)")
                elif image_url:
                    parts.append(f"![image]({image_url})")
                else:
                    parts.append("[图片]")

            else:
                # 未知類型（含 tool_streaming / custom）→ 全量回退
                return content_to_markdown(content)

        return "\n\n".join(part for part in parts if part).strip()

    def append_text(self, text: str, immediate_render: bool = True):
        """追加文本内容。

        Args:
            immediate_render: 批量加载路径传 False（T11），把本函数内的
                immediate 渲染请求降级为合并派发，避免 N 卡同帧全量重渲。
                非 immediate 的一处调用不受影响（本就合并）。
        """
        # [L3] 内容增长：此前的「宽度→高度」预测失效（流式期间本就不写缓存，
        # 这里兜底处理流式中途插入内容等路径）
        self._height_cache.clear()
        if self.role == "user" and self.viewer is None:
            self._ensure_user_viewer()
        if self.role == "assistant":
            # 🆕 Bug B 方案 F：优先**原地**追加文本块（不重建列表）。
            # append_text_block 在末尾非 text 块时走 ensure_content_blocks 重建整个
            # 列表 → 所有块 dict 对象被替换 → _tool_anchor_refs 对象引用失效 →
            # append_tool_result 稳定锚点退化 → finish 完整重渲染时思考/工具顺序错乱。
            # 流式中 _content_data 已是标准块列表，原地追加/合并保住引用稳定。
            if (
                isinstance(self._content_data, list)
                and self._content_data
                and isinstance(self._content_data[-1], dict)
                and self._content_data[-1].get("type") == "text"
            ):
                self._content_data[-1]["text"] = str(self._content_data[-1].get("text", "") or "") + str(text or "")
            elif isinstance(self._content_data, list) and all(isinstance(b, dict) for b in self._content_data):
                # 直接构造 text 块（避免 make_text_block 未导入），保持原地 append 不重建
                self._content_data.append({"type": "text", "text": str(text or "")})
            else:
                self._content_data = append_text_block(self._content_data, text)
            # 优化：懒渲染模式下直接跳过 markdown 渲染，避免不必要的计算
            if not self._lazy_rendered or not self.viewer:
                self._pending_content = self._content_data
                return
            # [PERF] 增量 markdown 構建：已完成的 tool_result 塊走緩存，只有文本塊即時轉換
            self.viewer._lazy_markdown_cb = self._build_incremental_md
            # 🆕 检测未闭合 <think> 标签：静默累积不触发渲染，与 append_reasoning 策略一致
            # 避免每个思考文本 chunk 都触发全量渲染 → reorganizeContent → think-streaming
            # DOM 节点反复 destroy+recreate 导致"思考中"状态闪烁。
            # 🆕 检测未闭合 <think> / 插件注册 tag / 渲染型 fence：静默累积不触发渲染，与 append_reasoning 策略一致
            last_block = self._content_data[-1] if self._content_data else None
            last_text = last_block.get("text", "") if isinstance(last_block, dict) else ""
            _think_unclosed = _has_unclosed_think(last_text)
            _tag_unclosed = _has_unclosed_registered_tag(last_text)
            _fence_unclosed = _has_unclosed_chart_fence(last_text)
            # 流式模式下增量追加纯文本到 DOM，让用户立即看到文字。
            # 🐛 修复（高块闪现）：think 未闭合期间**不**调用 _append_text_incremental ——
            # 否则思考内容会以普通正文逐行注入 #content-placeholder 堆叠成高块，
            # 待 </think> 闭合后才由 _inject_think_cards 折叠成 think-compact，高块
            # 闪现后消失。与 append_reasoning 一致：未闭合期间静默累积、仅靠全量
            # 渲染落地；think 已闭合 / 无 think 标签时保持原有增量注入行为不变。
            # 插件注册 tag（<mood> 等）与渲染型 fence（```echarts 等）未闭合同样
            # 跳过增量注入：生肉进 DOM 后会被全量渲染的卡片/图表替换 →
            # "文字/代码先流式出现又消失"
            if self._streaming and not _think_unclosed and not _tag_unclosed and not _fence_unclosed:
                self.viewer._append_text_incremental(text)
            if _think_unclosed:
                if not self.viewer._think_text_streaming_started:
                    # 首 chunk：立即渲染一次显示"深度思考中..." spinner
                    self.viewer._think_text_streaming_started = True
                    self.viewer._thinking_finalized = False
                    self.viewer._schedule_render(immediate=immediate_render)
                # 后续 chunk：静默累积，不触发渲染/高度更新
                self._content_just_loaded = True
                return
            # 🐛 插件注册 tag（<mood> 等）未闭合：增量注入已跳过（与 think 同策略）。
            # 状态翻转（tag 首现/闭合）时强制全量渲染一次——差量快路径的闭合段
            # append 与 tail 行内均被 tag 守卫拦截，占位行（"解析中…"）与完整
            # 卡片只能由全量管线的 _inject_tag_cards 产出；闭合翻转也走全量，
            # 卡片即刻展开而无需等下一个边界。
            if _tag_unclosed != getattr(self.viewer, "_tag_text_streaming", False):
                self.viewer._needs_full_render = True
                self.viewer._schedule_render(immediate=immediate_render)
            self.viewer._tag_text_streaming = _tag_unclosed
            # <think> 已闭合或无 think 标签：恢复正常渲染
            self.viewer._think_text_streaming_started = False
            # 恢复 _thinking_finalized：避免 _render_markdown_to_html 误剥离
            # 末尾闭合的 </think>（仅 reasoning_content 路径需要此行为）
            self.viewer._thinking_finalized = True
            # ── 差量渲染（2026-07-22）──
            # 文字即时性已由 _append_text_incremental 保证。全量 HTML 渲染
            # 仅在自然边界触发（段落结束 / 块闭合 / 句号软边界），非边界时只启安全定时器。
            # last_text 已通过 append_text_block 包含新追加文本，判断可靠。
            # [PERF] 软边界（句号）不再 immediate —— 与 append_chunk /
            # _schedule_render 保持一致，交由内部 90ms 短定时器合并。
            if self._streaming and self.viewer._has_reached_clean_boundary(last_text):
                self.viewer._schedule_render(immediate=immediate_render)
            else:
                self.viewer._schedule_render(immediate=False)
            self._content_just_loaded = True
            return

        self._content_data = str(self._content_data or "") + str(text or "")
        if self.viewer:
            self.viewer.append_chunk(str(text or ""))
            self._content_just_loaded = True

    def _tool_anchor_pos(self, tool_call_id: str) -> Optional[int]:
        """返回工具调用时刻的稳定逻辑位置（append_tool_result 插入位 / data-order 基准）。

        🆕 Bug B 方案 F：优先用块引用锚点——index(ref) + 1 在列表因其他工具结果
        插入、思考/正文追加而偏移后仍精确指向"工具调用时刻的逻辑末尾"。int 索引锚点
        （_tool_insert_anchors）在偏移后失效，是 finish 完整重渲染时"思考在前、
        工具在后"数据层错乱的根因。

        Returns:
            稳定位置（0..len）；无任何锚点（历史会话等非流式路径）→ None（调用方兜底 append）。
        """
        ref = self._tool_anchor_refs.get(tool_call_id)
        if ref is not None and isinstance(self._content_data, list):
            # 用对象身份（is）定位，避免内容相同的不同块误匹配
            for _i, _b in enumerate(self._content_data):
                if _b is ref:
                    return _i + 1
        return self._tool_insert_anchors.get(tool_call_id)

    def append_tool_result(
        self,
        tool_name: str,
        arguments: Dict[str, Any] = None,
        result: Any = None,
        success: bool = True,
        tool_call_id: str = None,
        diff: str = None,
        echarts: str = None,
    ):
        block = make_tool_result_block(
            tool_name=tool_name,
            arguments=arguments,
            result=result,
            success=success,
            tool_call_id=tool_call_id,
            diff=diff,
            echarts=echarts,
        )
        # 🆕 Bug B（顺序错乱修复）：按锚点插入（工具调用发生的位置），而非恒 append 末尾。
        # 锚点 = 工具调用时 _content_data 的长度（update_tool_streaming 记录），
        # 使"思考→工具→正文→工具→正文"按实际到达顺序交错，而不是思考恒顶部、
        # 工具恒底部。同锚点多工具（一轮并行调用）：跳过所有"启动序号更早"的
        # 已插入工具块插到其后，保证按调用顺序排列（结果晚到也不乱序）。
        # 乱序兜底：无锚点（历史会话渲染等非流式路径）/ content 非 list / 锚点越界
        # → append 末尾，与修复前行为一致。
        # 🆕 Bug B 方案 F：用**块引用**锚点定位（_tool_anchor_pos），替代 int 索引。
        # int 索引在"其他工具结果插入 + 思考/正文追加"后偏移，导致工具结果插到
        # 错误位置 → _content_data 顺序错 → finish 完整重渲染时思考/工具错乱。
        anchor = self._tool_anchor_pos(tool_call_id) if isinstance(self._content_data, list) else None
        if anchor is not None and isinstance(self._content_data, list) and 0 <= anchor <= len(self._content_data):
            my_order = self._tool_call_order.get(tool_call_id, 0)
            insert_at = anchor
            _n = len(self._content_data)
            while insert_at < _n:
                _blk = self._content_data[insert_at]
                if isinstance(_blk, dict) and _blk.get("type") == "tool_result":
                    _tid = _blk.get("tool_call_id", "")
                    if self._tool_call_order.get(_tid, 0) < my_order:
                        insert_at += 1
                        continue
                break
            self._content_data.insert(insert_at, block)
        else:
            self._content_data.append(block)
        # 标记为已完成：后续 streaming 更新直接跳过，避免在完成态工具块上
        # 错误挂载 data-streaming 属性导致样式混乱
        if tool_call_id:
            self._finished_streaming_ids.add(tool_call_id)
        # 优化：懒渲染模式下直接跳过 markdown 渲染，避免不必要的计算
        if not self._lazy_rendered or not self.viewer:
            self._pending_content = self._content_data
            return
        # 🐛 就近恢复 viewer 流式模式：finish_streaming 后 viewer._streaming=False，
        # 但工具结果可能在新一轮流式开始后才到达。先恢复再更新 callback，
        # 与 start_streaming_anim 中的恢复形成双重保险。
        if self.viewer and not self.viewer._streaming:
            self.viewer._streaming = True
        # 同步已完成工具集合给 viewer，供 restore 逻辑判断运行框是否可复活
        if self.viewer:
            self.viewer._restore_finished_ids = self._finished_streaming_ids
        # [PERF] 預計算並緩存此工具塊的 markdown，後續增量渲染直接拼接
        # 避免 content_to_markdown 遍歷全部 _content_data 做 _sanitize_result + 排序
        if tool_call_id and self.viewer:
            single_md = content_to_markdown([block])
            cache = getattr(self.viewer, "_tool_md_cache", None)
            if cache is not None:
                cache[tool_call_id] = single_md
        # 增量注入：直接通过 JS 追加工具块 HTML，跳过全量 markdown 重建
        # 避免 content_to_markdown() 遍历全部 content_data 持有 GIL 导致拖动卡顿
        try:
            # 编辑类工具注入到 content-placeholder，跳过回调与渲染避免闪烁。
            # DOM 已通过 JS 注入到位，markdown 缓存已就绪供后续全量渲染使用。
            _is_edit_tool = tool_name in _edit_tools()
            # 🐛 修复（停止吞框）：编辑工具结果也必须重设懒回调。finish_streaming
            # （停止/流式结束）的非流式渲染消费 _lazy_markdown_cb 刷新 _markdown_text；
            # 旧逻辑编辑分支跳过渲染时连 cb 一起跳过 → cb 停留 None（上一次流式渲染
            # 已消费）→ 停止渲染用旧 md（不含工具块）→ save 移除 DOM 完成框后，
            # restore 因 tid 已入 _finished_streaming_ids 不恢复 → 完成框被吞（永久消失）。
            # 此处只设 cb 不触发渲染，保持"编辑工具跳过即时渲染防闪烁"设计不变。
            self.viewer._lazy_markdown_cb = self._build_incremental_md
            if not _is_edit_tool:
                self.viewer._schedule_render(immediate=True)
            else:
                # 🐛 修复（编辑工具完成框被吞·在途异步渲染）：编辑工具不触发渲染
                # （防闪烁），但必须作废在途异步渲染——其 HTML 快照不含本工具完成块，
                # 落地时 save 把 DOM 完成框 el.remove()，restore 又因该 id 已 finished
                # 跳过恢复 → 完成框永久消失（长内容非流式渲染的时间窗口）。
                _invalidate = getattr(self.viewer, "invalidate_inflight_render", None)
                if _invalidate is not None:
                    _invalidate()

            # 简洁模式：工具块默认折叠；非简洁模式：默认展开便于查看结果
            _collapsed = self.viewer._tool_compact_mode if self.viewer else True
            block_html = render_tool_block(
                tool_name=tool_name,
                tool_args=arguments or {},
                result=str(result) if result is not None else None,
                success=success,
                collapsed=_collapsed,
                tool_call_id=tool_call_id,
                diff=diff,
                echarts=echarts,
            )
            # 🆕 方案 D：计算完成态工具块的 data-order（与 _inject_tool_streaming_html
            # 同口径）。无锚点（历史会话等非流式路径）→ 基准取当前末尾位置，与
            # _content_data.append 兜底行为一致（沉底不早于任何已有块）。
            # 🆕 Bug B 方案 F：用块引用锚点定位稳定位置（int 索引在列表偏移后失效）。
            _anchor = self._tool_anchor_pos(tool_call_id)
            _order = self._tool_call_order.get(tool_call_id) or 0
            _base = float(_count_think_tool_prefix(self._content_data, _anchor))
            _order_value_js = f"{_base + _order * 0.001:.3f}"
            safe_html = json.dumps(block_html).decode("utf-8")

            # 提取 inner HTML（去掉外层 <div> 包装），用于原地更新已有 DOM 节点
            # outerHTML 替换会销毁旧元素再创建新元素，在 WebEngine 渲染管线中
            # 可能形成"旧元素消失 → 新元素出现"的跨帧闪烁。
            # 原地更新保持同一 DOM 节点，消除闪烁。
            _inner_match = re.match(r"^<div[^>]*>(.*)</div>$", block_html, re.DOTALL)
            if _inner_match:
                inner_html = _inner_match.group(1).strip()
            else:
                inner_html = block_html  # 兜底：整个当作 inner HTML
            safe_inner = json.dumps(inner_html).decode("utf-8")

            # 提取外层 <div> 的 style 属性（如 display: flex; align-items: center;）
            # 用于 INLINE_TOOLS 原地转换时应用到现有元素，保持 flex 布局
            _outer_style_match = re.search(r'<div[^>]*\sstyle="([^"]*)"', block_html)
            outer_style = _outer_style_match.group(1) if _outer_style_match else ""
            safe_outer_style = json.dumps(outer_style).decode("utf-8")

            # 提取 block_key（用于设置 data-block-key 属性）
            _key_match = re.search(r'data-block-key="([^"]*)"', block_html)
            block_key = _key_match.group(1) if _key_match else ""

            # ── 增量更新解析：将 inner_html 拆分为 button 和 body 两部分 ──
            # 避免 existing.innerHTML = safe_inner 整体替换导致的子节点空窗期
            # （外层 div 子节点清空瞬间 margin 暴露为可见间距，详见 #间距修复）
            _btn_match = re.match(r"<button[^>]*>(.*?)</button>", inner_html, re.DOTALL)
            _body_match = re.search(r'<div[^>]*class="cm-collapsible__body"[^>]*>(.*)</div>$', inner_html, re.DOTALL)
            if _btn_match and _body_match:
                btn_inner = _btn_match.group(1)
                body_inner = _body_match.group(1)
                # 提取 body div 上可能携带的 style（如 expanded: height:auto）
                _body_style_match = re.search(r'<div[^>]*class="cm-collapsible__body"[^>]*style="([^"]*)"', inner_html)
                body_style = _body_style_match.group(1) if _body_style_match else ""
                safe_btn_inner = json.dumps(btn_inner).decode("utf-8")
                safe_body_inner = json.dumps(body_inner).decode("utf-8")
                safe_body_style = json.dumps(body_style).decode("utf-8")
                _use_incremental = "true"
            else:
                safe_btn_inner = safe_body_inner = safe_body_style = '""'
                _use_incremental = "false"

            # 编辑类工具始终注入到正文区域，不进入"工具与思考"
            tool_target = "content-placeholder" if tool_name in _edit_tools() else self.viewer._tool_target_id

            js_code = f"""
            (function() {{
                var tc = document.getElementById('{tool_target}');
                if (!tc) {{
                    tc = document.getElementById('content-placeholder');
                }}
                // 🐛 修复（完成框沉底·就地插位）：完成块注入/转换后立即按 data-order
                // 插到正确位置，不依赖后续 reorganizeContent。工具完成后的渲染可能
                // 走差量快路径（不跑排序）或不再有下一拍（S1：正文先于工具结束，
                // 终渲染已落地）→ replaceChild/appendChild 的物理位置（restore 恢复
                // 的底部）永久固化 → 完成框沉底。跳过运行中块（1e9 沉底语义）与
                // 无 data-order 块；仅在工具区容器生效（编辑类工具保留正文语义，
                // 不参与 order 重排）。
                var _tgt = null;
                function _insertByOrder(el, container) {{
                    if (container.id !== 'tool-content') return;
                    var od = parseFloat(el.getAttribute('data-order'));
                    if (isNaN(od)) return;
                    var kids = container.children;
                    for (var i = 0; i < kids.length; i++) {{
                        var k = kids[i];
                        if (k === el) continue;
                        if (k.classList && k.classList.contains('tool-streaming-block')) continue;
                        var kod = parseFloat(k.getAttribute('data-order'));
                        // 🐛 用 >= 而非 >：data-order 存在双尺度（JS 注入块 = 锚点前
                        // think/tool 计数；D+ 补齐块 = 容器 blocks 序号），跨尺度相等
                        // 时（如工具 od=1.0 与紧随的思考 od=1.0）严格大于永远找不到
                        // 插入点 → 沉底滞留。相等时插到该块之前，语义正确（工具在
                        // 其调用位置之后、后续思考之前）；同锚点多工具由 0.001 细分，
                        // 不受影响。
                        if (!isNaN(kod) && kod >= od) {{ container.insertBefore(el, k); return; }}
                    }}
                }}
                // [sink-diag] 工具区快照（物理顺序 vs data-order），DRIFOX_SINK_DIAG=1 时回传 Python 打日志
                function _snap() {{
                    var out = [];
                    for (var i = 0; i < tc.children.length; i++) {{
                        var k = tc.children[i];
                        out.push([k.getAttribute('data-tool-call-id'),
                                  (k.getAttribute('data-block-key') || '').slice(0, 10),
                                  k.getAttribute('data-order'),
                                  (k.className || '').slice(0, 40)]);
                    }}
                    return out;
                }}
                // 优先查找已有流式块（同一 tool_call_id），原地转换为完成态块
                var existing = document.querySelector('[data-tool-call-id="{tool_call_id}"]');
                if (existing) {{
                    // 🐛 修复：检测 existing 是否为流式态块（tool-streaming-block）。
                    // 流式态块内部是 spinner + preview text，没有 .cm-collapsible__summary
                    // 和 .cm-collapsible__body 子元素，增量更新查找返回 null，
                    // 仅改 className 不改内部结构，导致运行框卡在"运行中"。
                    // 对流式态块走 outerHTML 整体替换为完成态折叠框。
                    var _isStreamingBlock = existing.classList.contains('tool-streaming-block');
                    if (_isStreamingBlock) {{
                        // 🆕 方案 D：替换流式块时继承原 data-order（JS 注入块的排序位置），
                        // 避免完成态块丢失 data-order 后在 reorganizeContent 中 getPos=1e9
                        // 沉底，导致"思考在前、工具在后"的顺序错乱。
                        var _odOld = existing.getAttribute('data-order');
                        var _wrap = document.createElement('div');
                        _wrap.innerHTML = {safe_html};
                        var _newBlock = _wrap.firstElementChild;
                        if (_newBlock && existing.parentNode) {{
                            if (_odOld) {{
                                _newBlock.setAttribute('data-order', _odOld);
                            }} else {{
                                _newBlock.setAttribute('data-order', {_order_value_js});
                            }}
                            existing.parentNode.replaceChild(_newBlock, existing);
                            _tgt = _newBlock;
                        }}
                    }} else {{
                        // 原地更新：保持同一 DOM 节点，只替换 className / 属性
                        // 避免 outerHTML 销毁+重建导致的"消失再出现"闪烁
                        _tgt = existing;
                        existing.className = 'cm-collapsible tool-block';
                        existing.setAttribute('data-block-key', '{block_key}');
                        existing.setAttribute('data-expanded', 'false');
                        existing.removeAttribute('data-streaming');
                        existing.removeAttribute('data-tool-injected');
                        existing.setAttribute('style', {safe_outer_style});
                        // 🆕 方案 D：原地更新保留原 data-order；缺失时注入（兜底）
                        if (!existing.getAttribute('data-order')) {{
                            existing.setAttribute('data-order', {_order_value_js});
                        }}

                        if ({_use_incremental}) {{
                            var btn = existing.querySelector('.cm-collapsible__summary');
                            if (btn) btn.innerHTML = {safe_btn_inner};
                            var body = existing.querySelector('.cm-collapsible__body');
                            if (body) {{
                                body.innerHTML = {safe_body_inner};
                                if ({safe_body_style}) {{
                                    body.setAttribute('style', {safe_body_style});
                                }}
                            }}
                        }} else {{
                            existing.innerHTML = {safe_inner};
                        }}
                    }}
                    // 就地插位 + 快照回传（修复完成框沉底：不依赖后续渲染的排序修正）
                    if (_tgt) _insertByOrder(_tgt, tc);
                    // 确保 tool-section 可见
                    if (window._toolCompactMode) {{
                        var ts = document.getElementById('tool-section');
                        if (ts) {{ ts.style.display = ''; _updateToolSectionHeader(); }}
                    }}
                    // 区域独立 II：完成块替换是纯工具区更新 → bodyOnly 不碰正文容器
                    window._suppressScrollEvent = true;
                    if (!window._userScrolledWithin) {{
                        _autoScrollStreamingBody(true);
                    }} else {{
                        var _bd = Math.abs(document.body.scrollHeight - document.body.scrollTop - document.body.clientHeight);
                        if (_bd < {AUTO_SCROLL_THRESHOLD}) {{
                            _autoScrollStreamingBody(true);
                            window._userScrolledWithin = false;
                        }}
                    }}
                    window._prevScrollTop = document.body.scrollTop;
                    window._autoScrollTime = performance.now();
                    window._suppressScrollEvent = false;
                    if (typeof _scrollToolContentToBottom === 'function') _scrollToolContentToBottom();
                    reportHeight();
                    return _snap();
                }}
                // 无已有流式块时，追加新块（兜底逻辑）
                // 🐛 修复：不使用包装器 div（createElement+innerHTML+appendChild），
                // 改为直接追加 .tool-block 元素到 #tool-content。
                // 原包装器 div 不是 .tool-block，无 data-tool-call-id，
                // reorganizeContent 排序时 getPos=1e9 → 永远沉底，也无法被清理。
                var _wrap = document.createElement('div');
                _wrap.innerHTML = {safe_html};
                var _newBlock = _wrap.firstElementChild;
                if (_newBlock) {{
                    // 🆕 方案 D：追加完成块时注入 data-order（与流式注入同口径），
                    // 保证下次 reorganizeContent 排序能回到正确位置而非恒沉底。
                    _newBlock.setAttribute('data-order', {_order_value_js});
                    tc.appendChild(_newBlock);
                    // 🐛 修复（完成框沉底·就地插位）：appendChild 兑底同样立即归位，
                    // 不依赖后续渲染（S1 末轮无下一拍）。
                    _insertByOrder(_newBlock, tc);
                    _tgt = _newBlock;
                }}
                // 🐛 修复：追加新块后同步滚动 document.body，替换旧的 tc.scrollTop
                // 区域独立 II：追加完成块是纯工具区更新 → bodyOnly 不碰正文容器
                window._suppressScrollEvent = true;
                if (!window._userScrolledWithin) {{
                    _autoScrollStreamingBody(true);
                }} else {{
                    var _bd2 = Math.abs(document.body.scrollHeight - document.body.scrollTop - document.body.clientHeight);
                    if (_bd2 < {AUTO_SCROLL_THRESHOLD}) {{
                        _autoScrollStreamingBody(true);
                        window._userScrolledWithin = false;
                    }}
                }}
                window._prevScrollTop = document.body.scrollTop;
                window._autoScrollTime = performance.now();
                window._suppressScrollEvent = false;
                // 🐛 修复：工具区内部自动滚底
                if (typeof _scrollToolContentToBottom === 'function') _scrollToolContentToBottom();
                // 确保 tool-section 可见
                if (window._toolCompactMode) {{
                    var ts2 = document.getElementById('tool-section');
                    if (ts2) ts2.style.display = '';
                }}
                reportHeight();
                return _snap();
            }})();
            """
            # [B2] 工具 DOM 已被 JS 增量注入 → 标记脏，下一次 _perform_update 必须走
            # save/restore 保护（否则 updateContent 整块替换会抹掉 JS 注入的工具块）。
            # 代际递增：使在途渲染回调放弃清除（防止旧回调误清新 dirty）。
            # 结果块已注入 → 该工具从 pending 移除：markdown 已含结果块，后续全量
            # 渲染可由 markdown 重新生成，不再依赖 save/restore 保护。
            try:
                self.viewer._tool_dom_dirty = True
                self.viewer._tool_dom_dirty_gen = getattr(self.viewer, "_tool_dom_dirty_gen", 0) + 1
                pending = getattr(self.viewer, "_injected_pending_tools", None)
                if pending is not None:
                    pending.discard(tool_call_id)
            except Exception:
                pass

            def _sink_diag(r) -> None:
                # [sink-diag] 完成框注入后的工具区快照（物理顺序 vs data-order）。
                # DRIFOX_SINK_DIAG=1 时打日志，用于"沉底"类问题的现场取证。
                if not os.environ.get("DRIFOX_SINK_DIAG") or not r:
                    return
                try:
                    logger.info(f"[sink-diag] {tool_call_id} tool-content: {r}")
                except Exception:
                    pass

            self.viewer.page().runJavaScript(js_code, _sink_diag)
        except Exception as e:
            logger.warning(f"增量工具块注入失败: {e}")
        # 🆕 F2（S1 归位兜底）：最后一个工具完成时关闭坞态。
        # 流式文本可能先于工具结果结束（finish_streaming(keep_dock=True) 保留了坞态），
        # 此处是归位时机：所有已登记工具都完成 → 工具区从坞态沉底回到顶部。
        # ⚠️ 必须 hasattr 守卫：stub viewer（测试桩）无 _sync_streaming_dock 方法。
        # 🐛 F3（#R1 P1）：归位判据必须用 MessageCard 层 self._streaming——
        # 本函数中段「就近恢复 viewer 流式模式」（L8737）在 viewer._streaming=False 时
        # 无条件置 True，viewer 层状态已被污染，恒 True → 归位兜底永不触发
        # （会话末轮 dock 永久沉底）。self._streaming 由 start/stop_streaming_anim
        # 管理（本轮流式结束后 stop_streaming_anim 已置 False；新一轮开始置 True 时
        # 正确跳过归位），不受 8738 行恢复逻辑影响。
        # 🐛 F3（次要提示 1）：lambda 捕获动态属性判空——0ms 内 viewer 被 cleanup
        # 置 None 时避免 AttributeError traceback。
        try:
            if (
                self.viewer is not None
                and hasattr(self.viewer, "_sync_streaming_dock")
                and not self._streaming
                and not self._has_active_tools()
            ):
                # F2（S1 兜底归位）+ 简洁模式折叠：最后一个工具完成时归位。
                # [T30] 与 finish_streaming 主路径一致：归位即折叠（两段式往返
                # 峰已并入同一条 220px→0 曲线），不再单独派发折叠调用。
                # lambda 捕获动态属性判空——0ms 内 viewer 被 cleanup 置 None 时
                # 避免 AttributeError traceback。
                def _dock_off_and_collapse() -> None:
                    if self.viewer is None:
                        return
                    self.viewer._sync_streaming_dock(False, collapse_after=True)

                QTimer.singleShot(0, _dock_off_and_collapse)
        except Exception:
            pass

    def _copy_user_message(self):
        """用户卡片工具栏「复制」：直接复制全文，不走 actionRequested 信号链

        避免信号链引起的 _on_code_action（clipboard.setText + InfoBar 动画），
        消除主线程阻塞（大文本 clipboard 操作）和 InfoBar 滑入动画叠加造成的闪烁。
        """
        if hasattr(self.viewer, "_copy_to_clipboard"):
            self.viewer._copy_to_clipboard(copy_selection=False)

    def get_plain_text(self) -> str:
        if self.role == "assistant":
            return content_to_text(self._content_data, include_tool_results=True)
        if isinstance(self._content_data, list):
            return content_to_text(self._content_data)
        return str(self._content_data or "")

    def run_js(self, js_code: str):
        """运行 JavaScript 代码"""
        try:
            if self.viewer and hasattr(self.viewer, "page"):
                self.viewer.page().runJavaScript(js_code)
        except RuntimeError:
            pass

    def set_reasoning_content(self, content: str):
        """设置思考内容（用于 DeepSeek 思考模式）- 作为 reasoning block 写入 _content_data"""
        self._content_data.insert(0, {"type": "reasoning", "content": content})
        if content and hasattr(self.viewer, "_markdown_text"):
            self.viewer._markdown_text = content_to_markdown(self._content_data)
            self.viewer._schedule_render(immediate=True)

    def set_html_direct(self, html: str):
        """直接设置 HTML，绕过打字机效果"""
        try:
            if self.viewer:
                self.viewer._markdown_text = html
                self.viewer._streaming = False
                self.viewer._perform_update()
        except RuntimeError:
            pass

    def start_new_thinking_block(self):
        """开始一个新的思考块（每轮工具迭代调用一次）

        将 reasoning 作为 _content_data 的一个 block，
        与文本、工具结果自然交错排列。

        关键：立即在 DOM 端标记所有已有的流式思考块为完成态，
        使新块获得独立的 data-streaming 状态。
        """
        self._content_data.append({"type": "reasoning", "content": ""})
        # 🆕 Bug B：新块成为当前活动思考块。append_reasoning 只追加到它，
        # 避免后续 reasoning 内容合并进已完成的旧思考块导致多轮思考堆积顶部。
        self._active_thinking_block = self._content_data[-1]
        # 新一轮思考开始，仅重置 streaming 标志。
        # _thinking_finalized 留在 True（上一轮已完成），直到新 reasoning chunk 到达
        # （append_reasoning 首 chunk）才置为 False，防止在两轮之间的窗口期，
        # 已完成 think-block 的 </think> 被 _render_markdown_to_html 错误剥离。
        if self.viewer:
            self.viewer._reasoning_streaming_started = False
            self.viewer._think_text_streaming_started = False
        # DOM 端：将所有 data-streaming="true" 的旧块标记为完成
        # 兼容两种渲染形式：think-block（折叠框完成态）和 think-streaming（流式纯文本）
        if self.viewer and getattr(self.viewer, "page", None):
            try:
                self.viewer.page().runJavaScript("""
                (function() {
                    var blocks = document.querySelectorAll(
                        '.think-block[data-streaming="true"], .think-streaming[data-streaming="true"]'
                    );
                    blocks.forEach(function(block) {
                        block.setAttribute('data-streaming', 'false');
                    });
                })();
                """)
            except RuntimeError:
                pass

    # ── 工具流式调用块 ──────────────────────────────────

    def _inject_tool_streaming_html(
        self,
        tool_call_id: str,
        tool_name: str,
        preview: str,
        char_count: int = 0,
        completed: bool = False,
        add_lines: int = 0,
        del_lines: int = 0,
    ):
        """通过 JS 注入/更新工具流式块

        已有同 ID 块时原地更新预览文本，不重建 DOM，保持折叠/展开状态不丢失。

        preview 为 None 时表示仅更新 data-streaming 状态，不修改任何文字内容。
        用于 placeholder 阶段的 finish_tool_streaming 调用（参数全是占位键时）。
        """
        if not hasattr(self, "viewer") or not self.viewer:
            return

        # 标记内容加载，确保后续卡片高度变化时 _on_message_card_height_changed
        # 触发消息列表滚底。工具流式块注入属于内容加载，应滚动。
        # ⚠️ 不在此处调用 _schedule_render：全量渲染会执行 updateContent()
        # 销毁所有 JS 注入的 [data-tool-injected] 元素，导致流式块闪灭→再现。
        # 流式文本已由 _append_text_incremental 增量推送，不需要全量渲染。
        # 🔧 不设置 _content_just_loaded：工具流式更新不应触发外部消息列表滚动，
        # 仅 tool-content 内部自动滚底（见 JS 注入代码）。

        # 构建预览文本（含 char_count），用于后续内容比较和 JS 注入
        _text_only = preview is None
        preview_content = escape(preview) if preview else "准备中..."
        # 徽标作为预览 span 的**兄弟节点**更新（长文本省略号不会把它裁掉）
        badge_html = "" if completed else _format_tool_progress_badge(char_count, add_lines, del_lines)

        # ── 内容去重：预览文本**与徽标**都相同才跳过 JS 执行，减少流式高频更新压力 ──
        # 🐛 修复（编辑工具流式徽标不更新）：原实现只比较 preview_content，而编辑类
        # 工具的预览文本在路径完整后就恒定（如「写入文件中」），导致此后每个进度事件
        # 都被去重跳过 —— +N/-M 行数与字符数徽标停更，运行框看上去"卡死"在首帧。
        _cache_key = (tool_call_id, completed)
        _cache_val = (preview_content, badge_html)
        _last = getattr(self, "_tool_streaming_preview_cache", None) or {}
        if _last.get(_cache_key) == _cache_val:
            # 🐛 修复（编辑工具框运行中消失）：preview 相同不重新注入，但 DOM 中
            # 运行框仍在 → 仍需 dirty 保护标记。否则 dirty 被某次渲染回调清除后，
            # 该工具框永远失去 save/restore 保护，下一次全量渲染裸 updateContent
            # 抹掉它，直到 append_tool_result 才重现（"运行中→完成"中间消失）。
            # 代际递增防止"在途渲染回调误清本标记"。
            try:
                self.viewer._tool_dom_dirty = True
                self.viewer._tool_dom_dirty_gen = getattr(self.viewer, "_tool_dom_dirty_gen", 0) + 1
            except Exception:
                pass
            return
        if not hasattr(self, "_tool_streaming_preview_cache"):
            self._tool_streaming_preview_cache = {}
        self._tool_streaming_preview_cache[_cache_key] = _cache_val

        # 🐛 修复（编辑工具框运行中消失）：dirty 标记必须**先于** _schedule_render
        # 设置。completed=True 时 _schedule_render(immediate=True) 会立即执行
        # _perform_update，若此时 dirty 还是旧值（False），该渲染判定
        # _needs_save_restore=False → 裸 updateContent 抹掉旧运行框（新完成态块
        # 尚未注入），产生"运行框闪灭"。
        # pending 集合：该工具结果未 append_tool_result → 运行框/预览框只在 DOM，
        # 不在 markdown → 全量渲染必须 save/restore 保护（_clear_tool_dom_dirty_guarded
        # 据此阻止 dirty 清除）。
        try:
            self.viewer._tool_dom_dirty = True
            self.viewer._tool_dom_dirty_gen = getattr(self.viewer, "_tool_dom_dirty_gen", 0) + 1
            self.viewer._injected_pending_tools = getattr(self.viewer, "_injected_pending_tools", set())
            if not completed or tool_call_id not in getattr(self, "_finished_streaming_ids", set()):
                self.viewer._injected_pending_tools.add(tool_call_id)
        except Exception:
            pass

        # ── 停掉全量渲染定时器：流式更新期间不跑全量重渲染 ──
        # 同时重调度一个"静默后渲染"兜底，确保流式结束后最终状态同步
        if hasattr(self, "viewer") and self.viewer:
            if hasattr(self.viewer, "_render_timer") and self.viewer._render_timer.isActive():
                self.viewer._render_timer.stop()
            # 非完成态时重调度一次兜底渲染（500ms 后，流式更新会持续重置）
            if not completed:
                self.viewer._schedule_render(immediate=False)
            else:
                self.viewer._schedule_render(immediate=True)
        try:
            block_html = _render_tool_streaming_block(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                preview=preview if preview else "",
                char_count=char_count,
                completed=completed,
                add_lines=add_lines,
                del_lines=del_lines,
            )
            # 编辑类工具流式块始终注入到正文区域
            _stream_target = "content-placeholder" if tool_name in _edit_tools() else self.viewer._tool_target_id

            # 🆕 方案 D：计算工具块的 data-order（与 reorganizeContent 的 posMap 同尺度），
            # 使 JS 注入的流式块在下次全量渲染排序时能回到正确位置，而非恒沉底
            # （"所有思考在前、所有工具在后"的根因）。锚点 = 工具调用时 _content_data
            # 长度；基准 = 锚点前 think/tool 块计数；同锚点多工具按启动序号细分。
            # 🆕 Bug B 方案 F：用块引用锚点定位稳定位置（int 索引在列表偏移后失效）。
            _anchor = self._tool_anchor_pos(tool_call_id)
            _order = self._tool_call_order.get(tool_call_id) or 0
            _base = float(_count_think_tool_prefix(self._content_data, _anchor))
            _order_value_js = f"{_base + _order * 0.001:.3f}"

            safe_html = json.dumps(block_html).decode("utf-8")
            safe_preview = json.dumps(preview_content).decode("utf-8")
            safe_badge = json.dumps(badge_html).decode("utf-8")
            streaming_flag = "true" if not completed else "false"
            _text_only_js = "true" if _text_only else "false"
            js_code = f"""
            (function() {{
                var tc = document.getElementById('{_stream_target}');
                if (!tc) {{
                    tc = document.getElementById('content-placeholder');
                }}
                var el = document.querySelector('[data-tool-call-id="{tool_call_id}"]');
                var hr = (typeof reportHeightDebounced === 'function') ? reportHeightDebounced : reportHeight;
                // 徽标（+N/-M 或字符数）是预览 span 的兄弟节点，避免长文本省略号把徽标裁掉
                var _dfxSetBadge = function(_bel, _bhtml) {{
                    var _b = _bel.querySelector('.tool-streaming-badge');
                    if (_bhtml) {{
                        if (_b) {{ _b.outerHTML = _bhtml; }}
                        else {{
                            var _bp = _bel.querySelector('.tool-streaming-preview');
                            if (_bp && _bp.parentNode) {{ _bp.insertAdjacentHTML('afterend', _bhtml); }}
                        }}
                    }} else if (_b) {{ _b.remove(); }}
                }};
                if (el) {{
                    // 🐛 FIX: 清除旧 data-tool-injected，消除 save-remove-restore 闪烁循环
                    el.removeAttribute('data-tool-injected');
                    var curStreaming = el.getAttribute('data-streaming');
                    // text-only 模式：仅更新 data-streaming 状态，不碰文字
                    if ({_text_only_js}) {{
                        el.setAttribute('data-streaming', '{streaming_flag}');
                        // 🐛 修复：状态更新后 body 自动滚底
                        // 区域独立 II：状态更新是纯工具区更新 → bodyOnly
                        window._suppressScrollEvent = true;
                        if (!window._userScrolledWithin) {{
                            _autoScrollStreamingBody(true);
                        }}
                        // 🐛 修复：工具区内部自动滚底
                        if (typeof _scrollToolContentToBottom === 'function') _scrollToolContentToBottom();
                        window._prevScrollTop = document.body.scrollTop;
                        window._autoScrollTime = performance.now();
                        window._suppressScrollEvent = false;
                        hr();
                        return;
                    }}
                    // 防止状态回退：已完成的块（data-streaming="false"）不允许
                    // 再切回流式态（data-streaming="true"），避免 spinner 反复闪烁
                    if ('{streaming_flag}' === 'true' && curStreaming === 'false') {{
                        var previewEl2 = el.querySelector('.tool-streaming-preview');
                        if (previewEl2) {{
                            previewEl2.innerHTML = {safe_preview};
                        }}
                        _dfxSetBadge(el, {safe_badge});
                    }} else {{
                        el.setAttribute('data-streaming', '{streaming_flag}');
                        var previewEl = el.querySelector('.tool-streaming-preview');
                        if (previewEl) {{
                            previewEl.innerHTML = {safe_preview};
                        }}
                        _dfxSetBadge(el, {safe_badge});
                    }}
                    // 🐛 修复：预览内容更新后 body 自动滚底
                    // 区域独立 II：预览内容更新是纯工具区更新 → bodyOnly
                    window._suppressScrollEvent = true;
                    if (!window._userScrolledWithin) {{
                        _autoScrollStreamingBody(true);
                    }} else {{
                        var _bd = Math.abs(document.body.scrollHeight - document.body.scrollTop - document.body.clientHeight);
                        if (_bd < {AUTO_SCROLL_THRESHOLD}) {{
                            _autoScrollStreamingBody(true);
                            window._userScrolledWithin = false;
                        }}
                    }}
                    // 🐛 修复：工具区（#tool-content）内部自动滚底
                    if (typeof _scrollToolContentToBottom === 'function') _scrollToolContentToBottom();
                    window._prevScrollTop = document.body.scrollTop;
                    window._autoScrollTime = performance.now();
                    window._suppressScrollEvent = false;
                    hr();
                    // 预览文字打字机：预览文本更新后逐字补齐增量
                    if (typeof window._ptPlay === 'function') window._ptPlay();
                }} else {{
                    // text-only 模式下不存在块：不创建
                    if ({_text_only_js}) return;
                    // 新块：直接插入
                    var tmp = document.createElement('div');
                    tmp.innerHTML = {safe_html};
                    var block = tmp.firstElementChild;
                    if (block) {{
                        // 🆕 方案 D：注入 data-order，供 reorganizeContent 排序定位
                        // （JS 注入块不在 #content-placeholder 中，无 posMap 记录，
                        // 无 data-order 会 getPos=1e9 恒沉底 → 思考/工具不交错）。
                        block.setAttribute('data-order', {_order_value_js});
                        tc.appendChild(block);
                    }}
                    // 🐛 修复：追加新块后 body 自动滚底，替换旧的 tc.scrollTop
                    // 区域独立 II：新流式块追加是纯工具区更新 → bodyOnly
                    window._suppressScrollEvent = true;
                    if (!window._userScrolledWithin) {{
                        _autoScrollStreamingBody(true);
                    }} else {{
                        var _bd2 = Math.abs(document.body.scrollHeight - document.body.scrollTop - document.body.clientHeight);
                        if (_bd2 < {AUTO_SCROLL_THRESHOLD}) {{
                            _autoScrollStreamingBody(true);
                            window._userScrolledWithin = false;
                        }}
                    }}
                    window._prevScrollTop = document.body.scrollTop;
                    window._autoScrollTime = performance.now();
                    window._suppressScrollEvent = false;
                    // 工具区内部自动滚底（新块追加后）
                    if (typeof _scrollToolContentToBottom === 'function') _scrollToolContentToBottom();
                    // 确保 tool-section 可见
                    if (window._toolCompactMode) {{
                        var ts = document.getElementById('tool-section');
                        if (ts) {{ ts.style.display = ''; _updateToolSectionHeader(); }}
                    }}
                    hr();
                    // 预览文字打字机：新工具块的预览行逐字显现
                    if (typeof window._ptPlay === 'function') window._ptPlay();
                }}
            }})();
            """
            # [B2] 工具 DOM 已被 JS 增量注入 → 标记脏（已在函数开头 _schedule_render
            # 之前统一设置并维护 pending，此处不再重复设置——避免与开头逻辑分叉）。
            self.viewer.page().runJavaScript(js_code)
        except RuntimeError:
            pass

    def _maybe_finish_thinking_for_tool(self, tool_call_id: str):
        """当工具参数第一次到达时，标记当前思考块为完成态（💡）。

        修复 bug：reasoning 流结束 → tool_call 开始时，思考块 DOM 上还显示"思考中"。

        触发条件：update_tool_streaming / _on_tool_call_started 第一次被某个 tool_call_id 调用。

        实现：
        - 对 .think-block（已有折叠框结构）→ JS 更新 summary 文字为完成态
        - 对 .think-streaming（流式纯文本）→ Python 生成完整折叠框 HTML 替换

        🐛 修复：原实现只检查 _content_data[-1]，若 reasoning 后跟了空 text block
        则末尾为 text 类型，转换被跳过。改为向前遍历查找最后一个非空 reasoning block，
        与 append_reasoning 的查找逻辑保持一致。
        """
        if tool_call_id in self._tool_args_first_seen_ids:
            return
        self._tool_args_first_seen_ids.add(tool_call_id)

        # 检查 _content_data 中最后一个 reasoning block（允许其后存在空 text block）
        if not self._content_data or not isinstance(self._content_data, list):
            return
        # 🆕 Bug B：优先使用当前活动思考块（start_new_thinking_block 创建的新块），
        # 避免工具调用时误绑定到更早的已完成思考块（多轮思考堆积的根因之一）。
        last_block = self._active_thinking_block if isinstance(self._active_thinking_block, dict) else None
        if last_block is None:
            last_reasoning_idx = -1
            for i in reversed(range(len(self._content_data))):
                blk = self._content_data[i]
                if isinstance(blk, dict) and blk.get("type") == "reasoning":
                    last_reasoning_idx = i
                    break
                # 遇到非空非 reasoning block 停止向前查找（避免误绑定早期思考）
                if isinstance(blk, dict):
                    bt = blk.get("type")
                    if bt == "text" and (blk.get("text") or "").strip():
                        break
                    if bt in ("tool_result", "tool_streaming"):
                        break
                if isinstance(blk, str) and blk.strip():
                    break
            if last_reasoning_idx < 0:
                return
            last_block = self._content_data[last_reasoning_idx]
        if not isinstance(last_block, dict):
            return
        raw_content = last_block.get("content") or ""
        if not raw_content.strip():
            # 空 block（start_new_thinking_block 刚创建）跳过 — 等后续 reasoning chunks
            return
        # 🆕 Bug B：思考完成 → 活动块置空，后续 reasoning 不再追加到此块
        # （下一轮思考由 start_new_thinking_block 创建新块并重新登记）。
        self._active_thinking_block = None
        # 保持原始 content 用于 block-key 计算，确保与 _inject_think_cards
        # （通过 _build_incremental_md）产生的 key 一致。
        # 否则每次 _perform_update → reorganizeContent 会因 key 不匹配
        # 删除已有的 think-block 再重新创建，触发 CSS 入场动画（消失→重现）。
        content = raw_content

        # 懒渲染未就绪 / viewer 未创建
        if not self._lazy_rendered or not self.viewer:
            return

        # 通知 viewer：思考已完成，后续全量渲染不要再剥离 </think>
        self.viewer._thinking_finalized = True

        # [PERF-opt] 取消待处理的全量渲染定时器，防止覆盖增量 JS 思考框更新
        if hasattr(self.viewer, "_render_timer") and self.viewer._render_timer.isActive():
            self.viewer._render_timer.stop()

        # Python 端预计算分类（与 _render_think_block 一致），保留图标 + 分类标签
        tag = _classify_think_tag(content)
        think_icon = _get_think_icon_html()
        if tag:
            status_html = f'<span class="think-bulb">{think_icon}</span> {escape(tag)}'
        else:
            status_html = f'<span class="think-bulb">{think_icon}</span>'
        safe_status = json.dumps(status_html).decode("utf-8")

        # 预生成完成态折叠框 HTML（用于替换 .think-streaming 纯文本 div）
        compact = self.viewer._tool_compact_mode if self.viewer else False
        completed_html = _render_think_block(content, completed=True, compact=compact)
        safe_completed_html = json.dumps(completed_html).decode("utf-8")

        # 直接 JS 处理 DOM 上残留的"思考中"状态
        # 注意：不能走全量渲染 — `_render_markdown_to_html` 流式模式会去掉末尾 </think>，
        # 导致 markdown 仍被解析为 completed=False（"思考中"）。
        try:
            js_code = f"""
            (function() {{
                // ── 处理 .think-streaming 纯文本块：替换为完成态折叠框 ──
                var streamingBlocks = document.querySelectorAll('.think-streaming[data-streaming="true"]');
                streamingBlocks.forEach(function(block) {{
                    block.setAttribute('data-streaming', 'false');
                    var tmp = document.createElement('div');
                    tmp.innerHTML = {safe_completed_html};
                    var newBlock = tmp.firstElementChild;
                    if (newBlock) {{
                        // 标记为恢复块，跳过 CSS 入场动画（_toolBlockEnter opacity:0→1），
                        // 避免工具调用切换时思考折叠框"消失→重现"的视觉闪烁
                        newBlock.setAttribute('data-restored', 'true');
                        block.parentNode.replaceChild(newBlock, block);
                        // 🆕 方案 C：若替换后的 think-block 仍在 #content-placeholder
                        // （未被 reorganizeContent 迁移，如渲染节流/坞态切换），
                        // 立即迁移到 #tool-content，根治"思考框跑出折叠框"
                        // （正文区出现孤立思考框、折叠框内思考缺失）。
                        if (newBlock.parentNode === document.getElementById('content-placeholder')) {{
                            var _tc = document.getElementById('tool-content');
                            if (_tc) {{
                                _tc.appendChild(newBlock);
                            }}
                        }}
                    }}
                }});

                // ── 处理 .think-block 已有折叠框：只更新 summary 文字 ──
                var blocks = document.querySelectorAll('.think-block[data-streaming="true"]');
                blocks.forEach(function(block) {{
                    block.setAttribute('data-streaming', 'false');
                    var summary = block.querySelector('.think-block__summary');
                    if (summary) {{
                        var spans = summary.children;
                        var statusSpan = null;
                        for (var i = 0; i < spans.length; i++) {{
                            var s = spans[i];
                            var inline = s.getAttribute('style') || '';
                            if (inline.indexOf('white-space: nowrap') !== -1) {{
                                statusSpan = s;
                                break;
                            }}
                        }}
                        if (!statusSpan && spans.length >= 2) {{
                            statusSpan = spans[1];
                        }}
                        if (statusSpan) {{
                            statusSpan.innerHTML = {safe_status};
                        }}
                    }}
                }});
                if (typeof reportHeightDebounced === 'function') {{
                    reportHeightDebounced();
                }} else if (typeof reportHeight === 'function') {{
                    reportHeight();
                }}
                // 预览文字打字机：思考块转完成态时预览行逐字显现
                if (typeof window._ptPlay === 'function') window._ptPlay();
            }})();
            """
            self.viewer.page().runJavaScript(js_code)
        except RuntimeError:
            pass

    # ── 任务列表（卡片底部内嵌 todo 区，替代原悬浮卡片）──

    def update_todo_list(self, todos):
        """更新卡片底部任务列表

        Args:
            todos: [{status: pending|in_progress|completed, content: str, priority: ...}, ...]
                   空列表 → 隐藏任务区。
        """
        self._todos_snapshot = list(todos or [])
        self._push_todo_list()

    def _push_todo_list(self):
        """把 _todos_snapshot 推送到 viewer 内的 #todo-section

        viewer 未创建（懒加载）/ JS 未就绪时仅写 viewer._pending_todos，
        由 viewer 创建点或 _on_js_ready 兜底补推。
        """
        v = self.viewer
        if v is None:
            return
        # 灰度：纯 Qt viewer 走原生任务列表面板
        # 延迟导入：未开启灰度时 _MarkdownBlockViewerCls 为 None，
        # 此时 viewer 必然是 CodeWebViewer，跳过判断即可。
        _qt_cls = _MarkdownBlockViewerCls
        if _qt_cls is not None and isinstance(v, _qt_cls):
            v.update_todo_list(self._todos_snapshot or [])
            return
        if not isinstance(v, CodeWebViewer):
            return
        v._pending_todos = self._todos_snapshot
        if not getattr(v, "_is_js_ready", False):
            return
        try:
            payload = [
                {
                    "status": item.get("status", "pending") if isinstance(item, dict) else "pending",
                    "content": escape(item.get("content", "") if isinstance(item, dict) else str(item)),
                    # 优先级：high/medium/low（来自 todowrite 工具 _normalize_todos 默认 medium）
                    "priority": (item.get("priority", "medium") if isinstance(item, dict) else "medium") or "medium",
                }
                for item in (self._todos_snapshot or [])
            ]
            data = json.dumps(payload).decode("utf-8")
            v.page().runJavaScript(f"window._updateTodoList && window._updateTodoList({data});")
        except RuntimeError:
            pass

    def update_tool_streaming(
        self,
        tool_call_id: str,
        tool_name: str,
        partial_args: dict = None,
    ):
        """更新工具流式参数预览 — 更新已注入的工具块预览文本

        预览文本使用自然语言描述（如"搜索xxx中"），代替原始 JSON。
        流式期间附加"中"后缀表示进行中状态。

        Args:
            tool_call_id: 工具调用唯一 ID
            tool_name: 工具名
            partial_args: 部分参数
        """
        # 已完成参数接收或已追加工具结果的不再更新，防止完成态被退回 streaming 状态
        if tool_call_id in self._finished_streaming_ids:
            return
        # 🆕 Bug B：首次见 tool_call_id 记录插入锚点 = 工具调用发生时 _content_data 的长度。
        # append_tool_result 用 insert(锚点) 把结果插回工具调用发生的位置（而非恒末尾），
        # 保持"思考→工具→正文→工具→正文"交错顺序。同锚点多工具按启动序号保序。
        # 🆕 Bug B 方案 F：同时记录**块引用**锚点。int 索引（len）在后续其他工具结果
        # 插入 / 思考块追加后失效（列表偏移），引用在列表增删中保持稳定，
        # index(ref)+1 恒等于"工具调用时刻的逻辑末尾"——这是数据层正确顺序的关键。
        if tool_call_id not in self._tool_insert_anchors:
            self._tool_insert_anchors[tool_call_id] = (
                len(self._content_data) if isinstance(self._content_data, list) else 0
            )
            if isinstance(self._content_data, list) and self._content_data:
                self._tool_anchor_refs[tool_call_id] = self._content_data[-1]
            self._tool_call_order[tool_call_id] = len(self._tool_call_order)
        # 🆕 第一次工具参数到达时，标记当前思考块为完成态（💡）
        # 修复 bug：reasoning 流结束 → tool_call 开始时，思考块 DOM 还显示"思考中"
        self._maybe_finish_thinking_for_tool(tool_call_id)
        preview = ""
        char_count = 0
        add_lines = 0
        del_lines = 0
        if partial_args:
            display = {k: v for k, v in partial_args.items() if not k.startswith("_")}
            if display:
                try:
                    natural = _format_natural_preview(tool_name, display)
                    if natural:
                        # 有实参时：显示自然语言描述 + "中"，不需要字符数进度
                        preview = natural + "中"
                        char_count = 0
                    else:
                        args_str = json.dumps(display).decode("utf-8")
                        if len(args_str) > 100:
                            preview = args_str[:100] + "..."
                        else:
                            preview = args_str
                        char_count = 0
                except Exception:
                    preview = "..."
            else:
                # 参数尚未到达或全是 _ 占位键
                # 用 _args_len 获取缓冲区实际接收长度作为字符数进度
                args_len = partial_args.get("_args_len", 0)
                # 缓冲区提前提取的 _path（chat_worker regex 提取），用于预览带真实文件名
                _path = partial_args.get("_path", "")
                preview_args = {"path": _path} if _path else {}
                natural = _format_natural_preview(tool_name, preview_args)
                if natural:
                    preview = natural + "中"
                else:
                    # 🆕 编辑类工具在 path 未到达时不再空窗「准备中...」
                    _fallback = _FILE_EDIT_TOOLS_FALLBACK_TEXT.get(tool_name, "")
                    preview = f"{_fallback}中" if _fallback else "准备中..."
                char_count = args_len if args_len else len(preview)
                # 🆕 编辑类工具的增删行数（worker 从半截 JSON 估算），取代字数显示
                add_lines = int(partial_args.get("_add_lines") or 0)
                del_lines = int(partial_args.get("_del_lines") or 0)
        self._inject_tool_streaming_html(
            tool_call_id,
            tool_name,
            preview,
            char_count,
            completed=False,
            add_lines=add_lines,
            del_lines=del_lines,
        )

    def finish_tool_streaming(
        self,
        tool_call_id: str,
        tool_name: str,
        arguments: dict = None,
    ):
        """工具参数接收完成 — 将流式块转为完成态，显示自然语言预览

        使用自然语言描述代替原始 JSON，完成态不加"中"后缀。

        Args:
            tool_call_id: 工具调用唯一 ID
            tool_name: 工具名
            arguments: 完整参数
        """
        preview = ""
        char_count = 0
        if arguments:
            display = {k: v for k, v in arguments.items() if not k.startswith("_")}
            if display:
                try:
                    natural = _format_natural_preview(tool_name, display)
                    if natural:
                        # 完成态：自然语言描述，不加"中"后缀
                        preview = natural
                        char_count = len(preview)
                    else:
                        args_str = json.dumps(display).decode("utf-8")
                        if len(args_str) > 100:
                            preview = args_str[:100] + "..."
                        else:
                            preview = args_str
                        char_count = len(args_str)
                except Exception:
                    preview = "..."
            else:
                # 参数全部是 _ 前缀占位键（preview 阶段），仅更新状态不覆盖文字
                self._inject_tool_streaming_html(tool_call_id, tool_name, preview=None, char_count=0, completed=True)
                return
        self._inject_tool_streaming_html(tool_call_id, tool_name, preview, char_count, completed=True)

    def remove_tool_streaming(self, tool_call_id: str):
        """移除工具流式块 — 工具执行完成后清理"""
        if not hasattr(self, "viewer") or not self.viewer:
            return
        # 块从 DOM 移除 → 该工具不再需要 save/restore 保护（pending 移除）
        try:
            pending = getattr(self.viewer, "_injected_pending_tools", None)
            if pending is not None:
                pending.discard(tool_call_id)
        except Exception:
            pass
        try:
            js_code = f"""
            (function() {{
                var el = document.querySelector('[data-tool-call-id="{tool_call_id}"]');
                if (el) el.remove();
                reportHeight();
            }})();
            """
            self.viewer.page().runJavaScript(js_code)
        except RuntimeError:
            pass

    def append_reasoning(self, text: str):
        """追加思考内容到当前最后一个思考块（流式模式）

        将 reasoning 直接写入 _content_data 的 reasoning block，
        使其与文本、工具结果按实际发生顺序交错渲染。
        """
        # 🆕 Bug B：只追加到当前活动思考块（start_new_thinking_block 创建的新块）。
        # 兜底：无活动块时查找最后一个 reasoning block（兼容未走 start_new_thinking_block
        # 的流路径），仍未找到才新建块。活动块是 dict 对象引用，即使中间被工具结果
        # 锚点 insert 挤动列表位置，引用仍有效。
        _target_block = self._active_thinking_block if isinstance(self._active_thinking_block, dict) else None
        if _target_block is None:
            for i in reversed(range(len(self._content_data))):
                if self._content_data[i].get("type") == "reasoning":
                    _target_block = self._content_data[i]
                    break

        if _target_block is not None:
            # 找到已有的（活动）思考块，追加内容
            _target_block["content"] = (_target_block.get("content", "") or "") + text
        else:
            # 未找到，新增 reasoning 块
            self._content_data.append({"type": "reasoning", "content": text})
        self._reasoning_total_len += len(text)

        if not self._lazy_rendered or not self.viewer:
            self._pending_content = self._content_data
            return

        # 🔧 不设置 _content_just_loaded：思考流式更新不应触发外部消息列表滚动，
        # 仅 #tool-content 内部自动滚底（见 JS 注入代码）。与 _inject_tool_streaming_html
        # 行为一致——工具与思考区是卡片内部独立滚动容器，正文区未更新时外部滚动条
        # 不应被强制拉底（用户在阅读正文时会被打断）。
        #
        # 🆕 方案B：首个 reasoning chunk 渲染"深度思考中..." spinner，后续静默累积
        # 不更新 DOM / 不触发渲染定时器 / 不更新高度，等 thinking 结束后的全量渲染
        # （由 append_text / finish_streaming / _maybe_finish_thinking_for_tool 触发）一并处理
        if not self.viewer._reasoning_streaming_started:
            self.viewer._reasoning_streaming_started = True
            # 🐛 修复：仅在新 reasoning 真正开始接收内容时才重置 _thinking_finalized。
            # start_new_thinking_block 不再重置此标志，防止两轮之间的空窗期
            # 已完成 think-block 的 </think> 被错误剥离为 think-streaming。
            self.viewer._thinking_finalized = False
            # 首 chunk：立即全量渲染显示 spinner。_schedule_render 会触发高度报告，
            # 由 _on_message_card_height_changed 走"流式首屏"语义统一滚底——这与正文
            # 首次到达场景一致（用户期待滚底跟随）。
            # 不调用 _update_thinking_incremental：原方法会主动 reportHeightDebounced
            # 并设置 _content_just_loaded，导致外部 chat_scroll_area 在正文未更新时被强制
            # 滚底，破坏阅读。首 chunk 的全量渲染已自然带高度报告，无需额外触发。
            self.viewer._lazy_markdown_cb = self._build_incremental_md
            self.viewer._schedule_render(immediate=True)
        else:
            # 后续 chunk：只累积到 _content_data，静默不触发任何 DOM 操作
            self.viewer._lazy_markdown_cb = self._build_incremental_md
            # 不调用 _schedule_render / _update_thinking_incremental

    def _update_thinking_incremental(self, new_text: str):
        """流式思考增量更新（仅触发布局高度重算）

        思考中不再更新预览文字，仅显示转圈+思考中。
        结束时通过全量渲染更新预览文字到 summary 右侧。

        注意：本方法已被 append_reasoning 首 chunk 路径不再调用（保留为内部辅助函数，
        供未来增量思考场景使用）。不在此设置 _content_just_loaded，也不主动报告
        高度——避免外部 chat_scroll_area 因思考区内部高度变化被强制滚底，破坏正文阅读。
        """
        if not hasattr(self.viewer, "page"):
            return

        try:
            # 仅触发布局高度重算，不再更新 .think-streaming-preview
            self.viewer.page().runJavaScript("""
            (function() {
                if (typeof reportHeightDebounced === 'function') {
                    reportHeightDebounced();
                } else {
                    reportHeight();
                }
            })();
            """)
        except RuntimeError:
            pass

    def add_interactive_option(self, option: Dict[str, Any]):
        """添加交互选项"""
        self._interactive_options.append(option)

        option_widget = QWidget(self.options_widget)
        option_layout = QHBoxLayout(option_widget)
        option_layout.setContentsMargins(0, 0, 0, 0)
        option_layout.setSpacing(8)

        label = QLabel(f"• {option.get('label', '选项')}", self)
        label.setStyleSheet(f"color: #4a9eff; {get_font_family_css()} {font_size_css(13)} cursor: pointer;")
        label.setCursor(Qt.PointingHandCursor)
        label.option_data = option
        label.mousePressEvent = lambda e, opt=option: self._on_option_clicked(opt)

        option_layout.addWidget(label)
        option_layout.addStretch()

        self.options_layout.addWidget(option_widget)
        self.options_widget.setVisible(True)

    def add_interactive_options(self, options: List[Dict[str, Any]]):
        """批量添加交互选项"""
        if not options:
            return

        title_label = QLabel("👉 请选择：", self)
        title_label.setStyleSheet(f"color: #888; {get_font_family_css()} {font_size_css(12)} margin-top: 8px;")
        self.options_layout.addWidget(title_label)

        for option in options:
            self.add_interactive_option(option)

    def _on_option_clicked(self, option: Dict[str, Any]):
        """选项被点击"""
        self.optionSelected.emit(option)

    def set_intervention_mode(self, enabled: bool):
        """设置人工干预模式"""
        if enabled:
            self.interventionRequested.emit({"card_id": id(self), "message": "请求人工干预"})

    def finish_streaming(self, history: bool = False, force_dock_off: bool = False, immediate: bool = True):
        """流式结束收尾。

        Args:
            history: True 表示历史会话加载收尾（非流式渲染路径）。
                此时跳过 stop_streaming_anim()——它会置 _streaming_finished=True，
                使 ensure_rendered 把卡片误判为"本轮已结束的流式消息"
                （_is_history=False → 工具与思考不折叠、正文按流式坞态限高），
                与"历史会话默认折叠"的产品预期冲突。历史卡片从未启动过
                流式动画，跳过 stop_streaming_anim 无副作用。
            force_dock_off: True 表示打断/错误收尾强制归位（忽略活跃工具）。
                正常结束时文本先于工具完成（S1）会 keep_dock 保留坞态，
                等最后一个工具结果经 append_tool_result 兑底归位；但打断/
                错误路径 worker 已终止，工具结果永不到达，兑底永不触发
                → 坞态永久沉底、正文限矮（流式结构残留 bug 根因），
                故打断/错误调用方必须传 True。
            immediate: 透传给 viewer.finish_streaming（T11）。批量加载路径传
                False，避免 N 卡同帧全量重渲；交互路径保持默认 True。
        """
        try:
            # [PERF] 先停 20fps 流式脉冲动画：它会周期性 update() 整卡（重绘
            # 渐变边框/流动光点），与紧随其后的最终全量渲染抢主线程。
            # 这里只停定时器，不改任何状态标记（仍由下方 stop_streaming_anim 收尾）。
            try:
                self._anim_timer.stop()
            except RuntimeError:
                pass
            # 挂起的延迟小收缩作废：finish 后由非流式 _update_height 全量收敛接管
            self._cancel_pending_shrink()
            # [T29] 流式追踪落定：防抖/追踪都停，避免结束后的非流式收敛与
            # 追踪 tick 双写 viewer 高度（追踪的 _target_viewer_height 是流式
            # 旧目标，结束后应交回 FINISH 窗口的缓动路径）。
            if self._stream_height_anim_active or self._stream_height_tick.isActive():
                self._stop_stream_height_track()
            # 🆕 打开结束态高度缓动窗口：坞态归位 + 最终重排后卡片高度会一次收敛
            # 数百 px，交由 _update_height 缓动（只服务一次，消费或超时即失效）。
            # 历史会话加载（history=True）不打开——那是首帧建卡，无需过渡。
            if not history:
                self._finish_height_anim_until = time.monotonic() + FINISH_HEIGHT_ANIM_WINDOW_S
                self._finish_height_anim_left = FINISH_HEIGHT_ANIM_MAX_USES
            if self.viewer is not None and hasattr(self.viewer, "finish_streaming"):
                _keep_dock = self._has_active_tools() and not (history or force_dock_off)
                self.viewer.finish_streaming(keep_dock=_keep_dock, immediate=immediate)
                if hasattr(self.viewer, "_cleanup_render_cache"):
                    self.viewer._cleanup_render_cache()
                # [T30] 简洁模式的「归位后折叠」已并入 viewer.finish_streaming 的
                # 坞态归位事务（_setStreamingDock(false, collapse_after=true)）：
                # 归位展开到自然高度峰值再折叠收起的两段式往返，是结束态剧烈
                # 抖动的主因，终态本就是折叠。此处不再单独派发折叠调用。
        except RuntimeError:
            pass
        if history:
            self._streaming = False
        else:
            self.stop_streaming_anim()

    def _has_active_tools(self) -> bool:
        """是否有仍在执行中的工具（已登记但未完成）。

        dock 状态机的判据：只要还有工具在运行（流式文本已结束但工具结果未全部
        到达，S1 场景），工具区应保持坞态沉底；全部完成后才归位。
        """
        return any(tid not in self._finished_streaming_ids for tid in self._tool_call_order)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # 宽度同步由外层聊天窗口统一调度，避免卡片自身 resize 再次触发全量重算
        # 浮动按钮组不在布局里，需手工跟随卡片宽度变化重新贴靠时间戳左侧。
        if self.role == "user":
            self._sync_identity_header_width()
        elif self.role == "welcome":
            # tab 条按宽度折行：宽度变化后行数可能变，需重算宿主高度
            # （Qt5 对带 heightForWidth 子布局的 widget 估高偏大，见
            # _sync_welcome_tab_host_height，不能依赖布局自动收敛）
            self._sync_welcome_tab_host_height()

    def _disconnect_all_signals(self):
        """断开 MessageCard 发射的所有信号，打破信号-槽引用环路"""
        signals = [
            self.heightChanged,
            self.deleteRequested,
            self.undoRequested,
            self.actionRequested,
            self.contextActionRequested,
            self.optionSelected,
            self.interventionRequested,
            self.toolDiffRequested,
            self.subAgentLogRequested,
            self.cardDiffRequested,
            self.reviewRequested,
            self.saveFileRequested,
            self.lazyRenderCompleted,
            self.modelLabelClicked,
        ]
        for sig in signals:
            try:
                sig.disconnect()
            except (TypeError, RuntimeError):
                pass

    def cleanup(self):
        """
        清理 MessageCard 持有的资源，防止内存泄漏。
        应该在删除卡片前调用，或者在 closeEvent 中自动调用。
        """
        # 停止所有定时器
        timers_to_stop = [
            self._anim_timer,
            self._height_anim,
            self._elapsed_timer,
        ]
        for timer in timers_to_stop:
            try:
                if isinstance(timer, QTimer):
                    timer.stop()
                elif isinstance(timer, QVariantAnimation):
                    timer.stop()
            except RuntimeError:
                pass

        # 断开所有信号连接（打破引用环路）
        self._disconnect_all_signals()

        # [方案 4] viewer 回池：cleanup 时优先把 CodeWebViewer 归还复用池。
        # detach 成功自带 self.viewer=None → 下方 viewer 清理分支自然跳过；
        # detach 失败路径（None/流式/user 卡/非 CodeWebViewer/入池拒绝）自然回归
        # 原销毁行为。红线：禁止在 detach 失败路径补 deleteLater——detach 内部
        # 已处理失败分支的销毁，此处补会造成 double free。
        if self.viewer is not None:
            self.detach_viewer()

        # 调用 viewer 的清理方法（先清理后释放引用）
        # 🛡️ viewer 为 sip-deleted wrapper 时 hasattr 会抛 RuntimeError（既有隐患），
        # 这里整体兜底；alive viewer 的清理见内层 try。
        try:
            if hasattr(self.viewer, "cleanup"):
                try:
                    self.viewer.cleanup()
                except RuntimeError:
                    pass
        except RuntimeError:
            pass
        # [B4-强回收] 防悬挂：MessageCard 清理时同步清零 renderer PID
        self._renderer_pid = 0
        self.viewer = None  # 释放 viewer 引用，允许 GC

        # 清理大数据缓存
        self._content_data = None
        self._interactive_options = []
        self._markdown_text = None  # 大 markdown 文本
        self._last_rendered_html = None  # 大 HTML 字符串
        self._last_rendered_markdown = None  # 可能很大的 markdown
        self._rendered_code_blocks = []  # 代码块缓存
        self._pending_content = None  # 待渲染内容
        self._finished_streaming_ids.clear()  # 流式 ID 集合
        self._tool_args_first_seen_ids.clear()
        # 🐛 F3（次要提示 2）：工具登记集合随卡片销毁清空——否则边缘场景
        # （cleanup 后 _has_active_tools() 被误调）会因残留登记误判"仍有活跃工具"。
        self._tool_call_order.clear()

        # [PERF] preview 期间累积的 viewer 高度目标清理
        self._pending_viewer_height = None
        self._resize_preview_mode = False
        self._resize_preview_height = 0

        # 清理 markdown_cache 如果存在
        if hasattr(self, "_markdown_cache") and self._markdown_cache:
            self._markdown_cache.clear()
            self._markdown_cache = None

    def closeEvent(self, e):
        self.cleanup()
        super().closeEvent(e)


def resolve_initial_welcome_mode(saved_mode: str, saved_plugin_tab: str, registered_tabs: dict) -> str:
    """解析欢迎卡片初始 mode：上次选中的插件 tab 仍注册时优先，否则回退内置 mode

    - saved_plugin_tab: 配置里记忆的插件 mode_key（插件可能被卸载/停用）
    - registered_tabs: 当前 UIPluginRegistry 已注册的插件 tabs（dict，key 为 mode_key）
    """
    if saved_plugin_tab and saved_plugin_tab in registered_tabs:
        return saved_plugin_tab
    return saved_mode


def create_welcome_card(
    parent=None,
    agent_name: str = "",
    agent_description: str = "",
    recent_sessions: list = None,
    top_by_count: list = None,
    mode: str = "sessions",
    context_provider: Optional[Callable[[], Dict[str, Any]]] = None,
) -> MessageCard:
    """创建欢迎卡片

    Args:
        parent: 父控件
        agent_name: 当前智能体名称
        agent_description: 智能体描述
        recent_sessions: 最近的历史会话列表，每项包含 title, last_time, session_id, message_count
        top_by_count: 消息最多的会话列表，每项包含 title, last_time, session_id, message_count
        mode: 欢迎卡片模式（sessions / 插件注册 tab）
        context_provider: 窗口上下文提供者（无参回调 → dict）。多窗口隔离：
            渲染插件 tab 时注入当前窗口的 project_root / project_name /
            window_id，避免插件回读全局状态导致多标签页内容串项目。
    """
    card = MessageCard(role="welcome", timestamp="就绪", parent=parent)
    # 一次性把数据 + 模式交给卡片：tabs 在 PyQt 层；body 由卡片内部渲染
    card.set_welcome_content(
        recent_sessions=recent_sessions,
        top_by_count=top_by_count,
        mode=mode,
        context_provider=context_provider,
    )
    return card


def _render_welcome_body(
    mode: str,
    recent_sessions: list,
    top_by_count: list,
    window_context: Optional[dict] = None,
    suppress_anim: bool = False,
) -> str:
    """渲染欢迎卡片 body（不含标题和 tabs）；按 mode 分发

    Args:
        mode: 欢迎卡片模式（sessions / 插件注册 tab）
        recent_sessions: 最近会话列表
        top_by_count: 最活跃会话列表
        window_context: 当前窗口的 UI 上下文（project_root / project_name /
            window_id / session_id 等）。多窗口隔离的关键：注入插件 render_func，
            保证每个窗口渲染自己项目的内容，避免插件回读全局状态串项目。

    注意：**不缓存 render_func 结果**。部分插件 tab 是异步采集模式——首次渲染
    返回「加载中」占位，后台采集完成后再次调用 render_func 返回真实图表；
    缓存占位内容会导致数据永远不显示（project-dashboard 踩坑）。
    插件如需缓存应在自己内部做（如 collector 数据缓存），主程序不越俎代庖。
    """
    # 插件注册的欢迎 tab：render_func 返回 HTML 片段，走现有 markdown 管线
    try:
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

        tab = UIPluginRegistry.get_instance().get_welcome_tabs().get(mode)
        if tab is not None:
            # 注入主题上下文：插件 HTML 拿不到 Qt 主题，必须由主程序传入。
            # prefers-color-scheme 跟随 OS 而非 Qt 主题（theme_manager），
            # 单独依赖它会导致 Qt 暗色 + OS 亮色时不生效。
            try:
                from app.utils.theme_manager import theme_manager

                is_dark = not theme_manager.is_light_theme()
            except Exception:
                is_dark = False
            # 窗口上下文合并进插件 ctx（window_context 自带 is_dark，覆盖兜底值）
            ctx = {"is_dark": is_dark}
            if window_context:
                ctx.update(window_context)
            return tab.render_func(ctx) or ""
    except Exception:
        pass
    return _render_sessions_body(recent_sessions, top_by_count, suppress_anim=suppress_anim)


_SESSION_ROWS = 3  # 双列网格行数（每分类显示 3×2 = 6 张）
_SESSION_COLS = 2


def _session_duration_days(created_at: str) -> int:
    """计算会话持续天数（基于 created_at 与当前时间差）

    用于欢迎卡片最活跃会话卡片第二行展示「持续 X 天」。
    created_at 格式 "%Y-%m-%d %H:%M:%S"；空 / 解析失败返回 0。
    """
    if not created_at:
        return 0
    try:
        start = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        try:
            start = datetime.strptime(created_at[:10], "%Y-%m-%d")
        except (ValueError, TypeError):
            return 0
    return max((datetime.now() - start).days, 0)


def _render_sessions_body(recent_sessions: list, top_by_count: list, suppress_anim: bool = False) -> str:
    """渲染会话导览 body：最近 / 最活跃两个卡片双列网格（每分类 3 行）

    每张卡片：标题/副标题 + 右侧标签 + hover 滑入箭头（无图钉徽章：彩色 emoji
    贴片与卡片内线性图标体系混排显脏，信息量也不增，故移除）。
    复用 .context-tag 点击事件链（data-type="session" + data-session-id），
    仅替换视觉外观，JS 拦截逻辑不变。
    """

    def _render_item(s: dict, count_mode: bool, idx: int, suppress_anim: bool = False) -> str:
        """渲染单个会话卡片；idx 用于 stagger 动画延迟

        suppress_anim=True（软刷新：其他标签页会话变更广播到本窗口）时用内联
        `animation:none` 覆盖 CSS 进入动画，避免列表项重播 stagger fade-in。
        """
        t = escape(s.get("title", "未命名会话"))
        sid = escape(s.get("session_id", ""))
        if count_mode:
            # 第二行 = 日期（天）+ 持续天数（消息数移到右侧 tag，不重复显示）
            last_time = s.get("last_time") or ""
            date_str = last_time[:10] if len(last_time) >= 10 else last_time
            days = _session_duration_days(s.get("created_at") or "")
            days_part = f" · 持续 {days} 天" if days > 0 else ""
            meta = f"{date_str}{days_part}"
        else:
            meta = escape(s.get("last_time") or "")
        anim_style = "animation: none;" if suppress_anim else f"animation-delay:{idx * 55}ms"
        # 右侧 tag：最近 = 相对时间，最活跃 = 消息数（同一套中性色，仅文案不同）
        if count_mode:
            mc = s.get("message_count", 0)
            tag_html = f'<span class="session-item-tag">{mc} 条</span>'
        else:
            rel_label = format_relative_time(s.get("last_time") or "")
            tag_html = f'<span class="session-item-tag">{escape(rel_label)}</span>'
        return (
            f'<div class="context-tag session-item" data-type="session" '
            f'data-session-id="{sid}" data-action="session" '
            f'style="{anim_style}">'
            f'<span class="session-item-body">'
            f'<span class="session-item-title">{t}</span>'
            f'<span class="session-item-meta">{meta}</span>'
            f"</span>"
            f"{tag_html}"
            f'<span class="session-item-arrow">›</span>'
            f"</div>"
        )

    def _render_section(
        title: str,
        items: list,
        count_mode: bool = False,
        start_idx: int = 0,
        suppress_anim: bool = False,
        more_btn: str = "",
    ) -> str:
        """渲染单个分类 section；items 为空则返回空串

        start_idx: 全局连续卡片序号起点，保证跨分区的 stagger 动画连贯
        （否则两个分区各自从 0 开始，动画同时播放显得凌乱）。
        more_btn: 非空时在分区 header 右侧渲染快捷按钮，值为 data-type（action 名）。
        """
        if not items:
            return ""
        shown = items[: _SESSION_ROWS * _SESSION_COLS]
        rows = "".join(_render_item(s, count_mode, start_idx + i, suppress_anim) for i, s in enumerate(shown))
        more = ""
        if more_btn:
            more = (
                f'<span class="context-tag session-header-more" data-type="{escape(more_btn)}" '
                f'data-content="">全部 ›</span>'
            )
        return (
            f'<div class="session-section">'
            f'<div class="session-header">'
            f'<span class="session-header-title">{title}</span>'
            f"{more}"
            f"</div>"
            f'<div class="session-list">{rows}</div>'
            f"</div>"
        )

    recent_block = _render_section(
        "最近会话",
        recent_sessions,
        count_mode=False,
        start_idx=0,
        suppress_anim=suppress_anim,
        more_btn="workbench_history",
    )
    top_start = len(recent_sessions[: _SESSION_ROWS * _SESSION_COLS])
    top_block = _render_section(
        "最活跃会话", top_by_count, count_mode=True, start_idx=top_start, suppress_anim=suppress_anim
    )
    # 包根元素：软刷新增量替换的稳定锚点（refresh_welcome_data →
    # _refresh_welcome_body_incremental 只换本容器 innerHTML，不整页重建）
    if not (recent_block or top_block):
        return (
            '<div id="welcome-sessions-root"><div class="welcome-empty">还没有历史会话，开始第一次对话吧 ✨</div></div>'
        )
    return f'<div id="welcome-sessions-root">{recent_block}{top_block}</div>'
