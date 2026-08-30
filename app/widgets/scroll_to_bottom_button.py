# -*- coding: utf-8 -*-
"""「回到底部」浮动胶囊

滚动守卫（MainWidget._should_follow_bottom）变强之后，用户上滚阅读历史时不会被
流式输出强行拽回，但也就失去了「新内容到了」的感知。这个胶囊就是补偿：
视口离开底部超过阈值时浮出，点击恢复底部跟随并滚到底。

设计取舍：
- **浮动浮层**，不占静态布局空间（与 QUESTION 卡片 / 输入区按钮的视觉语言一致）。
- 定位锚点是 `chat_scroll_area`（不是 host）：它天然位于 top/bottom 卡容器之间，
  底边正好是「对话区结束、输入区开始」的位置，不需要去推算输入框高度。
- 主题感知：颜色全走 `Colors` token；实现 `refresh_theme()` 并注册到
  `theme_manager`，切换主题时自动重刷样式。
- 事件过滤父级与锚点的 Resize/Move/Show 来重定位，不引入额外定时器。
"""

from PyQt5.QtCore import QEvent, Qt
from PyQt5.QtWidgets import QPushButton

from app.utils.design_tokens import Colors
from app.utils.theme_manager import theme_manager

# 胶囊尺寸（px）
_BUTTON_HEIGHT = 32
_BUTTON_PADDING_X = 12
_MARGIN_RIGHT = 20  # 距 chat_scroll_area 右边缘
_MARGIN_BOTTOM = 14  # 距 chat_scroll_area 下边缘


class ScrollToBottomButton(QPushButton):
    """浮在对话区右下角的「回到底部」胶囊

    Args:
        parent: 宿主窗口（OpenAIChatToolWindow），胶囊作为其子 widget 浮在上层
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
        # 纯文字胶囊：不引图标，规避 FluentIcon 的深浅色图标路径差异
        self.setText("回到底部")
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
        """按当前主题重刷胶囊样式（浅色/深色都走 Colors token）"""
        light = theme_manager.is_light_theme()
        # Colors.CARD_BG 是带 {alpha} 占位符的模板，这里给足不透明度当作实心底
        try:
            bg = Colors.CARD_BG.format(alpha=0.92)
        except Exception:
            bg = "rgba(33, 33, 38, 0.92)"
        border = Colors.BORDER
        text = Colors.TEXT_PRIMARY
        hover = Colors.HOVER_BG
        # 浅色主题下 HOVER_BG 是白色半透明，叠在胶囊上太淡，改用边框色做 hover
        if light:
            hover = "rgba(0, 0, 0, 0.06)"

        self.setStyleSheet(
            f"""
            QPushButton#scrollToBottomButton {{
                background-color: {bg};
                border: 1px solid {border};
                border-radius: {_BUTTON_HEIGHT // 2}px;
                color: {text};
                padding: 0 {_BUTTON_PADDING_X}px;
                font-size: 13px;
            }}
            QPushButton#scrollToBottomButton:hover {{
                background-color: {hover};
            }}
            QPushButton#scrollToBottomButton:pressed {{
                background-color: {hover};
            }}
            """
        )
        self.setFixedHeight(_BUTTON_HEIGHT)
        # 文本变化后重算宽度（中文字数固定，但字体缩放会影响）
        self.adjustSize()

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
        胶囊没有意义，浮在那里反而碍事。
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
