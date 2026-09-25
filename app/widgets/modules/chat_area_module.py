# -*- coding: utf-8 -*-
"""chat_area 模块 — 对话滚动区装配（源 main_widget.setup_ui L3076-3105）

属性契约（host.setattr）：
- chat_scroll_area  SingleDirectionScrollArea
- chat_container    QWidget（承载消息的容器）
- chat_layout       QVBoxLayout（chat_container 内部布局）
- _on_chat_scrolled 滚动条回调（connect scroll_bar.valueChanged）

PR3 扩展（多区域主题插件基础设施）：
- _decoration_layer  DecorationLayer（host 子 widget，撑满整屏，按 anchor 摆放装饰件）
- _apply_decorations  应用 decorations 主题配置（apply_config）

注意：scene_layer（撑满整个 TabManagerWindow._chat_frame 的场景背景）
**不**在这里创建——它在 TabManagerWindow._setup_ui 里创建在 _chat_frame
上（详见 app/widgets/tab_manager_window.py）。原因：scene 背景需要覆盖整
个右侧圆角矩形（含 tab bar / LEFT/RIGHT/BOTTOM 停靠区 / UI 插件槽位等），
而 host（OpenAIChatToolWindow）只是 _chat_frame 内部嵌入的对话子窗口，
挂在自己身上无法覆盖 _chat_frame 内其它元素。
"""

import os

from app.plugins.contracts.ui_module import UIModule

# ── 对话区滚轮平滑参数（2026-09-26 手感修复） ──────────────────────────────
# 背景：qfluentwidgets 的 FixedStepSmoothScrollEngine 默认
# duration=400 / stepRatio=1.5 / acceleration=1 / SmoothMode.LINEAR，
# 实测在用户自然滚速（200-400ms 一格）下产生「划一下停一下」：
#   · LINEAR 插值把单格摊成三角形速度曲线——起步/收尾各 2-3 帧近乎静止，
#     中间冲到峰值，每格一次「等→冲→停」，慢滚时被感知为两次独立停顿；
#   · stepRatio=1.5 叠 acceleration=1，单格实滚 96-109px，而原生
#     QScrollBar 基准是 60px，位移过冲且与系统内其它列表不一致；
#   · duration=400ms 长于用户的滚轮间隔，队列来不及消化，新旧动画叠加。
#
# 定参依据（真实 60fps 定时器驱动，逐格扫过 100/200/350/500ms 四档滚轮间隔）：
#   CONSTANT 匀速插值消除头尾死区（静止占比 400ms 间隔下 0%，LINEAR 为 48%）；
#   stepRatio=1.0 + acceleration=0 让单格对齐原生 60px（实测 104px → 52-60px）；
#   duration=500ms 在稳态抖动（50%）与队列积压（峰 1-3，不断流）间平衡最优。
#
# ⚠️ stepsTotal = fps * duration / 1000 必须为整数，否则 stepsLeftQueue
#    永不排空 → 滚动彻底卡死（实测 233/267ms 会留下 3s+ 静止段）。
#    duration 请只取 200 / 300 / 400 / 500 / 600 这类 60 的整倍数值。
_SCROLL_SMOOTH_DURATION = 500
_SCROLL_SMOOTH_STEP_RATIO = 1.0
_SCROLL_SMOOTH_ACCELERATION = 0


def _configure_smooth_scroll(scroll_area) -> None:
    """收敛对话区滚轮手感：匀速插值 + 原生位移量 + 无加速度放大。

    在 build() 中于滚动区创建后立即调用。任何一步失败都不影响滚动区
    可用性（qfluentwidgets 内部结构变化时退化为默认手感，不抛异常）。
    """
    from qfluentwidgets.common.smooth_scroll import SmoothMode

    try:
        smooth = scroll_area.smoothScroll
        smooth.setSmoothMode(SmoothMode.CONSTANT)
        for engine in (smooth.fixedStepScrollEngine, smooth.adaptiveScrollEngine):
            engine.duration = _SCROLL_SMOOTH_DURATION
            engine.stepRatio = _SCROLL_SMOOTH_STEP_RATIO
            engine.acceleration = _SCROLL_SMOOTH_ACCELERATION
    except Exception:
        pass


def _resolve_chat_image(image: str) -> str:
    """解析图片路径（chat_area_module 局部 helper，复用 theme_manager.get_theme_resource）

    - 'qrc:/...' / ':/icons/...' / 绝对路径 → 原样返回
    - 相对路径 → 主题目录下查找，存在则返回绝对路径，否则原样返回（QPixmap.load 失败兜底）
    """
    from app.utils.theme_manager import theme_manager

    if not image or image.startswith(":") or os.path.isabs(image):
        return image
    theme_id = theme_manager.get_current_theme_id()
    resource_path = theme_manager.get_theme_resource(theme_id, image)
    if resource_path is not None:
        return str(resource_path)
    return image


class ChatAreaModule(UIModule):
    """对话区模块：top/bottom 卡容器间嵌入 chat scroll 区域"""

    module_id = "chat_area"

    def build(self, host) -> None:
        from PyQt5.QtCore import Qt, QTimer
        from PyQt5.QtWidgets import QSizePolicy, QVBoxLayout, QWidget
        from qfluentwidgets import SingleDirectionScrollArea

        # CHAT_SCROLL_STYLE 在 main_widget 模块级定义（避免循环引用）
        from app.main_widget import CHAT_SCROLL_STYLE
        from app.utils.theme_manager import theme_manager
        from app.widgets.decoration_layer import DecorationLayer

        layout = host.layout()

        # ── 对话滚动区 ──
        host.chat_scroll_area = SingleDirectionScrollArea(host)
        host.chat_scroll_area.setMinimumHeight(0)
        host.chat_scroll_area.setMinimumWidth(320)
        host.chat_scroll_area.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Ignored)
        host.chat_scroll_area.setStyleSheet(CHAT_SCROLL_STYLE)
        host.chat_scroll_area.setWidgetResizable(True)
        host.chat_scroll_area.setViewportMargins(2, 2, 10, 2)
        host.chat_scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        _configure_smooth_scroll(host.chat_scroll_area)

        host.chat_container = QWidget()
        host.chat_container.setStyleSheet("background: transparent;")
        host.chat_container.setAcceptDrops(True)
        try:
            host.chat_container.installEventFilter(host)
        except Exception:
            pass
        host.chat_layout = QVBoxLayout(host.chat_container)
        host.chat_layout.setContentsMargins(6, 6, 6, 6)
        host.chat_layout.setSpacing(6)  # 消息间距（2026-09-23 收敛：8→6，配合卡片内边距收紧）
        host.chat_layout.setAlignment(Qt.AlignBottom)
        host.chat_scroll_area.setWidget(host.chat_container)

        # 连接滚动事件（虚拟滚动回收）
        try:
            scroll_bar = host.chat_scroll_area.verticalScrollBar()
            scroll_bar.valueChanged.connect(host._on_chat_scrolled)
        except Exception:
            pass

        # ── 装配三段式：top 卡容器 → chat 滚动区 → bottom 卡容器 ──
        # _top_card_container / _bottom_card_container 已在 setup_ui 头部创建
        if getattr(host, "_top_card_container", None) is not None:
            layout.addWidget(host._top_card_container)
        layout.addWidget(host.chat_scroll_area, 1)
        if getattr(host, "_bottom_card_container", None) is not None:
            layout.addWidget(host._bottom_card_container)

        # ── PR3：装饰件叠加层（decorations） ──
        # 注意：scene_layer 由 TabManagerWindow 在自己的 _chat_frame 上创建
        # （撑满整个右侧圆角矩形），不在这里创建。
        # DecorationLayer 作为 host 子 widget（撑满 host 整屏，按 anchor 定位）
        host._decoration_layer = DecorationLayer(
            host,
            ref_resolver=lambda attr: getattr(host, attr, None),
        )
        # 浮在 host 所有子 widget 之上（蝴蝶结等装饰件在输入框之上）
        host._decoration_layer.raise_()

        # 注册主题刷新钩子：refresh_theme 触发重新应用配置
        host._apply_decorations = _make_apply(host)
        host._decoration_layer.refresh_theme = host._apply_decorations
        theme_manager.register_refresh_target(host._decoration_layer)

        # ── 「回到底部」浮动胶囊 ──
        # 必须在 _decoration_layer.raise_() 之后创建，否则会被装饰层盖住。
        # 滚动守卫变强后用户上滚不会被拽回，靠它补上「新内容到了」的感知与一键归位。
        from app.widgets.scroll_to_bottom_button import ScrollToBottomButton

        host._scroll_to_bottom_button = ScrollToBottomButton(
            host,
            anchor=host.chat_scroll_area,
            on_click=lambda: host._on_scroll_to_bottom_clicked(),
        )
        # 位置变化（valueChanged）与内容高度变化（rangeChanged）都要刷新显隐：
        # 后者覆盖「内容撑高把用户推离底部」这一场景 —— 此时 value 并没变。
        try:
            _bar = host.chat_scroll_area.verticalScrollBar()
            _bar.valueChanged.connect(lambda *_: host._update_scroll_to_bottom_button())
            _bar.rangeChanged.connect(lambda *_: host._update_scroll_to_bottom_button())
        except Exception:
            pass

        # 初始加载（延迟到首帧后，避免阻塞 setup_ui 关键路径）
        QTimer.singleShot(0, host._apply_decorations)


def _make_apply(host) -> callable:
    """闭包工厂：构造绑定 host 的 apply 函数（避免 self 闭包抽象）"""
    def apply():
        from app.utils.theme_manager import theme_manager

        bgs = theme_manager.get_theme_backgrounds(theme_manager.get_current_theme_id())
        if hasattr(host, "_decoration_layer") and host._decoration_layer is not None:
            host._decoration_layer.apply_config(bgs["decorations"], _resolve_chat_image)
    return apply