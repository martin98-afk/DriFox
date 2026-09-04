# -*- coding: utf-8 -*-
"""「回到底部」浮动图标按钮

滚动守卫（MainWidget._should_follow_bottom）变强之后，用户上滚阅读历史时不会被
流式输出强行拽回，但也就失去了「新内容到了」的感知。这个按钮就是补偿：
视口离开底部超过阈值时浮出，点击恢复底部跟随并滚到底。

设计取舍：
- **浮动浮层**，不占静态布局空间（与 QUESTION 卡片 / 输入区按钮的视觉语言一致）。
- 圆形图标按钮，**不带文字**：图标本体已经表达「向下到底」，加文字反而让浮层变宽、
  遮挡消息。语义靠 tooltip 兜底。
- **图标资源走项目统一机制**：`icons/scroll_to_bottom.svg`（深色主题，白色填充）+
  `icons/light/scroll_to_bottom.svg`（浅色主题，`#333333`），由
  `generate_icon_qrc.py` 自动生成浅色变体，qrc 编译进 `app/utils/icons_*.py`。
  加载顺序见 `_load_theme_icon()`：qrc → 源码树文件 → 内联 SVG 按主题着色（兜底）。
- 定位锚点是 `chat_scroll_area`（不是 host）：它天然位于 top/bottom 卡容器之间，
  底边正好是「对话区结束、输入区开始」的位置，不需要去推算输入框高度。
- 事件过滤父级与锚点的 Resize/Move/Show 来重定位，不引入额外定时器。
"""

from typing import Optional

import weakref
from pathlib import Path

from PyQt5.QtCore import QByteArray, QEasingCurve, QEvent, QFile, QPropertyAnimation, QSize, Qt
from PyQt5.QtGui import QIcon, QPainter, QPixmap
from PyQt5.QtSvg import QSvgRenderer
from PyQt5.QtWidgets import QGraphicsOpacityEffect, QPushButton

from app.utils.design_tokens import Animations, Colors
from app.utils.theme_manager import theme_manager

# qrc 编译产物：**导入即注册资源**。主程序链路只 import 了深色 icons_rc，
# 浅色 icons_light_rc 从未被引入（见 markdown_block_viewer.py 的同款补齐），
# 不在这里补一次，`:/icons_light/...` 会全部解析失败。
try:
    from app.utils import icons_light_rc as _icons_light_rc  # noqa: F401
    from app.utils import icons_rc as _icons_rc  # noqa: F401
except Exception:  # noqa: BLE001
    pass

# 圆形按钮直径（px）
_BUTTON_SIZE = 32
_MARGIN_RIGHT = 20  # 距 chat_scroll_area 右边缘
_MARGIN_BOTTOM = 14  # 距 chat_scroll_area 下边缘
_ICON_SIZE = 18

# 浮出/隐藏淡入淡出时长：浮层标准区间 125-200ms，隐藏稍短更利落
_FADE_IN_MS = 160
_FADE_OUT_MS = 120

# 图标资源名（qrc 前缀 / 源码树相对路径共用同一个文件名）
_ICON_DARK_REL = "icons/scroll_to_bottom.svg"
_ICON_LIGHT_REL = "icons/light/scroll_to_bottom.svg"
_ICON_DARK_QRC = ":/icons/scroll_to_bottom.svg"
_ICON_LIGHT_QRC = ":/icons_light/scroll_to_bottom.svg"

# 内联 SVG：向下箭头 + 底部横线（viewBox 1024×1024）
# 与 icons/scroll_to_bottom.svg 同形，只在**图标文件整体缺失**时兜底渲染，
# fill 在渲染时按主题替换。改图标请改 icons/ 下的文件，别改这里。
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


def _load_theme_icon(is_light: bool) -> QIcon:
    """按主题取「回到底部」图标。

    三级回退，越靠前越"正统"：
    1. **qrc**（`:/icons/...` 深色 / `:/icons_light/...` 浅色）—— 项目统一机制，
       打包后唯一可用路径；浅色变体由 `generate_icon_qrc.py` 自动生成。
    2. **源码树文件** —— qrc 尚未重新编译时的开发态兜底。
    3. **内联 SVG 按主题着色** —— 图标文件整体缺失时仍不至于空白。

    Args:
        is_light: 当前是否浅色主题
    """
    qrc_path = _ICON_LIGHT_QRC if is_light else _ICON_DARK_QRC
    file_rel = _ICON_LIGHT_REL if is_light else _ICON_DARK_REL
    candidates = []
    # 1) qrc：只有资源确实注册进去了才加入候选，否则 QIcon 会拿到空图标
    if QFile.exists(qrc_path):
        candidates.append(qrc_path)
    # 2) 源码树
    try:
        root = Path(__file__).resolve().parents[2]
        candidates.append(str(root / file_rel))
    except Exception:  # noqa: BLE001
        pass
    for path in candidates:
        try:
            icon = QIcon(path)
            if not icon.isNull():
                return icon
        except Exception:  # noqa: BLE001
            continue
    # 3) 内联 SVG 兜底
    return _build_icon(Colors.TEXT_PRIMARY)


def _build_icon(color: str, size: int = _ICON_SIZE) -> QIcon:
    """按给定颜色把内联 SVG 渲染成 QIcon（资源缺失时的兜底路径）

    QSvgRenderer 不支持 CSS 的 currentColor，只能把颜色烧进 fill。
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


# ── 主题切换刷新：走 UIEventBus，不是 theme_manager.register_refresh_target ──
# 🐛 根因（2026-08-30 实修）：主程序的主题切换走
# `main_widget._execute_batched_theme_refresh`，它只做 Colors.refresh() +
# theme_manager.on_theme_changed() + per-window `_apply_runtime_ui_settings()`，
# **从不调用 theme_manager.dispatch_refresh()** → 注册进 _refresh_targets 的
# widget 一个都收不到 refresh_theme()（main_widget.py:9185 的注释也点明了这一点）。
# dispatch_refresh() 只在 theme_manager.reload() / config_sync 里被调用。
# 可靠的广播源是 `on_theme_changed()` 发布的 EV_THEME_CHANGED 事件。
#
# ⚠️ UIEventBus.subscribe 持有回调的**强引用**，直接传 self._apply_style 会让
# 按钮永远不被回收（父窗口销毁后仍滞留）。因此这里用模块级 WeakSet 登记实例，
# 只订阅一次模块级函数。
_BUTTONS: "weakref.WeakSet" = weakref.WeakSet()
_theme_subscribed = False


def _on_theme_changed_event(payload):
    """EV_THEME_CHANGED 回调：把刷新分发给所有存活的按钮"""
    for btn in list(_BUTTONS):
        try:
            btn.refresh_theme()
        except Exception:  # noqa: BLE001
            pass


def _ensure_theme_subscription():
    """订阅主题切换事件（幂等，只在第一个按钮创建时真正执行一次）"""
    global _theme_subscribed
    if _theme_subscribed:
        return
    try:
        from app.core.ui_event_bus import EV_THEME_CHANGED, UIEventBus

        UIEventBus.get_instance().subscribe(EV_THEME_CHANGED, _on_theme_changed_event)
        _theme_subscribed = True
    except Exception:  # noqa: BLE001
        pass


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
        self._style_sig = None  # 已应用样式的主题签名（用于跳过重复刷新）
        # 淡入淡出状态：_opacity_value 跟随动画实时值，retarget 时从它续接
        self._fade_anim: Optional[QPropertyAnimation] = None
        self._fade_target = 1.0
        self._opacity_value = 1.0

        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.setObjectName("scrollToBottomButton")
        self.setToolTip("回到底部")
        self.setFixedSize(_BUTTON_SIZE, _BUTTON_SIZE)
        self.setIconSize(QSize(_ICON_SIZE, _ICON_SIZE))
        if on_click is not None:
            self.clicked.connect(on_click)

        # 主题切换：EV_THEME_CHANGED 才是真正会被派发的广播
        # （register_refresh_target 走的 dispatch_refresh 在主题切换路径上没人调，
        #  但 reload() / config_sync 会调，两条都留着，靠签名去重）
        self.refresh_theme = self._apply_style
        try:
            _BUTTONS.add(self)
            _ensure_theme_subscription()
            theme_manager.register_refresh_target(self)
        except Exception:  # noqa: BLE001
            pass

        self._apply_style()
        self.hide()

        # 父级 / 锚点尺寸变化时重定位（不引入额外定时器）
        if parent is not None:
            parent.installEventFilter(self)
        if anchor is not None:
            anchor.installEventFilter(self)

    # ── 样式 ──────────────────────────────────────────────────
    def _apply_style(self, force: bool = False):
        """按当前主题重刷按钮样式与图标（签名未变则跳过，避免重复渲染 SVG）

        Args:
            force: 忽略签名强制重刷（一般不用；签名已覆盖主题 id + 全部用到的颜色）
        """
        light = theme_manager.is_light_theme()
        # 签名 = 主题 id + 本方法实际读取的全部颜色 token —— 任一变化都必然重刷
        sig = (
            theme_manager.get_current_theme_id(),
            light,
            Colors.CARD_BG,
            Colors.BORDER,
            Colors.HOVER_BG,
            Colors.TEXT_PRIMARY,
        )
        if not force and sig == self._style_sig:
            return
        self._style_sig = sig

        # Colors.CARD_BG 是带 {alpha} 占位符的模板，这里给足不透明度当作实心底
        try:
            bg = Colors.CARD_BG.format(alpha=0.92)
        except Exception:
            bg = "rgba(33, 33, 38, 0.92)"
        border = Colors.BORDER
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
        # 图标走 icons/ + icons/light/ 两套资源，按主题选（内部还有两级回退）
        self.setIcon(_load_theme_icon(light))

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
    def showEvent(self, event):
        """浮出前按当前主题校准样式（第二道保险）

        按钮大部分时间是隐藏的（只在离底时浮出），主题切换事件虽然也会刷，
        但这里再补一道：签名未变时 `_apply_style` 直接 return，几乎零成本。
        """
        self._apply_style()
        super().showEvent(event)

    def set_visible_pref(self, visible: bool):
        """宿主调用：请求显示/隐藏

        实际可见性还要叠加「锚点确实有可滚内容」这一条件 —— 内容不足一屏时
        按钮没有意义，浮在那里反而碍事。
        """
        self._visible_pref = bool(visible)
        self._sync_visibility()

    def _sync_visibility(self):
        should_show = self._visible_pref and self._has_scrollable_content()
        target = 1.0 if should_show else 0.0
        settled = should_show == self.isVisible() and self._fade_target == target
        if settled:
            if should_show:
                self._reposition()
            return
        if should_show:
            self._reposition()
            self.raise_()
            if not self.isVisible():
                self.show()
            self._fade_to(1.0, _FADE_IN_MS)
        else:
            self._fade_to(0.0, _FADE_OUT_MS)

    def _fade_to(self, target: float, duration: int):
        """淡入/淡出到目标透明度（复用动画对象；中途反向时从当前值续接）"""
        self._fade_target = target
        if not Animations.motion_enabled():
            self._set_opacity(target)
            if target <= 0.0:
                self.hide()
            return
        if self._fade_anim is None:
            effect = QGraphicsOpacityEffect(self)
            effect.setOpacity(self._opacity_value)
            self.setGraphicsEffect(effect)
            anim = QPropertyAnimation(effect, b"opacity", self)
            anim.setEasingCurve(QEasingCurve(Animations.EASE_OUT))
            anim.valueChanged.connect(self._set_opacity)
            anim.finished.connect(self._on_fade_done)
            self._fade_anim = anim
        anim = self._fade_anim
        anim.stop()
        anim.setDuration(int(duration))
        anim.setStartValue(self._opacity_value)
        anim.setEndValue(target)
        anim.start()

    def _set_opacity(self, value: float):
        self._opacity_value = float(value)

    def _on_fade_done(self):
        # 淡出完成才真正隐藏；若动画期间宿主又请求显示（_fade_target 反向），跳过
        if self._fade_target <= 0.0 and self.isVisible() and not self._visible_pref:
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
