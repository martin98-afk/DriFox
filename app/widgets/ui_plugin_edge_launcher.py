# -*- coding: utf-8 -*-
"""UI 插件左侧边缘入口（Launcher）

设计目标：在主窗口左侧提供「细线 → 胶囊 → 菜单」三态切换，
让用户在不记忆斜杠命令的情况下发现并打开 UI 插件浮动卡片。

组件职责（严格限制）：
  1. 绘制细线 / 胶囊两态；
  2. 管理鼠标进入 / 离开 / 点击 / 收起等交互状态；
  3. 创建和显示插件菜单；
  4. 把菜单点击转发给 UIPluginRegistry（不直接创建卡片）。

不负责：
  - 插件卡片实例化（交给 UIPluginRegistry.toggle_floating_card）；
  - 插件启用 / 禁用 / 安装 / 卸载；
  - 替换斜杠命令系统；
  - 修改主布局（浮层不参与 sizeHint 计算）。
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from loguru import logger
from PyQt5.QtCore import QEvent, QPoint, QRect, QSize, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QIcon, QPainter, QPainterPath
from PyQt5.QtWidgets import (
    QAction,
    QApplication,
    QMenu,
    QWidget,
)
from qfluentwidgets import FluentIcon

from app.utils.design_tokens import Colors, font_size_css, scale_font_size
from app.utils.utils import _is_current_theme_light, get_font_family_css

# ── 尺寸常量（来自设计文档 §2）───────────────────────────────
LINE_WIDTH = 4  # 默认细线宽（px）
LINE_HEIGHT = 80  # 默认细线高（px）
CAPSULE_WIDTH = 22  # 胶囊展开后宽（比设计文档的 18 略宽，便于图标识别）
CAPSULE_HEIGHT = 64  # 胶囊高（设计文档 60，留 4px 给阴影/边距）
# 触发区与胶囊等宽（紧贴左边缘时，胶囊整体都应可点击命中）
TRIGGER_ZONE_WIDTH = CAPSULE_WIDTH
COLLAPSE_DELAY_MS = 220  # 鼠标离开后延迟收起（避免路过边缘闪烁）
MENU_MIN_WIDTH = 220  # 弹出菜单最小宽度
MENU_MAX_HEIGHT = 360  # 弹出菜单最大高度（超过则上下调整）
ANIM_DURATION_MS = 160  # 展开/收起动画时长
ANIM_TICK_MS = 16  # 动画帧间隔（~60fps）


class _LauncherVisual(QWidget):
    """Launcher 内部可见的"细线 / 胶囊"渲染层

    作为 :class:`UIPluginEdgeLauncher` 的子控件，承载动画和绘制。
    外层（Launcher）负责触发区命中检测与状态机，本控件仅负责视觉。
    鼠标事件透传给父控件（WA_TransparentForMouseEvents）。
    """

    def __init__(self, parent: "UIPluginEdgeLauncher"):
        super().__init__(parent)
        self._expansion: float = 0.0
        self._anim_timer: Optional[QTimer] = None
        self._anim_start: float = 0.0
        self._anim_target: float = 0.0
        self._anim_elapsed: int = 0
        self._anim_duration: int = ANIM_DURATION_MS

        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_OpaquePaintEvent, False)

    def set_expansion(self, value: float) -> None:
        """设置展开量（0.0 ~ 1.0），会同步重绘。"""
        self._expansion = max(0.0, min(1.0, value))
        self.update()

    def animate_to(self, target: float, duration_ms: int = ANIM_DURATION_MS) -> None:
        """动画到目标展开量。"""
        target = max(0.0, min(1.0, target))
        if self._anim_timer is None:
            self._anim_timer = QTimer(self)
            self._anim_timer.setInterval(ANIM_TICK_MS)
            self._anim_timer.timeout.connect(self._on_anim_tick)
        self._anim_timer.stop()
        self._anim_start = self._expansion
        self._anim_target = target
        self._anim_elapsed = 0
        self._anim_duration = max(1, duration_ms)
        self._anim_timer.start()

    def _on_anim_tick(self) -> None:
        self._anim_elapsed += ANIM_TICK_MS
        t = min(1.0, self._anim_elapsed / self._anim_duration)
        # smoothstep (ease-in-out)
        eased = t * t * (3 - 2 * t)
        value = self._anim_start + (self._anim_target - self._anim_start) * eased
        self.set_expansion(value)
        if t >= 1.0:
            self._anim_timer.stop()

    def paintEvent(self, event) -> None:  # noqa: D401
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)

        rect = QRect(0, 0, self.width(), self.height())
        if self._expansion <= 0.001:
            # 纯细线紧贴左边缘
            line_rect = QRect(0, 0, LINE_WIDTH, rect.height())
            self._paint_line(painter, line_rect)
        else:
            # 从左边缘向右展开的半胶囊
            current_w = LINE_WIDTH + (CAPSULE_WIDTH - LINE_WIDTH) * self._expansion
            cap_rect = QRect(0, 0, int(current_w), rect.height())
            self._paint_half_capsule(painter, cap_rect, self._expansion)

        painter.end()

    def _paint_line(self, painter: QPainter, rect: QRect) -> None:
        """绘制默认细线（紧贴左边缘，发光向右）"""
        accent = self._accent_color()
        # 发光底层（向右扩散，alpha 较低）
        glow_color = QColor(accent)
        glow_color.setAlpha(60)
        painter.setPen(Qt.NoPen)
        painter.setBrush(glow_color)
        painter.drawRoundedRect(rect.adjusted(0, 6, 2, -6), 2, 2)
        # 主线条
        solid = QColor(accent)
        solid.setAlpha(220)
        painter.setBrush(solid)
        painter.drawRoundedRect(rect, 2, 2)

    def _paint_half_capsule(self, painter: QPainter, rect: QRect, expansion: float) -> None:
        """绘制半胶囊：左边缘平直 + 右上右下圆角（向右展开）"""
        accent = self._accent_color()
        x, y, w, h = rect.x(), rect.y(), rect.width(), rect.height()
        r = w / 2.0  # 圆角半径 = 宽度的一半（胶囊感）

        # ── 半胶囊路径：左平右圆 ──
        path = QPainterPath()
        path.moveTo(x, y)
        path.lineTo(x + w - r, y)
        path.quadTo(x + w, y, x + w, y + r)
        path.lineTo(x + w, y + h - r)
        path.quadTo(x + w, y + h, x + w - r, y + h)
        path.lineTo(x, y + h)
        path.closeSubpath()

        # 背景填充
        bg = QColor(accent)
        bg.setAlpha(int(50 + 150 * expansion))
        painter.setPen(Qt.NoPen)
        painter.setBrush(bg)
        painter.drawPath(path)

        # 边框
        border = QColor(accent)
        border.setAlpha(int(160 + 95 * expansion))
        from PyQt5.QtGui import QPen

        pen = QPen(border)
        pen.setWidthF(1.0)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(path)

        # 中心 FluentIcon.MENU 图标
        icon_size = max(10, int(w * 0.6))
        menu_icon = FluentIcon.MENU.icon()
        icon_rect = QRect(
            int(x + w / 2 - icon_size / 2),
            int(y + h / 2 - icon_size / 2),
            icon_size,
            icon_size,
        )
        menu_icon.paint(painter, icon_rect, Qt.AlignCenter)

    def _accent_color(self) -> str:
        """从当前主题读取强调色，失败时回退到设计文档约定的暖色。"""
        try:
            accent = getattr(Colors, "TEXT_ACCENT", "") or getattr(Colors, "INPUT_FOCUS_BORDER", "")
            if accent:
                return accent
        except Exception:
            pass
        # 安全回退：暖色调（与设计文档描述一致，但不硬编码具体主题）
        return "#f59e9b"


class UIPluginEdgeLauncher(QWidget):
    """UI 插件左侧边缘入口 — 独立窗口模式

    与 LockButtonWidget 类似，本组件以独立顶层窗口形式存在（不参与主窗口布局），
    通过事件过滤器跟踪 MainWidget 的移动/缩放，始终固定在主窗口左边缘中部。

    生命周期：
      1. MainWidget.setup_ui 中创建并 show()；
      2. 插件热重载或首次加载完成时 ``refresh_plugins()`` 刷新列表；
      3. 自动跟踪 MainWidget 的 Move/Resize → ``_sync_position()`` 重定位；
      4. 窗口销毁时随父对象一起释放，无需手动 disconnect。

    状态机：
      COLLAPSED → 鼠标进入触发区 → EXPANDED
      EXPANDED → 鼠标离开 + 延迟 → COLLAPSED
      EXPANDED → 点击胶囊 → MENU_OPEN
      MENU_OPEN → 点击菜单项 / Esc / 空白 → EXPANDED（保持展开直到收起计时）

    信号：
      menu_visibility_changed(bool) — 菜单打开 / 关闭，供父窗口观察
    """

    menu_visibility_changed = pyqtSignal(bool)

    def __init__(self, parent: QWidget = None, *, main_widget=None):
        # 与 LockButtonWidget 一致：不设 Qt 父对象 → 完全独立顶层工具窗口
        # 避免 setGeometry/move 受父子坐标偏移影响
        super().__init__(None)
        # 保留 main_widget 强引用（与 MainWidget 生命周期一致）
        self._main_widget = main_widget
        # 顶层窗口引用（懒初始化：首次 _sync_position 时自动检测）
        # 不能在 init 时用 mw.window() — 此时 main_widget 尚未加入对话框
        self._top_window = None
        # 缓存的插件列表 [(card_id, title, plugin_name), ...]
        self._card_infos: List[Tuple[str, str, str]] = []
        # 状态机
        self._state: str = "COLLAPSED"  # COLLAPSED / EXPANDED / MENU_OPEN
        self._menu: Optional[QMenu] = None
        # 收起延迟定时器
        self._collapse_timer = QTimer(self)
        self._collapse_timer.setSingleShot(True)
        self._collapse_timer.setInterval(COLLAPSE_DELAY_MS)
        self._collapse_timer.timeout.connect(self._on_collapse_timeout)

        # ── 独立窗口标志（类似 LockButtonWidget）──
        # 独立顶层窗口：不参与父布局、不被父裁剪、可独立置顶
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)

        # 触发区自身就是 Launcher（this），负责命中检测
        self.setFixedWidth(TRIGGER_ZONE_WIDTH)
        # 视觉层紧贴触发区
        self._visual = _LauncherVisual(self)
        self._visual.show()

        # Qt 自身属性
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        # 不抢焦点（避免输入框失焦）
        self.setFocusPolicy(Qt.NoFocus)
        # 鼠标跟踪：enterEvent / leaveEvent
        self.setMouseTracking(True)

        # ── 父窗口位置跟踪（懒安装：首次 _sync_position 时自动 setup）──
        # 生命周期：主窗口销毁时自动清理（无 Qt parent 时需手动）
        if self._main_widget is not None:
            self._main_widget.destroyed.connect(self.deleteLater)
        # 定时保险：200ms 同步 + 置顶（与 LockButtonWidget 一致）
        self._sync_timer = QTimer(self)
        self._sync_timer.timeout.connect(self._sync_position)
        self._sync_timer.start(200)

    # ── 公开 API ────────────────────────────────────────────
    def update_geometry(self, chat_rect: QRect = QRect()) -> None:
        """兼容接口 —— 被 MainWidget.resizeEvent 调用，重定向到位置同步。

        Args:
            chat_rect: 不再使用（独立窗口模式下从主窗口实时读取），
                       保留参数签名避免破坏调用方。
        """
        self._sync_position()

    # ── 顶层窗口懒解析 ────────────────────────────────────
    def _resolve_top_window(self):
        """动态解析顶层窗口，自动安装事件过滤器

        首次调用时（或窗口变化后）安装事件过滤，避免 init 阶段
        main_widget 尚未加入对话框导致 _top_window 指向自身。
        """
        mw = self._main_widget
        if mw is None:
            return None
        top = mw.window()
        # main_widget 尚未加入对话框 → window() 返回自身，跳过
        if top is mw or top is None:
            return None
        if top is not self._top_window:
            # 窗口实例变化 → 切换事件过滤器
            if self._top_window is not None:
                try:
                    self._top_window.removeEventFilter(self)
                except RuntimeError:
                    pass
            self._top_window = top
            self._top_window.installEventFilter(self)
        return top

    # ── 事件过滤器（父窗口跟踪）──────────────────────────
    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        """跟踪顶层窗口 Move/Resize/WindowState → 自动同步"""
        if obj is self._top_window:
            if event.type() == QEvent.WindowStateChange:
                self._on_parent_window_state_changed()
            elif event.type() in (QEvent.Move, QEvent.Resize):
                self._sync_position()
        return super().eventFilter(obj, event)

    def _on_parent_window_state_changed(self) -> None:
        """主窗口最小化/恢复时同步显示状态"""
        top_win = self._resolve_top_window()
        if top_win is None:
            return
        if top_win.isMinimized() or not top_win.isVisible():
            self.hide()
        elif self._card_infos:
            self._sync_position()
            self.show()
            self.raise_()

    def showEvent(self, event) -> None:  # noqa: N802
        """显示时同步一次位置"""
        super().showEvent(event)
        self._sync_position()

    # ── 位置同步（独立窗口全局坐标定位）──────────────────
    def _sync_position(self) -> None:
        """同步自身到主窗口左边缘中部（全局屏幕坐标）

        与 LockButtonWidget 一致：独立顶层窗口，用 move()+resize() 全局定位。
        """
        mw = self._main_widget
        if not mw or not mw.isVisible():
            return

        top_win = self._resolve_top_window()
        if top_win is None or not top_win.isVisible():
            return

        h = max(LINE_HEIGHT, CAPSULE_HEIGHT) + 12
        # 左边缘 = 顶层窗口的屏幕左边缘（确保紧贴，无偏移）
        x = top_win.x()
        # 垂直居中于主窗口内容区
        global_center = mw.mapToGlobal(mw.rect().center())
        y = global_center.y() - h // 2

        # 用 move+resize 定位（与 LockButtonWidget 一致）
        self.move(int(x), int(y))
        self.resize(TRIGGER_ZONE_WIDTH, int(h))
        # 视觉层尺寸
        self._visual.setGeometry(0, 0, CAPSULE_WIDTH, int(h))
        # 置顶
        if self.isVisible():
            self.raise_()
        if self.isVisible():
            self.raise_()

    def refresh_plugins(self) -> None:
        """从 UIPluginRegistry 重新读取插件列表，决定是否显示入口

        异常：单个 FloatingCardInfo 数据损坏时跳过该项，其余继续显示。
        """
        try:
            from app.core.ui_plugin_registry import UIPluginRegistry

            cards = UIPluginRegistry.get_instance().get_floating_cards()
        except Exception as e:
            logger.warning(f"[EdgeLauncher] 读取 UIPluginRegistry 失败：{e}")
            self._card_infos = []
            self.hide()
            return

        infos: list = []
        for card_id, info in cards.items():
            try:
                title = (info.title or "").strip() or card_id
                infos.append((card_id, title, info.plugin_name))
            except Exception as e:
                logger.warning(f"[EdgeLauncher] 跳过异常插件项 card_id={card_id!r}: {e}")
        # 排序：按 title 字母序，保持稳定可预期
        infos.sort(key=lambda x: x[1].lower())
        self._card_infos = infos

        if self._card_infos:
            self._sync_position()
            self.show()
            self.raise_()
        else:
            self.hide()
            # 没有插件时也收起菜单
            self._close_menu()

    def apply_theme(self) -> None:
        """主题切换后调用：刷新视觉颜色与菜单样式。"""
        self._visual.update()

    # ── 鼠标事件（命中检测）────────────────────────────────
    def enterEvent(self, event) -> None:  # noqa: N802
        if not self._card_infos:
            return
        self._collapse_timer.stop()
        if self._state == "COLLAPSED":
            self._set_state("EXPANDED")
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self._state == "MENU_OPEN":
            # 菜单打开期间，鼠标离开不应立即收起
            super().leaveEvent(event)
            return
        if self._state == "EXPANDED":
            self._collapse_timer.start()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if not self._card_infos:
            super().mousePressEvent(event)
            return
        if event.button() == Qt.LeftButton and self._state in ("EXPANDED", "COLLAPSED"):
            self._open_menu()
        super().mousePressEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key_Escape and self._state == "MENU_OPEN":
            self._close_menu()
        super().keyPressEvent(event)

    # ── 状态机 ──────────────────────────────────────────────
    def _set_state(self, new_state: str) -> None:
        if new_state == self._state:
            return
        self._state = new_state
        if new_state == "COLLAPSED":
            self._visual.animate_to(0.0, duration_ms=ANIM_DURATION_MS)
        elif new_state == "EXPANDED":
            self._visual.animate_to(1.0, duration_ms=ANIM_DURATION_MS - 20)
        # MENU_OPEN 不改变视觉展开量（保持胶囊显示）

    def _on_collapse_timeout(self) -> None:
        # 鼠标仍未回到本控件才真正收起
        if self.underMouse():
            return
        self._set_state("COLLAPSED")

    # ── 菜单 ────────────────────────────────────────────────
    def _open_menu(self) -> None:
        if not self._card_infos:
            return
        self._collapse_timer.stop()
        self._set_state("MENU_OPEN")

        # 关闭旧菜单
        self._close_menu()
        menu = QMenu(self)
        menu.setAttribute(Qt.WA_TranslucentBackground, True)
        menu.setWindowFlags(menu.windowFlags() | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        menu.setStyleSheet(self._menu_stylesheet())
        menu.setMinimumWidth(MENU_MIN_WIDTH)

        for card_id, title, plugin_name in self._card_infos:
            label = title
            if plugin_name and plugin_name != "system" and not title.startswith(plugin_name):
                # 用户插件且 title 未含前缀时，附加 plugin_name 提示
                label = f"{title}  ·  {plugin_name}"
            action = QAction(label, menu)
            action.setData(card_id)
            action.triggered.connect(lambda checked=False, cid=card_id: self._on_menu_action(cid))
            menu.addAction(action)

        # 定位：菜单仅向右展开 — 起点为胶囊右上角（紧贴右边缘 + 4px 间距）
        # 菜单完全在胶囊右侧，不会向左扩张
        global_pos = self.mapToGlobal(QPoint(CAPSULE_WIDTH + 4, 0))
        # 调整：如果超出屏幕底部则向上偏移
        screen = QApplication.screenAt(global_pos) or QApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            menu_height_hint = min(
                MENU_MAX_HEIGHT,
                max(80, len(self._card_infos) * 32 + 24),
            )
            if global_pos.y() + menu_height_hint > avail.bottom():
                global_pos.setY(max(avail.top() + 8, avail.bottom() - menu_height_hint))

        # 关闭联动：菜单关闭（点击外部 / Esc / 选中项）后回到 EXPANDED/COLLAPSED
        menu.aboutToHide.connect(self._on_menu_about_to_hide)

        self._menu = menu
        self.menu_visibility_changed.emit(True)
        menu.popup(global_pos)
        menu.raise_()

    def _close_menu(self) -> None:
        if self._menu is not None:
            try:
                self._menu.close()
            except Exception:
                pass
            self._menu = None

    def _on_menu_about_to_hide(self) -> None:
        if self._menu is not None:
            try:
                self._menu.aboutToHide.disconnect(self._on_menu_about_to_hide)
            except Exception:
                pass
        self._menu = None
        self.menu_visibility_changed.emit(False)
        if self.underMouse():
            self._set_state("EXPANDED")
        else:
            self._set_state("COLLAPSED")
            self._collapse_timer.start()

    def _on_menu_action(self, card_id: str) -> None:
        """菜单项点击：调用当前窗口的 UIPluginRegistry.toggle_floating_card"""
        try:
            from app.core.ui_plugin_registry import UIPluginRegistry

            registry = UIPluginRegistry.get_instance()
            registry.toggle_floating_card(card_id, main_widget=self._main_widget)
        except Exception as e:
            logger.warning(f"[EdgeLauncher] 打开卡片 {card_id!r} 失败：{e}")

    # ── 样式 ────────────────────────────────────────────────
    def _menu_stylesheet(self) -> str:
        try:
            text_color = Colors.TEXT_PRIMARY
            hover_bg = Colors.HOVER_BG_STRONG
            border = Colors.BORDER
            bg = Colors.CARD_BG_SOLID
        except Exception:
            text_color = "#ffffff"
            hover_bg = "rgba(255,255,255,0.08)"
            border = "#3d3d3d"
            bg = "rgba(33,33,38,250)"
        is_light = _is_current_theme_light()
        if is_light:
            # 浅色主题下做轻量反转
            text_color = "#1f1f1f"
            hover_bg = "rgba(0,0,0,0.06)"
            border = "rgba(0,0,0,0.12)"
            bg = "rgba(255,255,255,245)"
        # 应用系统字体 + 缩放字号（与项目其它 UI 保持一致）
        font_family_css = get_font_family_css()
        # 菜单项：13px（中等强度可读性）
        item_font_size = scale_font_size(13)
        # 菜单背景容器：稍小的字号
        container_font_size = scale_font_size(12)
        return f"""
            QMenu {{
                background-color: {bg};
                border: 1px solid {border};
                border-radius: 8px;
                padding: 6px;
                {font_family_css}
                {font_size_css(12)}
                color: {text_color};
            }}
            QMenu::item {{
                padding: 8px 28px 8px 16px;
                margin: 1px 0;
                color: {text_color};
                border-radius: 5px;
                {font_family_css}
                {font_size_css(13)}
            }}
            QMenu::item:selected {{
                background-color: {hover_bg};
            }}
            QMenu::item:disabled {{
                color: rgba(128, 128, 128, 0.6);
            }}
            QMenu::separator {{
                height: 1px;
                background-color: {border};
                margin: 4px 8px;
            }}
        """
