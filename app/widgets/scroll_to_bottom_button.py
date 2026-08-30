# -*- coding: utf-8 -*-
"""「回到底部」浮动图标按钮

滚动守卫（MainWidget._should_follow_bottom）变强之后，用户上滚阅读历史时不会被
流式输出强行拽回，但也就失去了「新内容到了」的感知。这个按钮就是补偿：
视口离开底部超过阈值时浮出，点击恢复底部跟随并滚到底。

设计取舍：
- **浮动浮层**，不占静态布局空间（与 QUESTION 卡片 / 输入区按钮的视觉语言一致）。
- 圆形图标按钮，**不带文字**：图标本体已经表达「向下到底」，加文字反而让浮层变宽、
  遮挡消息。语义靠 tooltip 兜底。
- 图标是**内联 SVG**（见 `_ICON_PATH_D`），不落 icons/ 也不进 qrc：
  1. 打包后无需关心资源收集；
  2. 可以直接按当前主题把 fill 换成 `Colors.TEXT_PRIMARY`，一套图形通吃深浅色，
     不必像 register_input_button 那样维护 icon_path / icon_light_path 两份文件。
- 定位锚点是 `chat_scroll_area`（不是 host）：它天然位于 top/bottom 卡容器之间，
  底边正好是「对话区结束、输入区开始」的位置，不需要去推算输入框高度。
- 事件过滤父级与锚点的 Resize/Move/Show 来重定位，不引入额外定时器。
"""

from PyQt5.QtCore import QByteArray, QEvent, QSize, Qt
from PyQt5.QtGui import QIcon, QPainter, QPixmap
from PyQt5.QtSvg import QSvgRenderer
from PyQt5.QtWidgets import QPushButton

from app.utils.design_tokens import Colors
from app.utils.theme_manager import theme_manager

# 圆形按钮直径（px）
_BUTTON_SIZE = 32
_MARGIN_RIGHT = 20  # 距 chat_scroll_area 右边缘
_MARGIN_BOTTOM = 14  # 距 chat_scroll_area 下边缘
_ICON_SIZE = 18

# 内联 SVG：向下箭头 + 底部横线（viewBox 1024×1024）
# 来源：用户提供的 回到底部.svg；fill 在渲染时按主题替换。
_ICON_PATH_D = (
    "M512 1.28c-34.986667 0-63.573333 29.013333-63.573333 64.853333l-0.426667 544.426667"
    "-212.053333-212.053333c-24.746667-25.173333-64.853333-25.173333-89.6 0"
    "-24.746667 25.173333-24.746667 66.56 0 91.733333l311.466666 313.173333"
    "c11.093333 18.346667 31.146667 30.293333 53.76 30.293334 14.08 0 27.306667-4.693333"
    " 37.973334-12.8 2.986667-2.133333 5.546667-4.266667 8.106666-7.253334"
    " 2.56-2.56 4.693333-5.12 6.4-7.68l314.026667-317.866666"
    "c24.746667-25.173333 24.746667-66.56 0-91.733334s-64.853333-25.173333-89.6 0"
    "l-213.333333 215.466667 0.426666-545.706667C575.146667 30.293333 546.986667 1.28 512 1.28z"
    "M1024 960c0-35.413333-28.586667-64-64-64H64c-35.413333 0-64 28.586667-64 64"
    "s28.586667 64 64 64h896c35.413333 0 64-28.586667 64-64z"
)


def _build_icon(color: str, size: int = _ICON_SIZE) -> QIcon:
    """按给定颜色把内联 SVG 渲染成 QIcon

    QSvgRenderer 不支持 CSS 的 currentColor，只能把颜色烧进 fill。
    按主题重烧一遍即可，比维护深浅两套图标文件更省事。
    """
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024" '
        f'width="{size}" height="{size}">'
        f'<path d="{_ICON_PATH_D}" fill="{color}"/></svg>'
    )
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    try:
        renderer.render(painter)
    finally:
        painter.end()
    return QIcon(pixmap)


class ScrollToBottomButton(QPushButton):
    """浮在对话区右下角的「回到底部」圆形图标按钮

    Args:
        parent: 宿主窗口（OpenAIChatToolWindow），按钮作为其子 widget 浮在上层
        anchor: 定位锚点，即 chat_scroll_area
        on_click: 点击回调（由宿主提供：恢复跟随 + 滚到底）
    """

    def __init__(self, parent=None, anchor=None, on_click=None):
        super().__init__(parent)
        self._anchor = anchor
        self._on_click_cb = on_click
        self._visible_pref = False  # 宿主要求的显示状态（实际显示还要看有无可滚内容）

        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.setObjectName("scrollToBottomButton")
        self.setToolTip("回到底部")
        self.setFixedSize(_BUTTON_SIZE, _BUTTON_SIZE)
        self.setIconSize(QSize(_ICON_SIZE, _ICON_SIZE))
        if on_click is not None:
            self.clicked.connect(on_click)

        self._apply_style()
        self.hide()

        # 父级 / 锚点尺寸变化时重定位（不引入额外定时器）
        if parent is not None:
            parent.installEventFilter(self)
        if anchor is not None:
            anchor.installEventFilter(self)

        # 主题切换自动重刷样式
        self.refresh_theme = self._apply_style
        try:
            theme_manager.register_refresh_target(self)
        except Exception:
            pass

    # ── 样式 ──────────────────────────────────────────────────
    def _apply_style(self):
        """按当前主题重刷按钮样式与图标颜色"""
        light = theme_manager.is_light_theme()
        # Colors.CARD_BG 是带 {alpha} 占位符的模板，这里给足不透明度当作实心底
        try:
            bg = Colors.CARD_BG.format(alpha=0.92)
        except Exception:
            bg = "rgba(33, 33, 38, 0.92)"
        border = Colors.BORDER
        text = Colors.TEXT_PRIMARY
        # 浅色主题下 HOVER_BG 是白色半透明，叠在按钮上太淡，改用中性灰
        hover = "rgba(0, 0, 0, 0.06)" if light else Colors.HOVER_BG

        self.setStyleSheet(
            f"""
            QPushButton#scrollToBottomButton {{
                background-color: {bg};
                border: 1px solid {border};
                border-radius: {_BUTTON_SIZE // 2}px;
                padding: 0px;
            }}
            QPushButton#scrollToBottomButton:hover {{
                background-color: {hover};
            }}
            QPushButton#scrollToBottomButton:pressed {{
                background-color: {hover};
            }}
            """
        )
        # 图标颜色跟随正文色 —— 深浅主题自动适配
        self.setIcon(_build_icon(text))

    # ── 定位 ──────────────────────────────────────────────────
    def eventFilter(self, obj, event):
        if event.type() in (QEvent.Resize, QEvent.Move, QEvent.Show):
            self._reposition()
        return False  # 不消费事件，宿主自身的 eventFilter 照常工作

    def _reposition(self):
        """把自己摆到锚点右下角"""
        anchor = self._anchor
        if anchor is None:
            return
        try:
            geo = anchor.geometry()
            x = geo.right() - self.width() - _MARGIN_RIGHT
            y = geo.bottom() - self.height() - _MARGIN_BOTTOM
            self.move(max(0, x), max(0, y))
            self.raise_()
        except RuntimeError:
            pass

    # ── 显隐 ──────────────────────────────────────────────────
    def set_visible_pref(self, visible: bool):
        """宿主调用：请求显示/隐藏

        实际可见性还要叠加「锚点确实有可滚内容」这一条件 —— 内容不足一屏时
        按钮没有意义，浮在那里反而碍事。
        """
        self._visible_pref = bool(visible)
        self._sync_visibility()

    def _sync_visibility(self):
        should_show = self._visible_pref and self._has_scrollable_content()
        if should_show == self.isVisible():
            if should_show:
                self._reposition()
            return
        if should_show:
            self._reposition()
            self.raise_()
            self.show()
        else:
            self.hide()

    def _has_scrollable_content(self) -> bool:
        anchor = self._anchor
        if anchor is None:
            return False
        try:
            bar = anchor.verticalScrollBar()
            return bar is not None and bar.maximum() > 0
        except RuntimeError:
            return False
